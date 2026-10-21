"""hark_queue 後端：請求落地、取用回覆、out/ 複製進沙箱、續跑不重問。"""
import json
from pathlib import Path

import pytest

from trader.llm.base import FatalLLMError, TransportError
from trader.llm.hark_queue import HarkQueueRunner, job_key
from trader.models import JobSpec, TierConfig, prompt_backend


def _tier(timeout=1):
    return TierConfig(name="weak", backend="hark_queue", model="haiku", timeout_s=timeout, effort="max")


def _job(tmp_path):
    sb = tmp_path / "root" / "run-1" / "AAA" / "_DOSSIER_1"
    sb.mkdir(parents=True)
    (sb / "in.md").write_text("輸入", encoding="utf-8")
    return JobSpec(job_id="run-1:AAA:DOSSIER:1", run_id="run-1", job_type="DOSSIER", ticker="AAA",
                   asof="2026-10-15", sandbox=sb, prompt_text="提示", log_dir=tmp_path / "logs")


def test_request_then_response(tmp_path, monkeypatch):
    q = tmp_path / "q"
    monkeypatch.setenv("HARK_QUEUE_DIR", str(q))
    job = _job(tmp_path)
    calls = []

    def sleeper(_):
        calls.append(1)
        d = q / job_key(job)
        assert json.loads((d / "request.json").read_text())["job_type"] == "DOSSIER"
        assert (d / "prompt.txt").read_text(encoding="utf-8") == "提示"
        (d / "out" / "AAA").mkdir(parents=True)
        (d / "out" / "AAA" / "report_A.md").write_text("報告", encoding="utf-8")
        (d / "response.txt").write_text('{"ok": 1}', encoding="utf-8")

    r = HarkQueueRunner(_tier(60), denies=[], sleep=sleeper).run(job)
    assert calls and r.parsed_json == {"ok": 1}
    assert (job.sandbox / "AAA" / "report_A.md").read_text(encoding="utf-8") == "報告"
    assert r.files_written == ["AAA/report_A.md"] and r.cost_usd is None   # M5-D5-05：無 meta.json 不記 0.0


def test_existing_response_is_reused_without_waiting(tmp_path, monkeypatch):
    q = tmp_path / "q"
    monkeypatch.setenv("HARK_QUEUE_DIR", str(q))
    job = _job(tmp_path)
    d = q / job_key(job)
    d.mkdir(parents=True)
    (d / "response.txt").write_text('{"x": 2}', encoding="utf-8")
    r = HarkQueueRunner(_tier(), denies=[], sleep=lambda s: pytest.fail("不應等待")).run(job)
    assert r.parsed_json == {"x": 2}


def test_timeout_and_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("HARK_QUEUE_DIR", str(tmp_path / "q"))
    job = _job(tmp_path)
    with pytest.raises(TransportError):
        HarkQueueRunner(_tier(0), denies=[], sleep=lambda s: None).run(job)
    with pytest.raises(FatalLLMError):
        HarkQueueRunner(_tier(), denies=[tmp_path]).run(job)


def test_prompt_backend():
    assert prompt_backend("hark_queue") == "claude_code"
    assert prompt_backend("openai_compat") == "openai_compat"
