"""強模型同意閘門（M6a）：引擎等待、同意後續跑（含 RED_TEAM 免再同意）、延後、拒絕、作廢、暫停加碼、估算、CLI。"""
from __future__ import annotations

import json

import pytest

from test_engine import ASOF, RUN, dossier_ok, full_responses, make_env

from trader import cli, db
from trader.jobs import approvals as A
from trader.pipelines.approved import RunApprovedBusy, run_approved
from trader.portfolio import sizing as S


def _env(tmp_path, responses=None):
    env = make_env(tmp_path, responses or full_responses(), approval=["strong"])
    env.price_file("KO")
    return env


def _run_to_gate(env, ticker="KO", run=RUN, params=None):
    env.add(env.job(ticker, "DOSSIER", 1, params=params, run=run))
    return env.engine.run(run)


def _llm_calls(env, job_type):
    return env.conn.execute("SELECT COUNT(*) FROM llm_calls l JOIN jobs j ON j.job_id=l.job_id WHERE j.job_type=?",
                            (job_type,)).fetchone()[0]


def test_strong_job_waits_without_calling_model(tmp_path):
    env = _env(tmp_path)
    result = _run_to_gate(env)

    assert result["status"] == "awaiting_approval"
    assert result["awaiting_approval"] == 1 and result["done"] == 1
    assert [(r["job_type"], r["status"]) for r in env.rows()] == [("DOSSIER", "done"), ("FULL_A", "awaiting_approval")]
    assert _llm_calls(env, "FULL_A") == 0
    items = A.list_items(env.conn)
    assert len(items) == 1 and items[0]["ticker"] == "KO" and items[0]["status"] == "pending"
    assert items[0]["job_type"] == "FULL_A" and items[0]["tier"] == "strong"
    assert env.conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN,)).fetchone()[0] == "awaiting_approval"
    # 重跑 run 不重複建列、不呼叫模型
    assert env.engine.run(RUN)["status"] == "awaiting_approval"
    assert len(A.list_items(env.conn)) == 1


def test_approve_then_run_includes_red_team_without_second_approval(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env)
    assert A.approve(env.conn) == [1]

    out = run_approved(env.config, env.paths, env.conn, router=env.router, clients={})

    assert out["runs"][RUN]["status"] == "done"
    assert [(r["job_type"], r["status"]) for r in env.rows()] == [
        ("DOSSIER", "done"), ("FULL_A", "done"), ("FULL_B", "done"), ("RED_TEAM", "done")]
    assert A.list_items(env.conn, ("done",))[0]["ticker"] == "KO"
    assert A.add_block_reason(env.conn, "KO") is None
    # 沒有已同意項目時什麼都不做
    assert run_approved(env.config, env.paths, env.conn, router=env.router, clients={})["runs"] == {}


def test_deferred_not_in_approve_all_and_still_blocks_add(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env)
    A.defer(env.conn, [1])
    assert A.approve(env.conn) == []           # --all 不含延後
    assert A.summary(env.conn)["n_pending"] == 0 and A.summary(env.conn)["n_deferred"] == 1
    assert "延後" in S.add_blocked(env.conn, "KO", ASOF)
    assert A.approve(env.conn, [1]) == [1]     # 可逐檔同意延後項
    assert "已同意" in S.add_blocked(env.conn, "KO", ASOF)
    with pytest.raises(A.ApprovalError):
        A.defer(env.conn, [1])


def test_reject_writes_missing_data_and_skips_job(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env)
    dids = A.reject(env.conn, env.paths, [1], note="額度不足")

    assert env.by_type("FULL_A")[0]["status"] == "skipped"
    dec = env.conn.execute("SELECT decision_id, signal, reason_code FROM decisions").fetchall()
    assert [(r["decision_id"], r["signal"], r["reason_code"]) for r in dec] == [(dids[0], "NO_ACTION", "MISSING_DATA")]
    assert A.add_block_reason(env.conn, "KO") is None
    assert run_approved(env.config, env.paths, env.conn, router=env.router, clients={})["runs"] == {}
    with pytest.raises(A.ApprovalError):
        A.approve(env.conn, [1])


