#!/usr/bin/env python3
"""抓月價格（免費來源）並算 36 月年化波動、3 年回撤、最新價。

  python fetch_prices.py --tickers tickers.txt --out prices.csv [--asof 2026-10-12] [--manual manual_prices.csv]

來源優先序：
  1. --manual：使用者提供的 CSV（ticker,date,adj_close），任何來源皆可（例如券商匯出）。
  2. Yahoo Finance chart 介面（免費、非官方；query1.finance.yahoo.com/v8/finance/chart，月線、還原價），
     每次請求間隔 ≥0.5 秒。
抓不到的代號：price 等欄留空 → 不影響刪除判定（刪除只看財報），深度分析改用正式報價。
價格用於市值、波動、3 年回撤與困境標記；跌幅不是買賣理由。
時點：--asof 必須與 fetch_edgar.py 的決策日相同；只用 asof 當日或之前的價格（未給 asof＝今天、取最新報價）。
  Yahoo 月 K 線時間戳為月初、當月那根的收盤是抓取當下：給了早於今天的 asof 時，丟掉含 asof 的當月 K 線
  （price＝上月底收盤、price_date＝上月 K 線時間戳；A3-M1C-09）。--manual 不受影響。
拆股：Yahoo 的 close 已依「抓取當下為止」的全部拆股調整；輸出 splits（日期:比例；比例＝新股數/舊股數），
screen.py 以「封面股數申報日之後」的拆股比例調整股數，使市值＝close × 調整後股數 在同一拆股基準。
"""
import argparse
import json
import time
import urllib.request

import numpy as np
import pandas as pd

UA = "Mozilla/5.0 (research script)"


def yahoo_monthly(tk):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{tk.replace('.', '-')}?range=10y&interval=1mo&includeAdjustedClose=true&events=split"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]
    ts = pd.to_datetime(d["timestamp"], unit="s")
    q = d["indicators"]
    adj = q.get("adjclose", [{}])[0].get("adjclose") or q["quote"][0]["close"]
    close = q["quote"][0]["close"]
    s = pd.DataFrame({"date": ts, "adj_close": adj, "close": close}).dropna()
    last_px = d.get("meta", {}).get("regularMarketPrice")
    sp = (d.get("events") or {}).get("splits") or {}
    splits = ";".join(f"{pd.Timestamp(int(v['date']), unit='s').date()}:{float(v['numerator']) / float(v['denominator']):.6g}"
                      for v in sorted(sp.values(), key=lambda v: v["date"]) if v.get("denominator"))
    return s, last_px, splits


def feats(s, asof, last_px=None, drop_asof_month=False):
    """drop_asof_month：丟掉與 asof 同年同月的 K 線（Yahoo 月線＋歷史 asof；該根收盤晚於 asof，A3-M1C-09）。"""
    s = s[s.date <= asof]
    if drop_asof_month:
        d = pd.to_datetime(s.date)
        s = s[~((d.dt.year == asof.year) & (d.dt.month == asof.month))]
    s = s.set_index("date").sort_index()
    if s.empty:
        return {}
    m = s.adj_close.resample("ME").last().dropna()
    out = {"price": last_px if last_px else s.close.iloc[-1], "price_date": str(s.index[-1].date())}
    if len(m) >= 37:
        w = m.tail(37)
        out["vol_36m"] = w.pct_change().dropna().std() * np.sqrt(12)
        out["drawdown_3y"] = w.iloc[-1] / w.max() - 1
        w12 = w.tail(13)
        out["dd52"] = w12.iloc[-1] / w12.max() - 1  # 距 52 週（月線近似）高點跌幅，困境標記 T3 用
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", required=True)
    ap.add_argument("--out", default="prices.csv")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--manual", default=None)
    a = ap.parse_args()
    asof = pd.Timestamp(a.asof) if a.asof else pd.Timestamp.now()
    hist = a.asof is not None and asof.normalize() < pd.Timestamp.now().normalize()   # 歷史決策日
    tickers = [x.strip().upper() for x in open(a.tickers) if x.strip() and not x.startswith("#")]
    man = pd.read_csv(a.manual, parse_dates=["date"]) if a.manual else None
    rows = []
    for tk in tickers:
        r = {"ticker": tk}
        try:
            if man is not None and (man.ticker.str.upper() == tk).any():
                mm = man[man.ticker.str.upper() == tk]
                s = mm.assign(close=mm["close"] if "close" in mm else mm["adj_close"])[["date", "adj_close", "close"]]
                if "splits" in mm: r["splits"] = mm["splits"].dropna().iloc[0] if mm["splits"].notna().any() else ""
                r.update(feats(s, asof)); r["price_src"] = "manual"
            else:
                s, lp, splits = yahoo_monthly(tk)
                r.update(feats(s, asof, lp if a.asof is None else None, drop_asof_month=hist))
                r["price_src"] = "yahoo_chart"; r["splits"] = splits
                time.sleep(0.5)
        except Exception as e:
            r["price_src"] = f"失敗：{type(e).__name__}"
            time.sleep(0.5)   # 失敗也守請求間隔 ≥0.5 秒（A3-M1E-05）
        rows.append(r)
    pd.DataFrame(rows).to_csv(a.out, index=False)
    print(f"{len(rows)} 檔 → {a.out}")


if __name__ == "__main__":
    main()
