"""設定載入與驗證：config.yaml＋.env。秘密只從環境變數讀，設定檔只存環境變數名稱。"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .models import BACKENDS, JOB_TYPES, TIERS, TierConfig
from .paths import PROJECT_DIR

# 使用中模型的訓練資料截止日（D25）；不確定時取較晚者（多標註、不漏標）。換模型時在 config.yaml 改
DEFAULT_MODEL_CUTOFF = "2026-05-31"
_TOP_KEYS = frozenset({"account", "universe", "schedule", "models", "job_tier_overrides", "budget", "data", "dashboard", "sandbox", "approval", "valuation", "model_cutoff"})
_TIER_REQUIRED = ("strong", "medium", "weak")
_SCHEDULE_DEFAULTS: dict[str, Any] = {
    "timezone": "America/New_York",
    "daily_close": "17:30",
    "review_watch": "biweekly",          # 觀察名單檢查間隔：biweekly（14 天）| weekly（7 天）
    "quarterly_screen": ["03-20", "05-20", "08-20", "11-20"],   # 2 月那次改 03-20（D63：等多數 10-K 申報完）
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
    cash_plan_buffer: float = 1.2   # 新買可動用現金以計畫水位＝現金下限 5% × 此倍數計（P-35；設計決策 D15），須 ≥ 1
    # 宿主／使用者單檔權重硬上限（小數，0 < x ≤ 1；None＝無）。制度類（rulebook §9 Q1）：持股權重超過才減碼，
    # 只減到解除限制（設計決策 D01）；因上漲超過等級上限不賣、只停止加碼
    user_hard_cap: float | None = None


@dataclass
class BudgetCfg:
    max_llm_calls_per_run: int = 200         # 0＝不限（A3-M8C-02）
    max_cost_usd_per_run: float = 100.0      # 本 run 模型費用（美元）總和上限；0＝不限
    max_input_tokens_per_run: int = 0        # 輸入 token 總和上限（含快取讀取，會被代理式多回合放大）；0＝不限
    max_llm_calls_per_day: int = 0           # 每日（台北日界，所有 run 合計）模型呼叫次數上限；0＝不限（M5_D4-04）
    max_cost_usd_per_day: float = 0.0        # 每日（台北日界，所有 run 合計）模型費用上限（美元）；0＝不限


@dataclass
class DataCfg:
    sec_user_agent_env: str = "SEC_USER_AGENT"
    alpaca_key_env: str = "ALPACA_API_KEY"
    alpaca_secret_env: str = "ALPACA_SECRET_KEY"
    alpaca_feed: str = "iex"
    fred_key_env: str = "FRED_API_KEY"
    research_price_source: str = "alpaca"   # 初選／深度分析取價：alpaca | yahoo；撮合、限價、ADV 一律 Alpaca（D34）
    adv_feed: str = "iex"                   # daily ADV 用的 Alpaca feed；改 sip 前先確認帳戶權限（M4-D7-2）


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
class ValuationCfg:
    # ASK-1＝B：auto_buy_cap＝min(P2′, k×IV_base)；k 依等級（推論，無回測）。框架注入 cap_input，模型不填
    cap_discount_k: dict = field(default_factory=lambda: {"A": 0.95, "B": 0.85})


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
    valuation: ValuationCfg = field(default_factory=ValuationCfg)
    model_cutoff: str = DEFAULT_MODEL_CUTOFF   # D25：決策日早於此日 → 報告與決策日誌加註「可能有後見之明」

    def secret(self, env_name: str | None) -> str | None:
        """從環境變數讀秘密；未設定或空字串回傳 None。"""
        if not env_name:
            return None
        return os.environ.get(env_name) or None


# 秘密環境變數名稱：模型子程序（claude_code）啟動時從環境移除。來源：DataCfg 預設、.env 內出現的鍵、
# 載入的設定檔中 data.*_env 與各等級 api_key_env（M5-D8-02）
_SECRET_ENV_NAMES: set[str] = {f.default for f in fields(DataCfg) if f.name.endswith("_env")}


def secret_env_names() -> frozenset[str]:
    """目前已知的秘密環境變數名稱。"""
    return frozenset(_SECRET_ENV_NAMES)


def _register_secret_names(cfg: "Config") -> None:
    _SECRET_ENV_NAMES.update(getattr(cfg.data, f.name) for f in fields(DataCfg) if f.name.endswith("_env"))
    _SECRET_ENV_NAMES.update(t.api_key_env for t in cfg.models.values() if t.api_key_env)
    _SECRET_ENV_NAMES.discard("")


def load_dotenv(path: Path) -> dict[str, str]:
    """簡單的 KEY=VALUE 解析。不覆蓋已存在的環境變數；空值略過。回傳實際設定的鍵。
    檔內出現的鍵（不論是否已在環境中）都記為秘密名稱，不傳給模型子程序。"""
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
        end = value.find(value[0], 1) if value[:1] in ("'", '"') else -1
        if end > 0:   # 加引號：取到對應的結尾引號，之後的文字（如 ' # 註解'）忽略（M8-D1-08）
            value = value[1:end]
        else:
            # 未加引號時，" #" 之後視為註解
            if " #" in value:
                value = value.split(" #", 1)[0].rstrip()
        if not key or not value:
            continue
        _SECRET_ENV_NAMES.add(key)
        if key in os.environ:
            continue
        os.environ[key] = value
        set_keys[key] = value
    return set_keys


def resolve_config_path(path: str | Path | None = None) -> Path:
    """設定檔路徑優先序：參數 > 環境變數 TRADER_CONFIG > {PROJECT_DIR}/config.yaml。"""
    # 一律絕對路徑：排程／背景子行程的 cwd 不同，相對路徑會找不到設定檔（M8-D1-02）
    if path:
        return Path(path).expanduser().resolve()
    env = os.environ.get("TRADER_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return (PROJECT_DIR / "config.yaml").resolve()


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
        if default is None:   # 選填數字（預設 None＝不設）
            ok = value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))
        elif isinstance(default, bool):
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
    elif backend in ("claude_code", "fake", "hark_queue"):
        if not raw.get("model"):
            errors.append(f"{where}: {backend} 需要 model")
    if "effort" in raw and raw["effort"] not in EFFORTS:
        errors.append(f"{where}.effort: 必須是 {' | '.join(EFFORTS)} 之一（目前 {raw['effort']!r}）")

    if len(errors) > n_before:
        return None  # 此等級已有錯誤，不再建立實例（避免重複訊息）
    kwargs = {k: v for k, v in raw.items() if k in {f.name for f in fields(TierConfig)} and k != "name"}
    kwargs["name"] = name
    return TierConfig(**kwargs)


def _check_schedule(s: dict, errors: list[str]) -> None:
    """schedule 值在載入時驗證，不要等排程執行到才例外（M8-D7-07）。"""
    tz = s.get("timezone")
    try:
        ZoneInfo(tz)
    except Exception:   # noqa: BLE001 - 非字串、格式錯、找不到時區都算設定錯
        errors.append(f"schedule.timezone: 不是有效的 IANA 時區（目前 {tz!r}；例 America/New_York）")
    dc = s.get("daily_close")
    if isinstance(dc, int) and not isinstance(dc, bool):
        errors.append(f"schedule.daily_close: 請加引號，例 \"17:30\"（YAML 把不加引號的 HH:MM 讀成整數 {dc}）")
    elif not (isinstance(dc, str) and re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", dc)):
        errors.append(f"schedule.daily_close: 必須是 \"HH:MM\" 字串（目前 {dc!r}）")
    qs = s.get("quarterly_screen")
    if not isinstance(qs, list) or not qs or not all(isinstance(q, str) and _valid_mmdd(q) for q in qs):
        errors.append(f"schedule.quarterly_screen: 必須是 \"MM-DD\" 字串清單（目前 {qs!r}）")
    mf = s.get("monthly_freeze")
    if isinstance(mf, bool) or not str(mf).isdigit() or not 1 <= int(str(mf)) <= 28:
        errors.append(f"schedule.monthly_freeze: 必須是 1–28 的日期（目前 {mf!r}）")


def _valid_mmdd(text: str) -> bool:
    if not re.fullmatch(r"\d\d-\d\d", text):
        return False
    try:
        date(2001, int(text[:2]), int(text[3:]))   # 非閏年：02-29 不收（平年會被排程略過）
    except ValueError:
        return False
    return True


def _build_config(raw: dict, source: Path | None) -> Config:
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError("設定檔內容必須是對應表（mapping）")
    _check_unknown("頂層", raw, set(_TOP_KEYS), errors)

    account = _build_dataclass(AccountCfg, "account", raw.get("account"), errors)
    if account.cash_plan_buffer < 1:
        errors.append(f"account.cash_plan_buffer: 須 ≥ 1（5% 是硬下限；目前 {account.cash_plan_buffer!r}）")
    if account.user_hard_cap is not None and not 0 < account.user_hard_cap <= 1:
        errors.append(f"account.user_hard_cap: 須為 0 < x ≤ 1 的小數或 null（目前 {account.user_hard_cap!r}）")
    # 範圍在載入時驗證，不要等 daily 撮合中途才拋 BrokerError（A3-M8D-06）
    if not account.initial_cash > 0:
        errors.append(f"account.initial_cash: 須 > 0（目前 {account.initial_cash!r}）")
    if not 0 <= account.slippage_bps < 1000:
        errors.append(f"account.slippage_bps: 須為 0 ≤ x < 1000 的基點（目前 {account.slippage_bps!r}）")
    if not account.commission_per_order >= 0:
        errors.append(f"account.commission_per_order: 須 ≥ 0（目前 {account.commission_per_order!r}）")
    budget = _build_dataclass(BudgetCfg, "budget", raw.get("budget"), errors)
    for f_ in fields(BudgetCfg):   # 0＝不限；負值沒有意義（A3-M8C-02）
        if getattr(budget, f_.name) < 0:
            errors.append(f"budget.{f_.name}: 須 ≥ 0（0＝不限；目前 {getattr(budget, f_.name)!r}）")
    data = _build_dataclass(DataCfg, "data", raw.get("data"), errors)
    if data.research_price_source not in ("alpaca", "yahoo"):
        errors.append(f"data.research_price_source: 必須是 alpaca | yahoo（目前 {data.research_price_source!r}）")
    dashboard = _build_dataclass(DashboardCfg, "dashboard", raw.get("dashboard"), errors)
    sandbox = _build_dataclass(SandboxCfg, "sandbox", raw.get("sandbox"), errors)
    approval = _build_dataclass(ApprovalCfg, "approval", raw.get("approval"), errors)
    valuation = _build_dataclass(ValuationCfg, "valuation", raw.get("valuation"), errors)
    kd = valuation.cap_discount_k
    if not kd or not all(g in ("A", "B", "C", "D") and isinstance(k, (int, float)) and not isinstance(k, bool)
                         and 0 < k <= 1 for g, k in kd.items()):
        errors.append(f"valuation.cap_discount_k: 須為 {{等級 A/B/C/D: (0, 1] 的數字}}（目前 {kd!r}）")
    for t in approval.require_for_tiers:
        if t not in TIERS:
            errors.append(f"approval.require_for_tiers: 等級必須是 {' | '.join(TIERS)} 之一（目前 {t!r}）")

    model_cutoff = raw.get("model_cutoff", DEFAULT_MODEL_CUTOFF)
    model_cutoff = "" if model_cutoff is None else str(model_cutoff)   # YAML 未加引號的日期會讀成 date
    if model_cutoff and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", model_cutoff):
        errors.append(f"model_cutoff: 須為 YYYY-MM-DD 或空字串（目前 {model_cutoff!r}）")

    universe = raw.get("universe", "sp500")
    if universe != "sp500":   # 程式只實作 S&P 500 股票池（M8-D9-05）
        errors.append(f"universe: 目前只支援 sp500（目前 {universe!r}）；自訂名單請用 state/universe_override.txt")

    schedule_raw = raw.get("schedule") or {}
    if not isinstance(schedule_raw, dict):
        errors.append("schedule: 必須是對應表（mapping）")
        schedule_raw = {}
    schedule = copy.deepcopy(_SCHEDULE_DEFAULTS)
    _check_unknown("schedule", schedule_raw, set(_SCHEDULE_DEFAULTS) | {"review_holdings"}, errors)
    schedule.update(schedule_raw)
    _check_schedule(schedule, errors)
    if schedule.get("review_watch") not in ("biweekly", "weekly"):   # 排程讀這個鍵（M7-D2-09）
        errors.append(f"schedule.review_watch: 必須是 biweekly 或 weekly（目前 {schedule.get('review_watch')!r}）")
    if schedule.pop("review_holdings", "daily") != "daily":   # 持股固定每日隨 daily 檢查，不可設定
        errors.append("schedule.review_holdings: 持股固定每天隨 daily 檢查（會派模型工作），不可改；請刪除此鍵")

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
        valuation=valuation,
        model_cutoff=model_cutoff,
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
    cfg = _build_config(raw, cfg_path)
    _register_secret_names(cfg)
    return cfg
