"""RM7-2（D83）：0 股晚到 Alpaca 拆合股不因缺前後價格拒收；持股時仍拒收。"""
from trader.ledger import corp_actions as ca


def _ev(**kw):
    e = {"ticker": "ABC", "source": "alpaca", "date": "2026-10-05", "price_before": None, "price_after": None,
         "alpaca": {"type": "forward_split", "ratio_new_per_old": 2.0}}
    e.update(kw)
    return e


def _res():
    return {"ticker": "ABC", "action_type": "拆股", "effective_date": "2026-10-05", "ratio_new_per_old": 2.0,
            "cash_per_share": None, "fractional_handling": "現金了結", "needs_human": False}


def test_late_split_zero_shares_not_rejected_for_missing_prices():
    errs = ca.verify(_res(), _ev(), None)
    assert not any("缺前後價格" in e for e in errs), errs


def test_late_split_with_position_still_needs_prices():
    errs = ca.verify(_res(), _ev(), {"qty": 10, "avg_cost": 50.0})
    assert any("缺前後價格" in e for e in errs)


def test_negative_alpaca_ratio_not_treated_as_late_no_pos():
    ev = _ev(alpaca={"type": "forward_split", "ratio_new_per_old": -2.0})
    assert ca.verify(_res(), ev, None)
