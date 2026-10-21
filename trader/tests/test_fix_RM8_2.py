"""RM8-2：M8-R1-01（首次 daily 前入金不吞初始資金）、M8-R4-01（cap_input_file 相對路徑／舊絕對路徑退回）、
M8-R4-03（RETIRED 不被新分析改回）、M8-R5-01（觀察名單排除日與初選 X5／X1 同口徑）、M8-R7-01（首次 daily 讀全部歷史）。"""
from __future__ import annotations

import json

import pytest

from trader import db
from trader.analysis import writeback
from trader.ledger import book
from trader.paths import get_paths
from trader.pipelines import daily as DL
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


def _reg(conn, t, status, data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "first_buy_date": None,
                                 "data": data, "updated_at": NOW}, ["ticker"])


def test_deposit_before_first_daily_keeps_initial_cash(conn):
    book.adjust(conn, date="2026-10-19", deposit=50_000, note="入金")
    book.ensure_initial_deposit(conn, 100_000, "2026-10-20")
    book.ensure_initial_deposit(conn, 100_000, "2026-10-21")   # 冪等
    total = conn.execute("SELECT SUM(amount) FROM cash_ledger WHERE kind='deposit'").fetchone()[0]
    assert total == 150_000


def test_norm_eps_relative_and_foreign_absolute(conn, paths):
    (paths.state / "cap_inputs").mkdir(parents=True, exist_ok=True)
    (paths.state / "cap_inputs" / "XYZ.json").write_text(json.dumps({"norm_eps": 5}), encoding="utf-8")
    assert book._norm_eps("cap_inputs/XYZ.json", conn) == 5.0
    assert book._norm_eps("/no/such/B-machine/state/cap_inputs/XYZ.json", conn) == 5.0   # 換機後舊絕對路徑
    assert book._norm_eps(None, conn) is None


def test_holding_stats_uses_relative_cap_input(conn, paths):
    (paths.state / "cap_inputs").mkdir(parents=True, exist_ok=True)
    (paths.state / "cap_inputs" / "XYZ.json").write_text(json.dumps({"norm_eps": 5}), encoding="utf-8")
    _reg(conn, "XYZ", "WATCH", {"cap_input_file": "/old/B/state/cap_inputs/XYZ.json"})
    book._holding_stats(conn, "XYZ", 10, 100.0, True, NOW)
    d = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='XYZ'").fetchone()[0])
    assert d["entry_norm_pe"] == 20.0


def test_writeback_stores_relative_cap_input_path():
    src = open(writeback.__file__, encoding="utf-8").read()
    assert 'entry["cap_input_file"] = f"cap_inputs/{t}.json"' in src


def test_retired_not_reverted_by_new_analysis(conn):
    _reg(conn, "QRVO", "RETIRED", {})
    _reg(conn, "HLD", "HOLDING", {})
    notes: list[str] = []
    assert writeback._resolve_status(conn, "QRVO", "WATCH", notes) == "RETIRED" and notes
    assert writeback._resolve_status(conn, "HLD", "WATCH", []) == "HOLDING"
    assert writeback._resolve_status(conn, "NEW", "WATCH", []) == "WATCH"


def test_watchlist_exclusion_matches_screen(conn):
    _reg(conn, "AAA", "REJECT", {"last_full_review_date": "2026-10-15", "origin": "hark_b"})
    _reg(conn, "BBB", "REJECT", {"last_full_review_date": "2026-10-15"})
    _reg(conn, "CCC", "MISSING_DATA", {"last_full_review_date": "2026-10-15", "last_reason_code": "NOT_THIS_SKILL"})
    w = {x["ticker"]: x for x in M.watchlist(conn)}
    assert w["AAA"]["exclude_until"] is None and w["AAA"]["exclude_note"]
    assert w["BBB"]["exclude_until"] == "2027-10-15"
    assert w["CCC"]["exclude_until"] == "2027-10-15"


def test_first_daily_reads_full_history():
    assert DL.FIRST_DAILY_SINCE <= "2000-01-01"
    assert not hasattr(DL, "FIRST_DAILY_LOOKBACK_DAYS")
