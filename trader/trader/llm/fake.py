"""假模型：不連網、不呼叫外部程式，依設定回傳預先準備好的輸出（測試與乾跑用）。"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from ..models import JobSpec, LLMResult
from .base import FatalLLMError, TransportError, extract_json, materialize_files, write_raw


class FakeRunner:
    """查找順序：responses[(job_type, ticker)] → responses[job_type] → fixtures
    `{JOB_TYPE}__{TICKER}.json` → fixtures `{JOB_TYPE}.json` → responses["*"]；都沒有則 FatalLLMError。

    值的處理：list → 依 job.attempt 取第 i 個（超過取最後一個）；callable → value(job)；
    TransportError 實例 → 直接拋出（用來測試重試）；dict → 轉成 JSON 文字；str → 原文。
    """

    backend = "fake"

    def __init__(self, responses: dict | None = None, fixtures_dir: Path | None = None, model: str = "fake"):
        self.responses: dict[Any, Any] = dict(responses or {})
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir is not None else None
        self.model = model
        self.calls: list[JobSpec] = []  # 每次呼叫的工作複本，供測試檢查

    def _lookup(self, job: JobSpec) -> Any:
        r = self.responses
        if (job.job_type, job.ticker) in r:
            return r[(job.job_type, job.ticker)]
        if job.job_type in r:
            return r[job.job_type]
        if self.fixtures_dir is not None:
            for name in (f"{job.job_type}__{job.ticker}.json", f"{job.job_type}.json"):
                p = self.fixtures_dir / name
                if p.exists():
                    return p.read_text(encoding="utf-8")
        if "*" in r:
            return r["*"]
        raise FatalLLMError(f"FakeRunner 找不到回應：{job.job_type}/{job.ticker}")

    @staticmethod
    def _resolve(value: Any, job: JobSpec) -> str:
        if isinstance(value, list):
            if not value:
                raise FatalLLMError("FakeRunner 的回應清單是空的")
            value = value[min(job.attempt, len(value) - 1)]
        if callable(value):
            value = value(job)
        if isinstance(value, TransportError):
            raise value
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, indent=1)
        if isinstance(value, str):
            return value
        raise FatalLLMError(f"FakeRunner 不支援的回應型別：{type(value).__name__}")

    def run(self, job: JobSpec) -> LLMResult:
        self.calls.append(copy.copy(job))
        t0 = time.monotonic()
        text = self._resolve(self._lookup(job), job)
        write_raw(job.log_dir, job, "prompt.txt", job.prompt_text)
        raw_path = write_raw(job.log_dir, job, "response.txt", text)
        parsed = extract_json(text)
        files = materialize_files(parsed, job.sandbox)
        return LLMResult(
            text=text,
            parsed_json=parsed,
            input_tokens=len(job.prompt_text) // 4,
            output_tokens=len(text) // 4,
            model=self.model,
            latency_s=time.monotonic() - t0,
            raw_path=raw_path,
            cost_usd=0.0,
            files_written=files,
        )
