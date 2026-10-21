"""P13：分拆（spin-off）與拆股基準（M1-D1-05＝M4-D2-13 判定、M1-D1-03＋D69、M1-D4-05）。全離線。"""
from __future__ import annotations

import importlib
import sys

import numpy as np
import pandas as pd
import pytest

from trader.data import prices_adapter as PA
from trader.data import xbrl_shares as xs
from trader.data.yahoo import YahooClient
from trader.paths import get_paths

ASOF = "2026-10-15"


def _skill(module: str):
    d = str(get_paths().skill_screen / "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module(module)


@pytest.fixture(scope="module")
def S():
    return _skill("screen")


# ---------------------------------------------------------------- 判定函式（M1-D1-05／M4-D2-13）

@pytest.mark.parametrize("r", [2, 0.5, 4, 10, 0.1, 20, 1.5, 4 / 3, 1.33333, 1.25, 2.5, 1 / 1.5, 0.05, 100])
def test_is_split_ratio_true_for_integer_and_simple_fractions(S, r):
    assert S.is_split_ratio(r)


@pytest.mark.parametrize("r", [1.241, 1.057, 0.9535, 1.061, 1.327, 1.281, 1.253, 1.196, 2.39, 1.272, 1.067,
                               0, -2, float("nan"), float("inf"), None, "x"])
def test_is_split_ratio_false_for_spinoff_factors_and_junk(S, r):
    assert not S.is_split_ratio(r)


def _ts(day: str) -> int:
    # 美東 09:30 開盤時間戳，換回美東日期即為 day
    return int(pd.Timestamp(f"{day} 09:30", tz="America/New_York").timestamp())


def _chart():
    days = ["2026-05-29", "2026-06-01", "2026-09-01", "2026-09-02"]
    close = [100.0, 120.0, 60.0, 61.0]   # Yahoo close：已依兩個事件調整
    return {
        "timestamp": [_ts(d) for d in days],
        "indicators": {"quote": [{"open": close, "high": close, "low": close, "close": close,
                                   "volume": [1000] * 4}], "adjclose": [{"adjclose": close}]},
        "events": {"splits": {
            "a": {"date": _ts("2026-06-01"), "numerator": 1241.0, "denominator": 1000.0, "splitRatio": "1241:1000"},
            "b": {"date": _ts("2026-09-01"), "numerator": 2.0, "denominator": 1.0, "splitRatio": "2:1"},
        }},
    }


def _yahoo():
    c = YahooClient(cache_dir=None)
    c._mem["FDX"] = _chart()   # 不上網
    return c


def test_yahoo_spinoff_factor_is_spin_off_not_split():
    acts = _yahoo().corporate_actions(["FDX"], "2026-01-01", ASOF)
    by = {a["ex_date"]: a for a in acts}
    assert by["2026-06-01"]["type"] == "spin_off"
    assert by["2026-06-01"]["ratio_new_per_old"] is None
    assert by["2026-06-01"]["raw"]["yahoo_adj_factor"] == pytest.approx(1.241)
    assert by["2026-09-01"]["type"] == "forward_split" and by["2026-09-01"]["ratio_new_per_old"] == 2.0
    assert PA.splits_string(acts, "FDX", ASOF) == "2026-09-01:2"          # 分拆不進拆股字串
    assert PA.spinoffs_string(acts, "FDX", ASOF) == "2026-06-01:1.241"
    assert PA.spinoffs_string(acts, "FDX", "2026-05-31") == ""
    # Alpaca 的 spin_off（無係數）也收
    alp = [{"ticker": "GE", "type": "spin_off", "ex_date": "2024-04-02", "ratio_new_per_old": None}]
    assert PA.spinoffs_string(alp, "GE", ASOF) == "2024-04-02"


def test_yahoo_raw_bars_still_undo_spinoff_adjustment():
    """Yahoo close 已含分拆調整 → raw（當日實際成交價）仍要乘回係數（防退化）。"""
    raw = _yahoo().bars(["FDX"], "2026-01-01", ASOF, adjustment="raw").set_index("date")["close"]
    assert raw["2026-05-29"] == pytest.approx(100.0 * 1.241 * 2)
    assert raw["2026-06-01"] == pytest.approx(120.0 * 2)
    assert raw["2026-09-02"] == pytest.approx(61.0)


def test_spgi_type_cover_shares_not_multiplied_by_spinoff_factor(S):
    """SPGI：封面股數 2026-04-28 申報、2026-07-01 分拆係數 1.057 → shares_mcap／shares_cover＝1.0（M1-D1-05）。
    splits 字串仍混著係數（skill 自己的 fetch_prices.py 路徑）也不乘。"""
    f = pd.DataFrame([{"ticker": "SPGI", "shares_cover": 3e8, "shares_cover_filed": "2026-04-28", "shares": 3e8,
                       "shares_filed_date": "2026-02-10", "net_income": 4e9, "ni5_mean": 3.5e9, "fcf": 4e9,
                       "fy_last_end": "2025-12-31", "fy_last_filed": "2026-02-10"}])
    p = pd.DataFrame([{"ticker": "SPGI", "price": 500.0, "price_date": "2026-10-14", "splits": "2026-07-01:1.057"}])
    df = S.build(f, p, None, ASOF)
    assert df.loc[0, "shares_mcap"] / 3e8 == pytest.approx(1.0)
    assert S.split_factor("2024-06-10:10;2026-07-01:1.057", "2023-01-01") == 10


def test_fdx_share_change_not_distorted_by_spinoff_factor(S):
    """F04 回報：FDX 3 年股數變化被 1.241 錯調（−6.6%→−24.8%）。分拆係數不得進 share_change_3y。"""
    f = pd.DataFrame([{"ticker": "FDX", "shares": 2.43e8, "shares_filed_date": "2026-07-20",
                       "shares_3y_ago": 2.6e8, "shares_3y_filed": "2023-07-17", "shares_cover": 2.4e8,
                       "shares_cover_filed": "2026-07-20", "net_income": 4e9, "ni5_mean": 4e9, "fcf": 3e9,
                       "fy_last_end": "2026-05-31", "fy_last_filed": "2026-07-20"}])
    p = pd.DataFrame([{"ticker": "FDX", "price": 250.0, "price_date": "2026-10-14", "splits": "2026-06-01:1.241"}])
    df = S.build(f, p, None, ASOF)
    assert df.loc[0, "share_change_3y"] == pytest.approx(2.43 / 2.6 - 1)


# ---------------------------------------------------------------- M1-D1-03＋D69：財年後分拆 → 殖利率設缺

def _feat(tk, fy_end, filed="2026-02-20", **kw):
    r = {"ticker": tk, "shares_cover": 1e9, "shares_cover_filed": "2026-08-01", "shares": 1e9,
         "shares_filed_date": filed, "net_income": 2e9, "ni5_mean": 1.8e9, "fcf": 1.5e9,
         "fy_last_end": fy_end, "fy_last_filed": filed}
    r.update(kw)
    return r


def _px(tk, splits="", spinoffs=""):
    return {"ticker": tk, "price": 30.0, "price_date": "2026-10-14", "vol_36m": 0.2, "drawdown_3y": -0.1,
            "splits": splits, "spinoffs": spinoffs}


def test_spinoff_after_fiscal_year_end_blanks_yields(S):
    F = pd.DataFrame([
        _feat("FDX", "2026-05-31", "2026-07-20"),            # 期末後、申報前分拆（Yahoo 係數在 spinoffs 欄）
        _feat("HON", "2025-12-31"),                          # 申報後分拆，係數混在 splits（skill fetch_prices 路徑）
        _feat("OLD", "2025-12-31"),                          # 分拆在財年內 → 已反映在年報，照常
        _feat("ALP", "2025-12-31"),                          # Alpaca spin_off（只有日期）
        _feat("NOEND", None, "2026-02-20"),                  # 舊 features 無期末欄 → 退用申報日
        _feat("FUT", "2025-12-31"),                          # 分拆在決策日之後 → 不算
    ])
    P = pd.DataFrame([
        _px("FDX", spinoffs="2026-06-01:1.241"),
        _px("HON", splits="2025-10-30:1.061;2026-06-29:0.9535"),
        _px("OLD", spinoffs="2025-10-30:1.061"),
        _px("ALP", spinoffs="2026-04-02"),
        _px("NOEND", spinoffs="2026-03-01:1.2"),
        _px("FUT", spinoffs="2026-11-01:1.2"),
    ])
    df = S.build(F, P, None, ASOF).set_index("ticker")
    for tk in ("FDX", "HON", "ALP", "NOEND"):
        assert df.loc[tk, ["earn_yield", "fcf_yield", "norm_earn_yield"]].isna().all(), tk
        assert "財年後分拆" in df.loc[tk, "dq_note"], tk
        assert df.loc[tk, "market_cap"] > 0   # 市值不動（P/E 中位等仍可用）
    assert "財年後分拆 2026-06-01" in df.loc["FDX", "dq_note"]
    assert "1.241" not in df.loc["FDX", "dq_note"] and "係數" not in df.loc["FDX", "dq_note"]   # M1-R6-01：係數不進 dq_note
    assert df.loc["FDX", "spinoff_factors"] == "2026-06-01:1.241"                              # 改放 spinoff_factors
    assert "2026-06-29" in df.loc["HON", "dq_note"] and "2025-10-30" not in df.loc["HON", "dq_note"]
    for tk in ("OLD", "FUT"):
        assert df.loc[tk, "earn_yield"] == pytest.approx(2e9 / 3e10) and df.loc[tk, "dq_note"] == "", tk
    # HON 的 0.9535（合股×分拆合成值）不當拆股乘到股數
    assert df.loc["HON", "shares_mcap"] == pytest.approx(1e9)


def _universe(S, extra_f, extra_p, n=80):
    rng = np.random.default_rng(3)
    F = [_feat(f"T{i:03d}", "2025-12-31", net_income=float(rng.uniform(1e9, 3e9)), ni5_mean=1.5e9,
               fcf=1.2e9, sic=3500, is_financial=False, roe=0.15, roe_med5=0.15, roe_min5=0.1, roa=0.07,
               loss_yrs5=0, ni_prev=1e9, eq_prev=6e9, shareholders_equity=6e9, ni_sum5=7e9, ni5_med=1.5e9,
               n_ni5=5, operating_cash_flow=2e9, ocf_sum5=9e9, ocf_pos5=5, n_ocf5=5, eps_g1=0.05,
               op_margin_5y_std=0.02, debt_to_equity=0.5, share_change_3y=-0.01, liab_to_assets=0.5,
               rev_cagr_5y=0.05, fy_last=2025, max_filed_used="2026-08-01", asof=ASOF) for i in range(n)]
    P = [_px(f"T{i:03d}") for i in range(n)]
    F += extra_f
    P += extra_p
    df = S.build(pd.DataFrame(F), pd.DataFrame(P), None, ASOF)
    df = S.structural(df, ASOF)
    return S.statistical(df, 15, 0)[0].set_index("ticker")


def test_d69_parent_not_ranked_and_subsidiary_lacks_data(S):
    """D69（保守）：母公司在分拆後第一份年報前不排名（資料不足）；子公司價格不滿 3 年（vol_36m 缺）也是資料不足。"""
    base = dict(sic=3500, is_financial=False, roe=0.3, roe_med5=0.3, roe_min5=0.25, roa=0.15, loss_yrs5=0,
                ni_prev=4e9, eq_prev=6e9, shareholders_equity=6e9, ni_sum5=2e10, ni5_med=4e9, n_ni5=5,
                operating_cash_flow=5e9, ocf_sum5=2e10, ocf_pos5=5, n_ocf5=5, eps_g1=0.05, op_margin_5y_std=0.01,
                debt_to_equity=0.1, share_change_3y=-0.05, liab_to_assets=0.4, rev_cagr_5y=0.05, fy_last=2025,
                max_filed_used="2026-08-01", asof=ASOF)
    par = _feat("PAR", "2025-12-31", net_income=5e9, ni5_mean=4.5e9, fcf=4.5e9, **base)
    sub = _feat("SUB", "2025-12-31", net_income=5e9, ni5_mean=4.5e9, fcf=4.5e9, **base)
    psub = _px("SUB")
    psub["vol_36m"] = None
    df = _universe(S, [par, sub], [_px("PAR", spinoffs="2026-06-29:1.9"), psub])
    assert df.loc["PAR", "lane"] == "資料不足" and pd.isna(df.loc["PAR", "core_rank"])
    assert df.loc["SUB", "lane"] == "資料不足" and pd.isna(df.loc["SUB", "core_rank"])
    # 同一家沒有分拆 → 照常進核心道且排前面（證明是分拆防線造成）
    df2 = _universe(S, [par], [_px("PAR")])
    assert df2.loc["PAR", "lane"] == "核心道" and df2.loc["PAR", "core_rank"] <= 15


def test_screen_prices_carries_spinoffs_column_from_yahoo_actions():
    c = _yahoo()
    raw = c.bars(["FDX"], "2026-01-01", ASOF, adjustment="raw")
    allb = c.bars(["FDX"], "2026-01-01", ASOF, adjustment="all")
    acts = c.corporate_actions(["FDX"], "2026-01-01", ASOF)
    p = PA.screen_prices(raw, allb, acts, ASOF)
    assert list(p.columns) == PA.SCREEN_COLS and "spinoffs" in p.columns
    assert p.loc[0, "splits"] == "2026-09-01:2" and p.loc[0, "spinoffs"] == "2026-06-01:1.241"


def test_fetch_edgar_outputs_fy_last_end():
    fe = _skill("fetch_edgar")

    def cal(y, v):
        return {"form": "10-K", "fp": "FY", "start": f"{y}-06-01", "end": f"{y + 1}-05-31",
                "filed": f"{y + 1}-07-20", "val": v}
    us = {"NetIncomeLoss": [cal(y, 100) for y in range(2016, 2026)],
          "Revenues": [cal(y, 1000) for y in range(2016, 2026)]}
    facts = {"cik": 1, "entityName": "T", "facts": {"us-gaap": {k: {"units": {"USD": v}} for k, v in us.items()}}}
    r = fe.features("T", "1", facts, "4500", ASOF, False)
    assert r["fy_last_end"] == "2026-05-31"


# ---------------------------------------------------------------- M1-D4-05：xbrl_shares P/E 檢查套拆股

def test_xbrl_pe_check_applies_split_after_filing(tmp_path):
    """申報（2026-08-05，1,750,000 等值股）後 1 拆 50、價格為拆股後 2,000 → P/E＝2,000×1.75M×50/1e10＝17.5 → filled。
    修前用 2,000×1.75M/1e10＝0.35 → rejected。"""
    sys.path.insert(0, str(get_paths().root / "trader" / "tests"))
    T = importlib.import_module("test_xbrl_shares")
    feats = T._brk_feats()
    prices = pd.DataFrame({"ticker": ["BRK.B"], "price": [2_000.0], "splits": ["2026-09-01:50"]})
    out, report = xs.fill_missing_shares(feats, prices, T._make_sec(tmp_path), T.ASOF)
    assert report[0]["result"] == "filled", report
    assert "P/E 17.5" in report[0]["detail"]
    assert out.loc[0, "shares_cover_filed"] == "2026-08-05"   # 寫的是申報當時基準，screen.build 再依拆股調整
    # 沒有 splits 欄（舊 prices）→ 行為同修前
    prices2 = pd.DataFrame({"ticker": ["BRK.B"], "price": [100_000.0]})
    out2, report2 = xs.fill_missing_shares(feats, prices2, T._make_sec(tmp_path), T.ASOF)
    assert report2[0]["result"] == "filled"
