"""RM7-1：R7-01（SEC 失敗不推進 8-K 窗口）、M7-RS7-01（人工調股換算登錄表）、M7-RS7-02（人工暫停 SELL_ALL 不遺失）、
M7-S2-N01（健康度只認 daily_close／daily_skip）、D82 判斷。全部離線。"""
from __future__ import annotations

import json

import pytest

from trader import db
from trader.ledger import book, broker
from trader.ledger import corp_actions as ca
from trader.paths import get_paths
from trader.pipelines import daily as DL
from trader.portfolio import sizing as Z
from trader.report import metrics as M

NOW = "2026-10-16T08:00:00+00:00"


@pytest.fixture
def paths(state_dir):
    return get_paths().ensure()


@pytest.fixture
def conn(paths):
    c = db.connect(paths.db)
    db.migrate(c)
    yield c
    c.close()


def _run(conn, asof, summary, kind="daily_close", status="done", rid=None):
    db.insert(conn, "runs", {"run_id": rid or f"daily-{asof}", "kind": kind, "asof": asof, "status": status,
                             "started_at": NOW, "finished_at": NOW, "summary": json.dumps(summary)})


# ---------------------------------------------------------------- R7-01

def test_sec_8k_since_skips_back_over_stale_days(conn):
    _run(conn, "2026-10-13", {})
    _run(conn, "2026-10-14", {"sec_8k_stale": ["x.json"]})
    _run(conn, "2026-10-15", {"sec_8k_stale": ["y.json"]})
    assert DL._sec_8k_since(conn, "2026-10-16", "2026-10-15") == "2026-10-13"


def test_sec_8k_since_normal_is_prev(conn):
    _run(conn, "2026-10-15", {})
    assert DL._sec_8k_since(conn, "2026-10-16", "2026-10-15") == "2026-10-15"


def test_sec_8k_since_first_daily(conn):
    assert DL._sec_8k_since(conn, "2026-10-16", "2026-10-15") == "2026-10-15"


def test_detect_widened_8k_window_catches_missed_filing(conn):
    f = {"KO": [{"filingDate": "2026-10-14", "form": "8-K", "items": "5.03", "accessionNumber": "A1"}]}
    assert ca.detect(conn, "2026-10-16", "2026-10-15", ["KO"], [], f, {}, {}) == []
    ev = ca.detect(conn, "2026-10-16", "2026-10-15", ["KO"], [], f, {}, {}, since_8k="2026-10-13")
    assert [e["source"] for e in ev] == ["8k"] and ev[0]["date"] == "2026-10-14"


# ---------------------------------------------------------------- M7-RS7-02

def _hold(conn, t, qty, avg):
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?)", (t, qty, avg, "2025-01-02", NOW))
    conn.commit()


def _dec(conn, did, t, date, data):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "run_id": None,
                                  "job_id": None, "signal": data.get("action"), "reason_code": None,
                                  "data": data, "created_at": NOW})


def test_manual_pause_keeps_sell_all_order_but_blocks_other_orders(conn):
    book.ensure_initial_deposit(conn, 10_000, "2026-10-01")
    _hold(conn, "MSFT", 5, 50.0)
    _dec(conn, "MSFT-D-1", "MSFT", "2026-10-14", {"action": "SELL_ALL"})
    _dec(conn, "MSFT-D-2", "MSFT", "2026-10-14", {"action": "TRIM"})
    book.pause(conn, "MSFT", date="2026-10-14", note="人工")
    oid = broker.place_order(conn, "MSFT", "SELL", 5, None, "MSFT-D-1", "2026-10-14")
    with pytest.raises(broker.BrokerError):
        broker.place_order(conn, "MSFT", "SELL", 2, None, "MSFT-D-2", "2026-10-14")
    assert conn.execute("SELECT status FROM orders WHERE order_id=?", (oid,)).fetchone()[0] == "open"


# ---------------------------------------------------------------- M7-RS7-01

def test_manual_split_adjust_rescales_registry_once(conn):
    book.ensure_initial_deposit(conn, 10_000, "2026-10-01")
    _hold(conn, "KO", 100, 60.0)
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "first_buy_date": "2025-01-02",
                                 "data": {"auto_buy_cap": 70.0, "peak_qty": 100,
                                          "last_trim": {"line": 90.0, "shares": 10, "peak_shares": 100}},
                                 "updated_at": NOW}, ["ticker"])
    r = book.adjust(conn, date="2026-10-16", ticker="KO", set_qty=200, set_avg_cost=30.0, note="1 拆 2")
    assert r["registry_rescaled"]["ratio"] == 2
    d = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])
    assert d["auto_buy_cap"] == 35.0
    assert d["peak_qty"] == 200                       # 不被 _holding_stats 重複放大
    assert d["last_trim"]["line"] == 45.0 and d["last_trim"]["shares"] == 20


def test_reconcile_qty_change_does_not_rescale(conn):
    book.ensure_initial_deposit(conn, 10_000, "2026-10-01")
    _hold(conn, "KO", 100, 60.0)
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "first_buy_date": "2025-01-02",
                                 "data": {"auto_buy_cap": 70.0}, "updated_at": NOW}, ["ticker"])
    r = book.adjust(conn, date="2026-10-16", ticker="KO", set_qty=50, note="對帳")
    assert "registry_rescaled" not in r and r["warnings"]
    d = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])
    assert d["auto_buy_cap"] == 70.0


def test_avg_cost_only_adjust_does_not_rescale(conn):
    book.ensure_initial_deposit(conn, 10_000, "2026-10-01")
    _hold(conn, "KO", 100, 60.0)
    r = book.adjust(conn, date="2026-10-16", ticker="KO", set_avg_cost=61.0, note="成本更正")
    assert "registry_rescaled" not in r


# ---------------------------------------------------------------- M7-S2-N01

def test_health_ignores_ca_subrun(conn, paths):
    _run(conn, "2026-10-14", {})
    _run(conn, "2026-10-15", {}, kind="corp_action", rid="daily-2026-10-15:ca")
    last = conn.execute("SELECT MAX(asof) FROM runs WHERE kind IN ('daily_close', 'daily_skip') "
                        "AND status='done'").fetchone()[0]
    assert last == "2026-10-14"
    src = open(M.__file__, encoding="utf-8").read()
    assert "run_id LIKE 'daily-%'" not in src


# ---------------------------------------------------------------- D82

def test_price_triggered_hold_review(conn):
    _dec(conn, "KO-H-1", "KO", "2026-10-16", {"action": "TRIM", "trigger": {"full_stage": Z.PRICE_TRIGGER}})
    _dec(conn, "KO-H-2", "KO", "2026-10-16", {"action": "TRIM", "trigger": {"full_stage": "NEWS"}})
    assert DL._price_triggered(conn, "KO-H-1") is True
    assert DL._price_triggered(conn, "KO-H-2") is False
    assert DL._price_triggered(conn, None) is False
