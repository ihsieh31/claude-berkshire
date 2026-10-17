#!/usr/bin/env python3
"""合併初篩：結構性垃圾刪除 → 統計排除 → 分道 → 研究優先排序 → 前 N 檔（預設 15）交深度分析。

  python screen.py --features features.csv --prices prices.csv --asof 2026-10-15 \
      [--exclusions exclusions.csv] [--top 15] [--special-max 0] [--out result]

輸入：fetch_edgar.py 的 features.csv、fetch_prices.py 的 prices.csv（同一 --asof）、可選的制度類排除檔
（欄位 ticker 加任一 excl_asset_class／excl_competence／excl_unvaluable／excl_fraud／excl_recent_reject 為真）。
輸出：result.csv（全池逐檔）、result.md（給人看）、result_handoff.json（交深度分析）。

規則與證據見 ../SKILL.md 與 ../references/evidence.md。本腳本只排研究順序，不是買進訊號。
"""
import argparse
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, __import__("os").path.dirname(__file__))
import screen_quant as SQ  # noqa: E402  結構性刪除（J1/J3/J4/J7）、困境標記（T1–T5）、會計旗標（F5）

MIN_UNIVERSE_FOR_PCT = 60
# 名單 < 60 檔時的絕對門檻（大型股母體歷史各期分位數的中位數）
ABS = {"vol_36m": 0.35, "earn_yield": 0.025, "roa": 0.025, "roe_fin": 0.08, "drawdown_3y": -0.35}
EXCL_COLS = {"excl_asset_class": "X1_不適用資產", "excl_competence": "X2_能力圈外", "excl_unvaluable": "X3_無法估值",
             "excl_fraud": "X4_誠信紅旗", "excl_recent_reject": "X5_近期已淘汰"}
STRONG_HISTORY = {"loss_yrs5_max": 1, "roe_med5_min": 0.12}


def split_factor(splits, after_date):
    """after_date 之後發生的拆股比例連乘（比例＝新股數/舊股數）。splits 格式 'YYYY-MM-DD:ratio;...'。"""
    if not isinstance(splits, str) or not splits or not isinstance(after_date, str) or not after_date:
        return 1.0
    f = 1.0
    for item in splits.split(";"):
        if ":" in item:
            d, r = item.split(":")
            if d > after_date[:10]:
                f *= float(r)
    return f


def build(features, prices, exclusions, asof):
    df = features.merge(prices, on="ticker", how="left", suffixes=("", "_px"))
    if exclusions is not None:
        df = df.merge(exclusions, on="ticker", how="left")
    # 市值：close（抓取當下的拆股基準）× 封面股數（調整其申報日之後的拆股）；缺封面股數 → 用年報加權股數
    sh, sh_date = df.get("shares_cover"), df.get("shares_cover_filed")
    if sh is None:
        sh, sh_date = pd.Series(np.nan, index=df.index), pd.Series(None, index=df.index)
    use_cover = sh.notna() & (sh > 0)
    shares = np.where(use_cover, sh, df.get("shares"))
    base_date = np.where(use_cover, sh_date, df.get("shares_filed_date"))
    sp = df["splits"] if "splits" in df else pd.Series("", index=df.index)
    adj = [split_factor(s, d) for s, d in zip(sp, base_date)]
    df["shares_mcap"] = pd.to_numeric(pd.Series(shares, index=df.index), errors="coerce") * adj
    df["market_cap"] = df["price"] * df["shares_mcap"]
    mc = df["market_cap"].where(df["market_cap"] > 0)
    if "sic" in df:  # 產業恐慌標記（T5）用 SIC 前兩碼當產業
        df["sector"] = pd.to_numeric(df["sic"], errors="coerce").floordiv(100).astype("Int64").astype(str)
    df["earn_yield"] = df["net_income"] / mc
    df["fcf_yield"] = df["fcf"] / mc
    df["norm_earn_yield"] = df["ni5_mean"] / mc
    # 資料品質：盈餘殖利率絕對值 > 100% 多半是股數或單位錯誤 → 視為缺值並註記
    bad = df["earn_yield"].abs() > 1.0
    df.loc[bad, ["earn_yield", "fcf_yield", "norm_earn_yield"]] = np.nan
    df["dq_note"] = np.where(bad, "盈餘殖利率異常（疑似股數或單位錯誤），已設缺值", "")
    return df


def structural(df, asof):
    """結構性刪除（多年虧損／結構性損害）與制度類排除。回傳 delete_codes、distress_codes、flag_codes。"""
    q = SQ.screen(SQ.from_live(df.copy(), int(asof[:4])), decision_date=asof)
    for c in ("delete_codes", "distress_codes", "flag_codes", "pit_checked"):
        df[c] = q[c].values
    inst = pd.Series("", index=df.index)
    for c, code in EXCL_COLS.items():
        if c in df:
            m = df[c].fillna(False).astype(str).str.lower().isin(["1", "true", "yes", "y"])
            inst = inst.where(~m, inst + code + ";")
    df["delete_codes"] = (df["delete_codes"].fillna("") + ";" + inst.str.rstrip(";")).str.strip(";")
    return df


