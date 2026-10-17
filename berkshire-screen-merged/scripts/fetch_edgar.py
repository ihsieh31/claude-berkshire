#!/usr/bin/env python3
"""從 SEC EDGAR companyfacts（免費）抓年度財報並算初篩特徵。

  SEC_USER_AGENT="你的名稱 你的email" python fetch_edgar.py --tickers tickers.txt --out features.csv --asof 2026-10-12 [--cache edgar_cache]

- SEC 要求 User-Agent 帶聯絡方式：用環境變數 SEC_USER_AGENT 或 --ua 提供；請求間隔 ≥0.21 秒（≤5 req/s）。
- 市值用股數：封面股數（dei:EntityCommonStockSharesOutstanding，10-K／10-Q 皆可，申報日 < 決策日的最新一份；多類股加總），
  輸出 shares_cover 與其申報日 shares_cover_filed，供 screen.py 依之後的拆股調整（fetch_prices.py 提供拆股事件）。
- 只取 10-K／10-K/A 的全年值（fp=FY、期間約 1 年）；同一財年取「首次申報」版本，避免用到事後重編數字
  （可加 --latest 改取最新版本）。時點：只用申報日（filed）早於決策日 --asof 的資料（filed < asof；
  --asof 未給時預設為今天）；輸出 fy_last_filed（最近財年淨利的申報日）與 max_filed_used（實際用到的最晚申報日），
  並自檢 max_filed_used < asof，違反即中止（防前視）。
- 輸出欄位：ticker, cik, name, fy_last, years_of_data, net_income, ni5_mean, revenue, rev_cagr_5y,
  roe, roe_med5, roe_min5, roe_sd5, roa, eps_cagr5, loss_yrs5, op_margin_5y_std, fcf, shares,
  share_change_3y, debt_to_equity（有息負債／權益，P-07 口徑）, interest_coverage_med5,
  shareholders_equity, is_financial（SIC 6000–6799）, ni_prev, eq_prev, n_ni5, ni_sum5, ni5_med,
  operating_cash_flow, ocf_sum5, ocf_pos5, n_ocf5, eps_g1, n_roe5, util, asof, fy_last_filed, max_filed_used, data_notes。
- 缺的欄位留空（初篩判 PASS-資料不足，不刪）。盈餘為 GAAP 淨利代理；全域營業盈餘口徑在深度分析重編。
"""
import argparse
import json
import os
import time
import urllib.request

import numpy as np
import pandas as pd

UA = os.environ.get("SEC_USER_AGENT", "")
REQ_INTERVAL = 0.21
TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "InterestAndDividendIncomeOperating"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "operating_income": ["OperatingIncomeLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "debt_lt": ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"],
    "debt_cur": ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings", "CommercialPaper"],
    "interest": ["InterestExpense", "InterestExpenseDebt", "InterestPaidNet"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"],
}
_last = [0.0]


def get(url):
    wait = REQ_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as r:
        _last[0] = time.time()
        return json.loads(r.read().decode("utf-8"))


def cached(path, url):
    if os.path.exists(path):
        return json.load(open(path))
    d = get(url)
    json.dump(d, open(path, "w"))
    return d


def _one_tag(g, t, asof, latest):
    units = g[t]["units"]
    u = "USD" if "USD" in units else ("shares" if "shares" in units else ("USD/shares" if "USD/shares" in units else list(units)[0]))
    rows = []
    for x in units[u]:
        if not str(x.get("form", "")).startswith("10-K") or x.get("fp") != "FY":
            continue
        if asof and x.get("filed", "9999") >= asof:   # 申報日須早於決策日（同日申報也不用）
            continue
        if "start" in x:  # 期間型：約一年
            days = (pd.Timestamp(x["end"]) - pd.Timestamp(x["start"])).days
            if not 330 <= days <= 400:
                continue
        end = pd.Timestamp(x["end"])
        rows.append((end.year if end.month > 6 else end.year - 1, x["end"], x["filed"], x["val"]))
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["fy", "end", "filed", "val"])
    df = df[df.end == df.groupby("fy").end.transform("max")]
    df = df.sort_values("filed", ascending=not latest).drop_duplicates("fy")
    s = df.set_index("fy").val.sort_index()
    s.attrs["filed"] = df.set_index("fy").filed.sort_index().to_dict()
    return s


