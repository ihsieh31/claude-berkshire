"""X21：P13／P38 查核建議——is_split_ratio 分母 1–10（股票股利）、<1 混合分拆係數註記需人工、D66 SIC 誤標。全離線。"""
from __future__ import annotations

import importlib
import sys

import pandas as pd
import pytest

from trader.paths import get_paths

ASOF = "2026-10-15"


@pytest.fixture(scope="module")
def S():
    d = str(get_paths().skill_screen / "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module("screen")


# ---------------------------------------------------------------- 1. 分母 1–10：股票股利算拆股

@pytest.mark.parametrize("r", [1.2, 1 / 1.2, 1.4, 1.1, 1 / 1.1, 1.6, 1.8, 0.7, 1.142857])
def test_stock_dividend_ratios_are_splits(S, r):
    assert S.is_split_ratio(r)


@pytest.mark.parametrize("r", [1.327, 0.9535, 1.241, 1.057, 1.061, 1.193, 1.025, 1.272, 1.067, 1.487, 0.4725, 2.39,
                               1.806, 1.973, 1.04, 1.01, 1.03, 0.947, 0.487329, 1.3348, 1.589, 1.402])
def test_yahoo_spinoff_factors_still_not_splits(S, r):
    """state/cache/yahoo 的分拆係數（含 FTV 1.327、HON 0.9535）分母放寬後仍判非拆股。"""
    assert not S.is_split_ratio(r)


def test_stock_dividend_applied_to_shares(S):
    f = pd.DataFrame([{"ticker": "SD", "shares_cover": 1e8, "shares_cover_filed": "2026-02-01", "shares": 1e8,
                       "shares_filed_date": "2026-02-01", "net_income": 1e9, "ni5_mean": 1e9, "fcf": 1e9,
                       "fy_last_end": "2025-12-31", "fy_last_filed": "2026-02-01"}])
    p = pd.DataFrame([{"ticker": "SD", "price": 50.0, "price_date": "2026-10-14", "splits": "2026-05-01:1.2"}])
    df = S.build(f, p, None, ASOF)
    assert df.loc[0, "shares_mcap"] == pytest.approx(1.2e8)
    assert "財年後分拆" not in df.loc[0, "dq_note"]   # 6:5 股票股利不被 D69 當分拆而設缺
    assert df.loc[0, "earn_yield"] == pytest.approx(1e9 / 6e9)


# ---------------------------------------------------------------- 2. 非簡單比例且 <1 → 含合股、需人工

def _f(tk, **kw):
    r = {"ticker": tk, "shares": 3.2e8, "shares_filed_date": "2027-02-12", "shares_3y_ago": 6.6e8,
         "shares_3y_filed": "2024-02-09", "shares_cover": 3.17e8, "shares_cover_filed": "2027-02-12",
         "net_income": 5e9, "ni5_mean": 5e9, "fcf": 5e9, "fy_last_end": "2026-12-31", "fy_last_filed": "2027-02-12"}
    r.update(kw)
    return r


def _p(tk, splits="", spinoffs=""):
    return {"ticker": tk, "price": 200.0, "price_date": "2027-03-01", "splits": splits, "spinoffs": spinoffs}


def test_mixed_reverse_split_spinoff_flagged_for_human(S):
    """HON 型：2026-06-29 的 0.9535＝1:2 合股×分拆 1.907。分拆後第一份 10-K 出來後財年後分拆註記已消失，
    3 年股數變化約 −52% 只剩泛用註記 → 須明確註記含合股、需人工處理。"""
    F = pd.DataFrame([_f("HON"), _f("HONS"), _f("PURE"), _f("ALP"), _f("OLD", shares_3y_filed="2027-01-01",
                                                                         shares_cover_filed="2027-02-12")])
    P = pd.DataFrame([_p("HON", spinoffs="2026-06-29:0.9535"),
                      _p("HONS", splits="2025-10-30:1.061;2026-06-29:0.9535"),   # skill fetch_prices 混在 splits
                      _p("PURE", spinoffs="2026-06-29:1.241"),                    # 純分拆 >1：不註記
                      _p("ALP", spinoffs="2026-06-29"),                           # 無係數：無從判斷
                      _p("OLD", spinoffs="2026-06-29:0.9535")])                   # 事件早於所有股數基準日
    df = S.build(F, P, None, "2027-03-02").set_index("ticker")
    for tk in ("HON", "HONS"):
        n = df.loc[tk, "dq_note"]
        assert "含合股、需人工處理" in n and "2026-06-29 分拆事件" in n, tk
        assert "0.9535" not in n and "1.061" not in n and "係數" not in n, tk   # 係數（價格衍生）不進 dq_note（M1-R6-01）
        assert "2025-10-30" not in n, tk                                        # >1 的純分拆事件不得列入含合股註記
        assert df.loc[tk, "spinoff_factors"] == "2026-06-29:0.9535", tk          # 係數改放 spinoff_factors（只在 result.csv／.md）；只列 <1 的事件
        assert df.loc[tk, "share_change_3y"] == pytest.approx(3.2 / 6.6 - 1)   # 只註記，數值不動
    for tk in ("PURE", "ALP", "OLD"):
        assert "含合股" not in df.loc[tk, "dq_note"], tk


def test_mixed_factor_before_cover_but_market_cap_base_flagged(S):
    """封面股數在合股前申報（市值基準早於事件）也要註記。"""
    F = pd.DataFrame([_f("X", shares_3y_ago=None, shares_3y_filed=None, shares_cover_filed="2026-05-01")])
    df = S.build(F, pd.DataFrame([_p("X", spinoffs="2026-06-29:0.4873")]), None, "2027-03-02")
    assert "含合股、需人工處理" in df.loc[0, "dq_note"]


# ---------------------------------------------------------------- 3. D66 SIC 對照誤標

@pytest.mark.parametrize("sic,want", [(7990, ""), (7011, ""), (4833, ""), (4832, ""),
                                      (4813, "電信網路"), (4812, "電信網路"), (4841, "電信網路"),
                                      (4899, "電信網路"), (4512, "航空"), (6798, "REIT")])
def test_sel050_sic_map(S, sic, want):
    assert S.too_hard_type(sic) == want
