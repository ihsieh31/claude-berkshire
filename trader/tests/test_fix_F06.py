"""F06（M7-B 帳本與登錄表同步）回歸測試：M7-D1-04、M7-D7-03（＋設計決策 D06）、M7-D10-02（含 M7-D2-07、M7-D9-05）、
M7-D7-06。全部離線、只用暫存 state。"""
import json

import pytest

from trader import db
from trader.ledger import book, broker, corp_actions as ca
from trader.ledger.broker import BrokerError
from trader.paths import get_paths
from trader.pipelines.review import export_registry

import test_daily_pipeline as TD

D1, D2, D3 = "2026-10-14", "2026-10-15", "2026-10-16"


@pytest.fixture
def conn(state_dir):
    paths = get_paths().ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, 100_000, "2026-10-01")
    yield c
    c.close()


def _bar(o, h=None, lo=None, c=None):
    return {"open": o, "high": h if h is not None else o + 1, "low": lo if lo is not None else o - 1,
            "close": c if c is not None else o}


def _reg(conn, t, status, data, first=None):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "first_buy_date": first, "data": data,
                                 "updated_at": db.now_iso()}, ["ticker"])


def _reg_row(conn, t):
    r = conn.execute("SELECT status, first_buy_date, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return r["status"], r["first_buy_date"], json.loads(r["data"])


def _pos(conn, t, qty, cost=50.0, first="2025-01-02"):
    db.upsert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": cost, "first_buy_date": first,
                                  "updated_at": db.now_iso()}, ["ticker"])


def _dec(conn, did, t, date, action):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "signal": action,
                                  "data": {"ticker": t, "decision_date": date, "action": action},
                                  "created_at": db.now_iso()})


def _pause(conn, t, alp_type="cash_dividend"):
    """公司行動工作失敗暫停。D88（A3-M7C-01）後 SELL_ALL 放行只限現金股利；比例類／未知（例：拆股）連全清也不撮合。"""
    ev = {"event_id": f"{t}:alpaca:{D1}:1", "ticker": t, "source": "alpaca", "date": D1,
          "alpaca": {"ticker": t, "type": alp_type, "ex_date": D1}}
    db.insert(conn, "anomalies", {"ticker": t, "kind": "corp_action_job_failed", "detail": {"event": ev},
                                  "status": "open", "pause_trading": 1, "created_at": db.now_iso()})


HELD = {"auto_buy_cap": 120.0, "grade": "B", "cost": 100.0,
        "build_plan": {"target_weight": 0.1, "tranches_left": 2, "started": "2025-01-02"}}


# ---------------------------------------------------------------- M7-D1-04 賣光後清掉舊上限

def test_sell_out_clears_auto_buy_cap_and_review_due_has_no_price_lane(conn):
    _reg(conn, "RRR", "HOLDING", dict(HELD), first="2025-01-03")
    _pos(conn, "RRR", 10, 100.0)
    broker.place_order(conn, "RRR", "SELL", 10, None, None, D1)
    broker.match_orders(conn, D2, {"RRR": _bar(90)}, 10, 0)
    st, _, data = _reg_row(conn, "RRR")
    assert st == "WATCH"
    assert data["auto_buy_cap"] is None and data["build_plan"] is None and data["sold_out_at"] == D2
    assert data["grade"] == "B"                      # 其他欄位保留
    e = [x for x in export_registry(conn, "watch") if x["ticker"] == "RRR"][0]
    assert "auto_buy_cap" not in e                   # review_due 的 WATCH＋現價<上限 → PRICE 不再成立


def test_partial_sell_keeps_cap(conn):
    _reg(conn, "RRR", "HOLDING", dict(HELD))
    _pos(conn, "RRR", 10, 100.0)
    broker.place_order(conn, "RRR", "SELL", 4, None, None, D1)
    broker.match_orders(conn, D2, {"RRR": _bar(90)}, 0, 0)
    st, _, data = _reg_row(conn, "RRR")
    assert st == "HOLDING" and data["auto_buy_cap"] == 120.0


def test_sell_out_does_not_turn_reject_into_watch(conn):
    _reg(conn, "RRR", "REJECT", dict(HELD))
    _pos(conn, "RRR", 10, 100.0)
    broker.place_order(conn, "RRR", "SELL", 10, None, None, D1)
    broker.match_orders(conn, D2, {"RRR": _bar(90)}, 0, 0)
    st, _, data = _reg_row(conn, "RRR")
    assert st == "REJECT" and data["auto_buy_cap"] is None


def test_adjust_to_zero_clears_cap(conn):
    _reg(conn, "RRR", "HOLDING", dict(HELD))
    _pos(conn, "RRR", 10, 100.0)
    book.adjust(conn, date=D2, ticker="RRR", set_qty=0, note="下市清算")
    st, _, data = _reg_row(conn, "RRR")
    assert st == "WATCH" and data["auto_buy_cap"] is None and data["sold_out_at"] == D2


