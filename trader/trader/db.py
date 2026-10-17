"""SQLite 連線、migration 與基本寫入工具。版本 1 的表結構依 CONTRACTS.md 逐字實作。"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_V1 = """
CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE registry(ticker TEXT PRIMARY KEY, status TEXT, first_buy_date TEXT, data TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE decisions(decision_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, decision_date TEXT NOT NULL, run_id TEXT, job_id TEXT, signal TEXT, reason_code TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE signals(decision_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, signal TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE runs(run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, asof TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, finished_at TEXT, summary TEXT);

CREATE TABLE jobs(job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, job_type TEXT NOT NULL, ticker TEXT NOT NULL, asof TEXT NOT NULL, tier TEXT, model TEXT,
     status TEXT NOT NULL, deps TEXT NOT NULL DEFAULT '[]', params TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
     input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL, latency_s REAL,
     sandbox TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
CREATE INDEX jobs_run_status ON jobs(run_id, status);

CREATE TABLE llm_calls(call_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, run_id TEXT, tier TEXT, backend TEXT, model TEXT, attempt INTEGER,
     prompt_sha256 TEXT, input_path TEXT, output_path TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, latency_s REAL,
     ok INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL);
CREATE INDEX llm_calls_run ON llm_calls(run_id);

CREATE TABLE orders(order_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), qty INTEGER NOT NULL,
     limit_price REAL, status TEXT NOT NULL CHECK(status IN ('open','filled','cancelled','expired')), decision_id TEXT,
     created_date TEXT NOT NULL, cancel_reason TEXT, updated_at TEXT NOT NULL);

CREATE TABLE fills(fill_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL REFERENCES orders(order_id), ticker TEXT NOT NULL, side TEXT NOT NULL,
     qty INTEGER NOT NULL, price REAL NOT NULL, commission REAL NOT NULL DEFAULT 0, fill_date TEXT NOT NULL);

CREATE TABLE positions(ticker TEXT PRIMARY KEY, qty INTEGER NOT NULL, avg_cost REAL NOT NULL, first_buy_date TEXT, updated_at TEXT NOT NULL);

CREATE TABLE cash_ledger(entry_id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, amount REAL NOT NULL,
     kind TEXT NOT NULL CHECK(kind IN ('deposit','buy','sell','dividend','dividend_receivable','corp_action_cash','commission','adjust')), ref TEXT, note TEXT);

CREATE TABLE corporate_actions(ca_id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, action_type TEXT NOT NULL, effective_date TEXT NOT NULL,
     ratio_new_per_old REAL, cash_per_share REAL, new_ticker TEXT, status TEXT NOT NULL CHECK(status IN ('applied','anomaly','pending_human')),
     source TEXT, data TEXT, created_at TEXT NOT NULL, UNIQUE(ticker, action_type, effective_date));

CREATE TABLE nav_daily(date TEXT PRIMARY KEY, nav REAL NOT NULL, cash REAL NOT NULL, positions_value REAL NOT NULL, n_positions INTEGER NOT NULL, benchmark_close REAL, data TEXT);

CREATE TABLE anomalies(anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, kind TEXT NOT NULL, detail TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
     pause_trading INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT);

CREATE TABLE universe_versions(version_id INTEGER PRIMARY KEY AUTOINCREMENT, asof TEXT NOT NULL, source TEXT, tickers TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
"""

# 版本 2（M6）：opus 同意閘門。同一 run 同一檔至多一列；同意單位是一檔（含之後才產生的 RED_TEAM）
_V2 = """
CREATE TABLE approvals(approval_id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, run_id TEXT NOT NULL, asof TEXT NOT NULL,
     job_id TEXT NOT NULL, job_type TEXT NOT NULL, tier TEXT, holding INTEGER NOT NULL DEFAULT 0, reason TEXT,
     status TEXT NOT NULL CHECK(status IN ('pending','deferred','approved','rejected','superseded','done')),
     created_at TEXT NOT NULL, decided_at TEXT, decided_via TEXT, note TEXT, UNIQUE(run_id, ticker));
CREATE INDEX approvals_status ON approvals(status);
"""

MIGRATIONS: list[str] = [_V1, _V2]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def connect(path: Path) -> sqlite3.Connection:
    """開啟資料庫。自動建立父目錄；啟用 WAL、外鍵與忙碌等待。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def now_iso() -> str:
    """UTC ISO8601，秒精度，結尾 Z。"""
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def migrate(conn: sqlite3.Connection) -> int:
    """套用尚未套用的版本，回傳目前版本。冪等。每個版本在單一交易內套用。"""
    current = _current_version(conn)
    for i, sql in enumerate(MIGRATIONS, start=1):
        if i <= current:
            continue
        stamp = now_iso()
        script = (
            "BEGIN;\n" + sql + "\n"
            f"INSERT INTO schema_version(version, applied_at) VALUES ({i}, '{stamp}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = i
    return current


def _encode(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _check_ident(name: str) -> None:
    if not _IDENT.match(name):
        raise ValueError(f"不合法的識別字：{name!r}")


def insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    """插入一列並提交。dict／list 值自動 json 化。回傳 lastrowid。"""
    _check_ident(table)
    for col in row:
        _check_ident(col)
    cols = list(row)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    cur = conn.execute(sql, [_encode(row[c]) for c in cols])
    conn.commit()
    return int(cur.lastrowid)


def upsert(conn: sqlite3.Connection, table: str, row: dict, key: list[str]) -> None:
    """INSERT ... ON CONFLICT(key) DO UPDATE 並提交。key 欄位需有 UNIQUE 或 PRIMARY KEY 約束。"""
    _check_ident(table)
    if not key:
        raise ValueError("upsert 需要至少一個 key 欄位")
    for col in row:
        _check_ident(col)
    for col in key:
        if col not in row:
            raise ValueError(f"key 欄位 {col!r} 不在資料列中")
    cols = list(row)
    updates = [c for c in cols if c not in key]
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT({', '.join(key)}) "
    if updates:
        sql += "DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in updates)
    else:
        sql += "DO NOTHING"
    conn.execute(sql, [_encode(row[c]) for c in cols])
    conn.commit()