def statistical(df, top, special_max):
    fin = df["is_financial"].fillna(False).astype(str).str.lower().isin(["1", "true", "1.0"])
    nofin = ~fin & df["is_financial"].isna()
    # 無產業碼時才近似：負債/資產 > 80%、權益為正、ROA < 3%（排除以回購造成負權益或高負債的非金融公司）
    approx = (df["liab_to_assets"].fillna(0) > 0.8) & (df["shareholders_equity"] > 0) & (df["roa"] < 0.03)
    fin = fin | (nofin & approx.fillna(False))
    df["fin_source"] = np.where(nofin, "近似（無 SIC）", "SIC")
    df["fin_like"] = fin.astype(int)
    deleted = df["delete_codes"] != ""
    pool = df                                                          # 百分位以全池計（與實證研究的母體定義一致）
    use_pct = len(pool) >= MIN_UNIVERSE_FOR_PCT

    def pct(col, group=False):
        s = pool.groupby("fin_like")[col].rank(pct=True) if group else pool[col].rank(pct=True)
        return s.reindex(df.index)

    reasons = pd.Series("", index=df.index)

    def flag(mask, text):
        nonlocal reasons
        mask = mask.fillna(False) & ~deleted
        reasons = reasons.where(~mask, reasons + text + "；")

    if use_pct:
        flag(pct("vol_36m") >= 0.8, "股價波動屬最高 20%")
        flag((pct("earn_yield") <= 0.2) | (df["earn_yield"] <= 0), "盈餘殖利率屬最低 20% 或虧損")
        flag((df.fin_like == 0) & (pct("roa", True) <= 0.2), "資產報酬率屬非金融最低 20%")
        flag((df.fin_like == 1) & (pct("roe", True) <= 0.2), "ROE 屬金融最低 20%")
        deep = pct("drawdown_3y") <= 0.2
    else:
        flag(df["vol_36m"] > ABS["vol_36m"], f"年化波動 > {ABS['vol_36m']:.0%}")
        flag(df["earn_yield"] < ABS["earn_yield"], f"盈餘殖利率 < {ABS['earn_yield']:.1%} 或虧損")
        flag((df.fin_like == 0) & (df["roa"] < ABS["roa"]), f"ROA < {ABS['roa']:.1%}")
        flag((df.fin_like == 1) & (df["roe"] < ABS["roe_fin"]), f"金融 ROE < {ABS['roe_fin']:.0%}")
        deep = df["drawdown_3y"] < ABS["drawdown_3y"]
    df["exclude_reasons"] = reasons.str.rstrip("；")
    excluded = df["exclude_reasons"] != ""
    deep = deep.fillna(False) & ~deleted

    def g(col, higher=True):
        s = pct(col, True).fillna(0.5)
        return s if higher else 1 - s
    # 2026-10-17：營收成長改中性，換成 5 年最差 ROE（負權益年度為缺值 → 0.5 中性）；理由見 evidence_statistical.md §2
    nonfin = (g("fcf_yield") + g("norm_earn_yield") + g("roe_min5") + g("op_margin_5y_std", False)
              + g("debt_to_equity", False) + g("share_change_3y", False)) / 6
    finsc = (g("share_change_3y", False) + g("roe") + g("roa")) / 3
    raw = pd.Series(np.where(df.fin_like == 1, finsc, nonfin), index=df.index)
    df["research_priority"] = raw.where(~deleted).groupby(df.fin_like).rank(pct=True).round(3)

    lacking = df[["vol_36m", "earn_yield"]].isna().any(axis=1)
    strong = (df["loss_yrs5"] <= STRONG_HISTORY["loss_yrs5_max"]) & (df["roe_med5"] >= STRONG_HISTORY["roe_med5_min"])
    df["strong_history"] = strong.fillna(False)
    df["lane"] = np.select(
        [deleted, excluded & deep, excluded & df["strong_history"] & (df["distress_codes"].fillna("") != ""), excluded, lacking, deep],
        ["刪除（結構性損害／制度類）", "特殊情境（被排除且深度回撤）", "特殊情境（困境中但歷史經濟力佳）",
         "排除（統計高風險）", "資料不足", "特殊情境（深度回撤）"],
        default="核心道")
    core = df[df.lane == "核心道"].sort_values("research_priority", ascending=False)
    df["core_rank"] = np.nan
    df.loc[core.index, "core_rank"] = range(1, len(core) + 1)
    df["shortlist"] = df["core_rank"] <= top
    # 特殊情境交棒（預設 0 檔）：只交「歷史經濟力佳」者（5 年虧損 ≤1、5 年 ROE 中位 ≥12%），依 5 年最差 ROE 由高到低
    sp = df[df.lane.str.startswith("特殊情境") & df["strong_history"]]
    sp = sp.sort_values(["roe_min5", "roe_med5"], ascending=False).head(special_max)
    df["special_handoff"] = df.index.isin(sp.index)
    return df, use_pct


