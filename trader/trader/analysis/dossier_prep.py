"""深度分析前置：資料包骨架（呼叫 skill build_dossier.py）、SEC 申報全文取件與價格字句刪除、10-K 分項切段。

規則：價格隔離。財報骨架 `{T}.md` 不得含價格；價格只進 `{T}_price.json`。
時點：只用申報日 < asof 的 SEC 文件。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from trader.paths import get_paths

BUILD_TIMEOUT_S = 300

# 價格字句（不分大小寫）。寧可多刪，但不刪財報數字行（不得因含 $ 就刪）。
_PRICE_PATTERN_SRC = [
    r"aggregate market value",
    r"closing (sale )?price",
    r"market price of (our|the) (common )?stock",
    r"stock price",
    r"share price",
    r"price per share",       # 含 purchase price per share（收購）
    r"exercise price",        # 寧可多刪
    r"high and low (sales )?prices",
    r"price[- ]to[- ]earnings",
    r"P/E ratio",
    r"market capitali[sz]ation",
    r"price target",
    r"analyst rating",
    r"stock performance graph",
    r"cumulative total (shareholder|stockholder) return",
    r"average price paid",
    r"price paid per share",
    r"current (market |stock |share )?price",
    r"trading price",
    r"股價|市值|本益比|目標價|現價|收盤價?|報價|成交價",
]
PRICE_TEXT_PATTERNS: list[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in _PRICE_PATTERN_SRC]

# skill build_dossier.py 固定寫入的免責行（它自己提到「市值、本益比」字樣，不是價格資料）
_FIXED_DISCLAIMER_PREFIX = "**本檔不含任何價格"

_ITEM_RE = re.compile(
    r"^[ \t]*item[ \t]+(\d{1,2})([ABC])?(?=[ \t]*[.:–—\-]|[ \t]*$|[ \t]+[A-Za-z])",
    re.IGNORECASE | re.MULTILINE,
)


# 整段刪除的價格章節：10-K Item 5（股價區間、績效圖、回購均價表）與 10-Q 的庫藏股買回表。
# 表格數字列不含關鍵字，逐行刪不乾淨，所以從標題刪到下一個 Item 標題。
_PRICE_SECTION_START = re.compile(
    r"^[ \t]*(item[ \t]+5[ \t]*[.:–—\-]?[ \t]*market[ \t]+for|issuer[ \t]+purchases[ \t]+of[ \t]+equity[ \t]+securities)",
    re.IGNORECASE | re.MULTILINE)
_NEXT_ITEM = re.compile(r"^[ \t]*item[ \t]+\d{1,2}[ABC]?\b", re.IGNORECASE | re.MULTILINE)


def drop_price_sections(text: str) -> tuple[str, int]:
    """刪除價格章節（見上），回傳 (新文字, 刪除段數)。"""
    n = 0
    while True:
        m = _PRICE_SECTION_START.search(text)
        if not m:
            return text, n
        nxt = _NEXT_ITEM.search(text, m.end())
        end = nxt.start() if nxt else len(text)
        text = text[:m.start()] + "［框架已刪除價格章節］\n" + text[end:]
        n += 1


# 新聞標題的價格／分析師意見過濾（論點檢查不得看到目標價、評等、股價漲跌）
_HEADLINE_PRICE_SRC = [
    r"\bprice target\b", r"\bPT\b", r"\btarget price\b",
    r"\b(upgrades?|downgrades?|maintains|reiterates|initiates coverage|resumes coverage|assumes coverage)\b",
    r"\b(overweight|underweight|outperform|underperform|equal[- ]weight|market perform|sector perform)\b",
    r"\b(buy|sell|hold|neutral) rating\b", r"\banalyst",
    r"\b(shares|stock|stocks)\b.{0,40}\b(up|down|rise|rises|rising|fall|falls|falling|jump|jumps|drop|drops|"
    r"slide|slides|surge|surges|plunge|plunges|gain|gains|tumble|tumbles|sink|sinks|soar|soars|rall(y|ies)|trading)\b",
    r"52[- ]week (high|low)", r"\ball[- ]time (high|low)\b", r"\bvaluation\b", r"\bundervalued\b|\bovervalued\b",
    r"\bP/E\b", r"\bmarket cap",
]
_HEADLINE_PRICE_RE = [re.compile(p, re.IGNORECASE) for p in _HEADLINE_PRICE_SRC]


def is_price_headline(headline: str) -> bool:
    """新聞標題含價格、市值、目標價、分析師評等或股價漲跌 → True（應丟棄）。"""
    h = str(headline or "")
    return any(p.search(h) for p in PRICE_TEXT_PATTERNS) or any(p.search(h) for p in _HEADLINE_PRICE_RE)


class DossierError(Exception):
    """資料包建立或申報取件失敗；訊息可直接記錄。"""


def scrub_price_text(text: str) -> tuple[str, int]:
    """逐行刪除含價格資訊的行，回傳 (新文字, 刪除行數)。"""
    kept: list[str] = []
    removed = 0
    for line in text.split("\n"):
        if any(p.search(line) for p in PRICE_TEXT_PATTERNS):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed


_SENT_SPLIT = re.compile(r"(?<=[。；;！？])|(?<=[.!?])\s+")


def scrub_price_sentences(text: str) -> tuple[str, int]:
    """逐句刪除含價格資訊的句子（同一行其餘句子保留）；整行都是價格句才刪行。回傳 (新文字, 刪除句數)。
    用於論點檔等自己寫的中文文件：一句話理由常與價格寫在同一行，整行刪會連論點一起丟。"""
    out: list[str] = []
    removed = 0
    for line in text.split("\n"):
        if not any(p.search(line) for p in PRICE_TEXT_PATTERNS):
            out.append(line)
            continue
        parts = [x for x in _SENT_SPLIT.split(line) if x]
        keep = [x for x in parts if not any(p.search(x) for p in PRICE_TEXT_PATTERNS)]
        removed += len(parts) - len(keep)
        if keep and "".join(keep).strip():
            out.append("".join(keep).rstrip())
    return "\n".join(out), removed


def split_10k_items(text: str) -> list[tuple[str, str]]:
    """依 Item 標題切段，回傳 [(項目代號如 "1A", 段落文字)]，依出現順序。
    同一代號多次出現（目錄與內文）時取內容最長的一段；切不出來回傳 [("ALL", text)]。"""
    matches = [m for m in _ITEM_RE.finditer(text) if 1 <= int(m.group(1)) <= 16]
    if not matches:
        return [("ALL", text)]
    best: dict[str, tuple[int, int, str]] = {}  # 代號 → (起點, 長度, 文字)
    for i, m in enumerate(matches):
        code = m.group(1) + (m.group(2) or "").upper()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.start():end]
        if code not in best or len(seg) > best[code][1]:
            best[code] = (m.start(), len(seg), seg)
    ordered = sorted(best.items(), key=lambda kv: kv[1][0])
    return [(code, seg) for code, (_, _, seg) in ordered]


# 資料包補充欄位（skill build_dossier.py 未含）：SEL-055 有形資本報酬所需的資產負債項目。
# 時點與標籤補齊邏輯沿用 skill 初篩的 fetch_edgar.annual_series（只用決策日前已申報的 10-K）。
SUPPLEMENT_TAGS: dict[str, list[str]] = {
    "ppe_net": ["PropertyPlantAndEquipmentNet",
                "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"],
    "assets_cur": ["AssetsCurrent"],
    "liab_cur": ["LiabilitiesCurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "debt_cur": ["LongTermDebtCurrent", "DebtCurrent"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
    "oplease_rou": ["OperatingLeaseRightOfUseAsset"],
    "operating_income": ["OperatingIncomeLoss"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
}


def _fetch_edgar_module():
    d = str(get_paths().skill_screen / "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    import fetch_edgar  # noqa: PLC0415
    return fetch_edgar


def supplement_section(facts: dict, asof: str, years: int = 10) -> str:
    """回傳要附加到資料包的 markdown 段：資產負債補充欄位與有形資本報酬（SEL-055 口徑）。不含價格。"""
    import pandas as pd  # noqa: PLC0415
    F = _fetch_edgar_module()
    S = {k: F.annual_series(facts, v, asof) for k, v in SUPPLEMENT_TAGS.items()}
    ni = F.annual_series(facts, ["NetIncomeLoss"], asof)
    last = int(ni.index.max()) if not ni.empty else max((int(s.index.max()) for s in S.values() if not s.empty),
                                                           default=None)
    if last is None:
        return ""
    yrs = list(range(last - years + 1, last + 1))
    T = pd.DataFrame({k: v.reindex(yrs) for k, v in S.items()})
    wc = (T.assets_cur - T.cash.fillna(0)) - (T.liab_cur - T.debt_cur.fillna(0))
    tang = wc + T.ppe_net
    rate = (T.tax / T.pretax).where(lambda r: (r > 0) & (r < 0.6))
    rate = rate.fillna(rate.median() if rate.notna().any() else 0.21)
    nopat = T.operating_income * (1 - rate)

    def fmt(v):
        return "缺" if pd.isna(v) else f"{v / 1e6:,.0f}"

    rows = [("固定資產淨額", T.ppe_net), ("流動資產", T.assets_cur), ("流動負債", T.liab_cur),
            ("其中一年內到期債務", T.debt_cur), ("無形資產（不含商譽）", T.intangibles),
            ("營業租賃使用權資產", T.oplease_rou),
            ("營運資金（流動資產−現金）−（流動負債−一年內到期債務）", wc),
            ("有形資本＝營運資金＋固定資產淨額", tang)]
    head = "| 百萬美元 | " + " | ".join(str(y) for y in yrs) + " |"
    sep = "|:--|" + "--:|" * len(yrs)
    lines = ["", "## 補充資產負債欄位（框架補算；SEC companyfacts，只用決策日前已申報的 10-K）", "",
             "SEL-055 有形資本報酬＝稅後營業利益 ÷（營運資金＋固定資產淨額），排除商譽與無形資產。"
             "稅後營業利益＝營業利益 ×（1−有效稅率；有效稅率不在 0–60% 時用各年中位，全缺用 21%）。", "",
             head, sep]
    for name, s in rows:
        lines.append(f"| {name} | " + " | ".join(fmt(s.get(y)) for y in yrs) + " |")
    ratio = []
    for y in yrs:
        n, c = nopat.get(y), tang.get(y)
        if pd.isna(n) or pd.isna(c):
            ratio.append("缺")
        elif c <= 0:
            ratio.append("有形資本≤0")
        else:
            ratio.append(f"{n / c:.1%}")
    lines.append("| 稅後無槓桿有形資本報酬 | " + " | ".join(ratio) + " |")
    if T.assets_cur.isna().all() or T.liab_cur.isna().all():
        lines += ["", "註：申報未分流動／非流動（未分類資產負債表），營運資金無法由標籤計算，需由年報原文判斷。"]
    return "\n".join(lines) + "\n"


def _user_agent(sec: Any) -> str:
    """SEC User-Agent 來源：SecClient.user_agent → http 標頭 → 環境變數 SEC_USER_AGENT。都沒有拋 DossierError。不印出內容。"""
    ua = getattr(sec, "user_agent", None)
    if not ua:
        client = getattr(getattr(sec, "http", None), "_client", None)
        headers = getattr(client, "headers", None)
        if headers is not None:
            ua = headers.get("User-Agent")
            if ua and str(ua).startswith("python-httpx/"):  # httpx 預設值，不是 SEC 聯絡資訊
                ua = None
    if not ua:
        ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise DossierError("缺少 SEC User-Agent（SecClient 無 user_agent、http 標頭無 User-Agent、環境變數 SEC_USER_AGENT 皆未設定）")
    return str(ua)


def _skill_script() -> Path:
    return get_paths().skill_deep / "scripts" / "build_dossier.py"


def _write_cache(cache: Path, cik10: str, ticker_std: str, facts: dict) -> None:
    """寫入 skill 讀取的快取：代號對照（點號與連字號各一筆）與合併後的 companyfacts。"""
    cache.mkdir(parents=True, exist_ok=True)
    forms = [ticker_std]
    alt = ticker_std.replace(".", "-")
    if alt not in forms:
        forms.append(alt)
    table = {str(i): {"cik_str": int(cik10), "ticker": t, "title": ""} for i, t in enumerate(forms)}
    (cache / "company_tickers.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    (cache / f"CIK{cik10}.json").write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")


def _insert_before_text_section(md_text: str, section: str) -> str:
    """把補充段放在「年報文字節錄」段之前（DOSSIER 工作從該段起追加）；找不到則接在檔尾。"""
    i = md_text.find("\n## 年報文字節錄")
    if i < 0:
        return md_text.rstrip("\n") + "\n" + section
    return md_text[:i].rstrip("\n") + "\n" + section + md_text[i:]


def build_dossier_base(sec, ticker: str, asof: str, outdir: Path,
                       price: float | None, price_date: str | None,
                       rates: dict | None) -> dict:
    """建立單檔資料包骨架：財報表 `{T}.md` 與價格檔 `{T}_price.json`（見模組說明）。失敗拋 DossierError。"""
    ticker_std = ticker.strip().upper().replace("-", ".")
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    cache = outdir / "_cache"

    cik = sec.cik_for(ticker_std)
    if not cik:
        raise DossierError(f"SEC 對照表找不到 {ticker_std}")
    cik10 = str(cik).zfill(10)
    facts = sec.companyfacts(cik10)
    facts, used, failed = sec._with_predecessor_facts(cik10, facts)
    notes: list[str] = []
    if used:
        notes.append("含前身 CIK " + "、".join(used))
    if failed:
        notes.append("前身 CIK " + "、".join(failed) + " 抓取失敗未併入")

    ua = _user_agent(sec)
    env = dict(os.environ)
    env["SEC_USER_AGENT"] = ua

    cmd = [sys.executable, str(_skill_script()), "--ticker", ticker_std, "--asof", asof,
           "--outdir", str(outdir), "--cache", str(cache)]
    if price is not None:
        cmd += ["--price", repr(float(price))]
    if price_date is not None:
        cmd += ["--price-date", str(price_date)]
    rates = rates or {}
    if rates.get("t30") is not None:
        cmd += ["--t30", repr(float(rates["t30"]))]
    if rates.get("t30_date") is not None:
        cmd += ["--t30-date", str(rates["t30_date"])]
    if rates.get("t30_20y_median") is not None:
        cmd += ["--t30-20y-med", repr(float(rates["t30_20y_median"]))]

    md = outdir / f"{ticker_std}.md"
    price_json = outdir / f"{ticker_std}_price.json"
    try:
        _write_cache(cache, cik10, ticker_std, facts)
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise DossierError(f"build_dossier 逾時（>{BUILD_TIMEOUT_S} 秒）：{ticker_std}") from e
        if proc.returncode != 0:
            lines = [ln for ln in (proc.stderr or proc.stdout or "").strip().splitlines() if ln.strip()]
            msg = lines[-1] if lines else "無輸出"
            raise DossierError(f"build_dossier 失敗（exit {proc.returncode}）：{msg}")
        if not md.exists() or not price_json.exists():
            raise DossierError(f"build_dossier 未產出 {md.name} 或 {price_json.name}")
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    try:
        sup = supplement_section(facts, asof)
    except Exception as e:  # noqa: BLE001  補充欄位失敗不擋資料包，照實標示
        sup = f"\n## 補充資產負債欄位\n\n缺：框架補算失敗（{type(e).__name__}）\n"
    if sup:
        md.write_text(_insert_before_text_section(md.read_text(encoding="utf-8"), sup), encoding="utf-8")

    body_lines = [ln for ln in md.read_text(encoding="utf-8").split("\n")
                  if not ln.startswith(_FIXED_DISCLAIMER_PREFIX)]
    _, n_removed = scrub_price_text("\n".join(body_lines))
    if n_removed:
        raise DossierError(f"{md.name} 含價格字句（{n_removed} 行），價格隔離失敗")

    return {"md": md, "price_json": price_json, "predecessors_used": used, "notes": notes}


MIN_AMENDMENT_CHARS = 50_000  # 10-K/A 全文至少這麼長才視為整份重述而收進資料包


def _filing_url_ok(f: dict) -> bool:
    return bool(f.get("url")) and bool(f.get("filingDate"))


def _pick_filings(sec, cik: str, asof: str, max_8k: int) -> dict[str, list[dict]]:
    """依規則挑出各類申報（皆已限定申報日 < asof，並依申報日升冪）。"""
    tenk = [f for f in sec.filings(cik, asof, forms=["10-K", "10-K/A"]) if f["filingDate"] < asof]
    tenk = [f for f in tenk if _filing_url_ok(f)]
    tenk.sort(key=lambda f: f["filingDate"])
    # 年報主體取最新的原始 10-K；10-K/A 多半只是補封面或 Part III，不能取代年報。
    # 沒有原始 10-K 時才退而取最新 10-K/A。
    originals = [f for f in tenk if f["form"] == "10-K"]
    last10k = originals[-1:] or tenk[-1:]
    last_date = last10k[0]["filingDate"] if last10k else ""
    # 年報之後的最新 10-K/A 列為候選，fetch_sec_texts 讀到全文且夠長（整份重述）才收
    amend = [f for f in tenk if f["form"] == "10-K/A" and f["filingDate"] > last_date] if originals else []

    tenq = [f for f in sec.filings(cik, asof, forms=["10-Q"]) if f["filingDate"] < asof and _filing_url_ok(f)]
    tenq = [f for f in tenq if f["filingDate"] > last_date]
    tenq.sort(key=lambda f: f["filingDate"])
    tenq = tenq[-3:]

    proxy = [f for f in sec.filings(cik, asof, forms=["DEF 14A"]) if f["filingDate"] < asof and _filing_url_ok(f)]
    proxy.sort(key=lambda f: f["filingDate"])
    proxy = proxy[-1:]

    eightk = [f for f in sec.filings(cik, asof, forms=["8-K"]) if f["filingDate"] < asof and _filing_url_ok(f)]
    eightk = [f for f in eightk if last_date and f["filingDate"] > last_date]
    eightk.sort(key=lambda f: f["filingDate"])
    eightk = eightk[-max_8k:] if max_8k > 0 else []

    return {"10-K": last10k, "10-K/A": amend[-1:], "10-Q": tenq, "DEF 14A": proxy, "8-K": eightk}


_ACC_URL = re.compile(r"/Archives/edgar/data/(\d+)/(\d{18})/")


def exhibit_any(sec, url: str) -> str:
    """8-K 的 EX-99 附件（不限項目碼；公司行動公告多放在 8.01 的 EX-99）；沒有回空字串。"""
    m = _ACC_URL.search(str(url or ""))
    if not m:
        return ""
    cik, folder = m.group(1), m.group(2)
    try:
        ex = sec.exhibit_text(cik, f"{folder[:10]}-{folder[10:12]}-{folder[12:]}", "EX-99")
    except Exception:  # noqa: BLE001
        return ""
    return ("\n\n## 附件 EX-99\n" + ex) if ex else ""


def earnings_exhibit(sec, url: str, items: str | None) -> str:
    """財報 8-K（項目含 2.02）的 EX-99 新聞稿全文，前面加小標；非財報 8-K 或網址無法解析回傳空字串；抓不到標「缺」。"""
    if "2.02" not in str(items or ""):
        return ""
    m = _ACC_URL.search(str(url or ""))
    if not m:
        return ""
    cik, folder = m.group(1), m.group(2)
    acc = f"{folder[:10]}-{folder[10:12]}-{folder[12:]}"
    try:
        if hasattr(sec, "exhibit_texts"):
            exs = sec.exhibit_texts(cik, acc, "EX-99")
        else:
            one = sec.exhibit_text(cik, acc, "EX-99")
            exs = [("EX-99", one)] if one else []
    except Exception:  # noqa: BLE001  附件抓不到不擋流程，照實標示
        exs = []
    if not exs:
        return "\n\n## 附件 EX-99：缺（未取得）\n"
    out = "\n\n## 附件 EX-99（財報新聞稿）\n" + exs[0][1]
    # 其餘 EX-99.x 多為財務補充資料或法說簡報（保單數、留存率、續約率、在手訂單等產業指標常只在這裡）
    for typ, text in exs[1:1 + MAX_SUPPLEMENT_EXHIBITS]:
        cut = "\n［框架截斷：超過字數上限］" if len(text) > SUPPLEMENT_CHARS else ""
        out += f"\n\n## 附件 {typ}（財報補充資料／簡報）\n" + text[:SUPPLEMENT_CHARS] + cut
    return out


MAX_SUPPLEMENT_EXHIBITS = 2
SUPPLEMENT_CHARS = 40_000
ANNUAL_REPORT_CHARS = 60_000


def annual_report_exhibit(sec, cik: str, accession: str) -> str:
    """10-K 的 EX-13（致股東年報節錄，常含致股東信）；沒有回空字串。很多公司不附 EX-13，屬正常。"""
    if not accession or not hasattr(sec, "exhibit_texts"):
        return ""
    try:
        exs = sec.exhibit_texts(cik, accession, "EX-13")
    except Exception:  # noqa: BLE001
        return ""
    if not exs:
        return ""
    typ, text = exs[0]
    cut = "\n［框架截斷：超過字數上限］" if len(text) > ANNUAL_REPORT_CHARS else ""
    return f"\n\n## 附件 {typ}（致股東年報）\n" + text[:ANNUAL_REPORT_CHARS] + cut


def fetch_sec_texts(sec, cik: str, asof: str, outdir: Path, max_8k: int = 12) -> list[dict]:
    """取申報日 < asof 的 10-K（最新）、其後 10-Q（最新至多 3 份）、DEF 14A（最新一份）、10-K 之後 8-K（最新至多 max_8k 份）。
    每份全文經 scrub_price_text 後寫入 outdir；回傳依申報日排序的清單。"""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    picked = _pick_filings(sec, cik, asof, max_8k)
    chosen: list[dict] = []
    amend_ids = {id(f) for f in picked["10-K/A"]}
    for form in ("10-K", "10-K/A", "10-Q", "DEF 14A", "8-K"):
        chosen.extend(picked[form])
    chosen.sort(key=lambda f: f["filingDate"])

    out: list[dict] = []
    for f in chosen:
        form = f["form"]
        acc = str(f.get("accessionNumber") or "")
        acc_tail = acc.replace("-", "")[-6:]
        name = f"{form.replace('/', '').replace(' ', '')}_{f['filingDate']}_{acc_tail}.txt"
        raw = sec.filing_text(f["url"])
        if form.startswith("8-K"):  # 財報 8-K 的主文件通常只有一頁，數字在附件 EX-99 新聞稿
            raw += earnings_exhibit(sec, f["url"], f.get("items"))
        raw, dropped = drop_price_sections(raw)
        scrubbed, removed = scrub_price_text(raw)
        if form == "10-K":  # EX-13 另行刪價格（整段刪除找不到下一個 Item 時會刪到文末，不能和主文合併處理）
            ex, d2 = drop_price_sections(annual_report_exhibit(sec, str(f.get("cik") or cik), acc))
            ex, r2 = scrub_price_text(ex)
            scrubbed, dropped, removed = scrubbed + ex, dropped + d2, removed + r2
        if id(f) in amend_ids and len(scrubbed) < MIN_AMENDMENT_CHARS:
            continue  # 只補封面／Part III 的修正檔，不放進資料包
        header = f"# {form} {f['filingDate']} {f['url']}"
        (outdir / name).write_text(header + "\n" + scrubbed, encoding="utf-8")
        out.append({"file": name, "form": form, "filingDate": f["filingDate"], "url": f["url"],
                    "chars": len(scrubbed), "removed_lines": removed, "dropped_sections": dropped})
    out.sort(key=lambda r: r["filingDate"])
    return out
