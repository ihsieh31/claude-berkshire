#!/usr/bin/env python3
"""從 SEC EDGAR companyfacts（免費）抓年度財報並算初篩特徵。

  SEC_USER_AGENT="你的名稱 你的email" python fetch_edgar.py --tickers tickers.txt --out features.csv --asof 2026-10-12 [--cache edgar_cache]

- SEC 要求 User-Agent 帶聯絡方式：用環境變數 SEC_USER_AGENT 或 --ua 提供；請求間隔 ≥0.21 秒（≤5 req/s）。
- 市值用股數：封面股數（dei:EntityCommonStockSharesOutstanding，10-K／10-Q 皆可，申報日 < 決策日的最新一份；多類股加總），
  輸出 shares_cover 與其申報日 shares_cover_filed，供 screen.py 依之後的拆股調整（fetch_prices.py 提供拆股事件）。
- 只取 10-K／10-K/A 的全年值（期間約 1 年；不看 fp，有公司把年報標成 Q4）；同一財年跨標籤取「首次申報」版本，
  避免用到事後重編數字（可加 --latest 改取最新版本）。財年標籤：7–12 月期末＝期末年；1–6 月期末依 10-K 的
  DocumentFiscalYearFocus 決定是期末年或前一年（最近 2 份 10-K 一致就依它，DEI 慣例變過的公司如 TJX、STZ 依現在的自稱；
  不一致退回全部 10-K 多數；52/53 週制期末不撞號），以欄頭期末日為準。會計年度變更的過渡年（LHX FY2019、FERG FY2025 等）同財年有兩個期末：
  各欄統一取有全年損益的最晚期末（fiscal_year_ends），其他期末的數字不用並警示。TAGS／DEBT_TAGS／annual_series／debt_series 也被深度分析資料包（build_dossier、
  trader dossier_prep、peers）共用：改動須兩邊驗收。時點：只用申報日（filed）早於決策日 --asof 的資料（filed < asof；
  --asof 未給時預設為今天）；輸出 fy_last_filed（最近財年淨利的申報日）與 max_filed_used（實際用到的最晚申報日），
  並自檢 max_filed_used < asof，違反即中止（防前視）。
- 輸出欄位：ticker, cik, name, fy_last, years_of_data, net_income, ni5_mean, revenue, rev_cagr_5y,
  roe, roe_med5, roe_min5, roe_sd5, roa, eps_cagr5, loss_yrs5, op_margin_5y_std, fcf, shares,
  share_change_3y（未經拆股調整，screen.py 覆寫）, shares_3y_ago, shares_3y_filed,
  debt_to_equity（有息負債／權益，P-07 口徑；最近一年有息負債缺 → 空）, interest_coverage_med5,
  shareholders_equity, is_financial（SIC 6000–6799）, ni_prev, eq_prev, n_ni5, ni_sum5, ni5_med,
  operating_cash_flow, ocf_sum5, ocf_pos5, n_ocf5, eps_g1, n_roe5, util, asof, fy_last_filed, fy_last_end（最近財年期末日，screen.py 判斷財年後分拆用）, max_filed_used, data_notes。
- 缺的欄位留空（初篩判 PASS-資料不足，不刪）。盈餘為 GAAP 淨利代理；全域營業盈餘口徑在深度分析重編。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

import numpy as np
import pandas as pd

UA = os.environ.get("SEC_USER_AGENT", "")
REQ_INTERVAL = 0.21
MAX_403_STREAK = 5   # 連續幾檔 HTTP 403 就中止（同 trader sec.py；避免被 SEC 拒絕時逐檔打完整份名單）


class LookaheadError(RuntimeError):
    """前視自檢失敗（max_filed_used ≥ asof）：時點紀律被破壞，整批中止，不得吞成單檔「抓取失敗」（A3-M1C-02）。"""


# 前身 CIK 對照（公司改組後新 CIK 沒有歷史財報，如 XOM 0002115436 ← 0000034088）：預設讀 repo 內 trader 的資料檔
# （只讀 JSON、不匯入 trader 程式碼）；skill 單獨拿出去用時以 --predecessors 自備同格式檔，否則不合併前身。
PREDECESSORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "trader", "trader", "data",
                            "cik_predecessors.json")
TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "InterestAndDividendIncomeOperating"],
    # 歸屬母公司（普通股）淨利優先；含少數股權的 ProfitLoss 只在前兩者皆缺時用
    "net_income": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    "operating_income": ["OperatingIncomeLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity"],   # 只取歸屬母公司權益；含少數股權者口徑不同，不拿來補（缺即 nan）
    # 損益表利息費用；InterestPaidNet（現金流量表支付利息）口徑不同，不用。InterestExpenseOperating（DGX 2022 起）是
    # 全部利息費用只是歸在營業項下；InterestExpenseLongTermDebt 只是長期債部分（LOW 另有短借、其他利息），低估利息、
    # 高估利息保障，不收（缺時資料包警示，由原文補）
    "interest": ["InterestExpense", "InterestExpenseNonoperating", "InterestAndDebtExpense", "InterestExpenseDebt",
                 "InterestExpenseOperating"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"],
}
# 有息負債（P-07）分組取數再相加（debt_series）；每組內依優先序逐年擇一，不同組才相加。
DEBT_TAGS = {
    # 長期債：非流動優先；SeniorNotes／NotesPayable／LongTermDebt 含一年內到期部分（見 DEBT_LT_INCL_CURRENT）
    "debt_lt": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
                "LongTermDebtAndFinanceLeaseObligationsNoncurrent", "SeniorNotes", "NotesPayable", "LongTermDebt"],
    "cur_ltd": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
                "LongTermDebtAndFinanceLeaseObligationsCurrent"],          # 一年內到期長期債
    "st_borrow": ["ShortTermBorrowings", "CommercialPaper"],             # 短期借款
    "debt_current_total": ["DebtCurrent"],                               # 前兩組皆缺時才用
    "debt_total": ["DebtLongtermAndShorttermCombinedAmount"],            # 長短期合計；只在長期債組缺時用
}
DEBT_LT_INCL_CURRENT = ["SeniorNotes", "NotesPayable", "LongTermDebt"]  # 用到這些時不再加一年內到期長期債（避免重複）；
# 同年有多個時取最大值：其中任一個可能只是一部分（PHM 的 LongTermDebt＝合資小額；WELL／INVH／SYF 的 SeniorNotes 只是一類債）
_last = [0.0]


def get(url):
    wait = REQ_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as r:
        _last[0] = time.time()
        return json.loads(r.read().decode("utf-8"))


def cached(path, url, asof=None, notes=None):
    """讀快取或抓取。寫入先寫暫存檔再 os.replace（中途被殺不留半截檔）；既有快取解析失敗視同沒有快取、重抓。
    asof（YYYY-MM-DD，選填）：快取檔最後修改日早於 asof → 重抓（A3-M1E-05；未給＝有檔即用，build_dossier 等舊呼叫不變）；
    重抓失敗（Exception）退回舊檔，並把註記附加到 notes（list，選填）；HTTP 403（SEC 拒絕存取）例外照常往上拋，
    交給呼叫端的連續 403 中止保護（不吞掉、否則被封鎖時仍逐檔打請求）。"""
    old = None
    if os.path.exists(path):
        try:
            with open(path) as f:
                old = json.load(f)
        except ValueError:   # 半截／損壞的快取（JSONDecodeError、UnicodeDecodeError）
            old = None
    if old is not None:
        mdate = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))
        if not asof or mdate >= str(asof)[:10]:
            return old
        try:
            d = get(url)
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError) and e.code == 403:
                raise
            if notes is not None:
                notes.append(f"快取 {os.path.basename(path)}（{mdate}）早於決策日 {str(asof)[:10]}，重抓失敗"
                             f"（{type(e).__name__}），沿用舊快取")
            return old
    else:
        d = get(url)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return d


def load_predecessors(path):
    """{現行 CIK10: [前身 CIK10]}；格式同 trader/trader/data/cik_predecessors.json（{"map": {新: [舊…]}}）。"""
    with open(path, encoding="utf-8") as f:
        m = json.load(f)["map"]
    return {str(k).zfill(10): [str(o).zfill(10) for o in v] for k, v in m.items()}


def merge_facts(current, predecessors):
    """併入前身 companyfacts（同 trader sec.merge_companyfacts）：各 taxonomy→concept→unit 的列串接（前身在前），
    完全相同的列去重；其他欄位用現行公司。之後「同財年取首次申報」規則照舊（時點紀律不變）。"""
    out = {k: v for k, v in current.items() if k != "facts"}
    facts = {}
    for src in [*predecessors, current]:
        for tax, concepts in (src.get("facts") or {}).items():
            for concept, body in concepts.items():
                tgt = facts.setdefault(tax, {}).setdefault(concept, {"units": {}})
                tgt.update({k: v for k, v in body.items() if k != "units"})
                for unit, rows in (body.get("units") or {}).items():
                    tgt["units"].setdefault(unit, []).extend(rows)
    for concepts in facts.values():
        for body in concepts.values():
            for unit, rows in body["units"].items():
                seen, keep = set(), []
                for r in rows:
                    k = json.dumps(r, sort_keys=True)
                    if k not in seen:
                        seen.add(k)
                        keep.append(r)
                body["units"][unit] = keep
    out["facts"] = facts
    return out


def _fy_base(end):
    """期末日 → 財年標籤（日曆多數年規則）。先往前移 14 天，避免 52/53 週制期末在 6/30、12/31 前後跳動造成相鄰兩年同號。"""
    e = pd.Timestamp(end) - pd.Timedelta(days=14)
    return e.year if e.month > 6 else e.year - 1


def _early_end(end):
    """期末（往前移 14 天後）落在 1–6 月：這類公司的財年可能稱期末年（FDX、NVDA）或前一年（LOW），需 offset 判定。"""
    return (pd.Timestamp(end) - pd.Timedelta(days=14)).month <= 6


FY_VOTE_RECENT = 2   # 最近 2 份（1–6 月期末）10-K 的 offset 一致就用它，否則全歷史眾數（M4-D1-02）


def fiscal_year_offset(facts, asof=None):
    """1–6 月期末列的財年 offset（0 或 1，加在 _fy_base 上）：看 1–6 月期末 10-K 各份 DocumentFiscalYearFocus
    （companyfacts 的 fy 欄）與 _fy_base 的差。申報日最近的 FY_VOTE_RECENT 份差值一致 → 用它（DEI 慣例變過的公司，
    TJX、STZ 約 2021 年起改稱期末年，依現在的自稱）；不一致 → 全部 10-K 取眾數（fy 欄偶有單份填錯，如 CrowdStrike
    [1,1,1,0,0,0,1]、Salesforce 最新一份＝0，不被少數幾份帶偏）。
    7–12 月期末列一律不加 offset，所以換過會計年度的公司（L3Harris：6 月期末 → 曆年）曆年部分不受舊 6 月期末慣例影響；
    但過渡年（最後一個 6 月期末年度＋off 與第一個曆年同號）會撞號，由 _tag_rows／annual_series 依 fiscal_year_ends
    統一期末處理。無票 → 0。"""
    g = facts.get("facts", {}).get("us-gaap", {})
    for t in ("Assets", "NetIncomeLoss", "ProfitLoss", "Revenues"):
        acc = {}
        for rows in g.get(t, {}).get("units", {}).values():
            for x in rows:
                if x.get("form") != "10-K" or not x.get("fy") or not x.get("end"):
                    continue
                if asof and x.get("filed", "9999") >= asof:
                    continue
                k = x.get("accn") or x.get("filed")
                acc[k] = (max(acc.get(k, ("", 0))[0], x["end"]), int(x["fy"]), x.get("filed", ""))
        diffs = [fy - _fy_base(end) for end, fy, _ in
                 sorted((v for v in acc.values() if _early_end(v[0])), key=lambda v: v[2])]
        last = set(diffs[-FY_VOTE_RECENT:])
        if len(diffs) >= FY_VOTE_RECENT and len(last) == 1 and last <= {0, 1}:
            return last.pop()
        votes = Counter(d for d in diffs if d in (0, 1))
        if votes:
            return max(sorted(votes), key=lambda d: votes[d])
    return 0


TRANSITION_DAYS = 30   # 同財年兩個期末相距 >30 天＝會計年度變更過渡年（52/53 週擺動 ≤14 天不受影響）


def _transition_warn(fy, used, dropped):
    a, b = sorted([used, dropped])
    return (f"FY{int(fy)}：同財年有兩個年度期末 {a}／{b}（會計年度變更過渡年），各欄統一取 {used} 期末；"
            f"{dropped} 期末的數字不用（該財年沒有 {used} 期末數字的欄位留缺）")


def _tag_rows(g, t, asof, off, ends=None):
    """單一標籤的 10-K 全年值列（fy, end, filed, val）與警示。
    - 只看 form 為 10-K*（不看 fp：有公司把年報 fp 標成 Q4）且申報日 < asof。
    - 期間型：期間 330–400 天。時點型：期末須與該申報的年度期末相差整數年（±20 天），排除季末時點值。
    - 財年＝_fy_base(期末)，1–6 月期末再＋off（fiscal_year_offset）。給了 ends（{fy: 期末}，fiscal_year_ends 的全公司
      年度期末）時，只留與之相距 ≤30 天的列（不混期間）；之後同財年只留最晚期末的列。
      同財年期末相距 >30 天（會計年度變更的過渡年撞號）記警示。"""
    units = g[t].get("units") or {}
    if not units:
        return pd.DataFrame(columns=["fy", "end", "filed", "val"]), []
    u = "USD" if "USD" in units else ("shares" if "shares" in units else ("USD/shares" if "USD/shares" in units else list(units)[0]))
    xs = []
    for x in units[u]:
        if not str(x.get("form", "")).startswith("10-K") or not x.get("end"):
            continue
        if asof and x.get("filed", "9999") >= asof:   # 申報日須早於決策日（同日申報也不用）
            continue
        if "start" in x:  # 期間型：約一年
            days = (pd.Timestamp(x["end"]) - pd.Timestamp(x["start"])).days
            if not 330 <= days <= 400:
                continue
        xs.append(x)
    acc_end = {}
    for x in xs:
        k = x.get("accn") or x.get("filed")
        acc_end[k] = max(acc_end.get(k, ""), x["end"])
    rows = []
    for x in xs:
        end = pd.Timestamp(x["end"])
        if "start" not in x:
            d = (pd.Timestamp(acc_end[x.get("accn") or x.get("filed")]) - end).days
            if abs(d - round(d / 365.25) * 365.25) > 20:
                continue
        rows.append((_fy_base(end) + (off if _early_end(end) else 0), x["end"], x["filed"], x["val"]))
    df = pd.DataFrame(rows, columns=["fy", "end", "filed", "val"])
    warn = []
    if df.empty:
        return df, warn
    use = df.groupby("fy").end.transform("max")
    if ends:
        use = df.fy.map(ends).fillna(use)
    off_ = (pd.to_datetime(use) - pd.to_datetime(df.end)).dt.days.abs() > TRANSITION_DAYS
    for r in df.assign(use=use)[off_].drop_duplicates(["fy", "end"]).itertuples():
        warn.append(_transition_warn(r.fy, r.use, r.end))
    df = df[~off_]
    df = df[df.end == df.groupby("fy").end.transform("max")]
    return df, warn


def fiscal_year_ends(g, asof, off):
    """全公司各財年的年度期末（{fy: 期末}），供 _tag_rows 統一各標籤的期間：取有全年（12 個月）營收或淨利的最晚期末；
    都沒有的財年才用 Assets 的最晚期末。過渡期 10-KT（只涵蓋 5–7 個月，如 FERG 2025-12-31、MOS 2013-12-31）的
    資產負債表日沒有全年損益，不算年度期末。"""
    def last_end(tags):
        out = {}
        for t in tags:
            if t in g:
                df, _ = _tag_rows(g, t, asof, off)
                for y, e in df.groupby("fy").end.max().items():
                    out[int(y)] = max(e, out.get(int(y), ""))
        return out
    return {**last_end(["Assets"]), **last_end(TAGS["revenue"] + TAGS["net_income"])}


def annual_series(facts, tags, asof, latest=False):
    """回傳 {fy: value}：10-K 全年值；只用 asof 前已申報者；財年見 _tag_rows（1–6 月期末依 fiscal_year_offset）。
    同財年跨所有標籤取「首次申報」（latest=True 取最新申報）版本；同一申報日才依 tags 優先序。
    （公司換標籤時，不讓高優先標籤在後來年報的重編比較數蓋掉低優先標籤的真首次數字。）
    同一財年各標籤一律用同一個期末（fiscal_year_ends；過渡年其他期末的數字不用並警示）。
    attrs：filed／tag／end（各財年所用申報日、標籤、期末日）、warn（同財年兩個期末等警示）。"""
    g = facts.get("facts", {}).get("us-gaap", {})
    off = fiscal_year_offset(facts, asof)
    ends = fiscal_year_ends(g, asof, off)
    parts, warn = [], []
    for pri, t in enumerate(tags):
        if t in g:
            df, w = _tag_rows(g, t, asof, off, ends)
            warn += w
            if not df.empty:
                parts.append(df.assign(pri=pri, tag=t))
    if not parts:
        out = pd.Series(dtype=float)
        out.attrs.update(filed={}, tag={}, end={}, warn=list(dict.fromkeys(warn)))
        return out
    df = pd.concat(parts, ignore_index=True)
    late = df.groupby("fy").end.transform("max")   # 跨標籤也只留最晚期末（fiscal_year_ends 缺該年時的防線；不混期間）
    gap = (pd.to_datetime(late) - pd.to_datetime(df.end)).dt.days
    for r in df.assign(late=late)[gap > TRANSITION_DAYS].drop_duplicates("fy").itertuples():
        warn.append(_transition_warn(r.fy, r.late, r.end))
    df = df[gap <= TRANSITION_DAYS]
    warn = list(dict.fromkeys(warn))
    df = df.sort_values(["filed", "pri"], ascending=[not latest, True]).drop_duplicates("fy").set_index("fy").sort_index()
    out = df.val.astype(float)
    out.index = out.index.astype(int)
    out.attrs.update(filed={int(k): v for k, v in df.filed.items()}, tag={int(k): v for k, v in df.tag.items()},
                     end={int(k): v for k, v in df.end.items()}, warn=warn)
    return out


def debt_series(facts, asof, latest=False):
    """有息負債（P-07 口徑）。回傳 {"debt_lt", "debt_cur", "debt"}：
    debt_cur＝一年內到期長期債＋短期借款（兩組皆缺才用 DebtCurrent）；
    debt＝長期債＋debt_cur；長期債用的是含一年內到期部分的標籤（DEBT_LT_INCL_CURRENT，同年多個取最大值）時只加短期借款；
    長期債組缺而有長短期合計（debt_total）時用合計。
    某年所有組都缺 → 該年 nan（不得當 0）。attrs：filed（各組申報日取最晚）、tag（各組所用標籤）。"""
    S = {k: annual_series(facts, v, asof, latest) for k, v in DEBT_TAGS.items()}
    incl = {t: annual_series(facts, [t], asof, latest) for t in DEBT_LT_INCL_CURRENT}
    yrs = sorted(set().union(*[set(s.index) for s in S.values()]))
    lt_, cur_, tot_ = {}, {}, {}
    filed, tag = {}, {}
    for y in yrs:
        lt, cl, sb, dc, tc = (S[k].get(y, np.nan) for k in ("debt_lt", "cur_ltd", "st_borrow", "debt_current_total", "debt_total"))
        cur = (0 if pd.isna(cl) else cl) + (0 if pd.isna(sb) else sb) if (pd.notna(cl) or pd.notna(sb)) else dc
        lt_tag = S["debt_lt"].attrs["tag"].get(y)
        if lt_tag in DEBT_LT_INCL_CURRENT:   # 含一年內到期的三個標籤同年取最大值
            lt_tag = max((t for t in DEBT_LT_INCL_CURRENT if pd.notna(incl[t].get(y, np.nan))), key=lambda t: incl[t][y])
            lt = incl[lt_tag][y]
            S["debt_lt"].attrs["tag"][y], S["debt_lt"].attrs["filed"][y] = lt_tag, incl[lt_tag].attrs["filed"][y]
        has = lambda k: y in S[k].index and pd.notna(S[k][y])
        if pd.isna(lt) and pd.notna(tc):
            tot, used = tc, ["debt_total"]
        elif pd.isna(lt) and pd.isna(cur):
            tot, used = np.nan, []
        elif lt_tag in DEBT_LT_INCL_CURRENT:
            tot, used = lt + (0 if pd.isna(sb) else sb), ["debt_lt"] + (["st_borrow"] if has("st_borrow") else [])
        else:
            tot = (0 if pd.isna(lt) else lt) + (0 if pd.isna(cur) else cur)
            used = [k for k in ("debt_lt", "cur_ltd", "st_borrow") if has(k)]
            if not (has("cur_ltd") or has("st_borrow")) and has("debt_current_total"):
                used.append("debt_current_total")
        lt_[y], cur_[y], tot_[y] = lt, cur, tot
        if used:   # 只記實際用到的組（標籤字串用於換標籤警示）
            filed[y] = max(S[k].attrs["filed"][y] for k in used)
            tag[y] = "+".join(S[k].attrs["tag"][y] for k in used)
    out = {k: pd.Series(v, dtype=float) for k, v in (("debt_lt", lt_), ("debt_cur", cur_), ("debt", tot_))}
    for s in out.values():
        s.attrs.update(filed=dict(filed), tag=dict(tag), end={}, warn=[])
    return out


def cover_shares(facts, asof):
    """封面流通股數：申報日 < asof 的最新一份申報（同一 accn 的多類股加總）。回傳 (股數, 申報日) 或 (nan, None)。"""
    d = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    rows = [x for x in d if x.get("filed") and (not asof or x["filed"] < asof)]
    if not rows:
        return np.nan, None
    last = max(rows, key=lambda x: (x["filed"], x.get("end", "")))
    if asof and (pd.Timestamp(asof) - pd.Timestamp(last["filed"])).days > 400:
        return np.nan, None   # 過期（多類股公司常停報封面合計股數）→ 改用年報加權稀釋股數
    same = [x for x in rows if x.get("accn") == last.get("accn") and x.get("end") == last.get("end")]
    return float(sum(x["val"] for x in same)), last["filed"]


def cagr(s, n):
    s = s.dropna()
    if len(s) < n + 1 or s.iloc[-n - 1] <= 0 or s.iloc[-1] <= 0:
        return np.nan
    return (s.iloc[-1] / s.iloc[-n - 1]) ** (1 / n) - 1


def features(tk, cik, facts, sic, asof, latest):
    # 股數與每股盈餘取 asof 前「最新申報」版本：後續年報會依拆股追溯調整（規則書 P-17／CL-087 拆股調整）
    S = {k: annual_series(facts, v, asof, latest or k in ("shares", "eps")) for k, v in TAGS.items()}
    S["debt"] = debt_series(facts, asof, latest)["debt"]
    ni = S["net_income"]
    if ni.empty:
        return {"ticker": tk, "cik": cik, "asof": asof, "data_notes": "無年度淨利"}
    yrs = ni.index.max()
    idx10 = range(yrs - 9, yrs + 1)
    used = [v for s in S.values() for k, v in s.attrs.get("filed", {}).items() if k in idx10]
    max_filed = max(used) if used else None
    if asof and max_filed and max_filed >= asof:   # 自檢：不得用到決策日當天或之後申報的數字
        raise LookaheadError(f"前視：{tk} 用到 {max_filed} 申報資料，決策日 {asof}")
    idx = range(yrs - 9, yrs + 1)
    A = pd.DataFrame({k: v.reindex(idx) for k, v in S.items()})
    # annual_series 的警示（會計年度變更過渡年等）併入 data_notes（A3-M1C-01）
    notes = list(dict.fromkeys(w for s in S.values() for w in (s.attrs.get("warn") or [])))
    eq = A.equity
    avg_eq = (eq + eq.shift(1)) / 2
    roe = A.net_income / avg_eq.where(avg_eq > 0)
    debt = A.debt   # 某年有息負債各組標籤皆缺 → nan（不當 0）
    if debt.isna().all():
        notes.append("有息負債缺")
    elif pd.isna(debt.iloc[-1]):
        notes.append("最近一年有息負債缺")
    om = A.operating_income / A.revenue.where(A.revenue > 0)
    ebit = A.pretax + A.interest
    cov = (ebit / A.interest.where(A.interest > 0)).tail(5)
    sh = A.shares.dropna()
    # 3 年前股數：同取最新申報版，但 fy−3 只出現在較舊的 10-K（拆股前基準）；拆股調整在 screen.build 做
    y3 = int(sh.index[-1]) - 3 if len(sh) else None
    sh3 = sh.get(y3, np.nan) if y3 is not None else np.nan
    last = A.iloc[-1]
    fin = 6000 <= int(sic) <= 6799 if str(sic).isdigit() else np.nan
    out = {
        "ticker": tk, "cik": cik, "name": facts.get("entityName"), "sic": sic, "is_financial": fin,
        "fy_last": yrs, "years_of_data": int(ni.reindex(idx).notna().sum()),
        "net_income": last.net_income, "ni5_mean": A.net_income.tail(5).mean() if A.net_income.tail(5).notna().sum() >= 3 else np.nan,
        "revenue": last.revenue, "rev_cagr_5y": cagr(A.revenue, 5),
        "roe": roe.iloc[-1], "roe_med5": roe.tail(5).median() if roe.tail(5).notna().sum() >= 3 else np.nan,
        "roe_min5": roe.tail(5).min() if roe.tail(5).notna().sum() >= 5 else np.nan,
        "roe_sd5": roe.tail(5).std() if roe.tail(5).notna().sum() >= 3 else np.nan,
        "roa": last.net_income / last.assets if last.assets and last.assets > 0 else np.nan,
        "eps_cagr5": cagr(A.eps, 5), "loss_yrs5": int((A.net_income.tail(5) < 0).sum()) if A.net_income.tail(5).notna().sum() >= 5 else np.nan,
        "op_margin_5y_std": om.tail(5).std() if om.tail(5).notna().sum() >= 3 else np.nan,
        "fcf": last.ocf - abs(last.capex) if pd.notna(last.ocf) and pd.notna(last.capex) else np.nan,
        "shares": sh.iloc[-1] if len(sh) else np.nan,
        # 未經拆股調整；screen.build 以 shares_3y_ago／shares_3y_filed＋拆股事件重算並覆寫
        "share_change_3y": sh.iloc[-1] / sh3 - 1 if pd.notna(sh3) and sh3 > 0 else np.nan,
        "shares_3y_ago": sh3,
        "shares_3y_filed": S["shares"].attrs.get("filed", {}).get(y3) if pd.notna(sh3) else None,
        "debt_to_equity": debt.iloc[-1] / last.equity if pd.notna(last.equity) and last.equity > 0 and pd.notna(debt.iloc[-1]) else np.nan,
        "interest_coverage_med5": cov.median() if cov.notna().sum() >= 3 else np.nan,
        "shareholders_equity": last.equity,
        "total_assets": last.assets, "total_liabilities": last.liabilities,
        "liab_to_assets": last.liabilities / last.assets if pd.notna(last.liabilities) and last.assets and last.assets > 0 else np.nan,
        # 初篩（刪垃圾）與困境標記所需欄位
        "ni_prev": A.net_income.iloc[-2] if len(A) >= 2 else np.nan,
        "eq_prev": A.equity.iloc[-2] if len(A) >= 2 else np.nan,
        "n_ni5": int(A.net_income.tail(5).notna().sum()),
        "ni_sum5": A.net_income.tail(5).sum() if A.net_income.tail(5).notna().sum() >= 3 else np.nan,
        "ni5_med": A.net_income.tail(5).median() if A.net_income.tail(5).notna().sum() >= 3 else np.nan,
        "operating_cash_flow": last.ocf,
        "ocf_sum5": A.ocf.tail(5).sum() if A.ocf.tail(5).notna().sum() >= 3 else np.nan,
        "ocf_pos5": int((A.ocf.tail(5) > 0).sum()) if A.ocf.tail(5).notna().sum() >= 3 else np.nan,
        "n_ocf5": int(A.ocf.tail(5).notna().sum()),
        "eps_g1": (A.eps.iloc[-1] / A.eps.iloc[-2] - 1) if len(A) >= 2 and pd.notna(A.eps.iloc[-2]) and A.eps.iloc[-2] > 0 else np.nan,
        "n_roe5": int(roe.tail(5).notna().sum()),
        "util": (4900 <= int(sic) <= 4999) if str(sic).isdigit() else False,
        "asof": asof, "fy_last_filed": ni.attrs.get("filed", {}).get(yrs), "max_filed_used": max_filed,
        "fy_last_end": ni.attrs.get("end", {}).get(yrs),
        "data_notes": "；".join(notes + ["盈餘＝GAAP 淨利代理"]),
    }
    sc, scf = cover_shares(facts, asof)
    out["shares_cover"], out["shares_cover_filed"] = sc, scf
    out["shares_filed_date"] = S["shares"].attrs.get("filed", {}).get(int(sh.index[-1])) if len(sh) else None
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", required=True, help="每行一個代號")
    ap.add_argument("--out", default="features.csv")
    ap.add_argument("--cache", default="edgar_cache")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD 決策日；只用申報日早於此日的資料（預設今天）")
    ap.add_argument("--latest", action="store_true", help="同財年取最新申報版（預設首次申報）")
    ap.add_argument("--ua", default=None, help="SEC User-Agent（名稱＋email）；預設讀環境變數 SEC_USER_AGENT")
    ap.add_argument("--predecessors", default=None,
                    help="前身 CIK 對照 JSON（格式同 trader/trader/data/cik_predecessors.json）；預設讀 repo 內該檔，不存在則不合併")
    a = ap.parse_args()
    global UA
    UA = a.ua or UA
    if "@" not in UA:
        ap.error("SEC 要求 User-Agent 含聯絡 email：設定環境變數 SEC_USER_AGENT 或傳 --ua")
    if not a.asof:
        a.asof = time.strftime("%Y-%m-%d")
    os.makedirs(a.cache, exist_ok=True)
    tickers = [x.strip().upper() for x in open(a.tickers) if x.strip() and not x.startswith("#")]
    tn = []
    try:
        m = cached(os.path.join(a.cache, "company_tickers.json"), "https://www.sec.gov/files/company_tickers.json",
                   a.asof, tn)
    except urllib.error.HTTPError as e:
        if e.code == 403:   # 代號表就被拒：不往下逐檔打
            raise SystemExit("SEC 拒絕存取代號對照表（HTTP 403）：檢查 SEC_USER_AGENT，稍後重跑")
        raise
    for x in tn:
        print("警告：" + x, file=sys.stderr)
    t2c = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in m.values()}
    pre_path = a.predecessors or (PREDECESSORS if os.path.exists(PREDECESSORS) else None)
    pmap = load_predecessors(pre_path) if pre_path else {}
    rows = []
    streak_403 = 0
    for tk in tickers:
        cik = t2c.get(tk) or t2c.get(tk.replace(".", "-"))
        if not cik:
            rows.append({"ticker": tk, "data_notes": "SEC 對照表找不到代號"})
            continue
        try:
            cn = []   # 快取過期且重抓失敗、沿用舊檔的註記
            facts = cached(os.path.join(a.cache, f"CIK{cik}.json"), f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                           a.asof, cn)
            sub = cached(os.path.join(a.cache, f"sub_CIK{cik}.json"), f"https://data.sec.gov/submissions/CIK{cik}.json",
                         a.asof, cn)
            used, failed, pre = [], [], []
            for pc in pmap.get(cik, []):
                try:
                    pre.append(cached(os.path.join(a.cache, f"CIK{pc}.json"),
                                      f"https://data.sec.gov/api/xbrl/companyfacts/CIK{pc}.json", a.asof, cn))
                    used.append(pc)
                except Exception as e:   # 前身失敗：用現行 facts 照常算並留痕；403 交給下面的連續計數
                    if isinstance(e, urllib.error.HTTPError) and e.code == 403:
                        raise
                    failed.append(pc)
            row = features(tk, cik, merge_facts(facts, pre) if pre else facts, sub.get("sic", ""), a.asof, a.latest)
            notes = [row.get("data_notes")] + (["含前身 CIK " + "、".join(used)] if used else []) + \
                (["前身 CIK " + "、".join(failed) + " 抓取失敗未併入"] if failed else []) + cn
            row["data_notes"] = "；".join(x for x in notes if x)
            rows.append(row)
            streak_403 = 0
        except LookaheadError:   # 前視＝時點紀律破壞：整批中止、不寫輸出檔（A3-M1C-02）
            raise
        except Exception as e:  # 單檔失敗不影響其他檔；該檔列未評（記型別＋訊息前 120 字，A3-M1C-02）
            rows.append({"ticker": tk, "cik": cik, "data_notes": f"抓取失敗：{type(e).__name__}: {str(e)[:120]}"})
            streak_403 = streak_403 + 1 if isinstance(e, urllib.error.HTTPError) and e.code == 403 else 0
            if streak_403 >= MAX_403_STREAK:   # 不寫輸出檔（避免把半份名單當成完整結果）
                raise SystemExit(f"SEC 拒絕存取（連續 {streak_403} 檔 HTTP 403）：檢查 SEC_USER_AGENT，稍後重跑")
    pd.DataFrame(rows).to_csv(a.out, index=False)
    print(f"{len(rows)} 檔 → {a.out}")


if __name__ == "__main__":
    main()
