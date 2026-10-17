"""設定載入與驗證測試。不連網，不讀真實的 config.yaml 或 .env。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import trader.config as config_mod
from trader.config import ConfigError, load_config, load_dotenv
from trader.models import TierConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = PROJECT_ROOT / "config.example.yaml"

ENV_KEYS = [
    "SEC_USER_AGENT", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FRED_API_KEY",
    "PROVIDER_A_KEY", "PROVIDER_B_KEY", "TRADER_CONFIG", "TRADER_STATE_DIR",
    "TEST_DOTENV_NEW", "TEST_DOTENV_EXISTING", "TEST_DOTENV_EMPTY",
]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """清空相關環境變數（測試結束後還原），並把 PROJECT_DIR 指到 tmp，避免讀到真實設定。"""
    for k in ENV_KEYS:
        monkeypatch.setenv(k, "")  # 先記錄原值，teardown 時會還原
        monkeypatch.delenv(k)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(config_mod, "PROJECT_DIR", proj)
    return tmp_path


def _valid() -> dict:
    return {
        "models": {
            "strong": {"backend": "claude_code", "model": "opus"},
            "medium": {"backend": "openai_compat", "base_url": "https://x.example/v1",
                       "api_key_env": "PROVIDER_A_KEY", "model": "m1"},
            "weak": {"backend": "fake", "model": "fake"},
        },
    }


def _write(tmp: Path, data) -> Path:
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _load_bad(tmp: Path, data) -> str:
    with pytest.raises(ConfigError) as ei:
        load_config(_write(tmp, data), env_file=tmp / "none.env")
    return str(ei.value)


# ---------------------------------------------------------------- 正常載入
def test_load_minimal_valid_applies_defaults(isolated):
    cfg = load_config(_write(isolated, _valid()), env_file=isolated / "none.env")
    assert cfg.universe == "sp500"
    assert cfg.account.initial_cash == 100000
    assert cfg.account.benchmark == "SPY"
    assert cfg.budget.max_llm_calls_per_run == 200
    assert cfg.data.alpaca_feed == "iex"
    assert cfg.dashboard.port == 8787
    assert cfg.schedule["daily_close"] == "17:30"
    assert cfg.schedule["quarterly_screen"] == ["02-20", "05-20", "08-20", "11-20"]
    assert set(cfg.models) == {"strong", "medium", "weak"}
    assert isinstance(cfg.models["medium"], TierConfig)
    assert cfg.models["medium"].name == "medium"
    assert cfg.models["medium"].api_key_env == "PROVIDER_A_KEY"
    assert cfg.job_tier_overrides == {}


def test_schedule_user_values_override_defaults(isolated):
    data = _valid()
    data["schedule"] = {"daily_close": "16:00"}
    cfg = load_config(_write(isolated, data), env_file=isolated / "none.env")
    assert cfg.schedule["daily_close"] == "16:00"
    assert cfg.schedule["monthly_freeze"] == "01"


def test_example_file_loads(isolated):
    cfg = load_config(EXAMPLE, env_file=isolated / "none.env")
    assert cfg.models["strong"].backend == "claude_code"
    assert cfg.models["medium"].backend == "openai_compat"
    assert cfg.models["weak"].backend == "fake"


def test_default_path_from_env_var(isolated, monkeypatch):
    p = _write(isolated, _valid())
    monkeypatch.setenv("TRADER_CONFIG", str(p))
    cfg = load_config(env_file=isolated / "none.env")
    assert cfg.source_path == p


def test_default_path_is_project_config_yaml(isolated):
    _write(Path(config_mod.PROJECT_DIR), _valid())  # 寫到 PROJECT_DIR/config.yaml
    cfg = load_config(env_file=isolated / "none.env")
    assert cfg.source_path == Path(config_mod.PROJECT_DIR) / "config.yaml"


def test_missing_file_mentions_copy_hint(isolated):
    with pytest.raises(ConfigError, match="cp config.example.yaml config.yaml"):
        load_config(isolated / "nope.yaml", env_file=isolated / "none.env")


# ---------------------------------------------------------------- 驗證錯誤
def test_missing_tier_is_error(isolated):
    data = _valid()
    del data["models"]["weak"]
    assert "models: 缺少 weak" in _load_bad(isolated, data)


def test_invalid_backend_is_error(isolated):
    data = _valid()
    data["models"]["weak"]["backend"] = "gpt"
    assert "models.weak.backend" in _load_bad(isolated, data)


@pytest.mark.parametrize("missing", ["base_url", "api_key_env", "model"])
def test_openai_compat_requires_fields(isolated, missing):
    data = _valid()
    del data["models"]["medium"][missing]
    assert f"openai_compat 需要 {missing}" in _load_bad(isolated, data)


@pytest.mark.parametrize("backend", ["claude_code", "fake"])
def test_claude_code_and_fake_require_model(isolated, backend):
    data = _valid()
    data["models"]["strong"] = {"backend": backend}
    assert f"{backend} 需要 model" in _load_bad(isolated, data)


def test_plaintext_api_key_is_rejected(isolated):
    data = _valid()
    data["models"]["medium"]["api_key"] = "sk-secret-value"
    msg = _load_bad(isolated, data)
    assert "明文 api_key" in msg
    assert "sk-secret-value" not in msg  # 錯誤訊息不得回印金鑰


def test_job_tier_override_unknown_job_is_error(isolated):
    data = _valid()
    data["job_tier_overrides"] = {"NOPE": "strong"}
    assert "未知的工作類型 'NOPE'" in _load_bad(isolated, data)


def test_job_tier_override_unknown_tier_is_error(isolated):
    data = _valid()
    data["job_tier_overrides"] = {"FULL_B": "ultra"}
    assert "job_tier_overrides.FULL_B" in _load_bad(isolated, data)


def test_job_tier_override_valid(isolated):
    data = _valid()
    data["job_tier_overrides"] = {"FULL_B": "strong"}
    cfg = load_config(_write(isolated, data), env_file=isolated / "none.env")
    assert cfg.job_tier_overrides == {"FULL_B": "strong"}


def test_unknown_top_level_key_is_error(isolated):
    data = _valid()
    data["bogus"] = 1
    assert "頂層: 未知的鍵 'bogus'" in _load_bad(isolated, data)


def test_unknown_key_inside_section_is_error(isolated):
    data = _valid()
    data["account"] = {"initial_cash": 1, "typo_key": 2}
    assert "account: 未知的鍵 'typo_key'" in _load_bad(isolated, data)


def test_wrong_type_is_error(isolated):
    data = _valid()
    data["account"] = {"initial_cash": "lots"}
    assert "account.initial_cash: 型別錯誤" in _load_bad(isolated, data)


def test_all_errors_are_reported_together(isolated):
    data = _valid()
    data["models"]["weak"]["backend"] = "gpt"
    data["models"]["strong"] = {"backend": "claude_code"}
    data["job_tier_overrides"] = {"NOPE": "strong"}
    msg = _load_bad(isolated, data)
    assert "models.weak.backend" in msg
    assert "claude_code 需要 model" in msg
    assert "未知的工作類型" in msg
    assert "3 個錯誤" in msg


def test_yaml_syntax_error_is_config_error(isolated):
    p = isolated / "config.yaml"
    p.write_text("models: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(p, env_file=isolated / "none.env")


# ---------------------------------------------------------------- 秘密與 .env
def test_secret_reads_env_and_treats_empty_as_none(isolated, monkeypatch):
    cfg = load_config(_write(isolated, _valid()), env_file=isolated / "none.env")
    assert cfg.secret("PROVIDER_A_KEY") is None
    monkeypatch.setenv("PROVIDER_A_KEY", "")
    assert cfg.secret("PROVIDER_A_KEY") is None
    monkeypatch.setenv("PROVIDER_A_KEY", "value-123")
    assert cfg.secret("PROVIDER_A_KEY") == "value-123"
    assert cfg.secret(None) is None


def test_load_dotenv_parses_and_skips_comments_and_blanks(isolated):
    env = isolated / ".env"
    env.write_text(
        "# 註解\n\nTEST_DOTENV_NEW=abc\nexport TEST_DOTENV_NEW2=\"quoted value\"\n"
        "TEST_DOTENV_EMPTY=\n'single'\nnot a pair\n",
        encoding="utf-8",
    )
    import os
    set_keys = load_dotenv(env)
    assert set_keys == {"TEST_DOTENV_NEW": "abc", "TEST_DOTENV_NEW2": "quoted value"}
    assert os.environ["TEST_DOTENV_NEW"] == "abc"
    assert "TEST_DOTENV_EMPTY" not in os.environ
    # 測試結束還原
    os.environ.pop("TEST_DOTENV_NEW2", None)


def test_load_dotenv_does_not_override_existing_env(isolated, monkeypatch):
    monkeypatch.setenv("TEST_DOTENV_EXISTING", "from-shell")
    env = isolated / ".env"
    env.write_text("TEST_DOTENV_EXISTING=from-file\n", encoding="utf-8")
    assert load_dotenv(env) == {}
    import os
    assert os.environ["TEST_DOTENV_EXISTING"] == "from-shell"


def test_load_dotenv_missing_file_returns_empty(isolated):
    assert load_dotenv(isolated / "missing.env") == {}


def test_load_config_reads_env_file_without_overriding(isolated, monkeypatch):
    monkeypatch.setenv("PROVIDER_B_KEY", "shell-value")
    env = isolated / "keys.env"
    env.write_text("PROVIDER_A_KEY=file-a\nPROVIDER_B_KEY=file-b\n", encoding="utf-8")
    cfg = load_config(_write(isolated, _valid()), env_file=env)
    import os
    assert cfg.secret("PROVIDER_A_KEY") == "file-a"
    assert os.environ["PROVIDER_B_KEY"] == "shell-value"


def test_load_config_default_env_file_under_project_dir(isolated):
    proj = Path(config_mod.PROJECT_DIR)
    (proj / ".env").write_text("PROVIDER_A_KEY=from-project-env\n", encoding="utf-8")
    load_config(_write(isolated, _valid()))
    import os
    assert os.environ["PROVIDER_A_KEY"] == "from-project-env"


def test_tier_effort_validated(tmp_path):
    import pytest
    from trader.config import ConfigError, load_config
    p = tmp_path / "c.yaml"
    p.write_text("models:\n  strong: {backend: fake, model: f, effort: turbo}\n"
                 "  medium: {backend: fake, model: f}\n  weak: {backend: fake, model: f}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="effort"):
        load_config(p, env_file=tmp_path / "none.env")
    p.write_text("models:\n  strong: {backend: fake, model: f, effort: high}\n"
                 "  medium: {backend: fake, model: f}\n  weak: {backend: fake, model: f}\n", encoding="utf-8")
    cfg = load_config(p, env_file=tmp_path / "none.env")
    assert cfg.models["strong"].effort == "high" and cfg.models["weak"].effort == "medium"
