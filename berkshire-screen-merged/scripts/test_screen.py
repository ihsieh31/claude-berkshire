#!/usr/bin/env python3
"""初篩邏輯自測（合成資料，不連網）：python test_screen.py"""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import screen as S  # noqa: E402
import screen_quant as SQ  # noqa: E402

ASOF = "2026-10-15"
rng = np.random.default_rng(7)


def base(i):
    ni = float(rng.uniform(5e8, 3e9))
    return dict(ticker=f"T{i:03d}", name=f"Co{i}", sic=3500 + (i % 7) * 100, is_financial=False, fy_last=2025,
                net_income=ni, ni_prev=ni * 0.95, ni5_mean=ni * 0.9, ni_sum5=ni * 4.5, ni5_med=ni * 0.9, n_ni5=5,
                loss_yrs5=0, shareholders_equity=ni * 6, eq_prev=ni * 5.8, roe=0.16 + rng.normal(0, .04),
                roe_med5=0.16, roe_min5=0.12, roa=0.07 + rng.normal(0, .02), fcf=ni * 0.9,
                operating_cash_flow=ni * 1.2, ocf_sum5=ni * 5.5, ocf_pos5=5, n_ocf5=5, eps_g1=0.05,
                rev_cagr_5y=0.05 + rng.normal(0, .03), op_margin_5y_std=abs(rng.normal(.02, .01)),
                debt_to_equity=abs(rng.normal(.6, .3)), share_change_3y=rng.normal(-.02, .03),
                liab_to_assets=0.5, shares=1e9, shares_cover=1e9, shares_cover_filed="2026-08-01",
                shares_filed_date="2026-02-15", fy_last_filed="2026-02-15", max_filed_used="2026-08-01", asof=ASOF)


def price(i, vol=0.2, dd=-0.1, px=None):
    return dict(ticker=f"T{i:03d}", price=px if px else float(rng.uniform(20, 40)), price_date="2026-10-01",
                vol_36m=vol + rng.normal(0, .02), drawdown_3y=dd, dd52=dd / 2, splits="")


def make():
    F = [base(i) for i in range(120)]
    P = [price(i) for i in range(120)]
    # T000：持續虧損 → J1 刪除
    F[0].update(net_income=-5e8, ni_prev=-3e8, loss_yrs5=4, ni_sum5=-1e9, ni5_mean=-2e8, roe=-0.1, roe_med5=-0.05)
    # T001：此前經濟力佳、單年巨虧吃掉半數權益 → J4 例外：不刪、標困境；統計排除後進「歷史經濟力佳」特殊道
    F[1].update(net_income=-4e9, ni_prev=2e9, loss_yrs5=1, ni_sum5=4e9, shareholders_equity=5e9, eq_prev=1.2e10,
                roe=-0.4, roe_med5=0.2, roe_min5=0.15)
    # T002：波動最高 → 統計排除
    P[2].update(vol_36m=0.9)
    # T003：拆股（封面股數申報後 1 拆 4）→ 市值須乘 4
    P[3].update(price=25.0, splits="2026-09-01:4")
    F[3].update(shares_cover=1e9)
    return pd.DataFrame(F), pd.DataFrame(P)


def run(F, P, special_max=0):
    df = S.build(F, P, None, ASOF)
    df = S.structural(df, ASOF)
    return S.statistical(df, 15, special_max)[0]


def test_all():
    F, P = make()
    df = run(F, P, special_max=3).set_index("ticker")
    assert "J1_持續虧損" in df.loc["T000", "delete_codes"], df.loc["T000", "delete_codes"]
    assert df.loc["T000", "lane"].startswith("刪除")
    assert df.loc["T001", "delete_codes"] == "", "J4 例外不應刪除"
    assert "T1_去年虧損" in df.loc["T001", "distress_codes"]
    assert df.loc["T001", "lane"] == "特殊情境（困境中但歷史經濟力佳）", df.loc["T001", "lane"]
    assert bool(df.loc["T001", "special_handoff"])
    assert "波動" in df.loc["T002", "exclude_reasons"] and not df.loc["T002", "shortlist"]
    assert abs(df.loc["T003", "market_cap"] - 25.0 * 4e9) < 1, df.loc["T003", "market_cap"]
    sl = df[df.shortlist]
    assert len(sl) == 15 and (sl.lane == "核心道").all() and (sl.delete_codes == "").all()
    assert sl.core_rank.max() == 15
    # 預設不交特殊情境
    assert run(F, P).special_handoff.sum() == 0
    # 前視：申報日 ≥ 決策日 → 中止
    F2 = F.copy(); F2.loc[5, "fy_last_filed"] = ASOF
    try:
        run(F2, P); raise AssertionError("應因前視中止")
    except SQ.LookAheadError:
        pass
    # 端到端：寫檔
    with tempfile.TemporaryDirectory() as d:
        df2 = run(F, P)
        S.handoff(df2, ASOF, os.path.join(d, "r")); S.report(df2, True, os.path.join(d, "r"), 15, ASOF)
        import json
        h = json.load(open(os.path.join(d, "r_handoff.json")))
        assert len(h["core_shortlist"]) == 15 and all("screen_decision" in x for x in h["core_shortlist"])
        x = h["core_shortlist"][0]
        assert "price" not in x and "earn_yield" not in x and "core_rank" not in x and "price" in x["price_context"]
    test_quality_factor()
    print("全部通過")


def test_quality_factor():
    """非金融研究優先：5 年最差 ROE 高者排前；營收成長不影響分數（2026-10-17 修訂）。"""
    F, P = make()
    F.loc[10, "roe_min5"], F.loc[11, "roe_min5"] = 0.40, 0.01
    for c in ("fcf", "ni5_mean", "op_margin_5y_std", "debt_to_equity", "share_change_3y"):
        F.loc[11, c] = F.loc[10, c]
    P.loc[11, "price"] = P.loc[10, "price"]
    df = run(F, P).set_index("ticker")
    assert df.loc["T010", "research_priority"] > df.loc["T011", "research_priority"]
    F2 = F.copy(); F2["rev_cagr_5y"] = -F2["rev_cagr_5y"]
    df2 = run(F2, P).set_index("ticker")
    assert (df["research_priority"].fillna(-1) == df2["research_priority"].fillna(-1)).all(), "營收成長不應影響分數"


if __name__ == "__main__":
    test_all()
