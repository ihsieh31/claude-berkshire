"""X49：D44 收尾。(1) bootstrap 中斷改排 `analyze --asof D` 前，analyze-D 預算暫停就不排（analyze 會接著跑暫停中的工作）；
(2) 在預算暫停的 run 裡按「同意」：回應提示需按續跑，不回 started:true。不連網、不呼叫真模型。"""
from __future__ import annotations

import json

from test_approvals import _env
from test_engine import ASOF
from test_fix_P23 import _crash_then_analyze
from test_fix_P48 import _client
from test_scheduler import _sched

from trader import db


def _paused_analyze(env, asof):
    db.insert(env.conn, "runs", {"run_id": f"analyze-{asof}", "kind": "analyze", "asof": asof,
                                 "status": "paused_budget", "summary": json.dumps({"pending": 3})})
    env.conn.commit()


def test_x49_bootstrap_crash_with_paused_analyze_not_resumed(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-11-20T18:00")
    logs = []
    s.log = logs.append
    _paused_analyze(env, "2026-11-20")              # 分析在 bootstrap 中預算暫停，之後子行程 exit≠0
    _crash_then_analyze(run)
    s.once()
    assert "analyze (fail)" not in run.calls        # 不以 analyze 自動續跑暫停中的 run
    st = s.load_state()
    assert "reanalyze" not in st and st["last_screen"] == "2026-11-20" and not st.get("failures")
    assert sum("預算暫停（D44）" in m for m in logs) == 1
    clock.set("2026-11-20T19:00")                   # 之後的 tick 也不排、不重記
    s.once()
    assert "analyze (fail)" not in run.calls and sum("預算暫停（D44）" in m for m in logs) == 1


def test_x49_reanalyze_still_runs_when_analyze_not_paused(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-11-20T18:00")
    db.insert(env.conn, "runs", {"run_id": "analyze-2026-11-20", "kind": "analyze", "asof": "2026-11-20",
                                 "status": "running"})   # 被殺時殘留 running：照舊續跑
    env.conn.commit()
    _crash_then_analyze(run)
    s.once()
    assert "analyze (fail)" in run.calls


def _approval(env, run_id, run_status, ticker):
    db.insert(env.conn, "runs", {"run_id": run_id, "kind": "analyze", "asof": ASOF, "status": run_status})
    db.insert(env.conn, "jobs", {"job_id": f"{run_id}:{ticker}:FULL_A:1", "run_id": run_id, "job_type": "FULL_A",
                                 "ticker": ticker, "asof": ASOF, "status": "awaiting_approval",
                                 "created_at": db.now_iso()})
    db.insert(env.conn, "approvals", {"ticker": ticker, "run_id": run_id, "asof": ASOF,
                                      "job_id": f"{run_id}:{ticker}:FULL_A:1", "job_type": "FULL_A",
                                      "tier": "strong", "status": "pending", "created_at": db.now_iso()})
    env.conn.commit()
    return env.conn.execute("SELECT MAX(approval_id) FROM approvals").fetchone()[0]


def test_x49_approve_in_paused_run_says_resume_needed(tmp_path):
    env = _env(tmp_path)
    i = _approval(env, "p1", "paused_budget", "KO")
    c, h, calls = _client(env)
    r = c.post("/api/approvals/approve", content=json.dumps({"ids": [i], "via": "web"}), headers=h).json()
    assert r["approved"] == [i] and r["started"] is False and calls["spawn"] == 0
    assert "p1 預算暫停" in r["note"] and "續跑" in r["note"]


def test_x49_approve_mixed_spawns_and_notes_paused(tmp_path):
    env = _env(tmp_path)
    i = _approval(env, "p1", "paused_budget", "KO")
    j = _approval(env, "r2", "awaiting_approval", "PEP")
    c, h, calls = _client(env)
    r = c.post("/api/approvals/approve", content=json.dumps({"ids": [i, j], "via": "web"}), headers=h).json()
    assert r["started"] is True and calls["spawn"] == 1 and "p1 預算暫停" in r["note"]
    # 一般 run：回應不變
    k = _approval(env, "r3", "awaiting_approval", "MO")
    r = c.post("/api/approvals/approve", content=json.dumps({"ids": [k], "via": "web"}), headers=h).json()
    assert r == {"approved": [k], "started": True, "note": None}