def annual_series(facts, tags, asof, latest=False):
    """回傳 {fy: value}：10-K 全年值；同財年取首次申報（或最新）版本；只用 asof 前已申報者。
    公司常在不同年度換用不同標籤：依 tags 優先序逐年補齊（combine_first）。"""
    g = facts.get("facts", {}).get("us-gaap", {})
    out = pd.Series(dtype=float); filed = {}
    for t in tags:
        if t in g:
            s = _one_tag(g, t, asof, latest)
            if not s.empty:
                for k, v in s.attrs.get("filed", {}).items():
                    if k not in out.index or pd.isna(out.get(k)):
                        filed[k] = v
                out = s if out.empty else out.combine_first(s)
    out = out.sort_index(); out.attrs["filed"] = filed
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
    ni = S["net_income"]
    if ni.empty:
        return {"ticker": tk, "cik": cik, "asof": asof, "data_notes": "無年度淨利"}
    yrs = ni.index.max()
    idx10 = range(yrs - 9, yrs + 1)
    used = [v for s in S.values() for k, v in s.attrs.get("filed", {}).items() if k in idx10]
    max_filed = max(used) if used else None
    if asof and max_filed and max_filed >= asof:   # 自檢：不得用到決策日當天或之後申報的數字
        raise RuntimeError(f"前視：{tk} 用到 {max_filed} 申報資料，決策日 {asof}")
    idx = range(yrs - 9, yrs + 1)
    A = pd.DataFrame({k: v.reindex(idx) for k, v in S.items()})
    notes = []
    eq = A.equity
    avg_eq = (eq + eq.shift(1)) / 2
    roe = A.net_income / avg_eq.where(avg_eq > 0)
    debt = A.debt_lt.fillna(0) + A.debt_cur.fillna(0)
    if A.debt_lt.isna().all() and A.debt_cur.isna().all():
        debt = pd.Series(np.nan, index=A.index)
        notes.append("有息負債缺")
    om = A.operating_income / A.revenue.where(A.revenue > 0)
    ebit = A.pretax + A.interest
    cov = (ebit / A.interest.where(A.interest > 0)).tail(5)
    sh = A.shares.dropna()
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
        "share_change_3y": sh.iloc[-1] / sh.iloc[-4] - 1 if len(sh) >= 4 and sh.iloc[-4] > 0 else np.nan,
        "debt_to_equity": debt.iloc[-1] / last.equity if pd.notna(last.equity) and last.equity > 0 else np.nan,
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
    a = ap.parse_args()
    global UA
    UA = a.ua or UA
    if "@" not in UA:
        ap.error("SEC 要求 User-Agent 含聯絡 email：設定環境變數 SEC_USER_AGENT 或傳 --ua")
    if not a.asof:
        a.asof = time.strftime("%Y-%m-%d")
    os.makedirs(a.cache, exist_ok=True)
    tickers = [x.strip().upper() for x in open(a.tickers) if x.strip() and not x.startswith("#")]
    m = cached(os.path.join(a.cache, "company_tickers.json"), "https://www.sec.gov/files/company_tickers.json")
    t2c = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in m.values()}
    rows = []
    for tk in tickers:
        cik = t2c.get(tk) or t2c.get(tk.replace(".", "-"))
        if not cik:
            rows.append({"ticker": tk, "data_notes": "SEC 對照表找不到代號"})
            continue
        try:
            facts = cached(os.path.join(a.cache, f"CIK{cik}.json"), f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
            sub = cached(os.path.join(a.cache, f"sub_CIK{cik}.json"), f"https://data.sec.gov/submissions/CIK{cik}.json")
            rows.append(features(tk, cik, facts, sub.get("sic", ""), a.asof, a.latest))
        except Exception as e:  # 單檔失敗不影響其他檔；該檔列未評
            rows.append({"ticker": tk, "cik": cik, "data_notes": f"抓取失敗：{type(e).__name__}"})
    pd.DataFrame(rows).to_csv(a.out, index=False)
    print(f"{len(rows)} 檔 → {a.out}")


if __name__ == "__main__":
    main()
