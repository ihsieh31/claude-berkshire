"""RM5-4（reaudit M5_S1 M5-D1-04 PARTIAL）：代理式提示只列檔名，同名輸入檔內容改了提示仍逐字相同；
hark_queue 須另比對派題時記錄的沙箱輸入（request.json inputs），不得重播舊答案、把舊 out/ 蓋進新沙箱。"""
import json
import shutil

import pytest

from trader.llm.base import TransportError
from trader.llm.hark_queue import HarkQueueRunner, job_key
from trader.models import JobSpec, TierConfig

PROMPT = "請讀 dossier/KO.md，把報告寫到 KO/report_A.md"


def _runner(sleep=lambda s: None):
    return HarkQueueRunner(TierConfig(name="strong", backend="hark_queue", model="x", timeout_s=1),
                           denies=[], sleep=sleep)


def _sb(tmp_path, text="舊資料包：回購均價可推 $321.10"):
    sb = tmp_path / "root" / "run" / "KO" / "_FULL_A_1"
    shutil.rmtree(sb, ignore_errors=True)
    (sb / "dossier").mkdir(parents=True)
    (sb / "dossier" / "KO.md").write_text(text, encoding="utf-8")
    return sb


def _job(sb, tmp_path):
    return JobSpec(job_id="run:KO:FULL_A:1", run_id="run", job_type="FULL_A", ticker="KO", asof="2026-10-16",
                   sandbox=sb, prompt_text=PROMPT, log_dir=tmp_path / "logs")


def _answered(tmp_path, monkeypatch):
    """派題（無人答→逾時），再模擬外部代理作答（out/ 一份報告＋response.txt）。"""
    q = tmp_path / "queue"
    monkeypatch.setenv("HARK_QUEUE_DIR", str(q))
    sb = _sb(tmp_path)
    with pytest.raises(TransportError):
        _runner().run(_job(sb, tmp_path))
    qd = q / job_key(_job(sb, tmp_path))
    (qd / "out" / "KO").mkdir(parents=True)
    (qd / "out" / "KO" / "report_A.md").write_text("OLD REPORT", encoding="utf-8")
    (qd / "response.txt").write_text('{"verdict": "OLD"}', encoding="utf-8")
    return q, qd, sb


def _assert_redispatched(q, qd, sb):
    assert not (qd / "response.txt").exists()
    assert not (sb / "KO" / "report_A.md").exists()          # 舊 out/ 沒蓋進新沙箱
    retired = list(q.glob(f"{qd.name}.retired-*"))
    assert len(retired) == 1 and (retired[0] / "response.txt").read_text(encoding="utf-8") == '{"verdict": "OLD"}'


def test_changed_input_content_redispatches(tmp_path, monkeypatch):
    q, qd, sb = _answered(tmp_path, monkeypatch)
    sb = _sb(tmp_path, "新資料包：回購金額已遮蔽")             # 修好遮蔽：同名檔、沙箱重建、提示逐字相同
    with pytest.raises(TransportError):                        # 新題目沒人答 → 等候，不重播舊答案
        _runner().run(_job(sb, tmp_path))
    _assert_redispatched(q, qd, sb)
    inputs = json.loads((qd / "request.json").read_text(encoding="utf-8"))["inputs"]
    assert list(inputs) == ["dossier/KO.md"] and len(inputs["dossier/KO.md"]) == 64


def test_missing_input_redispatches(tmp_path, monkeypatch):
    q, qd, sb = _answered(tmp_path, monkeypatch)
    (sb / "dossier" / "KO.md").unlink()
    with pytest.raises(TransportError):
        _runner().run(_job(sb, tmp_path))
    _assert_redispatched(q, qd, sb)


def test_resume_with_out_overwriting_same_named_input_reuses(tmp_path, monkeypatch):
    """DOSSIER 類：輸出與輸入同路徑，取答時 out/ 版本複製進沙箱；續跑不得因此重派。"""
    q, qd, sb = _answered(tmp_path, monkeypatch)
    (qd / "out" / "dossier").mkdir()
    (qd / "out" / "dossier" / "KO.md").write_text("代理寫好的資料包", encoding="utf-8")
    r = _runner(sleep=lambda s: pytest.fail("不應等待")).run(_job(sb, tmp_path))
    assert (sb / "dossier" / "KO.md").read_text(encoding="utf-8") == "代理寫好的資料包"
    r2 = _runner(sleep=lambda s: pytest.fail("不應等待")).run(_job(sb, tmp_path))   # 再續跑一次
    assert r.parsed_json == r2.parsed_json == {"verdict": "OLD"} and not list(q.glob("*.retired-*"))


def test_prepare_rebuilds_identical_inputs_reuses(tmp_path, monkeypatch):
    q, qd, sb = _answered(tmp_path, monkeypatch)
    sb = _sb(tmp_path)                                         # 沙箱重建、內容相同
    (sb / "extra.txt").write_text("沙箱多出的檔不比", encoding="utf-8")
    r = _runner(sleep=lambda s: pytest.fail("不應等待")).run(_job(sb, tmp_path))
    assert r.parsed_json == {"verdict": "OLD"} and r.files_written == ["KO/report_A.md"]
    assert not list(q.glob("*.retired-*"))


def test_legacy_request_without_inputs_still_reused(tmp_path, monkeypatch):
    q, qd, sb = _answered(tmp_path, monkeypatch)
    req = json.loads((qd / "request.json").read_text(encoding="utf-8"))
    del req["inputs"]
    (qd / "request.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    sb = _sb(tmp_path, "內容不同，但舊格式不檢查")
    r = _runner(sleep=lambda s: pytest.fail("不應等待")).run(_job(sb, tmp_path))
    assert r.parsed_json == {"verdict": "OLD"} and not list(q.glob("*.retired-*"))
