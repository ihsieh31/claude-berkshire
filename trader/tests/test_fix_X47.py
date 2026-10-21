"""X47：預算暫停（runs.status='paused_budget'）的 run 不被排程／run-approved／「執行已同意」自動續跑（D44）；
只能由使用者「續跑」（trader jobs resume）續跑。不連網、不呼叫真模型。"""
from __future__ import annotations

from types import SimpleNamespace

from test_approvals import _env, _run_to_gate
from test_engine import ASOF, RUN
from test_dashboard import H
from test_fix_P48 import _client

from trader import db
from trader.jobs import approvals as A
from trader.jobs.engine import Engine
from trader.pipelines import approved as AP


def _calls(env) -> int:
    return env.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]


def _run_status(env, rid=RUN) -> str:
    return env.conn.execute("SELECT status FROM runs WHERE run_id=?", (rid,)).fetchone()[0]


def _paused_after_approval(env):
    """已同意 KO → run-approved 跑完 FULL_A 預算就用完：run paused_budget、FULL_B pending、同意列仍 approved。"""
    _run_to_gate(env)
    A.approve(env.conn, [1])
    env.config.budget.max_llm_calls_per_run = _calls(env) + 1          # 只夠 FULL_A
    out = AP.run_approved(env.config, env.paths, env.conn, router=env.router, clients={})
    assert out["runs"][RUN]["status"] == "paused_budget" and _run_status(env) == "paused_budget"
    assert ("FULL_B", "pending") in [(r["job_type"], r["status"]) for r in env.rows()]
    assert A.list_items(env.conn, ("approved",))[0]["ticker"] == "KO"


def test_x47_paused_run_not_auto_resumed_by_run_approved(tmp_path):
    env = _env(tmp_path)
    _paused_after_approval(env)
    env.config.budget.max_llm_calls_per_run = 200                      # 額度恢復（隔日／調高設定），沒人按續跑
    assert A.runnable(env.conn) == [] and A.summary(env.conn)["n_runnable"] == 0
    assert A.release(env.conn) == []
    n = _calls(env)
    out = AP.run_approved(env.config, env.paths, env.conn, router=env.router, clients={})
    assert out["runs"] == {} and _calls(env) == n and _run_status(env) == "paused_budget"
    assert A.summary(env.conn)["n_approved_waiting"] == 1               # 仍顯示在待同意頁


def test_x47_scheduler_and_run_endpoint_skip_paused_run(tmp_path):
    env = _env(tmp_path)
    _paused_after_approval(env)
    c, h, calls = _client(env)
    assert c.post("/api/approvals/run", content="{}", headers=h).status_code == 409 and calls["spawn"] == 0
    a = c.get("/api/approvals", headers=H).json()
    assert [r["run_id"] for r in a["paused_runs"]] == [RUN]             # 由「續跑／放棄」按鈕處理
    # 排程：同樣的已同意列，run 預算暫停就不排 approved:<日>，其他狀態照舊排
    from test_fix_P23 import _daily_ok
    from test_scheduler import _sched
    (tmp_path / "s").mkdir()
    env2, s, clock, run = _sched(tmp_path / "s", "2026-10-16T18:00")
    _daily_ok(env2, "2026-10-16")
    db.insert(env2.conn, "runs", {"run_id": "a", "kind": "analyze", "asof": "2026-10-16", "status": "paused_budget"})
    db.insert(env2.conn, "jobs", {"job_id": "a:KO:FULL_B:1", "run_id": "a", "job_type": "FULL_B", "ticker": "KO",
                                  "asof": "2026-10-16", "status": "pending", "created_at": db.now_iso()})
    db.insert(env2.conn, "approvals", {"ticker": "KO", "run_id": "a", "asof": "2026-10-16", "job_id": "a:KO:FULL_A:1",
                                       "job_type": "FULL_A", "status": "approved", "created_at": db.now_iso()})
    env2.conn.commit()
    st = {"start_date": "2026-10-16", "last_screen": "2026-08-20", "last_watch_review": "2026-10-16"}
    for status, planned in (("paused_budget", False), ("running", True)):
        env2.conn.execute("UPDATE runs SET status=? WHERE run_id='a'", (status,))
        env2.conn.commit()
        assert ("approved:2026-10-16" in [k for k, _ in s.plan(env2.conn, st)]) is planned, status


def test_x47_approving_in_paused_run_waits_for_resume(tmp_path, monkeypatch):
    """預算暫停的 run 裡另有檔在等同意：使用者同意後工作改回 pending，但不自動續跑整個 run（其他檔也會花額度）。"""
    env = _env(tmp_path)
    db.insert(env.conn, "runs", {"run_id": "p1", "kind": "analyze", "asof": ASOF, "status": "paused_budget"})
    for t, st in (("KO", "awaiting_approval"), ("PEP", "pending")):
        db.insert(env.conn, "jobs", {"job_id": f"p1:{t}:FULL_A:1", "run_id": "p1", "job_type": "FULL_A", "ticker": t,
                                     "asof": ASOF, "status": st, "created_at": db.now_iso()})
    db.insert(env.conn, "approvals", {"ticker": "KO", "run_id": "p1", "asof": ASOF, "job_id": "p1:KO:FULL_A:1",
                                      "job_type": "FULL_A", "tier": "strong", "status": "pending",
                                      "created_at": db.now_iso()})
    env.conn.commit()
    ran = []
    monkeypatch.setattr(AP, "Engine", lambda *a, **k: SimpleNamespace(ctx=SimpleNamespace(sec=None),
                                                                      run=lambda rid: ran.append(rid) or {}))
    A.approve(env.conn, [1], via="web")
    assert AP.run_approved(env.config, env.paths, env.conn, router=env.router, clients={})["runs"] == {}   # M8-R5-04 另回 new/stale_failed
    assert ran == []
    assert env.conn.execute("SELECT status FROM jobs WHERE job_id='p1:KO:FULL_A:1'").fetchone()[0] == "pending"


def test_x47_jobs_resume_still_continues_paused_run(tmp_path):
    """使用者續跑（cmd_jobs resume 直接 Engine.run，不經 release／runnable）照常跑完並結案同意列。"""
    env = _env(tmp_path)
    _paused_after_approval(env)
    env.config.budget.max_llm_calls_per_run = 200
    n = _calls(env)
    out = Engine(env.conn, env.router, env.paths, env.config).run(RUN)
    assert out["status"] == "done" and _run_status(env) == "done" and _calls(env) > n
    assert A.list_items(env.conn, ("done",))[0]["ticker"] == "KO" and A.runnable(env.conn) == []