def test_new_trigger_supersedes_old_undecided(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env)
    env.engine.start_run("r2", "review", ASOF)
    env.price_file("KO", run="r2")
    _run_to_gate(env, run="r2")

    rows = {r["run_id"]: r["status"] for r in A.list_items(env.conn, ("pending", "superseded"))}
    assert rows == {RUN: "superseded", "r2": "pending"}
    assert env.by_type("FULL_A")[0]["status"] == "skipped"
    assert env.by_type("FULL_A", run="r2")[0]["status"] == "awaiting_approval"


def test_holding_reason_and_tier_config(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env, params={"holding": True, "reasons": ["CEO 退休"]})
    it = A.list_items(env.conn)[0]
    assert it["holding"] == 1 and it["reason"].startswith("持股複查；CEO 退休")
    assert A.required(env.config, "strong") and not A.required(env.config, "medium")


def test_no_gate_when_tiers_empty(tmp_path):
    env = make_env(tmp_path, full_responses())   # 預設 []
    env.price_file("KO")
    assert _run_to_gate(env)["status"] == "done"
    assert A.list_items(env.conn) == []


def test_estimate_uses_actual_averages(tmp_path):
    env = _env(tmp_path)
    s = A.summary(env.conn)
    assert s["per_ticker"]["basis"] == "預設值" and s["n_pending"] == 0
    _run_to_gate(env)
    s = A.summary(env.conn)
    assert s["n_pending"] == 1 and s["est_input_tokens"] == 900_000
    assert A.label(s) == "1 檔 · 約 0.9M token · 約 6 分鐘"
    env.conn.execute("INSERT INTO jobs(job_id, run_id, job_type, ticker, asof, tier, status, input_tokens, "
                     "output_tokens, latency_s, created_at) VALUES ('x','x','FULL_A','Z',?, 'strong','done',"
                     "1200000, 40000, 600, 'now')", (ASOF,))
    s = A.summary(env.conn)
    assert s["per_ticker"]["basis"] == "實際平均" and s["est_input_tokens"] == 1_200_000 and s["est_minutes"] == 10.0


def test_run_approved_lock(tmp_path):
    import fcntl
    env = _env(tmp_path)
    with open(env.paths.state / ".run_approved.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RunApprovedBusy):
            run_approved(env.config, env.paths, env.conn, router=env.router, clients={})


def test_late_decision_picked_up_by_next_daily(tmp_path):
    env = _env(tmp_path)
    db.insert(env.conn, "runs", {"run_id": "daily-2026-10-20", "kind": "daily_close", "asof": "2026-10-20",
                                 "status": "done", "finished_at": "2026-10-20T21:40:00Z"})
    db.insert(env.conn, "decisions", {"decision_id": "KO-20261016-1", "ticker": "KO", "decision_date": ASOF,
                                      "signal": "BUY", "data": json.dumps({"action": "BUY"}),
                                      "created_at": "2026-10-21T15:00:00Z"})
    assert S.load_decisions(env.conn, "2026-10-21", since="2026-10-20") == []
    got = S.load_decisions(env.conn, "2026-10-21", since="2026-10-20", late_after="2026-10-20T21:40:00Z")
    assert [d["decision_id"] for d in got] == ["KO-20261016-1"]
    assert S.load_decisions(env.conn, "2026-10-22", since="2026-10-21", late_after="2026-10-21T21:40:00Z") == []


def test_cli_list_approve_reject(tmp_path, capsys):
    # 輸出 ticker 須與工作相符（M3_D1-10）：PEP 的 DOSSIER 回應另給
    env = _env(tmp_path, {**full_responses(), ("DOSSIER", "PEP"): {**dossier_ok(), "ticker": "PEP"}})
    env.price_file("PEP")
    _run_to_gate(env)
    _run_to_gate(env, ticker="PEP")
    st = ["--state-dir", str(env.paths.state)]
    assert cli.main(["approvals", "list", *st]) == 0
    out = capsys.readouterr().out
    assert "#1" in out and "KO" in out and "PEP" in out and "2 檔" in out
    assert cli.main(["approvals", "approve", *st]) == 2            # 沒給編號也沒 --all
    assert cli.main(["approvals", "reject", "2", "--note", "不要", *st]) == 0
    assert cli.main(["approvals", "approve", "--all", *st]) == 0
    assert "已同意：[1]" in capsys.readouterr().out
    assert cli.main(["approvals", "approve", "2", *st]) == 1      # 已拒絕
    assert cli.main(["approvals", "list", "--all", *st]) == 0
    out = capsys.readouterr().out
    assert "rejected" in out and "approved" in out
