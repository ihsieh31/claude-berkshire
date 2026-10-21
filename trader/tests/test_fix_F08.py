"""F08 回歸測試：X6（M6-D10-01 尾段：建倉計畫記來源決策與目標權重來源）、
F06 查核殘留（賣光清 t30_at_valuation，避免殖利率變動排 PRICE 用舊輸入回寫自動買進上限）。全部離線、只用暫存 state。"""
import json
import sys

import pytest

from trader import db
from trader.ledger import book, broker
from trader.paths import get_paths
from trader.pipelines.review import export_registry

import test_daily_pipeline as TD

HELD = {"auto_buy_cap": 120.0, "grade": "B", "cost": 100.0, "t30_at_valuation": 0.047,
        "sell_review_above": 150.0, "absurd_price_line": 400.0, "review_price": 100.0,
        "build_plan": {"target_weight": 0.1, "tranches_left": 2, "started": "2025-01-02"}}


@pytest.fixture
def conn(state_dir):
    paths = get_paths().ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, 100_000, "2026-10-01")
    yield c
    c.close()


def _setup_held(conn, t="RRR"):
    db.upsert(conn, "registry", {"ticker": t, "status": "HOLDING", "first_buy_date": "2025-01-03",
                                 "data": dict(HELD), "updated_at": db.now_iso()}, ["ticker"])
    db.upsert(conn, "positions", {"ticker": t, "qty": 10, "avg_cost": 100.0, "first_buy_date": "2025-01-03",
                                  "updated_at": db.now_iso()}, ["ticker"])


def _data(conn, t="RRR"):
    r = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return r["status"], json.loads(r["data"])


def _review_due():
    sys.path.insert(0, str(get_paths().skill_deep / "scripts"))
    import review_due
    return review_due


# ---------------------------------------------------------------- F06 殘留：賣光清 t30_at_valuation

def test_sell_out_clears_t30_so_rate_move_does_not_schedule_price(conn):
    _setup_held(conn)
    broker.place_order(conn, "RRR", "SELL", 10, None, None, "2026-10-14")
    broker.match_orders(conn, "2026-10-15", {"RRR": {"open": 90, "high": 91, "low": 89, "close": 90}}, 0, 0)
    st, data = _data(conn)
    assert st == "WATCH" and data["t30_at_valuation"] is None and data["auto_buy_cap"] is None
    # 只供持股賣出側／論點檢查用的欄位保留（未持有時 review_due 不據此排 PRICE）
    assert data["sell_review_above"] == 150.0 and data["absurd_price_line"] == 400.0 and data["review_price"] == 100.0
    e = [x for x in export_registry(conn, "watch") if x["ticker"] == "RRR"][0]
    assert "t30_at_valuation" not in e
    act, reasons, _ = _review_due().check(e, [], None, "2026-10-16", 0.065)   # 殖利率 4.7% → 6.5%
    assert act != "PRICE" and not any(r.startswith("[PRICE]") for r in reasons), reasons


def test_adjust_and_reconcile_sold_out_also_clear_t30(conn):
    _setup_held(conn)
    book.adjust(conn, date="2026-10-15", ticker="RRR", set_qty=0, note="下市清算")
    assert _data(conn)[1]["t30_at_valuation"] is None
    _setup_held(conn, "SSS")
    conn.execute("DELETE FROM positions WHERE ticker='SSS'")
    book.reconcile_registry(conn, "2026-10-15")
    st, data = _data(conn, "SSS")
    assert st == "WATCH" and data["t30_at_valuation"] is None


def test_partial_sell_keeps_t30(conn):
    _setup_held(conn)
    broker.place_order(conn, "RRR", "SELL", 4, None, None, "2026-10-14")
    broker.match_orders(conn, "2026-10-15", {"RRR": {"open": 90, "high": 91, "low": 89, "close": 90}}, 0, 0)
    st, data = _data(conn)
    assert st == "HOLDING" and data["t30_at_valuation"] == 0.047


# ---------------------------------------------------------------- X6：建倉計畫記來源

def test_daily_build_plan_has_source_fields(tmp_path, state_dir):
    from trader.config import load_config
    import test_engine as TE
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=state_dir).ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, cfg.account.initial_cash, "2026-10-01")
    try:
        TD._reg(c, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
        TD._dec(c, "AAA-20261015-1", "AAA", TD.D1, "BUY",
                card={**TD.CARD, "decision_log_id": "AAA-20261015-1", "target_weight_pct": 20})
        s = TD._run(cfg, paths, c, TD.D1, TD.FakeAlpaca(TD._bars()))
        assert "error" not in s, s
        assert len(s["orders"]) == 1
        bp = json.loads(c.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])["build_plan"]
        assert bp["target_weight"] == pytest.approx(0.10)          # 卡 20%、等級 B 上限 10%
        assert bp["decision_id"] == "AAA-20261015-1"
        assert bp["target_weight_basis"]["decision_id"] == "AAA-20261015-1"
        assert bp["target_weight_basis"]["grade"] == "B"
        assert isinstance(bp["target_weight_basis"]["notes"], list)
    finally:
        c.close()


