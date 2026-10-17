"""Alpaca 日線 → skill 的價格特徵（fetch_prices.feats）與 review_due 日線、拆股字串的轉接層。

不複製 skill 的計算公式：價格特徵一律呼叫 fetch_prices.feats。
"""
from __future__ import annotations

import importlib
import sys
from datetime import date

import pandas as pd

from trader.data.universe import normalize_ticker
from trader.paths import get_paths

SCREEN_COLS = ["ticker", "price", "price_date", "vol_36m", "drawdown_3y", "dd52", "price_src", "splits"]
SPLIT_TYPES = {"forward_split", "reverse_split"}


def _skill(name: str):
    """匯入 skill 腳本（只匯入不修改）。"""
    d = str(get_paths().skill_screen / "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module(name)


def splits_string(actions: list[dict], ticker: str, asof: str) -> str:
    """該檔 ex_date <= asof 的拆股事件，依日期升冪，格式 'YYYY-MM-DD:ratio;...'（與 screen.split_factor 相容）。"""
    tk = normalize_ticker(ticker)
    evs = []
    for a in actions:
        if a.get("type") not in SPLIT_TYPES or normalize_ticker(a.get("ticker") or "") != tk:
            continue
        ex, ratio = a.get("ex_date"), a.get("ratio_new_per_old")
        if not ex or ratio is None or str(ex)[:10] > asof[:10]:
            continue
        evs.append((str(ex)[:10], float(ratio)))
    evs.sort(key=lambda x: x[0])
    return ";".join(f"{d}:{r:.6g}" for d, r in evs)


def screen_prices(raw: pd.DataFrame, adj_all: pd.DataFrame, actions: list[dict], asof: str) -> pd.DataFrame:
    """每檔：價格取 raw 收盤，波動與回撤取全調整收盤，交給 skill 的 fetch_prices.feats 計算。"""
    fp = _skill("fetch_prices")
    cols = SCREEN_COLS
    rows = []
    asof_ts = pd.Timestamp(asof)
    raw_by = {t: g for t, g in raw.groupby("ticker")} if len(raw) else {}
    adj_by = {t: g for t, g in adj_all.groupby("ticker")} if len(adj_all) else {}
    for tk in sorted(raw_by):
        r = raw_by[tk][["date", "close"]].copy()
        a = adj_by.get(tk, pd.DataFrame(columns=["date", "close"]))[["date", "close"]].copy()
        # 以 date 內併；只保留 asof 當日（含）以前
        s = r.merge(a, on="date", how="inner", suffixes=("", "_adj"))
        s = s.rename(columns={"close_adj": "adj_close"})
        s["date"] = pd.to_datetime(s["date"])
        s = s[s["date"] <= asof_ts]
        splits = splits_string(actions, tk, asof)
        if s.empty:
            rows.append({"ticker": tk, "price": None, "price_date": None, "vol_36m": None,
                         "drawdown_3y": None, "dd52": None, "price_src": "失敗：無資料", "splits": splits})
            continue
        out = fp.feats(s.reset_index(drop=True), asof_ts)
        # 市值基準＝price_date 當天的收盤：只計入 ex_date <= price_date 的拆股（asof 當天除權而價格是前一日時不可計入）
        splits = splits_string(actions, tk, out.get("price_date") or asof)
        rows.append({
            "ticker": tk,
            "price": out.get("price"),
            "price_date": out.get("price_date"),
            "vol_36m": out.get("vol_36m"),
            "drawdown_3y": out.get("drawdown_3y"),
            "dd52": out.get("dd52"),
            "price_src": "alpaca",
            "splits": splits,
        })
    return pd.DataFrame(rows, columns=cols)


def daily_closes(split_adj: pd.DataFrame, asof: str, n_days: int = 130) -> pd.DataFrame:
    """review_due 用的 daily.csv：ticker,date,close；拆股調整、date <= asof、每檔最後 n_days 筆。"""
    cols = ["ticker", "date", "close"]
    if split_adj is None or split_adj.empty:
        return pd.DataFrame(columns=cols)
    df = split_adj[["ticker", "date", "close"]].copy()
    df["date"] = df["date"].astype(str).str[:10]
    df = df[df["date"] <= asof[:10]].sort_values(["ticker", "date"])
    df = df.groupby("ticker", group_keys=False).tail(n_days)
    return df[cols].reset_index(drop=True)


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 2/29 → 2/28
        return d.replace(year=d.year - years, day=28)


def fetch_price_inputs(client, tickers: list[str], asof: str, years: int = 4) -> dict:
    """抓 raw、全調整、拆股調整日線與公司行動，組成 prices／daily／actions／raw。

    沒有資料的代號在 prices 內以 price_src="失敗：無資料" 列出。
    """
    tks = sorted({normalize_ticker(t) for t in tickers})
    end = date.fromisoformat(asof[:10])
    start = _years_before(end, years).isoformat()
    end_s = end.isoformat()
    raw = client.bars(tks, start, end_s, adjustment="raw")
    adj_all = client.bars(tks, start, end_s, adjustment="all")
    split_adj = client.bars(tks, start, end_s, adjustment="split")
    actions = client.corporate_actions(tks, start, end_s)

    prices = screen_prices(raw, adj_all, actions, asof)
    # 補上完全沒有日線的代號
    have = set(prices["ticker"]) if len(prices) else set()
    missing = [t for t in tks if t not in have]
    if missing:
        extra = pd.DataFrame([{"ticker": t, "price": None, "price_date": None, "vol_36m": None,
                               "drawdown_3y": None, "dd52": None, "price_src": "失敗：無資料",
                               "splits": splits_string(actions, t, asof)} for t in missing])
        prices = pd.concat([prices, extra], ignore_index=True)

    return {
        "prices": prices,
        "daily": daily_closes(split_adj, asof),
        "actions": actions,
        "raw": raw,
    }
