"""RM8-3 回歸測試：M8-D2-04 返工、M8-RS2-01／02、M8-R5-03／04、M8-R3-02、M8-R4-04。"""
from __future__ import annotations

import json
import sqlite3

from trader.report import validation as V


def _full_row(**kw):
    x = {"decision_id": "D1", "decision_date": "2026-10-01", "time_zone": "America/New_York", "ticker": "AAA",
         "action": "NO_ACTION", "skill_hashes": {"a": "1"}, "screen_decision": "PASS", "reason_one_liner": "理由",
         "delete_reason": "", "gate3_failures": [], "failure_conditions_hit": [], "review_trigger_hit": [],
         "stop_mode": None, "stopped_at_gate": None, "certainty_grade": "B"}
    x.update(kw)
    return x


def _stop_row(**kw):
    x = {"decision_id": "D2", "decision_date": "2026-10-01", "time_zone": "America/New_York", "ticker": "BBB",
         "action": "NO_ACTION", "skill_hashes": {"a": "1"}, "screen_decision": "PASS", "stopped_at_gate": "STEP0",
         "reason_one_liner": "停止", "stop_mode": "minimal", "gate3_failures": None, "certainty_grade": None,
         "delete_reason": None}
    x.update(kw)
    return x


FIELDS = ["decision_id", "decision_date", "time_zone", "ticker", "action", "skill_hashes", "screen_decision",
          "reason_one_liner", "delete_reason", "gate3_failures", "failure_conditions_hit", "review_trigger_hit",
          "stop_mode", "stopped_at_gate", "certainty_grade"]


def test_missing_rates_all_zero_for_complete_rows():
    rates = V._missing_rates([_full_row(), _stop_row()], FIELDS)
    assert rates and all(v == 0 for v in rates.values()), rates
    assert "delete_reason" not in rates


def test_missing_rates_none_vs_empty_list_and_delete_reason():
    rates = V._missing_rates([_full_row(gate3_failures=None), _full_row(screen_decision="DELETE", delete_reason="")],
                             FIELDS)
    assert rates["gate3_failures"] == 0.5
    assert rates["failure_conditions_hit"] == 0
    assert rates["delete_reason"] == 1.0


def test_reject_log_has_time_zone_and_screen_decision(monkeypatch):
    from trader.jobs import approvals as A
    import trader.analysis.writeback as W
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs(job_id TEXT, run_id TEXT, ticker TEXT, params TEXT, status TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('j1','r1','AAA',?, 'awaiting_approval')",
                 (json.dumps({"screen_context": {"screen_decision": "PASS-困境"}}),))
    seen = {}
    monkeypatch.setattr(A, "_get", lambda c, i: {"approval_id": i, "status": "pending", "ticker": "AAA",
                                                  "asof": "2026-10-01", "run_id": "r1"})
    monkeypatch.setattr(A, "_close", lambda *a, **k: None)
    monkeypatch.setattr(A, "_skip_waiting", lambda *a, **k: None)

    def fake(conn, paths, t, asof, rid, phase_a, notes=None, tdir=None, *, log_fields=None, job_id=None):
        seen.update(log_fields or {})
        return "AAA-1"
    monkeypatch.setattr(W, "write_stop_result", fake)
    assert A.reject(conn, None, [1]) == ["AAA-1"]
    assert seen == {"screen_decision": "PASS-困境", "time_zone": "America/New_York"}
    A.reject(conn, None, [1], time_zone="Asia/Taipei")
    assert seen["time_zone"] == "Asia/Taipei"


def test_stop_negation_regex():
    assert not V._stop_sell({"reason": "不是價格停損，因護城河受損"})
    assert not V._stop_sell({"reason": "並非停損；不屬於價格停損"})
    assert not V._stop_sell({"reason": "不停損"})
    assert V._stop_sell({"reason": "觸發價格停損"})


def test_honest_text_days():
    assert "約 20 天" in V.honest_text("2026-10-01", "2026-10-21")
    assert "約 2 個月" in V.honest_text("2026-08-01", "2026-10-01")