def test_corp_action_ratio_to_zero_clears_cap(state_dir):
    paths = get_paths().ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    _reg(c, "RRR", "HOLDING", dict(HELD))
    _pos(c, "RRR", 3, 100.0)
    c.commit()
    with book.transaction(c):
        ca._apply_ratio(c, paths, ca._Files(), {"action_type": "合股", "fractional_handling": "現金了結"},
                        {"price_after": 1000.0}, "RRR", D2, 0.1, None)
    st, _, data = _reg_row(c, "RRR")
    assert "RRR" not in book.positions(c)
    assert st == "WATCH" and data["auto_buy_cap"] is None and data["sold_out_at"] == D2
    c.close()


# ---------------------------------------------------------------- M7-D7-03＋D06 暫停期間的全清

def test_paused_sell_all_is_placed_and_filled(conn):
    _reg(conn, "ZZZ", "HOLDING", dict(HELD))
    _pos(conn, "ZZZ", 100)
    _pause(conn, "ZZZ")
    _dec(conn, "ZZZ-SA", "ZZZ", D1, "SELL_ALL")
    oid = broker.place_order(conn, "ZZZ", "SELL", 100, None, "ZZZ-SA", D1)
    fills = broker.match_orders(conn, D2, {"ZZZ": _bar(40)}, 0, 0)
    assert [f["order_id"] for f in fills] == [oid]
    assert "ZZZ" not in book.positions(conn)


def test_d88_split_pause_holds_sell_all(conn):
    """D88：拆股事件工作失敗（事件日 ≤ 撮合日）→ SELL_ALL 不撮合、委託保留（拆後價×拆前股數會記錯現金）。"""
    _reg(conn, "ZZZ", "HOLDING", dict(HELD))
    _pos(conn, "ZZZ", 100)
    _pause(conn, "ZZZ", "forward_split")
    _dec(conn, "ZZZ-SA", "ZZZ", D1, "SELL_ALL")
    oid = broker.place_order(conn, "ZZZ", "SELL", 100, None, "ZZZ-SA", D1)
    assert broker.match_orders(conn, D2, {"ZZZ": _bar(40)}, 0, 0) == []
    assert book.positions(conn)["ZZZ"]["qty"] == 100 and [o["order_id"] for o in book.open_orders(conn)] == [oid]


def test_paused_blocks_buy_and_trim(conn):
    _reg(conn, "ZZZ", "HOLDING", dict(HELD))
    _pos(conn, "ZZZ", 100)
    _pause(conn, "ZZZ")
    _dec(conn, "ZZZ-T", "ZZZ", D1, "TRIM")
    with pytest.raises(BrokerError):
        broker.place_order(conn, "ZZZ", "BUY", 1, 40.0, None, D1)
    with pytest.raises(BrokerError):
        broker.place_order(conn, "ZZZ", "SELL", 50, None, "ZZZ-T", D1)
    with pytest.raises(BrokerError):
        broker.place_order(conn, "ZZZ", "SELL", 50, None, None, D1)   # 權重超限等無決策的減碼


def test_open_trim_not_matched_while_paused_but_open_sell_all_is(conn):
    """暫停前已掛的單：TRIM 等解除，SELL_ALL 照常撮合。"""
    _pos(conn, "ZZZ", 100)
    _pos(conn, "YYY", 100)
    _dec(conn, "ZZZ-T", "ZZZ", D1, "TRIM")
    _dec(conn, "YYY-SA", "YYY", D1, "SELL_ALL")
    trim = broker.place_order(conn, "ZZZ", "SELL", 50, None, "ZZZ-T", D1)
    sa = broker.place_order(conn, "YYY", "SELL", 100, None, "YYY-SA", D1)
    _pause(conn, "ZZZ")
    _pause(conn, "YYY")
    fills = broker.match_orders(conn, D2, {"ZZZ": _bar(40), "YYY": _bar(40)}, 0, 0)
    assert [f["order_id"] for f in fills] == [sa]
    assert book.positions(conn)["ZZZ"]["qty"] == 100
    assert [o["order_id"] for o in book.open_orders(conn)] == [trim]


def test_daily_sell_all_while_paused_is_not_lost(tmp_path, state_dir):
    """audit_tmp/M7_D7 test_sell_all_rejected_while_paused_is_never_retried 的修後行為：暫停中 SELL_ALL 下單並次日成交。"""
    cfg, paths, c = _world(tmp_path, state_dir)
    TD._reg(c, "ZZZ", "HOLDING", {"cik": "3", "grade": "B", "auto_buy_cap": 60.0})
    _pos(c, "ZZZ", 100, 40.0)
    c.commit()
    _pause(c, "ZZZ")
    TD._dec(c, "ZZZ-1", "ZZZ", TD.D1, "SELL_ALL", "THESIS")
    alp = TD.FakeAlpaca(TD._bars())
    s1 = TD._run(cfg, paths, c, TD.D1, alp)
    assert not s1["rejected"] and [o["side"] for o in s1["orders"]] == ["SELL"]
    TD._run(cfg, paths, c, TD.D2, alp)
    assert "ZZZ" not in book.positions(c)
    c.close()