HANDOFF_COLS = ["ticker", "name", "core_rank", "research_priority", "lane", "market_cap", "price", "price_date",
                "earn_yield", "fcf_yield", "norm_earn_yield", "roe", "roe_med5", "roe_min5", "roa", "rev_cagr_5y",
                "op_margin_5y_std", "debt_to_equity", "share_change_3y", "vol_36m", "drawdown_3y", "fin_like",
                "exclude_reasons", "delete_codes", "distress_codes", "flag_codes", "fy_last", "fy_last_filed",
                "max_filed_used", "dq_note"]


# 由價格衍生、或會洩露價格／排名的欄位：交接檔另放 price_context，深度分析前半（G1–G6、閘門 3）不得看到
PRICE_COLS = ["core_rank", "research_priority", "market_cap", "price", "price_date", "earn_yield", "fcf_yield",
              "norm_earn_yield", "vol_36m", "drawdown_3y"]


def records(d, split=False):
    keep = [c for c in HANDOFF_COLS if c in d.columns]
    rows = json.loads(d[keep].to_json(orient="records", force_ascii=False))
    if split:
        for r in rows:
            r["price_context"] = {c: r.pop(c) for c in PRICE_COLS if c in r}
    return rows


def handoff(df, asof, out):
    def ctx(d):
        r = records(d, split=True)
        for x in r:
            x["screen_decision"] = ("PASS-困境" if x.get("distress_codes") else "PASS")
        return r
    payload = {
        "asof": asof,
        "core_shortlist": ctx(df[df.shortlist].sort_values("core_rank")),
        "special_handoff": ctx(df[df.special_handoff]),
        "special_watch": records(df[df.lane.str.startswith("特殊情境")].sort_values("drawdown_3y")),
        "counts": df["lane"].value_counts().to_dict(),
        "note": "初篩只降低永久損失機率並排出研究順序，不是買進訊號；排名不得作為任何閘門的理由。"
                "core_shortlist／special_handoff 每檔的 price_context 含價格與排名，深度分析前半不得提供。",
    }
    with open(out + "_handoff.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def report(df, use_pct, out, top, asof):
    L = [f"# 初篩結果（決策日 {asof}，{len(df)} 檔）", "",
         "門檻：" + ("本次名單橫斷面百分位" if use_pct else f"名單少於 {MIN_UNIVERSE_FOR_PCT} 檔，改用絕對門檻（證據較弱）"), ""]
    L += ["| 分道 | 檔數 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in df["lane"].value_counts().items()]

    def tbl(d, cols):
        cols = [c for c in cols if c in d.columns]
        if not len(d):
            return "（無）"
        rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for _, r in d[cols].iterrows():
            rows.append("| " + " | ".join("" if pd.isna(v) else (f"{v:.3f}" if isinstance(v, float) else str(v)) for v in r) + " |")
        return "\n".join(rows)
    L += ["", f"## 核心短名單（前 {top}，交深度分析）", "",
          tbl(df[df.shortlist].sort_values("core_rank"), ["ticker", "name", "core_rank", "research_priority", "earn_yield",
              "fcf_yield", "roe", "op_margin_5y_std", "debt_to_equity", "share_change_3y", "distress_codes", "flag_codes"])]
    L += ["", "## 特殊情境（不是候選；只有寫得出危機論點時才研究）", "",
          tbl(df[df.lane.str.startswith("特殊情境")].sort_values("drawdown_3y").head(25),
              ["ticker", "name", "lane", "drawdown_3y", "roe_med5", "roe_min5", "strong_history", "special_handoff", "exclude_reasons"])]
    dl = df[df.delete_codes != ""]
    L += ["", f"## 刪除清單（{len(dl)} 檔；每筆都要人工複核，見 SKILL.md 第 6 節）", "",
          tbl(dl, ["ticker", "name", "delete_codes", "fin_like", "fy_last_filed"])]
    with open(out + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--features", required=True)
    ap.add_argument("--prices", required=True)
    ap.add_argument("--asof", required=True, help="YYYY-MM-DD；須與抓資料時相同")
    ap.add_argument("--exclusions")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--special-max", type=int, default=0, help="特殊情境道交深度分析的檔數上限（預設 0）")
    ap.add_argument("--out", default="result")
    a = ap.parse_args()
    feat, px = pd.read_csv(a.features), pd.read_csv(a.prices)
    if "asof" in feat and feat["asof"].dropna().astype(str).ne(a.asof).any():
        sys.exit("features.csv 的 asof 與 --asof 不同：請以同一決策日重抓")
    ex = pd.read_csv(a.exclusions) if a.exclusions else None
    df = build(feat, px, ex, a.asof)
    df = structural(df, a.asof)
    df, use_pct = statistical(df, a.top, a.special_max)
    df.to_csv(a.out + ".csv", index=False)
    handoff(df, a.asof, a.out)
    report(df, use_pct, a.out, a.top, a.asof)
    print(f"共 {len(df)} 檔；" + "，".join(f"{k}：{v}" for k, v in df["lane"].value_counts().items()))
    print(f"短名單 {int(df.shortlist.sum())} 檔；特殊情境交棒 {int(df.special_handoff.sum())} 檔 → {a.out}_handoff.json")


if __name__ == "__main__":
    main()
