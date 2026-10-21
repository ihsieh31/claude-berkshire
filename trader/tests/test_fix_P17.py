"""P17：未成交買單重核（M7-D4-01）＋ check-P16 附帶（已有有效委託的決策不重複下買單、延後買單只讀 done 的 daily）。"""
import json

import pytest

from trader import db
from trader.ledger import book, broker
from trader.pipelines import daily as DP
from trader.portfolio import sizing as Z


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    book.ensure_initial_deposit(c, 100000, "2026-10-01")
    yield c
    c.close()


def _reg(c, t, status, data):
    db.upsert(c, "registry", {"ticker": t, "status": status, "data": data, "updated_at": db.now_iso()}, key=["ticker"])


def _dec(c, did, t, d, log, created_at=None):
    db.insert(c, "decisions", {"decision_id": did, "ticker": t, "decision_date": d, "data": log,
                               "created_at": created_at or db.now_iso()})


def _apply(c, asof):
    """daily 9a 的做法：重核後撤單。"""
    cs = Z.revalidate_open_buys(c, asof)
    for x in cs:
        broker.cancel_order(c, x["order_id"], reason=x["reason"])
    return cs


def _status(c, oid):
    r = c.execute("SELECT status, cancel_reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    return r["status"], r["cancel_reason"]


def test_A_new_buy_cancelled_after_thesis_falsified(conn):
    _reg(conn, "XYZ", "WATCH", {"auto_buy_cap": 100.0, "grade": "A"})
    oid = broker.place_order(conn, "XYZ", "BUY", 50, 99.0, "d-buy", "2026-10-01")
    _dec(conn, "d-w", "XYZ", "2026-10-05", {"action": "WATCH", "review_type": "THESIS", "thesis_check": "證偽"})
    assert [x["order_id"] for x in _apply(conn, "2026-10-05")] == [oid]
    st, why = _status(conn, oid)
    assert st == "cancelled" and "論點" in why
    assert broker.match_orders(conn, "2026-10-06", {"XYZ": {"open": 98.5, "high": 99.5, "low": 98.0, "close": 99}},
                               0, 0) == []


def test_D_cap_lowered_below_limit_cancels(conn):
    _reg(conn, "QQQ1", "WATCH", {"auto_buy_cap": 96.0, "grade": "A"})
    oid = broker.place_order(conn, "QQQ1", "BUY", 10, 95.0, "d-b2", "2026-10-06")
    _reg(conn, "QQQ1", "WATCH", {"auto_buy_cap": 90.0, "grade": "A"})
    assert [x["order_id"] for x in _apply(conn, "2026-10-07")] == [oid]
    assert "上限" in _status(conn, oid)[1]
    assert broker.match_orders(conn, "2026-10-08", {"QQQ1": {"open": 94.0, "high": 96, "low": 93.5, "close": 95}},
                               0, 0) == []


def test_C_add_tranche_cancelled_when_thesis_suspect_build_plan_kept(conn):
    conn.execute("INSERT INTO positions VALUES ('ABC', 100, 50.0, '2026-10-01', ?)", (db.now_iso(),))
    conn.commit()
    bp = {"target_weight": 0.08, "tranches_left": 2, "days_above_cap": 0, "started": "2026-10-01"}
    _reg(conn, "ABC", "HOLDING", {"auto_buy_cap": 60.0, "grade": "A", "build_plan": bp})
    oid = broker.place_order(conn, "ABC", "BUY", 20, 55.0, None, "2026-10-08")
    _dec(conn, "d-th", "ABC", "2026-10-09", {"action": "HOLD", "review_type": "THESIS", "thesis_check": "疑似"})
    assert [x["order_id"] for x in _apply(conn, "2026-10-09")] == [oid]
    assert _status(conn, oid)[0] == "cancelled"
    assert Z._reg_data(conn, "ABC")["build_plan"]["tranches_left"] == 2   # 不停建倉，許可恢復後再排
    assert broker.match_orders(conn, "2026-10-10", {"ABC": {"open": 54.0, "high": 55, "low": 53.0, "close": 54}},
                               0, 0) == []


def test_counterexamples_price_or_fallback_keep_order(conn):
    _reg(conn, "KKK", "WATCH", {"auto_buy_cap": 100.0, "grade": "A"})
    oid = broker.place_order(conn, "KKK", "BUY", 10, 95.0, "d-k", "2026-10-01")
    _dec(conn, "d-p", "KKK", "2026-10-02", {"action": "NO_ACTION", "review_type": "PRICE"})
    _dec(conn, "d-f", "KKK", "2026-10-02", {"action": "WATCH", "review_type": "FALLBACK"})
    assert _apply(conn, "2026-10-02") == []
    assert _status(conn, oid)[0] == "open"


# 未持有（首批 BUY 未成交，登錄表仍 WATCH）時 writeback.write_thesis_result 對 THESIS 一律寫 action=WATCH（P17R）
def test_unheld_thesis_intact_alone_keeps_order(conn):
    _reg(conn, "KKK", "WATCH", {"auto_buy_cap": 100.0, "grade": "A"})
    oid = broker.place_order(conn, "KKK", "BUY", 10, 95.0, "d-k", "2026-10-01")
    _dec(conn, "d-ok", "KKK", "2026-10-02", {"action": "WATCH", "review_type": "THESIS", "thesis_check": "完好"})
    assert _apply(conn, "2026-10-02") == []
    assert _status(conn, oid)[0] == "open"


def test_unheld_thesis_suspect_then_intact_keeps_order(conn):
    _reg(conn, "KKK", "WATCH", {"auto_buy_cap": 100.0, "grade": "A"})
    oid = broker.place_order(conn, "KKK", "BUY", 10, 95.0, "d-k", "2026-10-01")
    # 較舊的疑似被較新的 THESIS 完好蓋過 → 不撤
    _dec(conn, "d-s", "KKK", "2026-10-03", {"action": "WATCH", "review_type": "THESIS", "thesis_check": "疑似"})
    _dec(conn, "d-ok", "KKK", "2026-10-04", {"action": "WATCH", "review_type": "THESIS", "thesis_check": "完好"})
    assert _apply(conn, "2026-10-04") == []
    assert _status(conn, oid)[0] == "open"


def test_decision_known_at_placement_does_not_cancel(conn):
    """委託前（同日、較早寫入）的決策是下單時已知的，不撤；冷靜單 created_date 為次日，其後寫入的證偽要撤。"""
    _reg(conn, "MMM", "WATCH", {"auto_buy_cap": 100.0, "grade": "A"})
    _dec(conn, "d-old", "MMM", "2026-10-05", {"action": "WATCH", "review_type": "THESIS"},
         created_at="2000-01-01T00:00:00Z")
    oid = broker.place_order(conn, "MMM", "BUY", 10, 95.0, "d-m", "2026-10-06")   # 冷靜單：D=10-05、created 10-06
    assert _apply(conn, "2026-10-06") == []
    _dec(conn, "d-new", "MMM", "2026-10-06", {"action": "WATCH", "review_type": "THESIS", "thesis_check": "證偽"},
         created_at="2999-01-01T00:00:00Z")
    assert [x["order_id"] for x in _apply(conn, "2026-10-06")] == [oid]


def test_paused_ticker_order_untouched(conn):
    _reg(conn, "PPP", "WATCH", {"auto_buy_cap": 90.0, "grade": "A"})
    oid = broker.place_order(conn, "PPP", "BUY", 10, 95.0, "d-p", "2026-10-01")
    db.insert(conn, "anomalies", {"ticker": "PPP", "kind": "x", "detail": "{}", "status": "open",
                                  "pause_trading": 1, "created_at": db.now_iso()})
    assert _apply(conn, "2026-10-02") == [] and _status(conn, oid)[0] == "open"


# ---- check-P16 附帶
CARD = {"target_weight_pct": 10, "certainty_grade": "B", "auto_buy_cap": 60.0,
        "value_per_share": {"conservative": 60.0}, "max_weight_pct": {"host": 15},
        "position_plan": {"first_tranche_pct_of_target": 50}}
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}


