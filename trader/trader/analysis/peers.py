"""同業資料（資料包補充）：產業共同衝擊年、主要競爭者、同業同口徑指標。

規則來源：
- 產業共同衝擊年（SKILL §3）：同產業（SIC 前兩碼）≥5 家，當年中位 ROE ≤0，或低於該產業前 5 年中位 ROE 的一半 → 衝擊年。
- PROC-071 第 4、7 步：讀至少一家主要競爭者年報、用同口徑比較；以公開第三方資料完成競爭問題並附出處。

做法（不做判斷，只準備資料）：
- 同業母體＝股票池（S&P 500 快取）。SIC 取自 SEC submissions；財報取自 companyfacts，時點規則沿用 skill 的 fetch_edgar.annual_series。
  股票池內同 SIC 前兩碼不足 SHOCK_TARGET 家時，以 SEC 公司搜尋（本公司與池內同業的 SIC 四碼）補衝擊年樣本。
- 可比同業＝股票池內同 GICS 子產業（缺 GICS 時用同 SIC 四碼），依營收取前 5 家。
- 主要競爭者＝本公司最新 10-K 業務段「競爭」文字中點名的公司（名稱比對，大寫開頭才算）：先比股票池；
  池外的 SEC 申報公司也比，但名稱（只去法律形式字）須至少兩個字、且 SIC 大類與本公司相同（製造業 20–39、運輸 40–49 等；降低誤認），
  最多 MAX_OUTSIDE_COMPETITORS 家。
- 只用決策日前已申報的資料；不含任何價格。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from .dossier_prep import _fetch_edgar_module, drop_price_sections, scrub_price_text, split_10k_items

MIN_SHOCK_PEERS = 5
SHOCK_TARGET = 8          # 股票池同 SIC 前兩碼少於此數 → 以 SEC 同 SIC 四碼公司補樣本（每年需 ≥5 家有效）
MAX_SHOCK_EXTRA = 30      # 補樣本最多讀幾家 companyfacts
MAX_OUTSIDE_COMPETITORS = 4
MAX_COMPETITORS = 2
MAX_SIC4_PEERS = 5
NARROW_GICS = 5   # 推論：S&P 500 內 ≤5 家的 GICS 子產業多為直接競爭組（建商、家用品、飲料、管理式醫療…）
PEER_ITEM_CHARS = 30_000

_SUFFIX = re.compile(r"\b(the|inc|incorporated|corp|corporation|co|company|companies|holdings?|group|plc|ltd|limited|"
                     r"n\.?v|s\.?a|ag|se|lp|llc|trust|international|worldwide|global)\b\.?", re.IGNORECASE)
# 名稱核心若只剩這些常用字，比對雜訊太大，不用
_COMMON = {"target", "ball", "match", "progressive", "general", "united", "american", "first", "southern", "public",
           "best", "dollar", "national", "state", "news", "global", "service", "services", "energy", "capital",
           "digital", "health", "realty", "republic", "universal", "eaton", "visa", "intuit", "linde", "nike"}


# 池外比對只去法律形式字（保留 Holdings、Group 等，Crown Holdings 不會縮成 Crown）
_LEGAL = re.compile(r"\b(the|inc|incorporated|corp|corporation|co|company|plc|ltd|limited|n\.?v|s\.?a|ag|se|lp|llc)\b\.?",
                    re.IGNORECASE)


_HYPHENS = "\\-\u2010\u2011\u2012\u2013\u2014"
_HY_SEP = r"[\s" + _HYPHENS + r"]+"

# SIC 大類（Division）：前兩碼區間 → 代號
_DIVISIONS = [(1, 9, "A"), (10, 14, "B"), (15, 17, "C"), (20, 39, "D"), (40, 49, "E"), (50, 51, "F"),
              (52, 59, "G"), (60, 67, "H"), (70, 89, "I"), (91, 99, "J")]


def sic_division(sic: str | None) -> str | None:
    """SIC 四碼 → 大類代號（製造業 20–39 為 D 等）；缺或不合法回傳 None。"""
    try:
        two = int(str(sic)[:2])
    except (TypeError, ValueError):
        return None
    return next((d for lo, hi, d in _DIVISIONS if lo <= two <= hi), None)


def title_core(title: str, legal_only: bool = False) -> str:
    t = re.sub(r"[,.&/]", " ", str(title or ""))
    t = (_LEGAL if legal_only else _SUFFIX).sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def competition_text(tenk_text: str) -> str:
    """10-K Item 1 中提到競爭的段落（含前後各 2 行）；找不到 Item 1 用全文前段。"""
    items = dict(split_10k_items(tenk_text))
    body = items.get("1") or tenk_text[:200_000]
    lines = body.splitlines()
    keep: set[int] = set()
    for i, ln in enumerate(lines):
        if re.search(r"compet", ln, re.IGNORECASE):
            keep.update(range(max(0, i - 2), min(len(lines), i + 3)))
    return "\n".join(lines[i] for i in sorted(keep))


def named_competitors(comp_text: str, titles: dict[str, str], universe: list[str], self_ticker: str,
                      strict: bool = False) -> list[str]:
    """競爭段落中點名、且在 universe 內的公司代號（依出現先後）。
    strict（池外比對用）：名稱只去法律形式字，且至少兩個字（單字名稱如 Strategy、Target 誤認太多）。"""
    hits: list[tuple[int, str]] = []
    min_single = 5
    for tk in universe:
        if tk == self_ticker:
            continue
        core = title_core(titles.get(tk, ""), legal_only=strict)
        words = core.split()
        if not words or (strict and len(words) < 2) \
                or (len(words) == 1 and (len(core) < min_single or core.lower() in _COMMON)):
            continue
        # 名稱各字之間允許空白或連字號（Sherwin-Williams）；前後不得緊接字母或連字號（避免 Williams 配到 Sherwin-Williams）
        pat = _HY_SEP.join(re.escape(w) for w in words)
        for m in re.finditer(r"(?<![A-Za-z" + _HYPHENS + r"])" + pat + r"(?![A-Za-z" + _HYPHENS + r"])", comp_text,
                             re.IGNORECASE):
            if m.group(0)[0].isupper():
                hits.append((m.start(), tk))
                break
    hits.sort()
    out: list[str] = []
    for _, tk in hits:
        if tk not in out:
            out.append(tk)
    return out


def _series(F, facts: dict, asof: str) -> pd.DataFrame:
    keys = ("revenue", "net_income", "operating_income", "equity")
    S = {k: F.annual_series(facts, F.TAGS[k], asof) for k in keys}
    return pd.DataFrame(S)


def roe_series(df: pd.DataFrame) -> pd.Series:
    """ROE＝淨利 ÷ 期初期末平均權益；平均權益 ≤0 → 不計（無經濟意義）。"""
    eq = df["equity"]
    avg = (eq + eq.shift(1)) / 2
    r = df["net_income"] / avg
    return r.where(avg > 0)


def shock_years(peer_roe: dict[str, pd.Series], years: list[int]) -> tuple[pd.DataFrame, list[int]]:
    """SKILL §3：每年同業中位 ROE（樣本 ≥5 家才算）；中位 ≤0，或 < 前 5 年中位的一半 → 衝擊年。"""
    all_years = sorted({int(y) for s in peer_roe.values() for y in s.dropna().index} | set(years))
    rows = []
    med: dict[int, float] = {}
    for y in all_years:
        vals = [float(s.get(y)) for s in peer_roe.values() if pd.notna(s.get(y))]
        med[y] = float(pd.Series(vals).median()) if len(vals) >= MIN_SHOCK_PEERS else float("nan")
        rows.append((y, len(vals), med[y]))
    out, shocks = [], []
    for y, n, m in rows:
        prev = [med[p] for p in range(y - 5, y) if p in med and pd.notna(med[p])]
        prev_med = float(pd.Series(prev).median()) if len(prev) >= 3 else float("nan")
        if pd.isna(m):
            flag = "樣本不足"
        elif m <= 0 or (pd.notna(prev_med) and m < prev_med / 2):
            flag = "衝擊年"
        elif pd.isna(prev_med):
            flag = "前 5 年中位不足 3 年，只判 ≤0"
        else:
            flag = "否"
        if y in years:
            out.append({"year": y, "n": n, "median_roe": m, "prev5_median": prev_med, "flag": flag})
            if flag == "衝擊年":
                shocks.append(y)
    return pd.DataFrame(out), shocks


def _metrics(df: pd.DataFrame, last: int) -> dict[str, Any]:
    yrs5 = list(range(last - 4, last + 1))
    d5 = df.reindex(yrs5)
    margin = (d5["operating_income"] / d5["revenue"]).dropna()
    roe = roe_series(df.reindex(range(last - 5, last + 1))).reindex(yrs5).dropna()
    rev = df["revenue"].dropna()
    rev_last = rev.get(last) if last in rev.index else (rev.iloc[-1] if not rev.empty else float("nan"))
    r0 = rev.get(last - 5)
    cagr = (rev_last / r0) ** (1 / 5) - 1 if pd.notna(r0) and r0 and r0 > 0 and pd.notna(rev_last) else float("nan")
    return {"fy": last, "revenue": rev_last, "rev_cagr5": cagr,
            "op_margin_med5": float(margin.median()) if not margin.empty else float("nan"),
            "roe_med5": float(roe.median()) if not roe.empty else float("nan")}


def _pct(v: float) -> str:
    return "缺" if v is None or pd.isna(v) else f"{v:.1%}"


def _m(v: float) -> str:
    return "缺" if v is None or pd.isna(v) else f"{v / 1e6:,.0f}"


def load_gics(csv_path: Path) -> dict[str, str]:
    """股票池 CSV 的 GICS 子產業（代號 → 子產業）；檔案或欄位缺 → 空 dict。"""
    try:
        df = pd.read_csv(csv_path)
    except Exception:  # noqa: BLE001
        return {}
    if "Symbol" not in df or "GICS Sub-Industry" not in df:
        return {}
    return {str(t).strip().upper().replace("-", "."): str(g) for t, g in zip(df["Symbol"], df["GICS Sub-Industry"])
            if pd.notna(g)}


def build_peer_section(sec, ticker: str, cik: str, asof: str, universe: list[str], tenk_text: str | None,
                       outdir: Path, gics: dict[str, str] | None = None) -> dict[str, Any]:
    """產生同業段落（markdown）與主要競爭者 10-K 節錄檔（寫到 outdir，檔名 peer_{T}_10-K_{日期}.txt）。

    回傳 {"markdown", "competitors", "sic", "shock_years", "peer_files"}。任何單一同業資料缺失只略過該家。
    """
    F = _fetch_edgar_module()
    T = ticker.strip().upper().replace("-", ".")
    sic = str((sec.submissions(cik) or {}).get("sic") or "")
    own = _series(F, sec.companyfacts(cik), asof)
    last = int(own["net_income"].dropna().index.max()) if not own["net_income"].dropna().empty else None
    years = list(range(last - 9, last + 1)) if last else []

    # 股票池的 SIC 與 CIK（submissions 快取）
    pool: dict[str, tuple[str, str]] = {}
    for tk in universe:
        tk = tk.strip().upper().replace("-", ".")
        if tk == T:
            continue
        c = sec.cik_for(tk)
        if not c:
            continue
        try:
            s = str((sec.submissions(c) or {}).get("sic") or "")
        except Exception:  # noqa: BLE001
            continue
        pool[tk] = (c, s)

    facts_cache: dict[str, pd.DataFrame] = {}

    def frame(tk: str) -> pd.DataFrame | None:
        if tk not in facts_cache:
            try:
                facts_cache[tk] = _series(F, sec.companyfacts(pool[tk][0]), asof)
            except Exception:  # noqa: BLE001
                facts_cache[tk] = None
        return facts_cache[tk]

    # 1. 產業共同衝擊年（SIC 前兩碼）
    sic2 = sic[:2]
    peer_roe = {}
    for tk, (_, s) in pool.items():
        if sic2 and s[:2] == sic2:
            df = frame(tk)
            if df is not None and not df.empty:
                peer_roe[tk] = roe_series(df)
    n_pool_shock = len(peer_roe)
    extra_shock, extra_names = 0, []
    if years and sic2 and n_pool_shock < SHOCK_TARGET:
        # 本公司 SIC 四碼最先（最接近的同業），其次股票池內同前兩碼者的四碼
        codes = [sic] + sorted({s for _, s in pool.values() if s[:2] == sic2} - {sic, ""})
        seen = {c for c, _ in pool.values()} | {str(cik).zfill(10)}
        cands: list[str] = []
        for code in codes:
            try:
                cands += [c for c in sec.sic_companies(code) if c not in seen and c not in cands]
            except Exception:  # noqa: BLE001  SEC 搜尋失敗：只用股票池
                continue
        for c in cands[:MAX_SHOCK_EXTRA]:
            try:
                fc = sec.companyfacts(c)
                r = roe_series(_series(F, fc, asof))
            except Exception:  # noqa: BLE001
                continue
            if r.reindex(years).notna().sum() >= 3:
                peer_roe[f"CIK{c}"] = r
                extra_shock += 1
                extra_names.append(str(fc.get("entityName") or f"CIK{c}"))
    shock_df, shocks = shock_years(peer_roe, years) if years else (pd.DataFrame(), [])

    # 2. 主要競爭者（10-K 點名）與同 SIC 四碼同業
    titles = sec.company_titles()
    comp_txt = competition_text(tenk_text) if tenk_text else ""
    named = named_competitors(comp_txt, titles, list(pool), T) if comp_txt else []
    outside: list[str] = []
    if comp_txt:
        pool_ciks = {c for c, _ in pool.values()} | {str(cik).zfill(10)}
        others = [tk for tk in titles if tk not in pool and tk != T]
        for tk in named_competitors(comp_txt, titles, others, T, strict=True):
            c = sec.cik_for(tk)
            if not c or c in pool_ciks:
                continue
            try:
                s = str((sec.submissions(c) or {}).get("sic") or "")
            except Exception:  # noqa: BLE001
                continue
            if sic_division(s) is None or sic_division(s) != sic_division(sic):
                continue   # 池外只收同 SIC 大類（擋掉 Freddie Mac 這類在融資段被提到的公司）
            pool[tk] = (c, s)
            pool_ciks.add(c)
            outside.append(tk)
            if len(outside) >= MAX_OUTSIDE_COMPETITORS:
                break
        named += outside
    gics = gics or {}
    g_self = gics.get(T)
    if g_self:
        sic4 = [tk for tk in pool if gics.get(tk) == g_self and tk not in named]
        group_label = f"同 GICS 子產業（{g_self}）"
    else:
        sic4 = [tk for tk, (_, s) in pool.items() if sic and s == sic and tk not in named]
        group_label = f"同 SIC {sic}"
    sic4_rows = []
    for tk in sic4:
        df = frame(tk)
        if df is not None and not df["revenue"].dropna().empty:
            sic4_rows.append((tk, df))
    sic4_rows.sort(key=lambda x: -float(x[1]["revenue"].dropna().iloc[-1]))
    sic4_rows = sic4_rows[:MAX_SIC4_PEERS]

    table_rows = []
    if last:
        table_rows.append((T, "本公司", _metrics(own, last)))
    for tk in named:
        df = frame(tk)
        if df is not None and not df["net_income"].dropna().empty:
            table_rows.append((tk, "10-K 競爭段點名", _metrics(df, int(df["net_income"].dropna().index.max()))))
    for tk, df in sic4_rows:
        if not df["net_income"].dropna().empty:
            table_rows.append((tk, group_label, _metrics(df, int(df["net_income"].dropna().index.max()))))

    # 3. 主要競爭者最新 10-K 節錄（Item 1、7）：10-K 點名者優先；未點名但 GICS 子產業很窄（含本公司 ≤3 家）→ 同組即直接競爭者
    narrow = [tk for tk, _ in sic4_rows] if g_self and len(sic4) + 1 <= NARROW_GICS else []
    to_fetch = (named + [t for t in narrow if t not in named])[:MAX_COMPETITORS]
    peer_files = []
    outdir = Path(outdir)
    for tk in to_fetch:
        try:
            fl = [f for f in sec.filings(pool[tk][0], asof, forms=["10-K"]) if f.get("url") and f["filingDate"] < asof]
            if not fl:
                continue
            f = sorted(fl, key=lambda r: r["filingDate"])[-1]
            raw, _ = drop_price_sections(sec.filing_text(f["url"]))
            text, _ = scrub_price_text(raw)
            items = dict(split_10k_items(text))
            parts = [f"## Item {k}\n{items[k][:PEER_ITEM_CHARS]}" for k in ("1", "7") if items.get(k)]
            if not parts:
                parts = [text[:2 * PEER_ITEM_CHARS]]
            name = f"peer_{tk}_10-K_{f['filingDate']}.txt"
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / name).write_text(f"# 競爭者 {tk} 10-K {f['filingDate']} {f['url']}\n" + "\n\n".join(parts),
                                       encoding="utf-8")
            peer_files.append(name)
        except Exception:  # noqa: BLE001  單一競爭者抓取失敗只略過
            continue

    # markdown
    L = ["", "## 同業資料（框架補算；SEC companyfacts／submissions，只用決策日前已申報者；股票池＝S&P 500）", ""]
    L.append(f"本公司 SIC：{sic or '缺'}；GICS 子產業：{g_self or '缺'}。")
    L += ["", "### 產業共同衝擊年（SKILL §3：SIC 前兩碼 ≥5 家；中位 ROE ≤0 或 < 前 5 年中位的一半）", ""]
    if shock_df.empty:
        L.append("缺：無法計算（本公司財報年度缺）。")
    else:
        extra = f"＋SEC 同 SIC 四碼補充 {extra_shock} 家（非 S&P 500，含已下市者的歷史年度）" if extra_shock else ""
        L.append(f"同業樣本：SIC 前兩碼 {sic2}，股票池內 {n_pool_shock} 家{extra}（不含本公司；ROE＝淨利÷平均權益，平均權益 ≤0 者不計）。")
        L += ["", "| 年度 | 有效家數 | 同業中位 ROE | 前 5 年中位 | 判定 |", "|--:|--:|--:|--:|:--|"]
        for r in shock_df.itertuples():
            L.append(f"| {r.year} | {r.n} | {_pct(r.median_roe)} | {_pct(r.prev5_median)} | {r.flag} |")
        L += ["", "產業共同衝擊年：" + ("、".join(str(y) for y in shocks) if shocks else "無")]
        if extra_shock:
            L += ["", "SEC 補充樣本：" + "、".join(extra_names) + "。"]
        if extra_shock and shocks:
            L += ["", "**判定說明（交分析在誠實標示寫明）**：本判定含 SEC 補充樣本，且 SIC 前兩碼可能混入業務不同的公司"
                  "（例：航空客運與航空快遞同屬 45）。照 SKILL §3 規則套用；分析須在誠實標示說明：本公司在上列衝擊年"
                  "是否同受衝擊（以本公司當年 ROE、營收、營業利益對照同業中位），以及剔除這些年度對正常化業主盈餘的方向"
                  "（剔除壞年 → E 偏高；剔除好年 → E 偏低）。"]
    L += ["", "### 主要競爭者與同業同口徑指標（百萬美元；5 年中位）", ""]
    in_pool = [t for t in named if t not in outside]
    if in_pool:
        L.append("10-K 競爭段點名且在股票池內：" + "、".join(in_pool) + "。")
    if outside:
        L.append("10-K 競爭段點名、不在股票池但在 SEC 申報：" + "、".join(outside) + "（名稱比對，是否為同一家由分析確認）。")
    if not named:
        L.append("10-K 競爭段點名的公司都不在 SEC 申報名單（可能非美國上市或未上市）。")
    if narrow:
        L.append(f"GICS 子產業很窄（股票池內含本公司 {len(sic4) + 1} 家）：同組公司視為直接競爭者（推論），"
                 f"附營收前 {MAX_COMPETITORS} 家的 10-K 節錄；是否真為競爭者由分析判斷。")
    elif not named:
        L.append("同 GICS 子產業涵蓋多種業務，表中同業只作同口徑參考，不視為直接競爭者；主要競爭者年報缺，需以其他公開資料補。")
    if len(table_rows) > 1:
        L += ["", "| 代號 | 來源 | 財年 | 營收 | 營收 5 年年複合 | 營業利益率 | ROE |", "|:--|:--|--:|--:|--:|--:|--:|"]
        for tk, src, m in table_rows:
            L.append(f"| {tk} | {src} | {m['fy']} | {_m(m['revenue'])} | {_pct(m['rev_cagr5'])} | "
                     f"{_pct(m['op_margin_med5'])} | {_pct(m['roe_med5'])} |")
    if peer_files:
        L += ["", "競爭者 10-K 節錄檔（DOSSIER 工作摘要）：" + "、".join(f"`sec/{n}`" for n in peer_files)]
    return {"markdown": "\n".join(L) + "\n", "competitors": named, "outside_competitors": outside,
            "shock_sample": {"pool": n_pool_shock, "sec_extra": extra_shock, "sec_extra_names": extra_names}, "narrow_gics_peers": narrow, "sic": sic, "shock_years": shocks,
            "peer_files": peer_files}
