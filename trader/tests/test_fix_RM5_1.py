"""RM5-1：引擎／LLM／daily 群（M5-RS1-02、M5-RS1-01、M5-RS2-02、M5-RS4-01、M5-RS4-02、M5-RS4-03、M5-RS8-01、M5-D1-09）。
全部用假模型／假子行程，不連網、不呼叫真的 claude。"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from test_daily_pipeline import D1, FakeAlpaca, _bars, _clients, _router, world  # noqa: F401 - world 為夾具
from test_engine import ASOF, RUN, _NoNewFilingsSec, dossier_ok, full_responses, make_env, thesis, triage
from trader import db
from trader.jobs import approvals as A
from trader.jobs import engine as E
from trader.jobs.engine import Engine, abandon, retry_failed
from trader.llm import claude_code as CC
from trader.llm.base import TransportError, call_with_retry, write_raw
from trader.llm.fake import FakeRunner
from trader.pipelines import daily as DP


class Killed(BaseException):
    pass


def _fresh(env) -> Engine:
    return Engine(env.conn, env.router, env.paths, env.config, sleep=lambda s: None)


def _run_status(env, run=RUN) -> str:
    return env.conn.execute("SELECT status FROM runs WHERE run_id=?", (run,)).fetchone()[0]


def _open_job_failed(env) -> list[str]:
    return [json.loads(a["detail"])["job_id"] for a in
            env.conn.execute("SELECT detail FROM anomalies WHERE kind='job_failed' AND status='open'")]


# ---------------------------------------------------------------- M5-RS1-02：已有替代工作者 retry 不重設

def _thesis_fail_env(tmp_path):
    """THESIS 斷線（非額度類，不重試）→ on_fail 記疑似並建 DOSSIER 替代工作 → 完整鏈跑完。"""
    env = make_env(tmp_path, {"THESIS": TransportError("連線中斷"), **full_responses()})
    env.engine.ctx.sec = _NoNewFilingsSec()
    env.price_file("KO")
    tid = env.add(env.job("KO", "THESIS", 1, params={"holding": True}))
    env.engine.run(RUN)
    return env, tid


def test_rs1_02_retry_does_not_reset_job_with_replacement(tmp_path):
    env, tid = _thesis_fail_env(tmp_path)
    assert env.by_type("THESIS")[0]["status"] == "failed"
    n_dec = env.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    n_calls = len(env.fake.calls)
    env.fake.responses["THESIS"] = thesis("證偽")
    assert retry_failed(env.conn, RUN) == []                       # 修前：['r1:KO:THESIS:1']
    assert retry_failed(env.conn, RUN, job_id=tid) == []
    assert env.engine.add_job(env.job("KO", "THESIS", 1, params={"holding": True, "x": 1})) is False   # 同 id 不復活
    _fresh(env).run(RUN)
    assert len(env.by_type("DOSSIER")) == 1 and len(env.by_type("FULL_A")) == 1   # 修前：第二條鏈 DOSSIER:2／FULL_A:2
    assert len(env.fake.calls) == n_calls
    assert env.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == n_dec
    row = env.by_type("THESIS")[0]
    assert row["status"] == "failed"
    assert json.loads(row["result"])["replaced_by"] == [env.by_type("DOSSIER")[0]["job_id"]]


def test_rs1_02_control_plain_failure_still_retried(tmp_path):
    """對照：沒有替代工作的正常失敗照舊可 retry、重跑完成。"""
    env = make_env(tmp_path, {"TRIAGE": TransportError("暫時性錯誤")})
    tid = env.add(env.job("KO", "TRIAGE", 1))
    env.engine.run(RUN)
    row = env.by_type("TRIAGE")[0]
    assert row["status"] == "failed" and "replaced_by" not in json.loads(row["result"])
    env.fake.responses["TRIAGE"] = triage(False)
    assert retry_failed(env.conn, RUN) == [tid]
    assert _fresh(env).run(RUN)["status"] == "done"
    assert env.by_type("TRIAGE")[0]["status"] == "done"


# ---------------------------------------------------------------- M5-RS1-01：on_invalid 之後被殺

def test_rs1_01_kill_after_on_invalid_resume_single_thesis(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": [{"ticker": "KO"}, {"ticker": "KO"}], "THESIS": thesis("完好")})
    env.engine.ctx.sec = _NoNewFilingsSec()
    tid = env.add(env.job("KO", "TRIAGE", 1))
    orig = Engine._update
    state = {"armed": True}

    def upd(self, job_id, **f):   # on_invalid（建 THESIS）已提交、標 conservative_fallback 之前被殺
        if state["armed"] and job_id == tid and f.get("status") == E.FALLBACK:
            state["armed"] = False
            raise Killed()
        return orig(self, job_id, **f)

    Engine._update = upd
    try:
        with pytest.raises(Killed):
            env.engine.run(RUN)
    finally:
        Engine._update = orig
    assert len(env.by_type("THESIS")) == 1
    _fresh(env).run(RUN)
    th = env.by_type("THESIS")
    assert len(th) == 1                                    # 修前：THESIS:1、THESIS:2 兩條替代鏈
    assert sum(1 for c in env.fake.calls if c.job_type == "TRIAGE") == 2   # 不重跑 TRIAGE
    tri = env.by_type("TRIAGE")[0]
    assert tri["status"] == "failed" and tri["error"] == E.POST_INTERRUPTED
    assert json.loads(tri["result"])["replaced_by"] == [th[0]["job_id"]]
    assert tid not in retry_failed(env.conn, RUN)          # 已有替代工作：retry 也不重設（RS1-02）


# ---------------------------------------------------------------- M5-RS2-02：使用者拒絕不算失敗

def _awaiting_env(tmp_path):
    env = make_env(tmp_path, full_responses(), approval=["strong"])
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1))
    assert env.engine.run(RUN)["status"] == "awaiting_approval"
    return env


def test_rs2_02_reject_then_run_is_done(tmp_path):
    env = _awaiting_env(tmp_path)
    aid = env.conn.execute("SELECT approval_id FROM approvals WHERE run_id=?", (RUN,)).fetchone()[0]
    A.reject(env.conn, env.paths, [aid], note="不要")
    out = _fresh(env).run(RUN)
    assert env.by_type("FULL_A")[0]["status"] == "skipped"
    assert out["status"] == "done" and _run_status(env) == "done"   # 修前：failed
    assert _open_job_failed(env) == []


def _fake_review(kind_seen, status, error):
    def fake(cfg, paths, conn, date, scope=None, run_id=None, **k):
        row = conn.execute("SELECT kind FROM runs WHERE run_id=?", (run_id,)).fetchone()
        kind_seen.append(row["kind"] if row is not None else None)
        if row is None:
            db.insert(conn, "runs", {"run_id": run_id, "kind": "review", "asof": date, "status": "running"})
        db.insert(conn, "jobs", {"job_id": f"{run_id}:AAA:FULL_A:1", "run_id": run_id, "job_type": "FULL_A",
                                 "ticker": "AAA", "asof": date, "status": status, "error": error,
                                 "created_at": db.now_iso()})
        conn.commit()
        eng = {"status": "done" if status == "skipped" else "failed", "failed": int(status == "failed"),
               "skipped": int(status == "skipped"), "done": 0}
        return {"run_id": run_id, "counts": {}, "skipped": [], "engine": eng}
    return fake


def _daily(world, monkeypatch, status, error):  # noqa: F811
    cfg, paths, conn = world
    seen: list = []
    monkeypatch.setattr(DP, "run_review", _fake_review(seen, status, error))
    s = DP.run_daily(cfg, paths, conn, D1, clients=_clients(FakeAlpaca(_bars())), skip_review=False,
                     router=_router(cfg, paths))
    return s, seen


def test_rs2_02_daily_user_rejected_skip_not_failed_ticker(world, monkeypatch):  # noqa: F811
    s, _ = _daily(world, monkeypatch, "skipped", "使用者拒絕以強模型分析：不要")
    assert s["review"]["ok"] is True
    assert s["review"]["failed_tickers"] == []                       # 修前：['AAA']


def test_rs2_02_daily_control_real_failure_counted(world, monkeypatch):  # noqa: F811
    s, _ = _daily(world, monkeypatch, "failed", "TransportError: x")
    assert s["review"]["failed_tickers"] == ["AAA"]


# ---------------------------------------------------------------- M5-RS4-01：abandon 關 failed

def test_rs4_01_abandon_closes_failed_and_rerun_stays_abandoned(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": TransportError("暫時性錯誤"), "CORP_ACTION": TransportError("暫時性錯誤")})
    tid = env.add(env.job("KO", "TRIAGE", 1))
    cid = env.add(env.job("KO", "CORP_ACTION", 1))
    assert env.engine.run(RUN)["status"] == "failed"
    assert _open_job_failed(env) == [tid]
    ids = abandon(env.conn, RUN)
    assert ids == [tid]                                             # 修前：[]（failed 不處理）
    st = {r["job_id"]: (r["status"], r["error"] or "") for r in env.rows()}
    assert st[tid][0] == "skipped" and st[tid][1].startswith("使用者放棄")
    assert st[cid][0] == "failed"                                   # CORP_ACTION 由 daily 重派，不放棄
    assert _open_job_failed(env) == [] and _run_status(env) == "abandoned"
    n = len(env.fake.calls)
    env.fake.responses["TRIAGE"] = triage(False)
    out = _fresh(env).run(RUN)                                      # 同 run 再跑一次
    assert len(env.fake.calls) == n                                 # 修前：重跑失敗工作
    assert out["status"] == "abandoned" and _run_status(env) == "abandoned"
    assert _open_job_failed(env) == []
    assert retry_failed(env.conn, RUN) == []


def test_rs4_01_scheduler_clears_reanalyze_of_abandoned_analyze(tmp_path):
    from test_fix_P23 import _crash_then_analyze
    from test_scheduler import _sched
    env, s, clock, run = _sched(tmp_path, "2026-11-20T18:00")
    db.insert(env.conn, "runs", {"run_id": "analyze-2026-11-20", "kind": "analyze", "asof": "2026-11-20",
                                 "status": "abandoned"})
    env.conn.commit()
    _crash_then_analyze(run)
    s.once()
    clock.set("2026-11-20T19:00")
    s.once()
    assert "analyze (fail)" not in run.calls                        # 修前：自動續跑使用者已放棄的分析
    assert "reanalyze" not in s.load_state()


# ---------------------------------------------------------------- M5-RS4-02：中斷喚醒退避、取消後不 Popen

class _QuotaOnce:
    resumable = False

    def __init__(self):
        self.calls = 0

    def run(self, job):
        self.calls += 1
        raise TransportError("rate limit", retry_after=5.0)


def test_rs4_02_stop_wakes_backoff_and_no_resend():
    runner, stop, failed = _QuotaOnce(), threading.Event(), []
    threading.Timer(0.2, stop.set).start()
    t0 = time.monotonic()
    with pytest.raises(TransportError):
        call_with_retry(runner, object(), retries=3, sleep=time.sleep, failed=failed, stop=stop)
    assert time.monotonic() - t0 < 3.0                              # 修前：睡滿 5 秒
    assert runner.calls == 1                                        # 修前：醒來又送一次
    assert failed == []                                             # 沒送出的重試不記帳


def _no_popen(monkeypatch, spawned):
    def popen(*a, **k):
        spawned.append(a)
        raise AssertionError("不得啟動")
    monkeypatch.setattr(CC.subprocess, "Popen", popen)


def test_rs4_02_cancelled_run_in_group_does_not_popen(monkeypatch):
    spawned: list = []
    _no_popen(monkeypatch, spawned)
    ev = threading.Event()
    CC.cancel_running(ev)
    assert ev.is_set()
    with pytest.raises(TransportError):
        CC.run_in_group(["true"], input="", timeout=1.0, text=True, cancelled=ev)
    assert spawned == []


def test_rs4_02_claude_runner_cancel_blocks_new_process(tmp_path, monkeypatch):
    from trader.models import JobSpec, TierConfig
    spawned: list = []
    _no_popen(monkeypatch, spawned)
    r = CC.ClaudeCodeRunner(TierConfig(name="weak", backend="claude_code", model="m"), denies=[])
    r.cancel()
    sb = tmp_path / "sb"
    sb.mkdir()
    job = JobSpec(job_id="j", run_id="r", job_type="TRIAGE", ticker="KO", asof=ASOF, sandbox=sb, prompt_text="p")
    with pytest.raises(TransportError):
        r.run(job)
    assert spawned == []


# ---------------------------------------------------------------- M5-RS4-03：每日上限依 run 種類豁免

@pytest.mark.parametrize("kind, exempt", [("corp_action", True), ("daily_review", True),
                                          ("review", False), ("daily", False)])
def test_rs4_03_daily_cap_exempts_by_kind(tmp_path, kind, exempt):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    env.config.budget.max_llm_calls_per_day = 1
    env.conn.execute("INSERT INTO llm_calls(job_id, run_id, tier, ok, created_at) VALUES('x', 'other', 'weak', 1, ?)",
                     (db.now_iso(),))
    env.conn.commit()
    env.engine.start_run("k1", kind, ASOF)
    env.add(env.job("KO", "TRIAGE", 1, run="k1"))
    out = env.engine.run("k1")
    assert out["status"] == ("done" if exempt else "paused_budget")
    an = env.conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind=?", (E.BUDGET_DAY_KIND,)).fetchone()[0]
    assert an == (0 if exempt else 1)


def test_rs4_03_daily_creates_daily_review_run(world, monkeypatch):  # noqa: F811
    s, seen = _daily(world, monkeypatch, "skipped", "使用者拒絕以強模型分析")
    assert seen == ["daily_review"]                                 # 修前：None（由 run_review 建 kind=review）
    assert s["review"]["run_id"] == f"review-{D1}-holdings"


# ---------------------------------------------------------------- M5-RS8-01：驗證重試等候中被中斷

class _ResumableInvalidThenBlock(FakeRunner):
    """可續跑後端：attempt 0 回不合格；第一次 attempt 1 寫提示後對主執行緒送 SIGTERM、阻塞到 release；之後回合格。"""
    resumable = True

    def __init__(self):
        super().__init__(responses={})
        self.release = threading.Event()
        self.attempts: list[int] = []
        self.prompts: list[str] = []
        self.blocked = False
        self.seen: dict = {}

    def cancel(self):
        pass

    def run(self, job):
        self.attempts.append(job.attempt)
        self.prompts.append(job.prompt_text)
        if job.attempt == 0:
            self.responses["TRIAGE"] = {"ticker": "KO"}
            return super().run(job)
        if not self.blocked:
            self.blocked = True
            write_raw(job.log_dir, job, "prompt.txt", job.prompt_text)
            signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
            self.release.wait(10)
            raise TransportError("等候被中止")
        sb = Path(job.sandbox)
        self.seen["marker"] = (sb / "subagent_was_here.txt").exists()
        self.seen["inode"] = os.stat(sb).st_ino
        self.responses["TRIAGE"] = triage(False)
        return super().run(job)


def test_rs8_01_resume_keeps_validation_attempt(tmp_path):
    env = make_env(tmp_path, {})
    runner = _ResumableInvalidThenBlock()
    env.router._fake = runner
    env.fake = runner
    tid = env.add(env.job("KO", "TRIAGE", 1))
    preps = {"n": 0}
    h = env.engine.handlers["TRIAGE"]
    orig = h.prepare

    def prep(job, ctx):
        preps["n"] += 1
        return orig(job, ctx)
    h.prepare = prep
    try:
        with pytest.raises(KeyboardInterrupt):
            env.engine.run(RUN)
        row = env.by_type("TRIAGE")[0]
        assert row["status"] == "running"
        res_attempt = json.loads(row["result"] or "{}").get("attempt")
        sb = Path(row["sandbox"])
        (sb / "subagent_was_here.txt").write_text("子代理正在讀這個沙箱", encoding="utf-8")
        ino = os.stat(sb).st_ino
        n_prep = preps["n"]
        runner.release.set()
        time.sleep(0.3)
        out = _fresh(env).run(RUN)
    finally:
        h.prepare = orig
    assert out["status"] == "done" and env.by_type("TRIAGE")[0]["status"] == "done"
    assert runner.attempts == [0, 1, 1]                             # 修前：[0, 1, 0, 1]（從 attempt 0 重播）
    assert runner.prompts[2] == runner.prompts[1]                   # 續跑送同一份 attempt 1 提示
    assert "未通過格式檢查" in runner.prompts[2]
    assert preps["n"] == n_prep                                     # 續跑不呼叫 prepare
    assert runner.seen["marker"] is True and runner.seen["inode"] == ino   # 沙箱未重建、標記檔留存
    calls = env.conn.execute("SELECT attempt FROM llm_calls WHERE job_id=? ORDER BY rowid", (tid,)).fetchall()
    assert [c["attempt"] for c in calls] == [0, 1]                  # llm_calls 2 列
    assert env.by_type("TRIAGE")[0]["attempts"] == 2
    assert res_attempt == 1                                         # 驗證重試送出時記下嘗試序號


# ---------------------------------------------------------------- M5-D1-09：傳輸重試前還原 dossier

class _DossierQuotaOnce(FakeRunner):
    def __init__(self):
        super().__init__(responses={"DOSSIER": dossier_ok()})
        self.n = 0
        self.seen: list[str] = []

    def run(self, job):
        if job.job_type != "DOSSIER":
            return super().run(job)
        self.n += 1
        md = Path(job.sandbox) / "dossier" / "KO.md"
        self.seen.append(md.read_text(encoding="utf-8"))
        if self.n == 1:
            with md.open("a", encoding="utf-8") as f:
                f.write("\n## 半截追加（逾時被殺）\n")
            raise TransportError("rate limit")
        return super().run(job)


def test_d1_09_transport_retry_restores_sandbox_dossier(tmp_path):
    env = make_env(tmp_path, {})
    runner = _DossierQuotaOnce()
    env.router._fake = runner
    env.fake = runner
    env.price_file("KO")
    original = (env.paths.work / RUN / "KO" / "dossier" / "KO.md").read_text(encoding="utf-8")
    env.add(env.job("KO", "DOSSIER", 1))
    env.engine.run(RUN)
    assert runner.n == 2
    assert runner.seen[0] == original
    assert "半截追加" not in runner.seen[1] and runner.seen[1] == original   # 修前：第二次看到半截檔
    assert env.by_type("DOSSIER")[0]["status"] == "done"