def test_plan_buys_skips_decision_with_filled_order(conn):
    """首次 daily 失敗後手動跑下一交易日：同一 bootstrap BUY 已成交（持有）→ 不當 ADD 再買一次。"""
    _reg(conn, "AAA", "HOLDING", {"auto_buy_cap": 60.0, "grade": "B"})
    oid = broker.place_order(conn, "AAA", "BUY", 99, 50.05, "AAA-1", "2026-10-15")
    broker.match_orders(conn, "2026-10-16", {"AAA": {"open": 50.0, "high": 51, "low": 49.5, "close": 50}}, 0, 0)
    assert _status(conn, oid)[0] == "filled" and "AAA" in book.positions(conn)
    d = {"decision_id": "AAA-1", "ticker": "AAA", "action": "BUY", "review_type": None,
         "log": {"action": "BUY", **GATES}, "card": CARD}
    ps = Z.plan_buys(conn, "2026-10-16", [d], {"AAA": 50.0}, {"AAA": 5e7}, 10)
    assert [p["qty"] for p in ps] == [0] and "已有有效委託" in ps[0]["reason"]


def test_deferred_buys_ignore_failed_daily(conn):
    _dec(conn, "AAA-1", "AAA", "2026-10-15", {"action": "BUY"})
    summ = json.dumps({"deferred_buys": [{"decision_id": "AAA-1", "ticker": "AAA", "action": "BUY"}]})
    db.insert(conn, "runs", {"run_id": "daily-2026-10-15", "kind": "daily_close", "asof": "2026-10-15",
                             "status": "failed", "started_at": db.now_iso(), "summary": summ})
    assert DP._deferred_buys(conn, "2026-10-16", "daily-2026-10-16", set()) == []
    conn.execute("UPDATE runs SET status='done'")
    assert [x["decision_id"] for x in DP._deferred_buys(conn, "2026-10-16", "daily-2026-10-16", set())] == ["AAA-1"]