# ---------------------------------------------------------------- F08R：X6 返工（PRICE 觸發的 BUY）、X7

def _price_triggered_buy(tmp_path, state_dir, reg_t):
    """完整分析卡 AAA-20261010-1（登錄表 basis 指向它）＋PRICE 複製卡 AAA-20261015-1，由 PRICE 觸發 BUY。
    回傳 build_plan。"""
    from trader.config import load_config
    import test_engine as TE
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=state_dir).ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, cfg.account.initial_cash, "2026-10-01")
    try:
        full = {**TD.CARD, "as_of": "2026-10-10", "decision_log_id": "AAA-20261010-1", "target_weight_pct": 20}
        TD._reg(c, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B", "target_weight": reg_t,
                                    "target_weight_basis": {"decision_id": "AAA-20261010-1", "grade": "B",
                                                            "notes": ["完整分析"]}})
        db.insert(c, "decisions", {"decision_id": "AAA-20261010-1", "ticker": "AAA", "decision_date": "2026-10-10",
                                   "signal": "WATCH", "data": {"action": "WATCH", "review_type": None},
                                   "created_at": db.now_iso()})
        db.insert(c, "signals", {"decision_id": "AAA-20261010-1", "ticker": "AAA", "signal": "WATCH", "data": full,
                                 "created_at": db.now_iso()})
        TD._dec(c, "AAA-20261015-1", "AAA", TD.D1, "BUY", review_type="PRICE",
                card={**full, "as_of": TD.D1, "decision_log_id": "AAA-20261015-1"})
        s = TD._run(cfg, paths, c, TD.D1, TD.FakeAlpaca(TD._bars()))
        assert "error" not in s, s
        assert len(s["orders"]) == 1
        return json.loads(c.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])["build_plan"]
    finally:
        c.close()


def test_price_triggered_buy_basis_points_to_full_analysis(tmp_path, state_dir):
    bp = _price_triggered_buy(tmp_path, state_dir, 0.10)
    assert bp["target_weight"] == pytest.approx(0.10)
    assert bp["decision_id"] == "AAA-20261015-1"                       # 觸發下單的 PRICE 決策
    assert bp["target_weight_basis"]["decision_id"] == "AAA-20261010-1"  # 決定 T 的完整分析
    assert bp["target_weight_basis"]["notes"] == ["完整分析"]


def test_basis_falls_back_to_card_when_registry_target_differs(tmp_path, state_dir):
    bp = _price_triggered_buy(tmp_path, state_dir, 0.05)   # 登錄表 T 與本次計畫 T 不同 → 不沿用登錄表 basis
    assert bp["target_weight"] == pytest.approx(0.10)
    assert bp["target_weight_basis"]["decision_id"] == "AAA-20261015-1"
    assert bp["target_weight_basis"]["grade"] == "B" and isinstance(bp["target_weight_basis"]["notes"], list)


def _write_price(conn, asof):
    from trader.analysis import writeback as W
    return W.write_price_result(conn, get_paths(), "RRR", asof, "r1", action="HOLD", gate1=True, gate2="通過",
                                gate3="通過", cap={"auto_buy_cap": 120.0, "IV_base": 150.0, "absurd_price_line": 400.0},
                                price=95.0, price_date=asof, t30=0.047, reason="x", notes=[])


def test_price_finished_after_sell_out_does_not_restore_cap_or_t30(conn):
    _setup_held(conn)
    broker.place_order(conn, "RRR", "SELL", 10, None, None, "2026-10-14")
    broker.match_orders(conn, "2026-10-15", {"RRR": {"open": 90, "high": 91, "low": 89, "close": 90}}, 0, 0)
    did = _write_price(conn, "2026-10-14")       # 持有期間排入（資料日 10-14），賣光（10-15）後才回寫
    st, data = _data(conn)
    assert st == "WATCH" and data["auto_buy_cap"] is None and data["t30_at_valuation"] is None
    assert data["last_review_date"] == "2026-10-14" and data["review_price"] == 95.0
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])
    assert any("已賣光" in n for n in log["framework_notes"])
    e = [x for x in export_registry(conn, "watch") if x["ticker"] == "RRR"][0]
    act, reasons, _ = _review_due().check(e, [], None, "2026-10-16", 0.065)
    assert act != "PRICE" and not any(r.startswith("[PRICE]") for r in reasons), reasons


def test_price_after_sold_out_date_still_writes_cap(conn):
    _setup_held(conn)
    data = dict(HELD, sold_out_at="2026-09-01")   # 先前賣光、之後重新持有：較新資料日照常回寫
    conn.execute("UPDATE registry SET data=? WHERE ticker='RRR'", (json.dumps(data),))
    conn.commit()
    _write_price(conn, "2026-10-14")
    d = _data(conn)[1]
    assert d["auto_buy_cap"] == 120.0 and d["t30_at_valuation"] == 0.047
