#!/usr/bin/env python3
"""資料包骨架：從 SEC companyfacts 產生 10 年財報表（只用決策日前已申報者），價格另存一檔（價格隔離）。

  SEC_USER_AGENT="名稱 email" python build_dossier.py --ticker KO --asof 2026-10-15 --outdir dossier \
      [--cache edgar_cache] [--price 86.1 --price-date 2026-10-14] [--t30 0.0475 --t30-date 2026-10-14] [--t30-20y-med 0.035]

輸出：
  dossier/代號.md         10 年財報表＋衍生比率＋申報日；**不含任何價格**。年報文字節錄由 DOSSIER_TEXT 工作接著追加。
  dossier/代號_price.json  價格、利率（作答閘門 3 問卷並存檔後才可開啟）。
需要 ../../berkshire-screen-merged/scripts/fetch_edgar.py（共用抓取與時點邏輯）。
"""
import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "berkshire-screen-merged", "scripts"))
import fetch_edgar as F  # noqa: E402

EXTRA = {"dna": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet"],
         "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
         "tax": ["IncomeTaxExpenseBenefit"], "gross": ["GrossProfit"], "goodwill": ["Goodwill"],
         "dividends": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
         "buyback": ["PaymentsForRepurchaseOfCommonStock"], "buyback_shares": ["StockRepurchasedDuringPeriodShares"],
         "equity_income": ["IncomeLossFromEquityMethodInvestments"], "cash": ["CashAndCashEquivalentsAtCarryingValue"]}


def build(facts, tk, asof):
    tags = dict(F.TAGS); tags.update(EXTRA)
    S = {k: F.annual_series(facts, v, asof, k in ("shares", "eps")) for k, v in tags.items()}
    if S["net_income"].empty:
        return None, None
    last = int(S["net_income"].index.max()); yrs = range(last - 9, last + 1)
    T = pd.DataFrame({k: v.reindex(yrs) for k, v in S.items()})
    R = pd.DataFrame(index=T.index)
    R["營業利益率"] = T.operating_income / T.revenue
    R["ROE（期初期末平均）"] = T.net_income / ((T.equity + T.equity.shift(1)) / 2)
    R["營業現金流÷淨利"] = T.ocf / T.net_income
    R["資本支出÷折舊"] = T.capex.abs() / T.dna
    R["資本支出÷營收"] = T.capex.abs() / T.revenue
    R["有效稅率"] = T.tax / T.pretax
    R["有息負債÷權益"] = (T.debt_lt.fillna(0) + T.debt_cur.fillna(0)) / T.equity
    R["利息保障（稅前息前÷利息）"] = (T.pretax + T.interest) / T.interest
    filed = {k: v.attrs.get("filed", {}) for k, v in S.items()}
    return T, (R, filed, last, checks(T))


def checks(T):
    """資料品質警示：只標示，不改數字。"""
    W = []
    for y, r in T.iterrows():
        if pd.notna(r.eps) and pd.notna(r.shares) and r.shares > 0 and pd.notna(r.net_income) and r.eps != 0:
            d = (r.net_income / r.shares) / r.eps - 1
            if abs(d) > 0.05:
                W.append(f"FY{y}：淨利÷稀釋股數 與 EPS 差 {d:+.0%}（特別股股利、少數股權、拆股基準或單位問題，需查年報）")
        if pd.notna(r.eps) and r.eps == round(r.eps) and r.eps != 0:
            W.append(f"FY{y}：EPS 為整數 {r.eps:g}，疑似四捨五入或單位錯誤，需對年報")
        if pd.notna(r.ocf) and pd.notna(r.net_income) and r.net_income > 0 and r.ocf / r.net_income < 0.5:
            W.append(f"FY{y}：營業現金流不到淨利一半（盈餘品質關需作答）")
    for c in ("revenue", "net_income", "equity", "ocf", "capex", "dna", "shares"):
        n = int(T[c].isna().sum()) if c in T else len(T)
        if n:
            W.append(f"{c}：10 年中缺 {n} 年")
    return W


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True); ap.add_argument("--asof", required=True)
    ap.add_argument("--outdir", default="dossier"); ap.add_argument("--cache", default="edgar_cache")
    ap.add_argument("--ua", default=None)
    ap.add_argument("--price", type=float); ap.add_argument("--price-date")
    ap.add_argument("--t30", type=float); ap.add_argument("--t30-date"); ap.add_argument("--t30-20y-med", type=float)
    a = ap.parse_args()
    F.UA = a.ua or F.UA
    os.makedirs(a.outdir, exist_ok=True); os.makedirs(a.cache, exist_ok=True)
    m = F.cached(os.path.join(a.cache, "company_tickers.json"), "https://www.sec.gov/files/company_tickers.json")
    t2c = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in m.values()}
    tk = a.ticker.upper(); cik = t2c.get(tk) or t2c.get(tk.replace(".", "-"))
    if not cik:
        sys.exit(f"找不到 {tk} 的 CIK")
    facts = F.cached(os.path.join(a.cache, f"CIK{cik}.json"), f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    T, extra = build(facts, tk, a.asof)
    if T is None:
        sys.exit(f"{tk}：決策日前沒有可用的年度淨利 → MISSING_DATA")
    R, filed, last, warn = extra
    fmt = lambda df: df.T.to_markdown(floatfmt=",.4g")
    L = [f"# 資料包：{tk}（CIK {cik}；決策日 {a.asof}）", "",
         "來源：SEC XBRL companyfacts，10-K 全年值；同財年取首次申報版（股數、EPS 取決策日前最新申報版）；只用申報日早於決策日者。",
         "**本檔不含任何價格、市值、本益比。** 缺值為 nan，不得補假設值。", "",
         "## 10 年財報（美元；EPS 每股；股數為稀釋加權）", "", fmt(T.drop(columns=["buyback_shares"])), "",
         "## 衍生比率（由上表計算）", "", fmt(R), "",
         "## 資料品質警示（只標示、未改數字；DOSSIER_TEXT 工作逐項到原文核對並寫結果）", "",
         *(["- " + w for w in warn] or ["- 無"]), "",
         "## 各財年淨利的 10-K 申報日", "", json.dumps({int(k): v for k, v in filed["net_income"].items()}, ensure_ascii=False), "",
         "## 年報文字節錄（DOSSIER_TEXT 工作追加：業務、分部、風險因素、管理層討論、債務到期表、會計師關鍵查核事項、委託書薪酬；每段標文件、節、申報日）", ""]
    open(os.path.join(a.outdir, f"{tk}.md"), "w", encoding="utf-8").write("\n".join(L))
    # 平均回購價（回購金額÷回購股數）是歷史價格水準：不放資料包，放價格檔，G7 之後才可比對內在價值
    ab = (T.buyback / T.buyback_shares).replace([float("inf"), -float("inf")], float("nan"))
    avg_buyback = {int(y): round(float(v), 4) for y, v in ab.items() if pd.notna(v)}
    json.dump({"ticker": tk, "decision_date": a.asof, "price": a.price, "price_date": a.price_date,
               "t30": a.t30, "t30_date": a.t30_date, "t30_20y_median": a.t30_20y_med,
               "avg_buyback_price_by_fy": avg_buyback,
               "note": "價格隔離：閘門 3 問卷與 G1–G6 存檔後才可開啟本檔"},
              open(os.path.join(a.outdir, f"{tk}_price.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{tk}：財報至 FY{last} → {a.outdir}/{tk}.md、{tk}_price.json")


if __name__ == "__main__":
    main()
