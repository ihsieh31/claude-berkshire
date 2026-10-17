"""設定載入與驗證：config.yaml＋.env。秘密只從環境變數讀，設定檔只存環境變數名稱。"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .models import BACKENDS, JOB_TYPES, TIERS, TierConfig
from .paths import PROJECT_DIR

_TOP_KEYS = frozenset({"account", "universe", "schedule", "models", "job_tier_overrides", "budget", "data", "dashboard", "sandbox", "approval"})
_TIER_REQUIRED = ("strong", "medium", "weak")
_SCHEDULE_DEFAULTS: dict[str, Any] = {
    "timezone": "America/New_York",
    "daily_close": "17:30",
    "review_holdings": "daily",
    "review_watch": "biweekly",
    "quarterly_screen": ["02-20", "05-20", "08-20", "11-20"],
    "monthly_freeze": "01",
}


class ConfigError(Exception):
    """設定錯誤。訊息列出所有問題（不是只第一個）。"""


@dataclass
class AccountCfg:
    initial_cash: float = 100000
    slippage_bps: float = 10
    commission_per_order: float = 0
    benchmark: str = "SPY"


@dataclass
class BudgetCfg:
    max_llm_calls_per_run: int = 200
    max_cost_usd_per_run: float = 100.0      # 本 run 模型費用（美元）總和上限；0＝不限
    max_input_tokens_per_run: int = 0        # 輸入 token 總和上限（含快取讀取，會被代理式多回合放大）；0＝不限


@dataclass
class DataCfg:
    sec_user_agent_env: str = "SEC_USER_AGENT"
    alpaca_key_env: str = "ALPACA_API_KEY"
    alpaca_secret_env: str = "ALPACA_SECRET_KEY"
    alpaca_feed: str = "iex"
    fred_key_env: str = "FRED_API_KEY"


@dataclass
class SandboxCfg:
    root: str = ""                 # 空＝平台預設（macOS：/Users/Shared/berkshire-sandbox-使用者）；不可在家目錄或暫存目錄下
    extra_deny: list = field(default_factory=list)   # 額外封鎖的目錄（模型不得讀寫）


@dataclass
class DashboardCfg:
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass
class ApprovalCfg:
    require_for_tiers: list = field(default_factory=lambda: ["strong"])   # 這些等級的工作須經使用者同意才執行（M6）


@dataclass
class Config:
    account: AccountCfg
    universe: str
    schedule: dict
    models: dict[str, TierConfig]
    job_tier_overrides: dict[str, str]
    budget: BudgetCfg
    data: DataCfg
    dashboard: DashboardCfg
    source_path: Path | None = None
    sandbox: SandboxCfg = field(default_factory=SandboxCfg)
    approval: ApprovalCfg = field(default_factory=ApprovalCfg)

    def secret(self, env_name: str | None) -> str | None:
        """從環境變數讀秘密；未設定或空字串回傳 None。"""
        if not env_name:
            return None
        return os.environ.get(env_name) or None


def load_dotenv(path: Path) -> dict[str, str]:
    """簡單的 KEY=VALUE 解析。不覆蓋已存在的環境變數；空值略過。回傳實際設定的鍵。"""
    path = Path(path)
    if not path.is_file():
        return {}
    set_keys: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # 未加引號時，" #" 之後視為註解
            if " #" in value:
                value = value.split(" #", 1)[0].rstrip()
        if not key or not value:
            continue
        if key in os.environ:
            continue
        os.environ[key] = value
        set_keys[key] = value
    return set_keys


def resolve_config_path(path: str | Path | None = None) -> Path:
    """設定檔路徑優先序：參數 > 環境變數 TRADER_CONFIG > {PROJECT_DIR}/config.yaml。"""
    if path:
        return Path(path)
    env = os.environ.get("TRADER_CONFIG")
    if env:
        return Path(env)
    return PROJECT_DIR / "config.yaml"


def _check_unknown(section: str, raw: dict, allowed: set[str], errors: list[str]) -> None:
    for k in raw:
        if k not in allowed:
            errors.append(f"{section}: 未知的鍵 {k!r}")


def _build_dataclass(cls, section: str, raw: Any, errors: list[str]):
    """依 dataclass 欄位建立實例；未知鍵與型別不符都記錄為錯誤。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        errors.append(f"{section}: 必須是對應表（mapping）")
        return cls()
    names = {f.name for f in fields(cls)}
    _check_unknown(section, raw, names, errors)
    kwargs = {}
    for name, value in raw.items():
        if name not in names:
            continue
        default = getattr(cls(), name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok = isinstance(value, type(default))
        if not ok:
            errors.append(f"{section}.{name}: 型別錯誤（{value!r}）")
            continue
        kwargs[name] = value
    return cls(**kwargs)


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _build_tier(name: str, raw: Any, errors: list[str]) -> TierConfig | None:
    where = f"models.{name}"
    if not isinstance(raw, dict):
        errors.append(f"{where}: 必須是對應表（mapping）")
        return None
    n_before = len(errors)
    if "api_key" in raw:
        errors.append(f"{where}: 不得在設定檔寫明文 api_key；請改用 api_key_env 指向環境變數名稱")
    allowed = {f.name for f in fields(TierConfig)} - {"name"}
    _check_unknown(where, raw, allowed | {"api_key"}, errors)

    backend = raw.get("backend")
    if backend not in BACKENDS:
        errors.append(f"{where}.backend: 必須是 {' | '.join(BACKENDS)} 之一（目前 {backend!r}）")
    if backend == "openai_compat":
        for key in ("base_url", "api_key_env", "model"):
            if not raw.get(key):
                errors.append(f"{where}: openai_compat 需要 {key}")
    elif backend in ("claude_code", "fake"):
        if not raw.get("model"):
            errors.append(f"{where}: {backend} 需要 model")
    if "effort" in raw and raw["effort"] not in EFFORTS:
        errors.append(f"{where}.effort: 必須是 {' | '.join(EFFORTS)} 之一（目前 {raw['effort']!r}）")

    if len(errors) > n_before:
        return None  # 此等級已有錯誤，不再建立實例（避免重複訊息）
    kwargs = {k: v for k, v in raw.items() if k in {f.name for f in fields(TierConfig)} and k != "name"}
    kwargs["name"] = name
    return TierConfig(**kwargs)


def _build_config(raw: dict, source: Path | None) -> Config:
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError("設定檔內容必須是對應表（mapping）")
    _check_unknown("頂層", raw, set(_TOP_KEYS), errors)

    account = _build_dataclass(AccountCfg, "account", raw.get("account"), errors)
    budget = _build_dataclass(BudgetCfg, "budget", raw.get("budget"), errors)
    data = _build_dataclass(DataCfg, "data", raw.get("data"), errors)
    dashboard = _build_dataclass(DashboardCfg, "dashboard", raw.get("dashboard"), errors)
    sandbox = _build_dataclass(SandboxCfg, "sandbox", raw.get("sandbox"), errors)
    approval = _build_dataclass(ApprovalCfg, "approval", raw.get("approval"), errors)
    for t in approval.require_for_tiers:
        if t not in TIERS:
            errors.append(f"approval.require_for_tiers: 等級必須是 {' | '.join(TIERS)} 之一（目前 {t!r}）")

    universe = raw.get("universe", "sp500")
    if not isinstance(universe, str):
        errors.append("universe: 必須是字串")

    schedule_raw = raw.get("schedule") or {}
    if not isinstance(schedule_raw, dict):
        errors.append("schedule: 必須是對應表（mapping）")
        schedule_raw = {}
    schedule = copy.deepcopy(_SCHEDULE_DEFAULTS)
    schedule.update(schedule_raw)

    models_raw = raw.get("models")
    models: dict[str, TierConfig] = {}
    if not isinstance(models_raw, dict):
        errors.append("models: 必須是對應表（mapping），且需含 strong／medium／weak")
    else:
        _check_unknown("models", models_raw, set(TIERS), errors)
        for tier in _TIER_REQUIRED:
            if tier not in models_raw:
                errors.append(f"models: 缺少 {tier}")
                continue
            cfg = _build_tier(tier, models_raw[tier], errors)
            if cfg is not None:
                models[tier] = cfg

    overrides_raw = raw.get("job_tier_overrides") or {}
    overrides: dict[str, str] = {}
    if not isinstance(overrides_raw, dict):
        errors.append("job_tier_overrides: 必須是對應表（mapping）")
    else:
        for job, tier in overrides_raw.items():
            if job not in JOB_TYPES:
                errors.append(f"job_tier_overrides: 未知的工作類型 {job!r}")
            if tier not in TIERS:
                errors.append(f"job_tier_overrides.{job}: 等級必須是 {' | '.join(TIERS)} 之一（目前 {tier!r}）")
            overrides[str(job)] = tier

    if errors:
        raise ConfigError("設定有 %d 個錯誤：\n  - %s" % (len(errors), "\n  - ".join(errors)))

    return Config(
        account=account,
        universe=universe,
        schedule=schedule,
        models=models,
        job_tier_overrides=overrides,
        budget=budget,
        data=data,
        dashboard=dashboard,
        source_path=source,
        sandbox=sandbox,
        approval=approval,
    )


def load_config(path: str | Path | None = None, env_file: str | Path | None = None) -> Config:
    """載入設定。先讀 .env（不覆蓋既有環境變數），再解析 config.yaml 並驗證。"""
    cfg_path = resolve_config_path(path)
    if not cfg_path.is_file():
        raise ConfigError(f"找不到設定檔 {cfg_path}；請執行 cp config.example.yaml config.yaml 後再修改")

    env_path = Path(env_file) if env_file else PROJECT_DIR / ".env"
    load_dotenv(env_path)

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"設定檔 YAML 解析失敗：{e}") from e
    return _build_config(raw, cfg_path)
