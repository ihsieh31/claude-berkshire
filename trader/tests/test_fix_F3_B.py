"""F3-B：A3 復查 G9（引擎／模型層）、G10（DOSSIER 續跑與底稿）、G11（觸發檢查工作）、G12（同意閘門）修復。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from trader import db
from trader.jobs import approvals as A
from trader.jobs import deep, isolation, validate
from trader.jobs.hold_jobs import check as hold_check, held_years
from trader.jobs.review_jobs import TriageHandler, _held_under_year
from trader.jobs.sandbox import create_sandbox
from trader.llm.base import LLMTimeout, TransportError, call_with_retry, is_quota_error, latest_raw
from trader.llm.hark_queue import HarkQueueRunner, job_key
from trader.llm.openai_compat import OpenAICompatRunner
from trader.models import JobSpec, TierConfig

from test_engine import ASOF, RUN, full_a, make_env, triage


# ---------------------------------------------------------------- G9：驗證重試帶前次輸出（A3-M5A-02）

def test_retry_prompt_includes_previous_output(tmp_path):
    bad = {"ticker": "KO", "bad_marker": "前次答案XYZ"}
    env = make_env(tmp_path, {"TRIAGE": [bad, triage(False)]})
    env.add(env.job("KO", "TRIAGE", 1))
    assert env.engine.run(RUN)["status"] == "done"
    p = env.fake.calls[1].prompt_text
    assert "未通過格式檢查" in p and "前次輸出" in p and "前次答案XYZ" in p


def test_error_feedback_previous_truncated():
    s = validate.error_feedback(["x"], "a" * (validate.PREVIOUS_OUTPUT_CHARS + 5))
    assert "以下截斷" in s and s.count("a") >= validate.PREVIOUS_OUTPUT_CHARS
    assert "前次輸出" not in validate.error_feedback(["x"])


def test_full_a_framework_retry_attaches_previous_files(tmp_path):
    a = {**full_a(), "holding_stop": {"gate": "G3", "reason_code": "REJECT", "reason": "x"}}   # rule_ids 缺 → 退回一次
    env = make_env(tmp_path, {"FULL_A": a})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    rows = env.by_type("FULL_A")
    assert len(rows) == 2
    p2 = json.loads(rows[1]["params"])
    assert p2["prior_files"] == deep.FULL_A_PRIOR
    assert "上一版輸出在 `KO/previous/`" in env.fake.calls[1].prompt_text
    assert (Path(rows[1]["sandbox"]) / "KO" / "previous" / "phaseA.json").is_file()


# ---------------------------------------------------------------- G9：延後的驗證重試保留回饋（A3-M5C-06）

def test_deferred_validation_retry_keeps_feedback(tmp_path):
    bad = {"ticker": "KO"}
    env = make_env(tmp_path, {"TRIAGE": [bad, triage(False)]}, max_calls=1)
    env.add(env.job("KO", "TRIAGE", 1))
    assert env.engine.run(RUN)["status"] == "paused_budget"
    row = env.by_type("TRIAGE")[0]
    assert row["status"] == "pending" and json.loads(row["result"])["attempt"] == 1
    env.config.budget.max_llm_calls_per_run = 10
    assert env.engine.run(RUN)["status"] == "done"
    second = env.fake.calls[1]
    assert second.attempt == 1 and "未通過格式檢查" in second.prompt_text


# ---------------------------------------------------------------- G9：HTTP 逾時走 D47 重試 1 次（A3-M5B-06）

def test_openai_compat_timeout_retried_once(tmp_path):
    n = []

    def handler(request):
        n.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    tier = TierConfig(name="weak", backend="openai_compat", model="m", base_url="http://x", api_key_env="K")
    r = OpenAICompatRunner(tier, client=httpx.Client(transport=httpx.MockTransport(handler)), env={"K": "k"})
    job = JobSpec(job_id="r:KO:TRIAGE:1", run_id="r", job_type="TRIAGE", ticker="KO", asof=ASOF, prompt_text="p")
    with pytest.raises(LLMTimeout):
        call_with_retry(r, job, sleep=lambda s: None)
    assert len(n) == 2


# ---------------------------------------------------------------- G9：額度字樣不含裸 credit（A3-M5C-02）

def test_quota_pattern_no_credit_facility():
    assert not is_quota_error(TransportError("claude 回報錯誤：公司的 revolving credit facility 到期"))
    assert not is_quota_error(TransportError("claude 回報錯誤：Credit balance is too low"))
    assert is_quota_error(TransportError("claude 回報錯誤：Claude AI usage limit reached|1760000000"))
    assert is_quota_error(TransportError("HTTP 429（限流）", retry_after=3))


# ---------------------------------------------------------------- G9：hark_queue 接回舊佇列也存原始提示（A3-M5E-02）

def _hq_job(tmp_path):
    sb = tmp_path / "root" / "run-1" / "AAA" / "_DOSSIER_1"
    sb.mkdir(parents=True)
    return JobSpec(job_id="run-1:AAA:DOSSIER:1", run_id="run-1", job_type="DOSSIER", ticker="AAA", asof=ASOF,
                   sandbox=sb, prompt_text="提示", log_dir=tmp_path / "logs")


def _hq_tier():
    return TierConfig(name="weak", backend="hark_queue", model="haiku", timeout_s=1, effort="max")


def test_hark_queue_reattach_writes_prompt(tmp_path, monkeypatch):
    q = tmp_path / "q"
    monkeypatch.setenv("HARK_QUEUE_DIR", str(q))
    job = _hq_job(tmp_path)
    d = q / job_key(job)
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text("提示", encoding="utf-8")
    (d / "request.json").write_text(json.dumps({"job_id": job.job_id}), encoding="utf-8")
    (d / "response.txt").write_text('{"x": 1}', encoding="utf-8")
    assert HarkQueueRunner(_hq_tier(), denies=[], sleep=lambda s: pytest.fail("不應等待")).run(job).parsed_json == {"x": 1}
    assert latest_raw(job.log_dir, job, "prompt.txt") is not None


def test_hark_queue_rereads_partial_response(tmp_path, monkeypatch):
    q = tmp_path / "q"
    monkeypatch.setenv("HARK_QUEUE_DIR", str(q))
    job = _hq_job(tmp_path)
    d = q / job_key(job)
    d.mkdir(parents=True)
    (d / "response.txt").write_text('{"x": ', encoding="utf-8")   # 寫一半
    r = HarkQueueRunner(_hq_tier(), denies=[], poll_s=0.01)

    class Ev:   # 取代 _cancelled：等候時寫完回覆
        def is_set(self):
            return False

        def wait(self, t):
            (d / "response.txt").write_text('{"x": 2}', encoding="utf-8")
            return False
    r._cancelled = Ev()
    assert r.run(job).parsed_json == {"x": 2}


# ---------------------------------------------------------------- G9：隔離工作沙箱終態也刪（A3-M5E-03）

def test_isolated_sandbox_removed_on_done(tmp_path):
    env = make_env(tmp_path, {})
    root = tmp_path / "sbroot"
    root.mkdir()
    env.engine.ctx.sandbox_root = root
    spec = env.job("KO", "DOSSIER", 1)
    spec.log_dir = tmp_path / "logs"
    sb = create_sandbox(root, spec)
    (sb / "dossier").mkdir()
    (sb / "dossier" / "KO.md").write_text("x", encoding="utf-8")
    env.engine._drop_sandbox(spec)
    assert not sb.exists()
    assert latest_raw(spec.log_dir, spec, "manifest.json") is not None   # 刪前仍留清單


# ---------------------------------------------------------------- G9：macOS 使用者可寫系統路徑也封鎖（A3-M5E-04）

def test_darwin_denies_usr_local(monkeypatch):
    if not Path("/usr/local").exists():
        pytest.skip("無 /usr/local")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert Path("/usr/local").resolve() in isolation.deny_roots()
    monkeypatch.setattr(sys, "platform", "linux")
    assert Path("/usr/local").resolve() not in isolation.deny_roots()


# ---------------------------------------------------------------- G10：DOSSIER 續跑判定與底稿（A3-M4E-03／M5C-04、M5E-01）

def _td(env, t="KO"):
    d = env.paths.work / RUN / t
    (d / "dossier").mkdir(parents=True, exist_ok=True)
    (d / "sec").mkdir(parents=True, exist_ok=True)
    return d


def test_dossier_resume_requires_meta(tmp_path):
    env = make_env(tmp_path, {})
    td = _td(env)
    (td / "dossier" / "KO.md").write_text("# 資料包\n", encoding="utf-8")
    (td / "sec" / "_index.json").write_text("[]", encoding="utf-8")
    (td / "dossier" / "_sources.json").write_text("{}", encoding="utf-8")
    job = env.job("KO", "DOSSIER", 1)
    with pytest.raises(RuntimeError, match="SEC 客戶端"):   # 缺 _meta.json → 重做前置（此處無 SEC 客戶端）
        deep.DossierHandler().pre_script(job, env.engine.ctx)
    (td / "dossier" / deep.META).write_text("{}", encoding="utf-8")
    deep.DossierHandler().pre_script(job, env.engine.ctx)   # 舊資料夾三者皆在 → 續跑


def test_dossier_rerun_uses_base(tmp_path):
    env = make_env(tmp_path, {})
    td = _td(env)
    (td / "dossier" / "KO.base.md").write_text("# 底稿\n", encoding="utf-8")
    (td / "dossier" / "KO.md").write_text("# 底稿\n## 模型追加\n", encoding="utf-8")   # 上次 post 覆寫的模型版
    job = env.job("KO", "DOSSIER", 1)
    deep.DossierHandler().prepare(job, env.engine.ctx)
    assert (Path(job.sandbox) / "dossier" / "KO.md").read_text(encoding="utf-8") == "# 底稿\n"
    (Path(job.sandbox) / "dossier" / "KO.md").write_text("# 底稿\n## 半截", encoding="utf-8")
    env.engine._before_transport_retry(job)
    assert (Path(job.sandbox) / "dossier" / "KO.md").read_text(encoding="utf-8") == "# 底稿\n"
    (td / "dossier" / "KO.base.md").unlink()   # 舊資料夾沒有底稿 → 退回 {T}.md
    assert deep.dossier_base(td, "KO") == td / "dossier" / "KO.md"


# ---------------------------------------------------------------- G11：觸發檢查工作

def test_triage_no_escalate_logged_on_retry(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    env.add(env.job("KO", "TRIAGE", 1, params={"reasons": ["8-K 2.02"]}))
    assert env.engine.run(RUN)["status"] == "done"   # 不經 review 流程（例如 jobs retry、run-approved）
    rows = env.conn.execute("SELECT signal, job_id, data FROM decisions WHERE ticker='KO'").fetchall()
    assert len(rows) == 1 and rows[0]["job_id"] is None
    log = json.loads(rows[0]["data"])
    assert log["review_type"] == "TRIAGE" and log["source_job_id"] == f"{RUN}:KO:TRIAGE:1"
    # 冪等：同工作再走一次不重寫
    TriageHandler().next_jobs(env.job("KO", "TRIAGE", 1), triage(False), env.engine.ctx)
    assert env.conn.execute("SELECT COUNT(*) FROM decisions WHERE ticker='KO'").fetchone()[0] == 1


def test_triage_prescript_clears_old(tmp_path):
    env = make_env(tmp_path, {})
    td = _td(env)
    (td / "triage_sec").mkdir()
    (td / "triage_sec" / "8-K_2026-01-01.txt").write_text("舊", encoding="utf-8")
    job = env.job("KO", "TRIAGE", 1, params={"triage": []})
    TriageHandler().pre_script(job, env.engine.ctx)
    assert not (td / "triage_sec" / "8-K_2026-01-01.txt").exists()


def test_triage_on_fail_escalates_on_context_overflow(tmp_path):
    env = make_env(tmp_path, {})
    job = env.job("KO", "TRIAGE", 1, deps=["x"], params={"base_action": "PRICE"})
    out = TriageHandler().on_fail(job, "ContextOverflowError: 提示超過上限", env.engine.ctx)
    assert [j.job_type for j in out] == ["THESIS"] and out[0].deps == []
    assert TriageHandler().on_fail(job, "TransportError: 暫時性錯誤", env.engine.ctx) == []   # 暫時性：留給 retry


def test_held_year_leap():
    assert held_years("2023-03-01", "2024-02-29") == 0 and _held_under_year("2023-03-01", "2024-02-29")   # 365 天未滿一年
    assert held_years("2023-03-01", "2024-03-01") == 1 and not _held_under_year("2023-03-01", "2024-03-01")
    assert held_years("2024-02-29", "2025-02-28") == 0 and held_years("2024-02-29", "2025-03-01") == 1
    assert held_years(None, ASOF) is None and _held_under_year(None, ASOF)
    trim = {"action": "TRIM", "sell_category": "估值", "trim_fraction": 0.5, "falsified_assumption": "x"}
    errs, _ = hold_check(trim, iv_base=10.0, price=20.0, held_days=365, years=0)
    assert any("未滿一年" in e for e in errs)
    errs, _ = hold_check(trim, iv_base=10.0, price=20.0, held_days=366, years=1)
    assert not any("未滿一年" in e for e in errs)


# ---------------------------------------------------------------- G12：同意閘門

def _job_row(env, job_id, jt, status, result=None):
    db.insert(env.conn, "jobs", {"job_id": job_id, "run_id": RUN, "job_type": jt, "ticker": "KO", "asof": ASOF,
                                 "status": status, "deps": [], "params": {}, "attempts": 1,
                                 "created_at": db.now_iso(), **({"result": result} if result else {})})


def _approval(env, holding):
    db.insert(env.conn, "approvals", {"ticker": "KO", "run_id": RUN, "asof": ASOF, "job_id": f"{RUN}:KO:FULL_A:1",
                                      "job_type": "FULL_A", "tier": "strong", "holding": holding, "reason": "x",
                                      "status": "approved", "created_at": db.now_iso()})


def test_close_finished_replaced_failure(tmp_path):
    env = make_env(tmp_path, {})
    _job_row(env, f"{RUN}:KO:FULL_A:1", "FULL_A", "failed", {"replaced_by": [f"{RUN}:KO:HOLD_REVIEW:1"]})
    _job_row(env, f"{RUN}:KO:HOLD_REVIEW:1", "HOLD_REVIEW", "done")
    _approval(env, 1)
    db.insert(env.conn, "anomalies", {"ticker": "KO", "kind": "job_failed", "status": "open", "pause_trading": 0,
                                      "detail": {"job_id": f"{RUN}:KO:FULL_A:1"}, "created_at": db.now_iso()})
    A.close_finished(env.conn, RUN)
    r = env.conn.execute("SELECT status, note FROM approvals").fetchone()
    assert r["status"] == "done" and r["note"] == A.REPLACED_NOTE
    assert A.add_block_reason(env.conn, "KO") is None
    env.engine._resolve_recovered(RUN)
    assert env.conn.execute("SELECT status FROM anomalies WHERE kind='job_failed'").fetchone()[0] == "resolved"


def test_close_finished_unreplaced_failure_stays(tmp_path):
    env = make_env(tmp_path, {})
    _job_row(env, f"{RUN}:KO:FULL_A:1", "FULL_A", "failed")
    _approval(env, 0)
    A.close_finished(env.conn, RUN)
    r = env.conn.execute("SELECT status, note FROM approvals").fetchone()
    assert r["status"] == "approved" and r["note"] == A.FAILED_NOTE


def test_reject_writeback_failure_retryable(tmp_path, monkeypatch):
    from test_approvals import _env, _run_to_gate
    from trader.analysis import writeback as W
    env = _env(tmp_path)
    _run_to_gate(env)
    real = W.write_stop_result

    def boom(*a, **k):
        raise RuntimeError("寫回失敗")
    monkeypatch.setattr(W, "write_stop_result", boom)
    with pytest.raises(RuntimeError):
        A.reject(env.conn, env.paths, [1])
    assert A.list_items(env.conn)[0]["status"] == "pending"   # 未結案，可再拒絕
    assert env.by_type("FULL_A")[0]["status"] == "awaiting_approval"
    monkeypatch.setattr(W, "write_stop_result", real)
    dids = A.reject(env.conn, env.paths, [1])
    assert len(dids) == 1 and A.list_items(env.conn, ("rejected",))[0]["approval_id"] == 1
    log = json.loads(env.conn.execute("SELECT data FROM decisions WHERE decision_id=?", (dids[0],)).fetchone()[0])
    assert log["source_job_id"] == "reject:1"


def test_estimate_includes_possible_retries(tmp_path):
    from test_approvals import _env, _run_to_gate
    env = _env(tmp_path)
    _run_to_gate(env)
    s = A.summary(env.conn)
    assert s["n_pending"] == 1
    assert s["est_input_tokens_max"] == s["est_input_tokens"] * A.MAX_FULL_A_RUNS
    assert s["est_minutes_max"] > s["est_minutes"]
    assert "可能退回" in s["note"]
