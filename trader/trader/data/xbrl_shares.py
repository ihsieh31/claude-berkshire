"""XBRL 分類別股數換算：補足 companyfacts 無法取得的多類股公司股數。

多類股公司（BRK.B、V、HSY、STZ、ERIE、ARES 等）的封面股數 dei:EntityCommonStockSharesOutstanding
與 EPS 都依 us-gaap:StatementClassOfStockAxis 分類別申報，companyfacts 的無維度數值因此為空。
本模組直接解析最新一份 10-K／10-Q 的 XBRL instance（inline XBRL 抽出的 *_htm.xml），
依各類別 EPS 比例換算成「與 ticker 同一類別等值」的總股數。

安全原則（這是會用於金錢交易的系統）：
- 不猜比例：任何缺值、EPS ≤ 0、代號對不上、重複但數值衝突，一律回 None。
- 換算結果另以 P/E 合理性檢查把關；超出 [3, 200] 直接拒絕、不寫值。
- 網路請求只經 sec.http.get()（經 sec._request 包裝：封鎖冷卻），且只允許 https://www.sec.gov/ 開頭的網址。
- 命名空間只用 URI 判斷（含 /dei/、/us-gaap/），不寫死前綴與年份。
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd

ARCHIVE = "https://www.sec.gov/Archives/edgar/data/"
SEC_PREFIX = "https://www.sec.gov/"
FORMS = ["10-K", "10-K/A", "10-Q", "10-Q/A"]
AXIS = "StatementClassOfStockAxis"
SHARES_CONCEPT = "EntityCommonStockSharesOutstanding"
SYMBOL_CONCEPT = "TradingSymbol"
TITLE_CONCEPT = "Security12bTitle"
EPS_DILUTED = "EarningsPerShareDiluted"
EPS_BASIC = "EarningsPerShareBasic"
EPS_TOL = 0.01          # 無基準類別時，各類 EPS 相對差距需小於 1%
PE_MIN, PE_MAX = 3.0, 200.0
MAX_TRY = 3   # 由新到舊最多試幾份申報（M1-D7-6）
_NON_INSTANCE_SUFFIX = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml")


class XbrlError(Exception):
    """XBRL 申報取得或解析失敗（找不到 instance、網址不合法等）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _norm(sym: Any) -> str:
    """代號比對用正規化：大寫；- / 空白 一律視同 "."（BRK.B、BRK-B、BRK/B 相同）。"""
    return re.sub(r"[-/\s]", ".", str(sym).strip().upper())


def _split_tag(tag: str) -> tuple[str, str]:
    """ElementTree 的 '{uri}local' → (uri, local)；無命名空間時 uri 為空字串。"""
    if tag.startswith("{"):
        uri, _, name = tag[1:].partition("}")
        return uri, name
    return "", tag


def _axis_local(value: str) -> str:
    """'us-gaap:StatementClassOfStockAxis' → 'StatementClassOfStockAxis'（不依賴前綴）。"""
    return value.strip().split(":")[-1]


def _require_sec_url(url: str) -> None:
    if not url.startswith(SEC_PREFIX):
        raise ValueError(f"只允許 SEC 網址（{SEC_PREFIX}）：{url}")


def _fmt_int(x: float) -> str:
    return f"{x:,.0f}"


