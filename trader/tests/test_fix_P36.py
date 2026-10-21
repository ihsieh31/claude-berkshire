"""P36（D51／ASK-2＝A）：decisions.provenance 欄 migration（冪等、舊 DB 可升級）；
Hark 匯出腳本在副本填 provenance、只動副本、產生 tar 與 sha256（hark/ 不在時略過）。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from trader import db

ROOT = Path(__file__).resolve().parents[2]
EXPORT_SH = ROOT / "hark" / "export.sh"


def _cols(conn, table="decisions"):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _old_db(path: Path, monkeypatch) -> None:
    """建一個只套到版本 2 的舊 DB（升級前的樣子）。"""
    conn = db.connect(path)
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:2])
        assert db.migrate(conn) == 2
    conn.close()


def test_fresh_db_has_provenance_column(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.migrate(conn) == len(db.MIGRATIONS) == 3
    assert "provenance" in _cols(conn)
    db.insert(conn, "decisions", {"decision_id": "d1", "ticker": "KO", "decision_date": "2026-10-15",
                                  "data": {}, "created_at": db.now_iso(),
                                  "provenance": {"origin": "x", "effort": "medium"}})
    got = conn.execute("SELECT provenance FROM decisions").fetchone()[0]
    assert json.loads(got) == {"origin": "x", "effort": "medium"}


def test_old_v2_db_upgrades_and_is_idempotent(tmp_path, monkeypatch):
    p = tmp_path / "old.db"
    _old_db(p, monkeypatch)
    conn = db.connect(p)
    assert "provenance" not in _cols(conn)
    db.insert(conn, "decisions", {"decision_id": "d1", "ticker": "KO", "decision_date": "2026-10-15",
                                  "signal": "REJECT", "data": {"a": 1}, "created_at": db.now_iso()})
    assert db.migrate(conn) == 3
    assert db.migrate(conn) == 3
    assert "provenance" in _cols(conn)
    row = conn.execute("SELECT signal, data, provenance FROM decisions WHERE decision_id='d1'").fetchone()
    assert (row[0], json.loads(row[1]), row[2]) == ("REJECT", {"a": 1}, None)
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")] == [1, 2, 3]


def _seed_state(state: Path, monkeypatch) -> None:
    state.mkdir()
    (state / "reports").mkdir()  # 白名單至少一項（export.sh 對空白名單 tar 會失敗；真實 state 一定有）
    (state / "reports" / "r.json").write_text("{}")
    _old_db(state / "trader.db", monkeypatch)
    conn = db.connect(state / "trader.db")
    now = db.now_iso()
    db.insert(conn, "runs", {"run_id": "r1", "kind": "analyze", "asof": "2026-10-15", "status": "done"})
    db.insert(conn, "jobs", {"job_id": "j1", "run_id": "r1", "job_type": "DOSSIER", "ticker": "KO",
                             "asof": "2026-10-15", "status": "done", "created_at": now})
    db.insert(conn, "llm_calls", {"job_id": "j1", "run_id": "r1", "backend": "hark_queue", "ok": 1, "created_at": now})
    for did, t in (("KO-20261015-1", "KO"), ("PEP-20261015-1", "PEP")):
        db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": "2026-10-15", "run_id": "r1",
                                      "signal": "REJECT", "data": {"reason_one_liner": "x"}, "created_at": now})
        db.insert(conn, "signals", {"decision_id": did, "ticker": t, "signal": "REJECT", "data": {}, "created_at": now})
        db.insert(conn, "registry", {"ticker": t, "status": "REJECT", "data": {}, "updated_at": now})
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


@pytest.mark.skipif(not EXPORT_SH.exists() or not shutil.which("sqlite3"), reason="hark/export.sh 只在 Hark（B 機）")
def test_export_fills_provenance_on_copy_only(tmp_path, monkeypatch):
    state, out = tmp_path / "state", tmp_path / "out"
    _seed_state(state, monkeypatch)
    env = dict(os.environ, OUT=str(out), EXPORT_SUFFIX="_t", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(["bash", str(EXPORT_SH), str(state)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=110)
    assert r.returncode == 0, r.stdout + r.stderr
    c = sqlite3.connect(out / "export_state" / "trader.db")
    prov = {d: json.loads(p) for d, p in c.execute("SELECT decision_id, provenance FROM decisions")}
    assert prov["KO-20261015-1"] == {"origin": "hark_b", "effort": "max", "price_source": "yahoo", "backend": "hark_queue"}
    assert prov["PEP-20261015-1"]["backend"] is None and prov["PEP-20261015-1"]["origin"] == "hark_b"
    assert c.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 3
    c.close()
    src = sqlite3.connect(f"file:{state / 'trader.db'}?immutable=1", uri=True)
    assert "provenance" not in _cols(src)  # 原 state 不動
    src.close()
    m = json.loads((out / "export_state" / "MANIFEST.json").read_text(encoding="utf-8"))
    assert m["effort"] == "max" and m["schema_version"] == 3
    tars = list(out.glob("berkshire_state_B_*_t.tar.gz"))
    assert len(tars) == 1
    chk = subprocess.run(["sha256sum", "-c", tars[0].name + ".sha256"], cwd=out, capture_output=True, text=True)
    assert chk.returncode == 0, chk.stdout + chk.stderr


@pytest.mark.skipif(not EXPORT_SH.exists(), reason="hark/export.sh 只在 Hark（B 機）")
def test_export_rejects_bad_effort(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _seed_state(state, monkeypatch)
    env = dict(os.environ, OUT=str(tmp_path / "out"), EFFORT="max'; drop table decisions;--")
    r = subprocess.run(["bash", str(EXPORT_SH), str(state)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "EFFORT" in r.stdout
