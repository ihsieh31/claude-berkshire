"""資料庫測試：連線、migration 冪等、表與約束、insert／upsert。全部使用 tmp_path。"""
from __future__ import annotations

import re
import sqlite3

import pytest

from trader import db

EXPECTED_TABLES = {
    "schema_version", "registry", "decisions", "signals", "runs", "jobs", "llm_calls", "orders",
    "fills", "positions", "cash_ledger", "corporate_actions", "nav_daily", "anomalies", "universe_versions",
}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "nested" / "trader.db")
    yield c
    c.close()


def _tables(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_connect_creates_parent_dirs_and_pragmas(tmp_path):
    c = db.connect(tmp_path / "a" / "b" / "x.db")
    try:
        assert (tmp_path / "a" / "b" / "x.db").exists()
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert c.row_factory is sqlite3.Row
    finally:
        c.close()


def test_migrate_returns_version_and_is_idempotent(conn):
    assert db.migrate(conn) == len(db.MIGRATIONS) == 2
    assert db.migrate(conn) == 2
    rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert "approvals" in _tables(conn)


def test_all_v1_tables_exist(conn):
    db.migrate(conn)
    assert EXPECTED_TABLES <= _tables(conn)


def test_indexes_exist(conn):
    db.migrate(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"jobs_run_status", "llm_calls_run"} <= names


def test_corporate_actions_unique_constraint(conn):
    db.migrate(conn)
    row = {"ticker": "AAPL", "action_type": "SPLIT", "effective_date": "2026-01-02",
           "status": "applied", "created_at": db.now_iso()}
    db.insert(conn, "corporate_actions", row)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert(conn, "corporate_actions", row)
    # 不同生效日不衝突
    db.insert(conn, "corporate_actions", {**row, "effective_date": "2026-02-02"})


def test_insert_returns_lastrowid_and_json_encodes(conn):
    db.migrate(conn)
    rid = db.insert(conn, "decisions", {
        "decision_id": "d1", "ticker": "KO", "decision_date": "2026-03-01",
        "data": {"reason": "護城河", "tags": ["a", "b"]}, "created_at": db.now_iso(),
    })
    assert rid == 1  # rowid 型別表，lastrowid 為隱含 rowid
    row = conn.execute("SELECT data FROM decisions WHERE decision_id='d1'").fetchone()
    assert '"護城河"' in row["data"]  # ensure_ascii=False


def test_insert_list_value_is_json(conn):
    db.migrate(conn)
    db.insert(conn, "jobs", {
        "job_id": "r:KO:DOSSIER:1", "run_id": "r", "job_type": "DOSSIER", "ticker": "KO",
        "asof": "2026-03-01", "status": "pending", "deps": ["x:1", "x:2"], "created_at": db.now_iso(),
    })
    row = conn.execute("SELECT deps, params FROM jobs WHERE job_id='r:KO:DOSSIER:1'").fetchone()
    assert row["deps"] == '["x:1", "x:2"]'
    assert row["params"] == "{}"  # 預設值


def test_insert_rejects_bad_identifier(conn):
    db.migrate(conn)
    with pytest.raises(ValueError):
        db.insert(conn, "jobs; DROP TABLE jobs", {"job_id": "x"})
    with pytest.raises(ValueError):
        db.insert(conn, "runs", {"run_id": "x", "bad-col": 1})


def test_orders_check_constraint(conn):
    db.migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert(conn, "orders", {"order_id": "o1", "ticker": "KO", "side": "HOLD", "qty": 1,
                                   "status": "open", "created_date": "2026-01-01", "updated_at": db.now_iso()})


def test_fill_requires_existing_order(conn):
    db.migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):  # foreign_keys=ON
        db.insert(conn, "fills", {"order_id": "missing", "ticker": "KO", "side": "BUY",
                                  "qty": 1, "price": 1.0, "fill_date": "2026-01-01"})


def test_upsert_inserts_then_updates(conn):
    db.migrate(conn)
    row = {"ticker": "KO", "status": "watch", "data": {"v": 1}, "updated_at": db.now_iso()}
    db.upsert(conn, "registry", row, key=["ticker"])
    db.upsert(conn, "registry", {**row, "status": "holding", "data": {"v": 2}}, key=["ticker"])
    rows = conn.execute("SELECT status, data FROM registry WHERE ticker='KO'").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "holding"
    assert '"v": 2' in rows[0]["data"]


def test_upsert_validates_key(conn):
    db.migrate(conn)
    with pytest.raises(ValueError):
        db.upsert(conn, "registry", {"ticker": "KO", "data": "{}", "updated_at": "x"}, key=[])
    with pytest.raises(ValueError):
        db.upsert(conn, "registry", {"ticker": "KO"}, key=["nope"])


def test_now_iso_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", db.now_iso())
