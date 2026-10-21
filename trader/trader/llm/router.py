"""工作 → 等級 → Runner 的對應。規則見 ../routing/model_routing.json。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import JobSpec, TierConfig
from .base import Runner
from ..jobs.isolation import deny_roots
from .claude_code import ClaudeCodeRunner
from .fake import FakeRunner
from .openai_compat import OpenAICompatRunner

if TYPE_CHECKING:  # 只供型別提示，執行期不匯入 config
    from ..config import Config


def build_runner(tier: TierConfig, extra_deny: list[str] | None = None, state_dir: Path | None = None) -> Runner:
    """依等級設定的 backend 建立 Runner。state_dir：以 --state-dir 指到專案外時也要封鎖。"""
    if tier.backend == "claude_code":
        return ClaudeCodeRunner(tier, denies=deny_roots(extra_deny, state_dir))
    if tier.backend == "openai_compat":
        return OpenAICompatRunner(tier)
    if tier.backend == "hark_queue":
        from .hark_queue import HarkQueueRunner  # 延遲匯入：只有選用 hark_queue 時才需要此模組（D55）
        return HarkQueueRunner(tier, denies=deny_roots(extra_deny, state_dir))
    if tier.backend == "fake":
        return FakeRunner(model=tier.model)
    raise ValueError(f"未知的 backend：{tier.backend!r}")


class Router:
    def __init__(self, config: "Config", routing_path: Path, dry_run: bool = False, fake: FakeRunner | None = None,
                 state_dir: Path | None = None):
        self.config = config
        self.state_dir = state_dir
        self.routing = json.loads(Path(routing_path).read_text(encoding="utf-8"))
        self.dry_run = dry_run
        self._fake = fake
        self._runners: dict[str, Runner] = {}

    def _jobs_entry(self, job_type: str) -> dict:
        return self.routing.get("jobs", {}).get(job_type, {})

    def _routed_tier(self, job_type: str, holding: bool | None = None) -> str:
        """覆寫優先；否則查 routing 表。THESIS 的 model 是依 holding 分流的 dict。"""
        override = self.config.job_tier_overrides.get(job_type)
        if override:
            tier = override
        else:
            spec = self._jobs_entry(job_type).get("model")
            if isinstance(spec, dict):
                tier = spec["HOLDING" if holding else "其他"]
            else:
                tier = spec
        if tier is None or tier == "none":
            raise ValueError(f"工作 {job_type} 沒有可用的模型等級（routing 值為 none 或缺漏）")
        return tier

    def tier_for(self, job: JobSpec) -> str:
        return self._routed_tier(job.job_type, holding=bool(job.params.get("holding")))

    def runner_for(self, tier: str) -> Runner:
        if self.dry_run:
            if self._fake is None:
                self._fake = FakeRunner()
            return self._fake
        if tier not in self._runners:
            self._runners[tier] = build_runner(self.tier_config(tier), getattr(getattr(self.config, "sandbox", None), "extra_deny", None),
                                               self.state_dir)
        return self._runners[tier]

    def cancel_all(self) -> None:
        """引擎被中斷時：請已建立的 Runner 結束執行中的呼叫（有 cancel 方法者，例如 claude_code 結束子行程群組）。"""
        for r in list(self._runners.values()) + ([self._fake] if self._fake is not None else []):
            cancel = getattr(r, "cancel", None)
            if callable(cancel):
                cancel()

    def tier_config(self, tier: str) -> TierConfig:
        try:
            return self.config.models[tier]
        except KeyError:
            raise ValueError(f"未知的模型等級：{tier!r}") from None

    def max_context_tokens(self, job_type: str, tier: str | None = None) -> int:
        """工作的上下文上限 = min(routing 工作層級或預設值, 等級上限)。

        未指定 tier 時，取該工作可能用到的所有等級中的最小上限（保守）。
        """
        base = self._jobs_entry(job_type).get(
            "max_context_tokens", self.routing.get("defaults", {}).get("max_context_tokens", 100000)
        )
        override = self.config.job_tier_overrides.get(job_type)
        if tier is not None:
            tiers = [tier]
        elif override:
            tiers = [override]
        else:
            spec = self._jobs_entry(job_type).get("model")
            tiers = list(spec.values()) if isinstance(spec, dict) else [spec]
        caps = [self.tier_config(t).max_context_tokens for t in tiers if t and t != "none"]
        return min([base, *caps])
