"""Claude Code 沙箱隔離：封鎖清單、沙箱位置檢查、--settings 規則（不呼叫 claude）。"""
import json
from pathlib import Path

import pytest

from trader.jobs import isolation as iso
from trader.llm.base import FatalLLMError
from trader.llm.claude_code import ClaudeCodeRunner
from trader.models import JobSpec, TierConfig
from trader.paths import PKG_ROOT


def test_deny_roots_cover_home_tmp_project_and_state(tmp_path):
    d = iso.deny_roots(extra=[str(tmp_path / "x")], state_dir=tmp_path)
    assert Path.home().resolve() in d
    assert PKG_ROOT.resolve() in d
    assert tmp_path.resolve() in d
    assert any(str(p) in ("/private", "/tmp") for p in d)


def test_sandbox_under_denied_root_rejected(tmp_path):
    with pytest.raises(iso.IsolationError):
        iso.check_sandbox(tmp_path / "sb", iso.deny_roots())          # pytest 暫存在 /private 或家目錄下
    with pytest.raises(iso.IsolationError):
        iso.check_sandbox(Path.home() / "anything", iso.deny_roots())
    iso.check_sandbox(Path("/opt-not-real/sb"), [Path("/opt-other")])  # 不在封鎖範圍 → 通過


def test_settings_json_rules():
    s = json.loads(iso.settings_json([Path("/Users/someone"), Path("/private")]))
    rules = s["permissions"]["deny"]
    assert "Read(//Users/someone/**)" in rules and "Write(//private/**)" in rules and "Edit(//private/**)" in rules
    assert len([r for r in rules if "(//**/" not in r]) == 6  # 2 個封鎖目錄 × Read／Edit／Write（設定檔規則另計）
    assert s["disableAllHooks"] is True


def test_runner_command_has_settings_and_refuses_bad_sandbox(tmp_path):
    tier = TierConfig(name="weak", backend="claude_code", model="haiku")
    r = ClaudeCodeRunner(tier, runner_fn=lambda *a, **k: None, denies=[tmp_path.resolve()])
    cmd = r.command()
    s = json.loads(cmd[cmd.index("--settings") + 1])
    assert f"Read(/{tmp_path.resolve().as_posix()}/**)" in s["permissions"]["deny"]
    sb = tmp_path / "sb"
    sb.mkdir()
    job = JobSpec(job_id="r:KO:FULL_A:1", run_id="r", job_type="FULL_A", ticker="KO", asof="2026-10-16")
    job.sandbox = sb
    job.prompt_text = "x"
    with pytest.raises(FatalLLMError):
        r.run(job)


def test_default_root_outside_denies():
    import sys
    if sys.platform != "darwin":
        pytest.skip("只有 macOS 有預設")
    iso.check_sandbox(iso.default_sandbox_root(), iso.deny_roots())


def test_purge_stale_sandboxes_removes_only_old_price_visible_ones(tmp_path):
    """殘留的看價格沙箱（如 FULL_B 的 *_price.json）會讓兄弟隔離沙箱讀到價格：超過時限就刪；隔離工作與新沙箱保留。"""
    import os
    import time

    from trader.jobs.sandbox import STALE_SANDBOX_HOURS, purge_stale_sandboxes, remove_sandbox
    old = time.time() - (STALE_SANDBOX_HOURS + 1) * 3600
    mk = {}
    for name in ("_FULL_B_1", "_DOSSIER_1", "_PRICE_2"):
        d = tmp_path / "run1" / "OTIS" / name
        d.mkdir(parents=True)
        (d / "OTIS_price.json").write_text("{}") if name != "_DOSSIER_1" else None
        mk[name] = d
    os.utime(mk["_FULL_B_1"], (old, old))
    os.utime(mk["_DOSSIER_1"], (old, old))
    removed = purge_stale_sandboxes(tmp_path)
    assert removed == [str(mk["_FULL_B_1"])]
    assert mk["_DOSSIER_1"].exists() and mk["_PRICE_2"].exists()          # 隔離工作／未逾時保留
    assert remove_sandbox(tmp_path / "run1", tmp_path) is False             # 不是工作沙箱層級 → 不刪
    assert remove_sandbox(mk["_PRICE_2"], tmp_path) is True and not mk["_PRICE_2"].exists()


def test_build_runner_denies_custom_state_dir(tmp_path):
    """--state-dir 指到專案外：deny 清單仍要包含它。"""
    from trader.llm.router import build_runner
    st = tmp_path / "elsewhere-state"
    st.mkdir()
    tier = TierConfig(name="weak", backend="claude_code", model="haiku")
    r = build_runner(tier, None, st)
    assert st.resolve() in [Path(p).resolve() for p in r.denies]
