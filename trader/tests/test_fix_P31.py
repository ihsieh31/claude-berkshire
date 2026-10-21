"""P31：引擎中斷、續跑、預算與原始紀錄（M5-D1-02、M5_D9-02、M5_D9-03、M5_D4-02、M5-D1-07、M5_D4-04、
M3_D9-01、M3_D1-10）＋設計決策 D47。全部用假模型／假子行程，不呼叫真的 claude。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import test_engine as TE
from test_engine import ASOF, RUN, full_a, full_b, full_responses, make_env, red_team, triage
from trader import db
from trader.jobs import engine as E
from trader.jobs import validate
from trader.jobs.engine import Engine
from trader.llm import claude_code as CC
from trader.llm.base import LLMTimeout, TransportError, call_with_retry, latest_raw, write_raw
from trader.llm.fake import FakeRunner
from trader.models import JobSpec, TierConfig


class Killed(BaseException):
    pass


def _kill_after_post(env, job_suffix="FULL_B:1"):
    """模擬 SIGTERM 落在 post（回寫＋add_job 已提交）之後、標 done 之前。"""
    orig = Engine._update
    state = {"armed": True}

    def upd(self, job_id, **f):
        if state["armed"] and job_id.endswith(job_suffix) and f.get("status") == "done":
            state["armed"] = False
            raise Killed()
        return orig(self, job_id, **f)

    Engine._update = upd
    try:
        with pytest.raises(Killed):
            env.engine.run(RUN)
    finally:
        Engine._update = orig


def _decisions(env, t="KO"):
    return env.conn.execute("SELECT decision_id, signal FROM decisions WHERE ticker=?", (t,)).fetchall()


# ---------------------------------------------------------------- M5-D1-02

def test_kill_after_post_watch_resume_does_not_duplicate_decision(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": full_b("WATCH"), "RED_TEAM": red_team(False)})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    _kill_after_post(env)
    assert len(_decisions(env)) == 1
    out = Engine(env.conn, env.router, env.paths, env.config, sleep=lambda s: None).run(RUN)   # 下一輪續跑
    assert len(_decisions(env)) == 1           # 修前：post 重跑 → 2 筆
    fb = env.by_type("FULL_B")[0]
    assert fb["status"] == "failed" and fb["error"] == E.POST_INTERRUPTED
    assert out["status"] == "failed"
    an = env.conn.execute("SELECT detail FROM anomalies WHERE kind='job_failed'").fetchall()
    assert [json.loads(a["detail"])["job_id"] for a in an] == [fb["job_id"]]
    assert sum(1 for c in env.fake.calls if c.job_type == "FULL_B") == 1


def test_kill_after_post_buy_resume_single_red_team(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": full_b("BUY"), "RED_TEAM": red_team(False)})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    _kill_after_post(env)
    Engine(env.conn, env.router, env.paths, env.config, sleep=lambda s: None).run(RUN)
    reds = env.by_type("RED_TEAM")
    assert len(reds) == 1                       # 修前：2 個 RED_TEAM
    assert reds[0]["status"] == "skipped"       # FULL_B 需人工確認 → 不放行 BUY
    assert all(d["signal"] != "BUY" for d in _decisions(env))


def test_next_job_reuses_child_id_when_post_reruns(tmp_path):
    """jobs retry 重跑 post：同一父工作再產生同類型後續工作時沿用 job_id，add_job 去重。"""
    from trader.jobs.handlers import next_job
    env = make_env(tmp_path, {})
    parent = env.job("KO", "FULL_B", 1)
    env.add(parent)
    first = next_job(parent, "RED_TEAM", env.engine.ctx)
    env.add(first)
    again = next_job(parent, "RED_TEAM", env.engine.ctx)
    assert again.job_id == first.job_id
    other = next_job(env.job("KO", "FULL_B", 2), "RED_TEAM", env.engine.ctx)
    assert other.job_id != first.job_id


# ---------------------------------------------------------------- M5_D9-02／M5_D9-03／M5_D4-02

class _InterruptOnce(FakeRunner):
    """第一次 FULL_A 呼叫：寫原始提示後對本行程送 SIGTERM，阻塞到 release；之後照 FakeRunner 回應。"""

    def __init__(self, responses, resumable: bool):
        super().__init__(responses=responses)
        self.resumable = resumable
        self.release = threading.Event()
        self.first = True
        self.seen: dict = {}

    def run(self, job):
        if job.job_type == "FULL_A" and self.first:
            self.first = False
            self.calls.append(job)
            write_raw(job.log_dir, job, "prompt.txt", job.prompt_text)
            self.seen["prompt1"] = job.prompt_text
            # 直接送給主執行緒（gVisor 下 os.kill 可能落在送出者執行緒）；送出後才開始計時
            signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
            if getattr(self, "auto", False):
                threading.Timer(0.5, self.release.set).start()
            self.release.wait(10)
            raise TransportError("rate limit（被中止）")
        if job.job_type == "FULL_A":
            sb = Path(job.sandbox)
            self.seen["marker"] = (sb / "subagent_was_here.txt").exists()
            self.seen["inode"] = os.stat(sb).st_ino
            self.seen["prompt2"] = job.prompt_text
        return super().run(job)


def _interrupt_env(tmp_path, resumable):
    env = make_env(tmp_path, {})
    runner = _InterruptOnce(full_responses(), resumable)
    env.router._fake = runner
    env.fake = runner
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    return env, runner


def test_sigterm_resumable_runner_keeps_sandbox_and_prompt(tmp_path):
    env, runner = _interrupt_env(tmp_path, resumable=True)
    calls = {"pre": 0}
    h = env.engine.handlers["FULL_A"]
    orig_pre = h.pre_script

    def pre(job, ctx):
        calls["pre"] += 1
        return orig_pre(job, ctx)
    h.pre_script = pre
    try:
        with pytest.raises(KeyboardInterrupt):     # SIGTERM → KeyboardInterrupt
            env.engine.run(RUN)
        assert signal.getsignal(signal.SIGTERM) is not None and calls["pre"] == 1
        row = env.by_type("FULL_A")[0]
        assert row["status"] == "running"           # 可續跑後端：保持 running，等下次接上
        sb = Path(row["sandbox"])
        (sb / "subagent_was_here.txt").write_text("子代理正在讀這個沙箱", encoding="utf-8")
        ino = os.stat(sb).st_ino
        runner.release.set()
        time.sleep(0.3)
        Engine(env.conn, env.router, env.paths, env.config, sleep=lambda s: None).run(RUN)
    finally:
        h.pre_script = orig_pre
    assert runner.seen["marker"] is True and runner.seen["inode"] == ino   # 修前：沙箱被 rmtree 重建
    assert runner.seen["prompt2"] == runner.seen["prompt1"]
    assert calls["pre"] == 1                                               # 前置腳本沒有重跑
    assert env.by_type("FULL_A")[0]["status"] == "done"


def test_sigterm_non_resumable_records_call_no_retry_and_pending(tmp_path):
    env, runner = _interrupt_env(tmp_path, resumable=False)
    runner.auto = True     # release 計時在送出 SIGTERM 之後才開始
    with pytest.raises(KeyboardInterrupt):
        env.engine.run(RUN)
    row = env.by_type("FULL_A")[0]
    assert row["status"] == "pending" and row["error"].startswith("中止")
    assert len([c for c in runner.calls if c.job_type == "FULL_A"]) == 1     # stop 後不再重試（額度類錯誤原本會重試）
    calls = env.conn.execute("SELECT ok, error FROM llm_calls").fetchall()
    assert [(c["ok"], "rate limit" in c["error"]) for c in calls] == [(0, True)]   # 已完成的呼叫有記帳
    assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.default_int_handler) or callable(
        signal.getsignal(signal.SIGTERM))
    # 續跑：重建沙箱、重新執行
    out = Engine(env.conn, env.router, env.paths, env.config, sleep=lambda s: None).run(RUN)
    assert env.by_type("FULL_A")[0]["status"] == "done" and out["status"] in ("done", "failed")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        st = Path(f"/proc/{pid}/status").read_text()
        return "\nState:\tZ" not in st
    except OSError:
        return True


def test_run_in_group_timeout_kills_grandchild(tmp_path):
    pidf = tmp_path / "gc.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        CC.run_in_group(["sh", "-c", f"sleep 30 & echo $! > {pidf}; wait"], input="", timeout=1.0, text=True)
    time.sleep(0.3)
    assert not _alive(int(pidf.read_text()))      # 修前（subprocess.run）：孫行程存活


def test_scheduler_timeout_kills_process_group(tmp_path, monkeypatch):
    from trader import scheduler as S
    from trader.paths import get_paths
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    pidf = tmp_path / "gc.pid"
    monkeypatch.setattr(S, "TASK_TIMEOUT_S", 1.5)
    code = f"import subprocess;p=subprocess.Popen(['sleep','30']);open({str(pidf)!r},'w').write(str(p.pid));p.wait()"
    rc = S.subprocess_runner(paths)([sys.executable, "-B", "-c", code])
    assert rc == 124
    time.sleep(0.3)
    assert not _alive(int(pidf.read_text()))


# ---------------------------------------------------------------- M5-D1-07／D47

def test_quota_retries_each_recorded_with_usage(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text("{}", encoding="utf-8")
    n = {"i": 0}

    def resp(job):
        n["i"] += 1
        if n["i"] <= 3:
            raise TransportError("usage limit reached", input_tokens=5, output_tokens=1, cost_usd=0.01, raw_path=raw)
        return triage(False)
    env = make_env(tmp_path, {"TRIAGE": resp})
    env.add(env.job("KO", "TRIAGE", 1))
    env.engine.run(RUN)
    rows = env.conn.execute("SELECT ok, input_tokens, cost_usd, input_path, output_path FROM llm_calls ORDER BY call_id").fetchall()
    assert [r["ok"] for r in rows] == [0, 0, 0, 1]                   # 修前：只記 1 列
    assert [r["input_tokens"] for r in rows[:3]] == [5, 5, 5] and rows[0]["output_path"] == str(raw)
    assert rows[3]["input_path"] and Path(rows[3]["input_path"]).is_file()
    job = env.by_type("TRIAGE")[0]
    assert job["status"] == "done" and job["cost_usd"] == pytest.approx(0.03)


def test_d47_timeout_retried_once_other_errors_not_retried(tmp_path):
    class R:
        backend = "fake"

        def __init__(self, errs):
            self.errs, self.calls = list(errs), 0

        def run(self, job):
            self.calls += 1
            raise self.errs.pop(0)
    job = JobSpec(job_id="r:KO:TRIAGE:1", run_id="r", job_type="TRIAGE", ticker="KO", asof=ASOF)
    r = R([LLMTimeout("t1"), LLMTimeout("t2"), LLMTimeout("t3")])
    with pytest.raises(LLMTimeout):
        call_with_retry(r, job, retries=3, sleep=lambda s: None)
    assert r.calls == 2                                               # 修前：4 次
    r = R([TransportError("claude 結束碼 1：segfault"), TransportError("x")])
    with pytest.raises(TransportError):
        call_with_retry(r, job, retries=3, sleep=lambda s: None)
    assert r.calls == 1


def test_budget_counts_inflight_calls(tmp_path):
    resp = {("TRIAGE", t): {**triage(False), "ticker": t} for t in ("KO", "MSFT", "PEP")}
    env = make_env(tmp_path, resp, max_calls=1)
    for t in ("KO", "MSFT", "PEP"):
        env.add(env.job(t, "TRIAGE", 1))
    out = env.engine.run(RUN)
    assert len(env.fake.calls) == 1                                    # 修前：同時送出 2 個（併發上限）
    assert out["status"] == "paused_budget"


def test_validation_retry_waits_for_budget(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": [{"ticker": "KO"}, triage(False)]}, max_calls=1)
    env.add(env.job("KO", "TRIAGE", 1))
    out = env.engine.run(RUN)
    assert len(env.fake.calls) == 1 and out["status"] == "paused_budget"   # 修前：預算 1 仍跑了驗證重試
    assert env.by_type("TRIAGE")[0]["status"] == "pending"


def test_full_b_capped_at_three_per_ticker_per_run(tmp_path):
    env = make_env(tmp_path, full_responses())
    env.price_file("KO")
    for i in (1, 2, 3):
        env.add(env.job("KO", "FULL_B", i))
        env.conn.execute("UPDATE jobs SET status='done', attempts=1 WHERE job_id=?", (f"{RUN}:KO:FULL_B:{i}",))
    env.conn.commit()
    env.add(env.job("KO", "FULL_B", 4))
    env.engine.run(RUN)
    fb4 = env.conn.execute("SELECT status, error FROM jobs WHERE job_id=?", (f"{RUN}:KO:FULL_B:4",)).fetchone()
    assert fb4["status"] == "failed" and "D47" in fb4["error"]
    assert not [c for c in env.fake.calls if c.job_type == "FULL_B"]


# ---------------------------------------------------------------- M5_D4-04

def test_daily_budget_exhausted_by_earlier_run_pauses_new_run(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    env.config.budget.max_llm_calls_per_day = 2
    for _ in range(2):
        db.insert(env.conn, "llm_calls", {"job_id": "old:KO:TRIAGE:1", "run_id": "old", "attempt": 0, "ok": 1,
                                          "cost_usd": 0.0, "created_at": db.now_iso()})
    env.add(env.job("KO", "TRIAGE", 1))
    out = env.engine.run(RUN)
    assert out["status"] == "paused_budget" and env.fake.calls == []
    an = env.conn.execute("SELECT detail FROM anomalies WHERE kind=?", (E.BUDGET_DAY_KIND,)).fetchall()
    assert len(an) == 1 and json.loads(an[0]["detail"])["run_id"] == RUN
    env.engine.run(RUN)                                               # 同一 run 再暫停不重複記
    assert env.conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind=?", (E.BUDGET_DAY_KIND,)).fetchone()[0] == 1
    env.config.budget.max_llm_calls_per_day = 0                       # 0＝不限
    assert env.engine.run(RUN)["status"] == "done"


def test_config_daily_budget_keys(tmp_path):
    from trader.config import load_config
    p = TE._write_config(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace(
        "budget:\n", "budget:\n  max_llm_calls_per_day: 50\n  max_cost_usd_per_day: 20\n"), encoding="utf-8")
    b = load_config(p, env_file=tmp_path / "none.env").budget
    assert (b.max_llm_calls_per_day, b.max_cost_usd_per_day) == (50, 20)


# ---------------------------------------------------------------- M3_D9-01

def test_write_raw_keeps_each_call(tmp_path):
    job = JobSpec(job_id="r:KO:FULL_A:1", run_id="r", job_type="FULL_A", ticker="KO", asof=ASOF)
    a = write_raw(tmp_path, job, "prompt.txt", "一")
    b = write_raw(tmp_path, job, "prompt.txt", "二")
    assert a != b and a.read_text(encoding="utf-8") == "一"           # 修前：同名覆寫
    assert latest_raw(tmp_path, job, "prompt.txt") == b
    job.attempt = 1
    assert latest_raw(tmp_path, job, "prompt.txt") is None


def test_claude_error_carries_usage_and_timeout_class(tmp_path):
    tier = TierConfig(name="strong", backend="claude_code", model="m", claude_bin="/opt/claude", timeout_s=9)
    sb = tmp_path / "sb"
    sb.mkdir()
    job = JobSpec(job_id="r:KO:FULL_A:1", run_id="r", job_type="FULL_A", ticker="KO", asof=ASOF,
                  sandbox=sb, log_dir=tmp_path / "log", prompt_text="p")
    out = json.dumps({"is_error": True, "result": "boom", "usage": {"input_tokens": 100, "output_tokens": 7},
                      "total_cost_usd": 1.25})
    fake = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, out, "err")   # noqa: E731
    with pytest.raises(TransportError) as ei:
        CC.ClaudeCodeRunner(tier, runner_fn=fake, denies=[]).run(job)
    e = ei.value
    assert (e.input_tokens, e.output_tokens, e.cost_usd) == (100, 7, 1.25) and e.raw_path.is_file()

    def to(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 9)
    with pytest.raises(LLMTimeout):
        CC.ClaudeCodeRunner(tier, runner_fn=to, denies=[]).run(job)


# ---------------------------------------------------------------- M3_D1-10

def test_validate_expect_ticker_and_date():
    schemas = validate.load_schemas(TE.get_paths().schemas_json)
    _, errs = validate.check("TRIAGE", {**triage(False), "ticker": "KO"}, schemas, expect={"ticker": "PEP"})
    assert any("TRIAGE.ticker" in e for e in errs)
    _, errs = validate.check("TRIAGE", {**triage(False), "ticker": "brk-b"}, schemas, expect={"ticker": "BRK.B"})
    assert errs == []
    _, errs = validate.check("TRIAGE", {**triage(False), "ticker": "brk/b"}, schemas, expect={"ticker": "BRK.B"})
    assert errs == []     # '/' 與 universe.normalize_ticker、writeback._std_ticker 一致
    d = TE.dossier_ok()
    _, errs = validate.check("DOSSIER", {**d, "decision_date": "2026-10-15"}, schemas,
                             expect={"ticker": "KO", "decision_date": ASOF})
    assert any("decision_date" in e for e in errs)
    _, errs = validate.check("DOSSIER", d, schemas, expect={"ticker": "KO", "decision_date": ASOF})
    assert errs == []


def test_engine_wrong_ticker_output_is_retried(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": [triage(False), {**triage(False), "ticker": "MSFT"}]})
    env.add(env.job("MSFT", "TRIAGE", 1))
    env.engine.run(RUN)
    row = env.by_type("TRIAGE")[0]
    assert row["status"] == "done" and row["attempts"] == 2
    err = env.conn.execute("SELECT error FROM llm_calls ORDER BY call_id").fetchone()["error"]
    assert "TRIAGE.ticker" in err
