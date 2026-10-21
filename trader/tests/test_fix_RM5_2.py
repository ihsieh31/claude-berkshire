"""RM5-2（M5-RS2-01）：舊 run 已同意列的鏈已停（被閘工作 failed／skipped，或沒有未完成工作）時，
新觸發要作廢它；否則新 run 跑完後舊 approved 列永遠擋加碼／建倉批次。"""
from __future__ import annotations

from trader.jobs import approvals as A
from trader.portfolio.sizing import add_blocked

from test_engine import ASOF, RUN, full_responses, make_env
from test_hold_review import _holding


def _approval_env(tmp_path):
    env = make_env(tmp_path, full_responses(), approval=["strong"])
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1))
    env.engine.run(RUN)
    return env


def _new_trigger(env):
    env.engine.start_run("r2", "daily", ASOF)
    env.price_file("KO", run="r2")
    env.add(env.job("KO", "DOSSIER", 1, run="r2"))
    env.engine.run("r2")


def _status(env):
    return {(r["run_id"], r["status"]) for r in env.conn.execute("SELECT run_id, status FROM approvals")}


def _fail_gated_after_approval(env, status="failed", close=True):
    A.approve(env.conn, [1])
    A.release(env.conn)
    fa = env.by_type("FULL_A")[0]["job_id"]
    env.conn.execute("UPDATE jobs SET status=?, error='TransportError' WHERE job_id=?", (status, fa))
    env.conn.commit()
    if close:
        A.close_finished(env.conn, RUN)      # 有 failed → 留 approved＋FAILED_NOTE
    return fa


def test_old_approved_row_with_failed_gated_job_is_superseded_and_unblocks_add(tmp_path):
    env = _approval_env(tmp_path)
    _holding(env)
    fa = _fail_gated_after_approval(env)
    _new_trigger(env)
    assert _status(env) == {(RUN, "superseded"), ("r2", "pending")}
    note = env.conn.execute("SELECT note FROM approvals WHERE run_id=?", (RUN,)).fetchone()[0]
    assert "r2" in note
    # 舊 failed 工作不動（job_failed 異常照舊，由 retry／abandon 處理）
    assert env.conn.execute("SELECT status FROM jobs WHERE job_id=?", (fa,)).fetchone()[0] == "failed"
    new_id = env.conn.execute("SELECT approval_id FROM approvals WHERE run_id='r2'").fetchone()[0]
    A.approve(env.conn, [new_id])
    A.release(env.conn)
    env.engine.run("r2")
    assert ("r2", "done") in _status(env)
    assert A.add_block_reason(env.conn, "KO") is None


def test_old_approved_row_with_skipped_gated_job_is_superseded(tmp_path):
    """被閘工作 skipped、舊 run 尚未 close_finished（例如中斷）→ 舊 approved 列同樣作廢。"""
    env = _approval_env(tmp_path)
    _fail_gated_after_approval(env, status="skipped", close=False)
    _new_trigger(env)
    assert _status(env) == {(RUN, "superseded"), ("r2", "pending")}


def test_retry_old_failed_job_after_supersede_does_not_run_strong_model(tmp_path):
    """作廢後使用者 retry 舊 run：被閘 FULL_A 經 gate 改 skipped（同意已作廢），不再呼叫強模型。"""
    from trader.jobs.engine import retry_failed
    env = _approval_env(tmp_path)
    fa = _fail_gated_after_approval(env)
    _new_trigger(env)
    retry_failed(env.conn, RUN)
    env.engine.run(RUN)
    row = env.conn.execute("SELECT status, error FROM jobs WHERE job_id=?", (fa,)).fetchone()
    assert row["status"] == "skipped" and row["error"].startswith("同意已作廢")


def test_old_chain_still_running_is_kept(tmp_path):
    """對照：舊鏈有 running 工作 → 不作廢（P30R E2 語義不變）。"""
    env = _approval_env(tmp_path)
    A.approve(env.conn, [1])
    A.release(env.conn)
    fa = env.by_type("FULL_A")[0]["job_id"]
    env.conn.execute("UPDATE jobs SET status='running' WHERE job_id=?", (fa,))
    env.conn.commit()
    _new_trigger(env)
    assert _status(env) == {(RUN, "approved"), ("r2", "pending")}
    assert add_blocked(env.conn, "KO", ASOF) is not None
