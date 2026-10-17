"""價格轉接層測試：假 client（不連網），驗證 raw／全調整／拆股調整的用途與拆股市值基準。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trader.data import prices_adapter as PA
from trader.paths import get_paths

ASOF = "2026-10-15"
SPLIT_DATE = "2026-03-01"


def _skill(name):
    d = str(get_paths().skill_screen / "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    return __import__(name)


def _bars_frame(ticker, dates, close):
    return pd.DataFrame({
        "ticker": ticker, "date": dates, "open": close, "high": close, "low": close,
        "close": close, "volume": 1000, "vwap": close,
    })


def _series(seed=0):
    """AAA：2026-03-01 做 2:1 正向拆股。全調整與拆股調整在拆股前皆為「拆股後尺度」（÷2）。"""
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2022-10-01", ASOF)]
    rng = np.random.RandomState(seed)
    adj = 50 + np.cumsum(rng.normal(0, 0.5, len(dates)))
    adj = np.clip(adj, 5, None)
    split_flag = np.array([d >= SPLIT_DATE for d in dates])
    raw = np.where(split_flag, adj, adj * 2)  # 拆股前原始價格為 2 倍
    return dates, raw, adj


class FakeClient:
    def __init__(self, frames, actions):
        self.frames = frames          # {adjustment: DataFrame}
        self.actions = actions
        self.calls = []

    def bars(self, symbols, start, end, adjustment="raw", timeframe="1Day"):
        self.calls.append(("bars", tuple(symbols), start, end, adjustment))
        df = self.frames.get(adjustment, pd.DataFrame())
        if df.empty:
            return df
        return df[df.ticker.isin(symbols) & (df.date >= start) & (df.date <= end)].reset_index(drop=True)

    def corporate_actions(self, symbols, start, end, types=None):
        self.calls.append(("ca", tuple(symbols), start, end))
        return list(self.actions)


SPLIT_ACTION = {"ticker": "AAA", "type": "forward_split", "ex_date": SPLIT_DATE, "process_date": None,
                "record_date": None, "payable_date": None, "ratio_new_per_old": 2.0,
                "cash_per_share": None, "new_ticker": None, "raw": {}}


@pytest.fixture
def aaa():
    dates, raw, adj = _series()
    raw_df = _bars_frame("AAA", dates, raw)
    all_df = _bars_frame("AAA", dates, adj)
    return dates, raw, adj, raw_df, all_df


# ---------- splits_string ----------

def test_splits_string_filters_by_asof_and_sorts():
    acts = [
        dict(SPLIT_ACTION),
        {"ticker": "AAA", "type": "reverse_split", "ex_date": "2020-05-01", "ratio_new_per_old": 0.1},
        {"ticker": "AAA", "type": "forward_split", "ex_date": "2027-01-01", "ratio_new_per_old": 3.0},  # 未來
        {"ticker": "AAA", "type": "cash_dividend", "ex_date": "2025-01-01", "cash_per_share": 1.0},   # 非拆股
        {"ticker": "BBB", "type": "forward_split", "ex_date": "2025-01-01", "ratio_new_per_old": 4.0},  # 別檔
    ]
    assert PA.splits_string(acts, "AAA", ASOF) == "2020-05-01:0.1;2026-03-01:2"
    assert PA.splits_string(acts, "AAA", "2026-02-01") == "2020-05-01:0.1"
    assert PA.splits_string(acts, "CCC", ASOF) == ""


def test_splits_string_compatible_with_split_factor():
    screen = _skill("screen")
    s = PA.splits_string([dict(SPLIT_ACTION)], "AAA", ASOF)
    assert screen.split_factor(s, "2026-01-15") == 2.0   # 申報日在拆股前 → ×2
    assert screen.split_factor(s, "2026-03-05") == 1.0   # 申報日在拆股後 → 不調整


# ---------- screen_prices ----------

def test_price_uses_raw_close_and_vol_uses_adjusted(aaa):
    dates, raw, adj, raw_df, all_df = aaa
    prices = PA.screen_prices(raw_df, all_df, [dict(SPLIT_ACTION)], ASOF)
    row = prices.iloc[0]
    assert row["ticker"] == "AAA"
    assert row["price_src"] == "alpaca"
    assert row["price"] == pytest.approx(raw[-1])        # 原始收盤（未調整）
    assert row["price_date"] == dates[-1]
    assert row["splits"] == "2026-03-01:2"
    assert row["vol_36m"] == pytest.approx(
        _direct_feats(dates, adj)["vol_36m"])               # 波動用全調整收盤


def _direct_feats(dates, adj_close, raw_close=None):
    fp = _skill("fetch_prices")
    s = pd.DataFrame({"date": pd.to_datetime(dates), "close": adj_close if raw_close is None else raw_close,
                      "adj_close": adj_close})
    return fp.feats(s, pd.Timestamp(ASOF))


def test_vol_36m_identical_to_direct_feats_call(aaa):
    dates, raw, adj, raw_df, all_df = aaa
    prices = PA.screen_prices(raw_df, all_df, [], ASOF)
    direct = _direct_feats(dates, adj, raw)
    row = prices.iloc[0]
    assert row["vol_36m"] == direct["vol_36m"]          # 完全相同，不是近似
    assert row["drawdown_3y"] == direct["drawdown_3y"]
    assert row["dd52"] == direct["dd52"]
    assert row["price"] == direct["price"]


def test_no_data_ticker_marked_failed(aaa):
    dates, raw, adj, raw_df, all_df = aaa
    prices = PA.screen_prices(raw_df.iloc[0:0], all_df.iloc[0:0], [], ASOF)
    assert prices.empty  # raw 無任何代號時不產生列
    # raw 有但 all 完全沒有 → 內併後為空 → 失敗：無資料
    prices = PA.screen_prices(raw_df, all_df.iloc[0:0], [], ASOF)
    assert prices.iloc[0]["price_src"] == "失敗：無資料"


# ---------- daily_closes ----------

def test_daily_closes_tail_and_asof(aaa):
    dates, raw, adj, raw_df, all_df = aaa
    split_adj = _bars_frame("AAA", dates, adj)
    daily = PA.daily_closes(split_adj, "2026-06-30", n_days=130)
    assert list(daily.columns) == ["ticker", "date", "close"]
    assert len(daily) == 130
    assert daily["date"].max() <= "2026-06-30"
    assert daily.iloc[-1]["close"] == pytest.approx(adj[dates.index(daily.iloc[-1]["date"])])


# ---------- 拆股市值基準 ----------

def test_market_cap_base_after_split_adjustment(aaa):
    screen = _skill("screen")
    dates, raw, adj, raw_df, all_df = aaa
    prices = PA.screen_prices(raw_df, all_df, [dict(SPLIT_ACTION)], ASOF)
    features = pd.DataFrame([{
        "ticker": "AAA", "shares_cover": 1_000_000.0, "shares_cover_filed": "2026-01-15",
        "shares": 1_000_000.0, "shares_filed_date": "2026-01-15",
        "net_income": 1e6, "ni5_mean": 1e6, "fcf": 1e6,
    }])
    df = screen.build(features, prices, None, ASOF)
    # 申報日（01-15）在拆股（03-01）之前 → 股數 ×2；價格為拆股後的原始收盤
    assert df.iloc[0]["shares_mcap"] == pytest.approx(2_000_000.0)
    assert df.iloc[0]["market_cap"] == pytest.approx(raw[-1] * 2_000_000.0)


# ---------- fetch_price_inputs ----------

def test_fetch_price_inputs_end_to_end(aaa):
    dates, raw, adj, raw_df, all_df = aaa
    client = FakeClient({"raw": raw_df, "all": all_df, "split": _bars_frame("AAA", dates, adj)},
                        [dict(SPLIT_ACTION)])
    out = PA.fetch_price_inputs(client, ["AAA", "zzz"], ASOF, years=4)
    adjs = [c[4] for c in client.calls if c[0] == "bars"]
    assert adjs == ["raw", "all", "split"]
    start = [c[2] for c in client.calls if c[0] == "bars"][0]
    assert start == "2022-10-15"
    assert set(out) == {"prices", "daily", "actions", "raw"}
    prices = out["prices"].set_index("ticker")
    assert prices.loc["AAA", "price_src"] == "alpaca"
    assert prices.loc["ZZZ", "price_src"] == "失敗：無資料"   # 沒有資料的代號也要列出
    assert len(out["daily"]) > 0 and set(out["daily"]["ticker"]) == {"AAA"}


def test_split_on_asof_excluded_when_price_is_previous_close():
    """asof 當天除權、但最後收盤是前一交易日 → 拆股字串不得計入（否則市值基準錯一倍）。"""
    from trader.data.prices_adapter import screen_prices
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2022-10-03", "2026-10-15")]
    close = np.linspace(50, 60, len(dates))
    raw = _bars_frame("BBB", dates, close)
    act = [{"ticker": "BBB", "type": "forward_split", "ex_date": "2026-10-16", "ratio_new_per_old": 2.0}]
    row = screen_prices(raw, raw, act, "2026-10-16").iloc[0]
    assert row["price_date"] == "2026-10-15"
    assert row["splits"] == ""