def test_close_finished_clears_failed_note():
    from trader.jobs import approvals as A
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs(job_id TEXT, run_id TEXT, ticker TEXT, status TEXT)")
    conn.execute("CREATE TABLE approvals(approval_id INTEGER, run_id TEXT, ticker TEXT, status TEXT, note TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('j1','r1','AAA','done')")
    conn.execute("INSERT INTO approvals VALUES (1,'r1','AAA','approved',?)", (A.FAILED_NOTE,))
    conn.execute("INSERT INTO jobs VALUES ('j2','r1','BBB','done')")
    conn.execute("INSERT INTO approvals VALUES (2,'r1','BBB','approved','別的註記')")
    A.close_finished(conn, "r1")
    rows = {r["approval_id"]: (r["status"], r["note"]) for r in conn.execute("SELECT * FROM approvals")}
    assert rows == {1: ("done", None), 2: ("done", "別的註記")}


def test_run_approved_ignores_stale_failed(monkeypatch, tmp_path):
    from trader.pipelines import approved as P
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs(job_id TEXT, run_id TEXT, ticker TEXT, status TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('old','r1','AAA','failed')")
    conn.execute("INSERT INTO jobs VALUES ('new','r1','BBB','awaiting_approval')")
    calls = iter([["r1"], []])
    monkeypatch.setattr(P.approvals, "release", lambda c, rid=None, only_new=False: next(calls))

    class Eng:
        def __init__(self, *a, **k):
            self.ctx = type("C", (), {})()

        def run(self, rid):
            conn.execute("UPDATE jobs SET status='done' WHERE job_id='new'")
            return {"status": "done", "done": 1, "failed": 1}
    monkeypatch.setattr(P, "Engine", Eng)
    paths = type("Pa", (), {"state": tmp_path, "routing_json": None})()
    out = P.run_approved(None, paths, conn, router=object(), clients={})
    assert P.exit_code(out) == 0
    assert out["stale_failed"] == {"r1": ["old"]} and out["new_failed"] == {}
    st = json.loads((tmp_path / P.STATUS_FILE).read_text(encoding="utf-8"))
    assert st["exit_code"] == 0 and st["stale_failed"] == {"r1": ["old"]}


def test_exit_code_new_failed():
    from trader.pipelines import approved as P
    assert P.exit_code({"runs": {}, "new_failed": {"r1": ["x"]}}) == 1


def test_last_screen_counts_non_dry_run_screen_without_analyze():
    from trader.scheduler import Scheduler
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs(run_id TEXT, kind TEXT, asof TEXT, status TEXT, summary TEXT)")
    conn.execute("INSERT INTO runs VALUES ('screen-2026-07-01','bootstrap','2026-07-01','done',?)",
                 (json.dumps({"dry_run": False}),))
    conn.execute("INSERT INTO runs VALUES ('screen-2026-10-01','bootstrap','2026-10-01','done',?)",
                 (json.dumps({"dry_run": True}),))
    conn.execute("INSERT INTO runs VALUES ('screen-2026-04-01','bootstrap','2026-04-01','done','{}')")
    assert Scheduler._last_screen(None, conn, {}) == "2026-07-01"


def test_doctor_state_warns_sched_without_run(tmp_path):
    from trader import cli
    db = tmp_path / "trader.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE runs(run_id TEXT, status TEXT)")
    conn.execute("INSERT INTO runs VALUES ('review-2026-10-01-watch','done')")
    conn.commit()
    conn.close()
    paths = type("Pa", (), {"db": db})()
    res: list = []
    cli._check_sched_runs(paths, {"last_screen": "2026-10-05", "last_watch_review": "2026-10-01"}, res)
    tags = {r[1]: r[0] for r in res}
    assert tags["scheduler.json last_screen 對 runs"] == "WARN"
    assert tags["scheduler.json last_watch_review 對 runs"] == "OK"
