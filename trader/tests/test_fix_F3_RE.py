"""F3-RE：V3-1 FG-M3-review_jobs（A3-M3C-01；A3-M1A-07 只改註解）與 FG-M3-engine（A3-M3E-01、A3-M3E-02）修復，
以及 A3-M5E-03 遺留的重複隔離沙箱清除段刪除後的行為護欄。"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from trader.jobs import context, isolation
from trader.jobs.engine import Engine
from trader.jobs.handlers import Handler
from trader.jobs.review_jobs import TriageHandler
from trader.jobs.sandbox import STALE_SANDBOX_HOURS, create_sandbox, ticker_dir
from trader.models import JobSpec, LLMResult

from test_engine import ASOF, RUN, make_env, triage


# ---------------------------------------------------------------- A3-M3C-01：8-K 原文缺 → 升 THESIS

def _reg(env, t="KO"):
    r = env.conn.execute("SELECT data FROM registry WHERE ticker=?", (t,)).fetchone()
    return json.loads(r["data"]) if r is not None and r["data"] else {}


def test_triage_text_missing_escalates(tmp_path):
    env = make_env(tmp_path, {})
    job = env.job("KO", "TRIAGE", 1, params={
        "triage": [{"type": "8-K", "url": "https://x/8k", "date": "2026-10-01", "items": ["2.02"]}]})
    TriageHandler().pre_script(job, env.engine.ctx)          # ctx.sec 為 None → 標 text_missing
    assert job.params["triage"][0]["text_missing"] is True
    res = triage(False)                                       # 模型只看標題答「否」、escalate false
    out = TriageHandler().next_jobs(job, res, env.engine.ctx)
    assert [j.job_type for j in out] == ["THESIS"]
    assert "text_missing" in res["framework_note"]
    assert env.conn.execute("SELECT COUNT(*) FROM decisions WHERE ticker='KO'").fetchone()[0] == 0   # 未判否記帳


def test_triage_without_text_missing_still_no_escalate(tmp_path):
    env = make_env(tmp_path, {})
    job = env.job("KO", "TRIAGE", 1, params={"triage": [{"type": "8-K", "text_file": "sec/8-K_2026-10-01.txt"}]})
    assert TriageHandler().next_jobs(job, triage(False), env.engine.ctx) == []   # 原文在 → 照舊判否


# ---------------------------------------------------------------- A3-M3E-02：預設 pkg 不是專案根；基底 post 不收模型檔

def test_default_pkg_not_project_root(tmp_path):
    env = make_env(tmp_path, {})
    job = env.job("KO", "TRIAGE", 1)
    text = context.build_prompt(job, "fake", 10 ** 7, env.paths.root, sections=[], values={})
    assert str(env.paths.root) not in text
    assert context.NO_PKG in text


def test_base_post_ignores_model_files(tmp_path):
    env = make_env(tmp_path, {})
    job = env.job("KO", "X", 1)
    sb = create_sandbox(env.paths.work, job)
    (sb / "cap_result.json").write_text("{}", encoding="utf-8")
    assert Handler().post(job, {"files": ["cap_result.json"]}, env.engine.ctx) == []
    assert not (ticker_dir(env.paths.work, job) / "cap_result.json").exists()


# ---------------------------------------------------------------- A3-M3E-01：output_schemas.json 改過 → 驗證重讀

def test_schemas_reloaded_when_file_changes(tmp_path):
    env = make_env(tmp_path, {})
    src = tmp_path / "output_schemas.json"
    shutil.copy(env.paths.schemas_json, src)
    eng = Engine(env.conn, env.router, env.paths, env.config, sleep=lambda s: None)
    eng._schemas_path = src
    eng.schemas = eng.ctx.schemas = eng._load_schemas()
    spec = env.job("KO", "TRIAGE", 1)
    ok, errs = eng._check(spec, LLMResult(text="", parsed_json=triage(False)))
    assert errs == []
    s = json.loads(src.read_text(encoding="utf-8"))
    s["TRIAGE"]["new_required_marker"] = "string"        # 模擬 run 中同步 routing：TRIAGE 多一個必填欄
    src.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    ok, errs = eng._check(spec, LLMResult(text="", parsed_json=triage(False)))
    assert any("new_required_marker" in e for e in errs)
    assert eng.ctx.schemas is eng.schemas and "new_required_marker" in eng.ctx.schemas["TRIAGE"]
    # 寫到一半（壞 JSON）→ 沿用舊快取，不拋例外
    src.write_text("{", encoding="utf-8")
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 2 * 10 ** 9))
    assert "new_required_marker" in eng._current_schemas()["TRIAGE"]


def test_injected_schemas_not_reloaded(tmp_path):
    env = make_env(tmp_path, {})
    sch = {"TRIAGE": {}}
    eng = Engine(env.conn, env.router, env.paths, env.config, schemas=sch, sleep=lambda s: None)
    assert eng._current_schemas() is sch


# ---------------------------------------------------------------- A3-M5E-03 遺留段：非重複（保留），引擎啟動清理護欄

def test_engine_start_cleanup_unchanged(tmp_path, monkeypatch):
    env = make_env(tmp_path, {})
    root = tmp_path / "sbroot"
    root.mkdir()
    isolation.priced_root(root).mkdir()
    mk = lambda jt, t: JobSpec(job_id=f"r:{t}:{jt}:1", run_id="r", job_type=jt, ticker=t, asof="d")
    priced = create_sandbox(root, mk("FULL_B", "Y"))                  # priced 根的殘留：立即清
    iso = create_sandbox(root, mk("FULL_A", "X"))                     # 隔離工作沙箱：不清
    legacy = root / "r" / "Z" / "_PRICE_1"                            # 舊版留在隔離根的看價格沙箱（逾時）
    legacy.mkdir(parents=True)
    young = root / "r" / "W" / "_FULL_B_1"                            # 同上但未逾 24 小時：只有引擎這段會清
    young.mkdir(parents=True)
    old = time.time() - (STALE_SANDBOX_HOURS + 1) * 3600
    os.utime(legacy, (old, old))
    monkeypatch.setattr(Engine, "_sandbox_root", staticmethod(lambda c, p, r: root))
    Engine(env.conn, env.router, env.paths, env.config, sleep=lambda s: None)
    assert not priced.exists() and not legacy.exists() and not young.exists() and iso.exists()