def _missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _to_float(v: Any) -> float | None:
    if _missing(v):
        return None
    try:
        x = float(str(v).replace(",", "").strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _cik_str(cik: Any) -> str:
    """CIK 可能是 '0001067983'、1067983 或 1067983.0；統一成不補零的整數字串。"""
    s = str(cik).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return str(int(s))


# ---------------------------------------------------------------------------
# 1. 找 instance 網址、2. 抓取（含快取）
# ---------------------------------------------------------------------------
def _get(sec: Any, url: str):
    """經 SecClient._request 送出（SEC 封鎖起算冷卻、冷卻後重試，A3-M4E-02）；沒有 _request 的替身直接 http.get。"""
    req = getattr(sec, "_request", None)
    return req(sec.http.get, url) if req is not None else sec.http.get(url)


def find_instance_url(sec: Any, cik: Any, accession: str) -> str | None:
    """依申報目錄 index.json 找 XBRL instance。

    優先 *_htm.xml（inline XBRL 抽出的 instance）；否則取其他 .xml，排除 FilingSummary.xml
    與 _cal／_def／_lab／_pre.xml。都沒有回 None。
    """
    acc = accession.replace("-", "")
    base = f"{ARCHIVE}{int(cik)}/{acc}/"
    _require_sec_url(base)
    data = _get(sec, base + "index.json").json()
    items = (data.get("directory") or {}).get("item") or []
    names = [str(it["name"]) for it in items if it.get("name")]
    inline = sorted(n for n in names if n.lower().endswith("_htm.xml"))
    if inline:
        return base + inline[0]
    plain = sorted(
        n for n in names
        if n.lower().endswith(".xml")
        and n.lower() != "filingsummary.xml"
        and not n.lower().endswith(_NON_INSTANCE_SUFFIX)
    )
    return base + plain[0] if plain else None


def fetch_instance(sec: Any, cik: Any, filing: dict) -> str:
    """抓取申報的 XBRL instance 全文。快取於 sec.cache_dir/xbrl/{accession}.xml（申報內容不變，不過期）。"""
    acc = str(filing.get("accessionNumber") or "").strip()
    if not acc:
        raise XbrlError("申報缺 accessionNumber")
    path = Path(sec.cache_dir) / "xbrl" / f"{acc}.xml"
    if path.exists():
        return path.read_text(encoding="utf-8")
    url = find_instance_url(sec, cik, acc)
    if url is None:
        raise XbrlError(f"找不到 XBRL instance：{acc}")
    _require_sec_url(url)
    text = _get(sec, url).text
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")  # 先寫暫存檔再換名，避免半寫入的快取
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return text


# ---------------------------------------------------------------------------
# 3. 解析 instance
# ---------------------------------------------------------------------------
def _to_number(el: ET.Element) -> float | None:
    """數值事實：處理 scale、sign；非有限數或無法解析回 None。"""
    text = (el.text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    scale = el.get("scale")
    if scale not in (None, ""):
        try:
            v *= 10 ** int(scale)
        except ValueError:
            return None
    if el.get("sign") == "-":
        v = -v
    return v if math.isfinite(v) else None


def _parse_contexts(root: ET.Element) -> dict[str, dict]:
    """context id → {"date": 期間結束或瞬時日期 (YYYY-MM-DD), "days": 期間天數（瞬時為 0）, "dims": {axis: member}}。

    typedMember 以空字串記為維度值，不會被當成任何類別成員。
    """
    out: dict[str, dict] = {}
    for el in root.iter():
        uri, name = _split_tag(el.tag)
        if name != "context" or "xbrl.org" not in uri:
            continue
        cid = el.get("id")
        if not cid or cid in out:
            continue
        instant = start = end = None
        dims: dict[str, str] = {}
        for child in el.iter():
            _, cn = _split_tag(child.tag)
            text = (child.text or "").strip()
            if cn == "instant":
                instant = text
            elif cn == "startDate":
                start = text
            elif cn == "endDate":
                end = text
            elif cn == "explicitMember":
                dims[_axis_local(child.get("dimension", ""))] = _axis_local(text)
            elif cn == "typedMember":
                dims[_axis_local(child.get("dimension", ""))] = ""
        try:
            if instant:
                date_s, days = instant[:10], 0
                _dt.date.fromisoformat(date_s)
            elif start and end:
                date_s = end[:10]
                days = (_dt.date.fromisoformat(date_s) - _dt.date.fromisoformat(start[:10])).days
            else:
                continue
        except ValueError:
            continue
        out[cid] = {"date": date_s, "days": days, "dims": dims}
    return out


def _pick(cands: list[tuple[dict, set]], keyfn) -> tuple[Any, Any, bool]:
    """從候選（context, 數值集合）中取 keyfn 最大者。

    回傳 (值, 最大鍵, 是否衝突)；最大鍵上出現多個不同數值即為衝突，值回 None 且標記衝突。
    """
    if not cands:
        return None, None, False
    best = max(keyfn(c) for c, _ in cands)
    vals: set = set()
    for c, vs in cands:
        if keyfn(c) == best:
            vals |= vs
    if len(vals) != 1:
        return None, best, True
    return next(iter(vals)), best, False


def parse_instance(xml_text: str) -> dict:
    """解析 XBRL instance，回傳 {類別 member 本地名 或 None(無維度): 欄位}。

    每個類別欄位：
      symbol  dei:TradingSymbol（同類別 context）
      shares  dei:EntityCommonStockSharesOutstanding（同類別 context，取瞬時日期最新者）
      eps     us-gaap:EarningsPerShareDiluted，沒有則 Basic（同類別 context，且只有該類別一個維度；
              取期間結束最新、期間最長者）
      eps_concept / eps_end / eps_days  取用的 EPS 概念與期間，供說明字串與人工檢查
    同一 context＋概念重複出現只算一次；若同一 context 出現不同數值，該欄位視為衝突而回 None。
    """
    root = ET.fromstring(xml_text)
    contexts = _parse_contexts(root)

    # 事實收集：概念 → context id → 數值集合（重複的同值自動去重，不同值即衝突）
    facts: dict[str, dict[str, set]] = {}
    for el in root.iter():
        cref = el.get("contextRef")
        if cref is None:
            continue
        uri, name = _split_tag(el.tag)
        if "/dei/" in uri and name in (SHARES_CONCEPT, SYMBOL_CONCEPT, TITLE_CONCEPT):
            concept = name
        elif "/us-gaap/" in uri and name in (EPS_DILUTED, EPS_BASIC):
            concept = name
        else:
            continue
        val = (el.text or "").strip() if concept in (SYMBOL_CONCEPT, TITLE_CONCEPT) else _to_number(el)
        if val is None or val == "":
            continue
        facts.setdefault(concept, {}).setdefault(cref, set()).add(val)

    def collect(concept: str) -> dict:
        """{member 或 None: [(context, 數值集合)]}；只收「無維度」或「僅有 StatementClassOfStockAxis 一個維度」的 context。"""
        out: dict = {}
        for cid, vals in facts.get(concept, {}).items():
            ctx = contexts.get(cid)
            if ctx is None:
                continue
            dims = ctx["dims"]
            if not dims:
                member = None
            elif list(dims) == [AXIS] and dims[AXIS] != "":
                member = dims[AXIS]
            else:
                continue
            out.setdefault(member, []).append((ctx, vals))
        return out

    shares_c = collect(SHARES_CONCEPT)
    symbol_c = collect(SYMBOL_CONCEPT)
    title_c = collect(TITLE_CONCEPT)
    eps_d_c = collect(EPS_DILUTED)
    eps_b_c = collect(EPS_BASIC)

    keys: set = set(shares_c) | set(symbol_c) | set(eps_d_c) | set(eps_b_c)
    out: dict = {}
    for k in keys:
        conflict: list[str] = []
        symbol, _, amb = _pick(symbol_c.get(k, []), lambda c: c["date"])
        if amb:
            conflict.append("symbol")
        title, _, _ = _pick(title_c.get(k, []), lambda c: c["date"])  # 只作對應參考，多值則不用
        shares, _, amb = _pick(shares_c.get(k, []), lambda c: c["date"])
        if amb:
            conflict.append("shares")
        # EPS 只取期間（days>0）；基本優先：兩類別法下 Σ 股數×基本 EPS＝淨利，比例才與淨利一致；
        # 稀釋 EPS 假設他類已轉換，用來算比例會失真。沒有基本才退回稀釋。
        eps_src = EPS_BASIC
        cands = [(c, v) for c, v in eps_b_c.get(k, []) if c["days"] > 0]
        if not cands:
            eps_src = EPS_DILUTED
            cands = [(c, v) for c, v in eps_d_c.get(k, []) if c["days"] > 0]
        eps, best, amb = _pick(cands, lambda c: (c["date"], c["days"]))
        if amb:
            conflict.append("eps")
        eps_all: dict = {}   # (期間結束, 天數) → EPS；同期間數值衝突者剔除
        for c, vs in cands:
            per = (c["date"], c["days"])
            eps_all[per] = None if (per in eps_all and eps_all[per] != next(iter(vs))) or len(vs) != 1 \
                else next(iter(vs))
        out[k] = {
            "eps_all": {per: v for per, v in eps_all.items() if v is not None},
            "symbol": symbol,
            "title": title,
            "shares": shares,
            "eps": eps,
            "eps_concept": eps_src if cands else None,
            "eps_end": best[0] if best else None,
            "eps_days": best[1] if best else None,
            "conflict": conflict,  # 非空＝該欄位在同一期間出現不同數值，換算一律拒絕
        }
    return out


# ---------------------------------------------------------------------------
# 4. 換算成與 ticker 同一類別等值的總股數
# ---------------------------------------------------------------------------
def _class_key(member: str) -> str:
    """類別成員名的對應鍵：去掉 Member 後綴與 Common／Equivalent／Stock 字樣。

    例：CommonClassAMember 與 EquivalentClassAMember → "ClassA"（有些公司 EPS 掛在 Equivalent 類、
    股數掛在 Common 類）；CommonStockMember → ""（一般普通股，不與其他成員合併）。
    """
    k = re.sub(r"Member$", "", member)
    return re.sub(r"Common|Equivalent|Stock", "", k)


def _merge_classes(parsed: dict) -> dict | None:
    """同一對應鍵（非空）的成員合併欄位；同欄位兩邊都有值且不同 → None（拒絕）。"""
    out: dict = {}
    groups: dict = {}
    for k, v in parsed.items():
        ck = _class_key(k) if k is not None else None
        if not ck:
            out[k] = v
            continue
        groups.setdefault(ck, []).append((k, v))
    for ck, members in groups.items():
        if len(members) == 1:
            out[members[0][0]] = members[0][1]
            continue
        m: dict = {"conflict": [], "eps_all": {}}
        for _, v in members:
            m["conflict"] += v.get("conflict") or []
            for f in ("symbol", "shares", "eps", "eps_concept", "eps_end", "eps_days"):
                if v.get(f) is not None:
                    if m.get(f) is not None and m[f] != v[f]:
                        return None
                    m[f] = v[f]
            for per, e in (v.get("eps_all") or {}).items():
                if per in m["eps_all"] and m["eps_all"][per] != e:
                    return None
                m["eps_all"][per] = e
        out["+".join(k for k, _ in members)] = m
    return out


def _common_period_eps(holders: dict, base_key) -> tuple[dict, tuple] | None:
    """各類別與基準類別共同擁有的最新（結束日最新、同日取最長）期間的 EPS；沒有共同期間 → None。"""
    keys = list(holders) + ([base_key] if base_key not in holders else [])
    sets = []
    for k in keys:
        v = holders.get(k) if k in holders else None
        sets.append(set((v or {}).get("eps_all") or {}))
    common = set.intersection(*sets) if sets else set()
    if not common:
        return None
    per = max(common)
    return {k: (holders[k]["eps_all"][per]) for k in holders}, per


def equivalent_shares(parsed: dict, ticker: str) -> tuple[float, str] | None:
    """回傳 (等值總股數, 給人看的說明)；無法可靠換算回 None。

    各類別 EPS 一律取「所有類別共同的同一期間」，避免季度與年度 EPS 混用造成比例錯誤。
    """
    tk = _norm(ticker)
    parsed = _merge_classes(parsed)
    if parsed is None:
        return None
    # 任一欄位有數值衝突 → 整份申報不可信，拒絕換算
    if any(v.get("conflict") for v in parsed.values()):
        return None
    # 多類別公司中，只要有類別（有代號或 EPS）卻缺股數，總數必然少算 → 拒絕
    # 只有代號的成員（例如債券 context 也帶類別軸）不是股權類別，不計入
    named = [k for k, v in parsed.items()
             if v.get("shares") is not None or v.get("eps") is not None]
    # 無維度的總數與各類別並存時，以類別為準
    classes = [k for k in named if k is not None] if any(k is not None for k in named) else named
    if len(classes) > 1 and any(parsed[k].get("shares") is None for k in classes):
        return None
    holders = {k: v for k, v in parsed.items() if v.get("shares") is not None}
    note = ""
    if len(holders) > 1 and None in holders:
        # 無維度總數與各類別並存：以類別為準，避免重複計算
        holders.pop(None)
        note = "（忽略無維度總數，避免與各類別重複計算）"
    if not holders:
        return None
    for v in holders.values():
        if v["shares"] <= 0:
            return None

    if len(holders) == 1:
        (k, v), = holders.items()
        sym = v.get("symbol")
        if sym and _norm(sym) != tk:
            return None  # 唯一有股數的類別代號與 ticker 不符，不換算
        label = sym or ("無維度" if k is None else str(k))
        return float(v["shares"]), f"單一類別 {label} 股數 {_fmt_int(v['shares'])}，比例 1{note}"

    base = [k for k, v in parsed.items() if v.get("symbol") and _norm(v["symbol"]) == tk]
    if base == [None]:
        # 代號掛在無維度 context（類別成員都沒有代號）：
        # 1) 同 context 的證券名稱（Security12bTitle）寫明「Class X」→ 對應鍵為 ClassX 的唯一成員；
        # 2) 否則以唯一的一般普通股成員（CommonStockMember 類）為基準。
        if any(parsed[k].get("symbol") for k in holders):
            return None
        m = re.search(r"\bClass\s+([A-Z0-9]+)\b", str(parsed[None].get("title") or ""), re.I)
        if m:
            cand = [k for k in holders if k is not None and _class_key(k).lower() == f"class{m.group(1)}".lower()]
        else:
            cand = [k for k in holders if k is not None and _class_key(k) == ""]
        base = cand if len(cand) == 1 else []
        if not base:
            return None
    if len(base) > 1:
        return None  # 同一代號對應多個類別，無法判定基準
    if base:
        if base[0] not in holders:
            return None  # 基準類別沒有股數
        cp = _common_period_eps(holders, base[0])
        if cp is None:
            return None  # 各類別沒有同一期間的 EPS
        eps_by, per = cp
        eps_t = eps_by[base[0]]
        if eps_t is None or eps_t <= 0:
            return None  # 基準類別 EPS ≤0（虧損期比例不可靠）
        total = 0.0
        parts: list[str] = [f"EPS 期間 {per[0]}（{per[1]} 天）"]
        for k, v in holders.items():
            e = eps_by[k]
            if e is None or e <= 0:
                return None
            total += v["shares"] * e / eps_t
            label = v.get("symbol") or ("無維度" if k is None else str(k))
            parts.append(f"{label} 股數 {_fmt_int(v['shares'])} × EPS {e:g}/{eps_t:g}＝{e / eps_t:.6g}")
        return total, f"基準 {tk}（EPS {eps_t:g}）；" + "；".join(parts) + note

    # 找不到基準類別：只有各類 EPS 幾乎相同時才能直接加總
    eps_vals = []
    for v in holders.values():
        e = v.get("eps")
        if e is None or e <= 0:
            return None
        eps_vals.append(e)
    lo, hi = min(eps_vals), max(eps_vals)
    if (hi - lo) / lo >= EPS_TOL:
        return None
    total = float(sum(v["shares"] for v in holders.values()))
    return total, f"無基準類別且各類 EPS 相同（差 <1%），直接加總{note}"


# ---------------------------------------------------------------------------
# 5. 批次填補
# ---------------------------------------------------------------------------
RATIOS_FILE = Path(__file__).with_name("share_class_ratios.csv")  # 套件內：有申報原文佐證的類別比例


def load_ratios(user_path: Path | None = None) -> dict[str, dict[str, float]]:
    """類別比例檔：ticker,member,ratio,evidence（ratio＝每股相當於幾股交易類別；無經濟權益＝0）。

    先讀套件內檔，再以使用者檔（若存在）覆蓋同代號（整個代號取代）。欄位或數值錯誤屬設定錯誤，直接拋 ValueError。
    """
    out: dict[str, dict[str, float]] = {}
    for f in (RATIOS_FILE, user_path):
        if f is None or not Path(f).exists():
            continue
        df = pd.read_csv(f, dtype=str, keep_default_na=False, comment="#")
        need = {"ticker", "member", "ratio", "evidence"}
        if not need <= set(df.columns):
            raise ValueError(f"類別比例檔欄位需含 {sorted(need)}：{f}")
        mine: dict[str, dict[str, float]] = {}
        for rec in df.to_dict("records"):
            r = _to_float(rec["ratio"])
            if r is None or r < 0 or not str(rec["evidence"]).strip():
                raise ValueError(f"類別比例檔數值或佐證缺漏：{f} {rec}")
            mine.setdefault(_norm(rec["ticker"]), {})[str(rec["member"]).strip()] = r
        out.update(mine)
    return out


def ratio_shares(parsed: dict, ratios: dict[str, float]) -> tuple[float, str] | None:
    """依人工比例換算：每個有股數的類別成員都必須在比例表內（出現新類別 → None，避免漏算）。"""
    if any(v.get("conflict") for v in parsed.values()):
        return None
    holders = {k: v for k, v in parsed.items() if v.get("shares") is not None}
    if any(k is not None for k in holders):
        holders.pop(None, None)          # 有分類別時忽略無維度總數
    if not holders or any(k not in ratios for k in holders):
        return None
    total = sum(v["shares"] * ratios[k] for k, v in holders.items())
    if total <= 0:
        return None
    parts = [f"{k} 股數 {_fmt_int(v['shares'])} × {ratios[k]:g}" for k, v in holders.items()]
    return total, "類別比例表：" + "；".join(parts)


def _price_map(prices: pd.DataFrame | None) -> dict[str, float]:
    if prices is None or len(prices) == 0:
        return {}
    if "ticker" not in prices.columns or "price" not in prices.columns:
        raise ValueError("prices 需含 ticker、price 欄位")
    out: dict[str, float] = {}
    for rec in prices.to_dict("records"):
        p = _to_float(rec["price"])
        if p is not None:
            out[_norm(rec["ticker"])] = p
    return out


def _append_note(existing: Any, note: str) -> str:
    if _missing(existing):
        return note
    return f"{existing}；{note}"


def fill_missing_shares(feats: pd.DataFrame, prices: pd.DataFrame, sec: Any, asof: str,
                        ratios_path: Path | None = None) -> tuple[pd.DataFrame, list[dict]]:
    """替 shares_cover 與 shares 都缺、且有 cik 的列補上等值股數。

    回傳 (新 DataFrame, 逐檔報告)；不修改傳入的 feats。報告的 result：
      filled（XBRL 依 EPS 比例換算）｜ratio（XBRL 股數 × 類別比例表）｜rejected（換算不可靠或 P/E 超出範圍，未寫值）｜
      failed（抓取或解析例外、無可用申報，未寫值）
    """
    out = feats.copy()
    for col in ("shares_cover", "shares_cover_filed", "data_notes"):
        if col not in out.columns:
            out[col] = None
        out[col] = out[col].astype(object)
    ratios = load_ratios(ratios_path)
    prices_by = _price_map(prices)
    splits_by = ({_norm(t): v for t, v in zip(prices["ticker"], prices["splits"]) if isinstance(v, str) and v}
                 if prices is not None and "splits" in prices.columns else {})
    report: list[dict] = []
    asof10 = asof[:10]

    for pos, (_, row) in enumerate(feats.iterrows()):
        if _missing(row.get("cik")):
            continue
        if not _missing(row.get("shares_cover")) or not _missing(row.get("shares")):
            continue
        ticker = str(row["ticker"])
        key = _norm(ticker)
        try:
            result, detail, shares_val, filed, note = _fill_one(
                row, key, sec, asof10, ratios, prices_by, splits_by)
        except Exception as e:  # 單檔例外不得中斷其他檔
            result, detail, note = "failed", f"{type(e).__name__}：{e}", f"多類股股數換算失敗：{type(e).__name__}"
        report.append({"ticker": ticker, "result": result, "detail": detail})
        if result in ("filled", "ratio"):
            out.iat[pos, out.columns.get_loc("shares_cover")] = shares_val
            out.iat[pos, out.columns.get_loc("shares_cover_filed")] = filed
        if note:   # rejected／failed 也寫原因（不含 P/E、價格；M1-D5-04），screen.csv 看得出為何資料不足
            cn = out.columns.get_loc("data_notes")
            out.iat[pos, cn] = _append_note(out.iat[pos, cn], note)
    return out, report


def _fill_one(row: pd.Series, key: str, sec: Any, asof10: str, ratios: dict,
              prices_by: dict[str, float], splits_by: dict[str, str] | None = None
              ) -> tuple[str, str, float | None, str | None, str | None]:
    """單檔處理，回傳 (result, detail, 寫入股數, 寫入 filed, 寫入的 data_notes 片段)。
    依申報日由新到舊最多試 MAX_TRY 份，取第一份能換算者：只修正封面或 Part III、無 EPS 的 10-K/A、10-Q/A
    不再讓整季落入資料不足（M1-D7-6）。data_notes 片段不含 P/E 或價格（screen_context 會帶進價格盲前半）。"""
    cik = _cik_str(row["cik"])
    filings = [f for f in sec.filings(cik, asof10, forms=FORMS)
               if str(f.get("filingDate", ""))[:10] < asof10]
    if not filings:
        return "failed", "asof 前無 10-K／10-Q 申報", None, None, "多類股股數換算失敗：asof 前無 10-K／10-Q 申報"
    use_ratio = key in ratios
    eq, tried, err, n_err = None, [], None, 0
    for latest in sorted(filings, key=lambda f: str(f["filingDate"]), reverse=True)[:MAX_TRY]:
        tried.append(f"{latest['form']} {latest['filingDate']}")
        src_cik = _cik_str(latest.get("cik") or cik)   # 有前身 CIK 時，申報可能屬於前身
        try:
            parsed = parse_instance(fetch_instance(sec, src_cik, latest))
        except Exception as e:  # 該份無 instance 或解析失敗：試下一份
            err, n_err = e, n_err + 1
            continue
        eq = ratio_shares(parsed, ratios[key]) if use_ratio else equivalent_shares(parsed, str(row["ticker"]))
        if eq is not None:
            break
    if eq is None:
        if n_err == len(tried):   # 每份都抓不到或解析失敗 → failed
            raise err
        return ("rejected", f"XBRL 無法可靠換算等值股數（試過 {'、'.join(tried)}）", None, None,
                "多類股股數換算被拒：XBRL 無法可靠換算等值股數")
    total, desc = eq
    rej = "多類股股數換算被拒：換算結果未通過合理性檢查"

    ni = _to_float(row.get("net_income"))
    if ni is not None and ni > 0:
        price = prices_by.get(key)
        if price is None:
            return "rejected", "net_income>0 但缺價格，無法檢查 P/E", None, None, "多類股股數換算被拒：缺價格，無法做合理性檢查"
        # 價格是 price_date 當天基準；股數是申報當時基準 → 乘申報日後的拆股（與 screen.build 同一個 split_factor，M1-D4-05）
        from .prices_adapter import _skill
        pe = price * total * _skill("screen").split_factor((splits_by or {}).get(key, ""), str(latest["filingDate"])[:10]) / ni
        if not (PE_MIN <= pe <= PE_MAX):
            return "rejected", f"P/E {pe:.1f} 超出 [{PE_MIN:g}, {PE_MAX:g}]；{desc}", None, None, rej
        pe_txt = f"P/E {pe:.1f}"
    else:
        pe_txt = "淨利 ≤0 或缺，未檢查 P/E"
    form, filed = latest["form"], latest["filingDate"]
    how = "類別比例表" if use_ratio else "EPS 比例"
    return ("ratio" if use_ratio else "filled", f"{desc}；{pe_txt}", total, str(filed)[:10],
            f"股數＝XBRL 分類別換算（{how}；{form} {filed}）")
