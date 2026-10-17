"""共用資料類別與常數。所有模組都從這裡匯入，不要另行定義同名類別。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JOB_TYPES = ("DOSSIER", "TRIAGE", "PRICE", "THESIS", "FULL_A", "FULL_B", "RED_TEAM", "CORP_ACTION", "HOLD_REVIEW")
TIERS = ("strong", "medium", "weak")
BACKENDS = ("claude_code", "openai_compat", "fake")

# 沙箱內不得出現價格的工作（routing defaults.price_isolation；DOSSIER 也不給價格檔）
PRICE_ISOLATED_JOBS = frozenset({"DOSSIER", "TRIAGE", "THESIS", "FULL_A"})


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    TERMINAL = frozenset({"done", "failed", "skipped"})


@dataclass
class TierConfig:
    name: str                       # strong | medium | weak
    backend: str                    # claude_code | openai_compat | fake
    model: str
    base_url: str | None = None     # openai_compat 必填
    api_key_env: str | None = None  # openai_compat 必填；只存環境變數名稱
    max_output_tokens: int = 16000
    temperature: float = 0.0
    timeout_s: int = 1800
    concurrency: int = 2
    json_mode: bool = True
    max_context_tokens: int = 100000
    claude_bin: str = "claude"      # claude_code 用
    effort: str = "medium"          # claude_code 用：--effort（low|medium|high|xhigh|max）；明確指定，不受環境或個人設定影響


@dataclass
class JobSpec:
    job_id: str                     # f"{run_id}:{ticker}:{job_type}:{n}"，可重放、冪等
    run_id: str
    job_type: str                   # JOB_TYPES 之一
    ticker: str
    asof: str                       # YYYY-MM-DD
    deps: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)  # 工作參數（輸入檔、holding、price_file…）
    tier: str | None = None         # router 決定
    sandbox: Path | None = None     # 本工作的隔離輸入／輸出目錄
    log_dir: Path | None = None     # 原始提示與回應存放處（state/llm_calls/{run_id}/）
    prompt_text: str = ""           # context.build_prompt 產出
    attempt: int = 0                # 第幾次嘗試（驗證重試 +1）


@dataclass
class LLMResult:
    text: str                                 # 模型最終文字
    parsed_json: dict[str, Any] | None        # 抽出的 JSON（已 materialize files）
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    latency_s: float = 0.0
    raw_path: Path | None = None              # 原始回應存檔
    cost_usd: float | None = None
    files_written: list[str] = field(default_factory=list)  # 寫進沙箱的檔名（相對路徑）