def _world(tmp_path, state_dir):
    from trader.config import load_config
    cfg = load_config(TD.TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=state_dir).ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, cfg.account.initial_cash, "2026-10-01")
    return cfg, paths, c


# ---------------------------------------------------------------- M7-D10-02（M7-D2-07、M7-D9-05）登錄表對帳

def test_adjust_sets_holding_from_any_status_and_syncs_cost(conn):
    _reg(conn, "ACQ", "REJECT", {})
    book.adjust(conn, date=D2, ticker="ACQ", set_qty=50, set_avg_cost=80.0, first_buy_date="2025-06-01",
                note="併購換得")
    st, first, data = _reg_row(conn, "ACQ")
    assert st == "HOLDING" and data["cost"] == 80.0 and first == "2025-06-01"
    assert "ACQ" in [e["ticker"] for e in export_registry(conn, "holdings")]


def test_adjust_cost_only_syncs_registry_cost(conn):
    _pos(conn, "ZZZ", 100, 80.0)
    _reg(conn, "ZZZ", "HOLDING", {"cost": 80.0})
    book.adjust(conn, date=D2, ticker="ZZZ", set_avg_cost=40.0, note="人工改成本")
    assert _reg_row(conn, "ZZZ")[2]["cost"] == 40.0


def test_reconcile_registry(conn):
    _pos(conn, "NEWCO", 50, 40.0, first="2025-06-01")
    _reg(conn, "NEWCO", "WATCH", {"auto_buy_cap": 30.0})         # 完整分析把未登錄持股寫成 WATCH
    db.insert(conn, "anomalies", {"ticker": "NEWCO", "kind": "unregistered_holding", "detail": {}, "status": "open",
                                  "pause_trading": 0, "created_at": db.now_iso()})
    _reg(conn, "GONE", "HOLDING", dict(HELD))                     # 登錄表 HOLDING 但帳上無股
    _reg(conn, "KEEP", "HOLDING", {"cost": 10.0})
    _pos(conn, "KEEP", 5, 10.0)
    notes = book.reconcile_registry(conn, D2)
    st, _, data = _reg_row(conn, "NEWCO")
    assert st == "HOLDING" and data["cost"] == 40.0 and data["auto_buy_cap"] == 30.0
    st, _, data = _reg_row(conn, "GONE")
    assert st == "WATCH" and data["auto_buy_cap"] is None
    assert _reg_row(conn, "KEEP")[0] == "HOLDING"
    assert conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='unregistered_holding' AND status='open'"
                        ).fetchone()[0] == 0
    assert len(notes) == 3
    assert book.reconcile_registry(conn, D3) == []                # 冪等


def test_daily_reconciles_before_trigger_check(tmp_path, state_dir):
    cfg, paths, c = _world(tmp_path, state_dir)
    _pos(c, "ZZZ", 100, 40.0)
    TD._reg(c, "ZZZ", "WATCH", {"cik": "3", "grade": "B"})
    c.commit()
    s = TD._run(cfg, paths, c, TD.D1, TD.FakeAlpaca(TD._bars()))
    assert "error" not in s
    assert _reg_row(c, "ZZZ")[0] == "HOLDING"
    assert any("ZZZ" in n and "HOLDING" in n for n in s["notes"])
    c.close()


# ---------------------------------------------------------------- M7-D7-06 人工改股數時的委託

def test_adjust_qty_change_cancels_buys_and_rescales_sells(conn):
    _pos(conn, "SPL", 100, 50.0)
    _reg(conn, "SPL", "HOLDING", {"cost": 50.0})
    sell = broker.place_order(conn, "SPL", "SELL", 100, None, None, D1)
    lim = broker.place_order(conn, "SPL", "SELL", 30, 80.0, None, D1)
    buy = broker.place_order(conn, "SPL", "BUY", 10, 49.0, None, D1)
    r = book.adjust(conn, date=D2, ticker="SPL", set_qty=200, set_avg_cost=25.0, note="2:1 拆股人工入帳")
    rows = {x["order_id"]: dict(x) for x in conn.execute("SELECT * FROM orders")}
    assert rows[sell]["status"] == "open" and rows[sell]["qty"] == 200
    assert rows[lim]["qty"] == 60 and rows[lim]["limit_price"] == pytest.approx(40.0)
    assert rows[buy]["status"] == "cancelled" and "重新規劃" in rows[buy]["cancel_reason"]
    assert r["cancelled_orders"] == [buy] and len(r["rescaled_sell_orders"]) == 2


def test_adjust_same_qty_leaves_orders(conn):
    _pos(conn, "SPL", 100, 50.0)
    buy = broker.place_order(conn, "SPL", "BUY", 10, 49.0, None, D1)
    book.adjust(conn, date=D2, ticker="SPL", set_avg_cost=45.0, note="改成本")
    assert book.open_orders(conn)[0]["order_id"] == buy
