"""OpenAI 相容後端：以 httpx 呼叫 chat/completions。

**未驗證**：只有單元測試（假 HTTP），從未對真實服務跑過正式工作；正式運作用 claude_code 後端。

金鑰只從環境變數讀取，不寫入存檔，錯誤訊息中若出現金鑰字串會被遮蔽。
"""
from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

import httpx

from ..models import JobSpec, LLMResult, TierConfig
from .base import FatalLLMError, LLMTimeout, TransportError, extract_json, materialize_files, write_raw

_FATAL_STATUS = frozenset({400, 401, 403, 404})


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After 為秒數時解析；HTTP-date 格式不解析（回 None，改用退避）。"""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class OpenAICompatRunner:
    backend = "openai_compat"

    def __init__(self, tier: TierConfig, client: httpx.Client | None = None, env: Mapping[str, str] | None = None):
        self.tier = tier
        self.client = client or httpx.Client(timeout=float(tier.timeout_s))
        self.env: Mapping[str, str] = os.environ if env is None else env

    def _key(self) -> str:
        name = self.tier.api_key_env
        key = self.env.get(name) if name else None
        if not key:
            raise FatalLLMError(f"缺少金鑰：請設定環境變數 {name or '（未指定 api_key_env）'}")
        return key

    @staticmethod
    def _redact(text: str, key: str) -> str:
        return text.replace(key, "***")

    def run(self, job: JobSpec) -> LLMResult:
        key = self._key()
        if not self.tier.base_url:
            raise FatalLLMError("openai_compat 需要 base_url")
        url = self.tier.base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": self.tier.model,
            "messages": [{"role": "user", "content": job.prompt_text}],
            "temperature": self.tier.temperature,
            "max_tokens": self.tier.max_output_tokens,
        }
        if self.tier.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {key}"}
        write_raw(job.log_dir, job, "prompt.txt", job.prompt_text)

        t0 = time.monotonic()
        try:
            resp = self.client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMTimeout("HTTP 逾時") from e   # D47：逾時重試 1 次（與 claude 逾時同類，A3-M5B-06）
        except httpx.HTTPError as e:
            raise TransportError(f"連線失敗（{type(e).__name__}）") from e
        latency = time.monotonic() - t0

        code = resp.status_code
        if code == 429:
            raise TransportError("HTTP 429（限流）", retry_after=_parse_retry_after(resp.headers.get("Retry-After")))
        if code in _FATAL_STATUS:
            raise FatalLLMError(f"HTTP {code}：{self._redact(resp.text[:300], key)}")
        if code >= 400:
            raise TransportError(f"HTTP {code}：{self._redact(resp.text[:300], key)}")

        raw_path = write_raw(job.log_dir, job, "response.json", self._redact(resp.text, key))
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise TransportError("回應格式不符（缺少 choices[0].message.content）") from e

        usage = data.get("usage") or {}
        parsed = extract_json(text)
        files = materialize_files(parsed, job.sandbox)
        return LLMResult(
            text=text,
            parsed_json=parsed,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=self.tier.model,
            latency_s=latency,
            raw_path=raw_path,
            cost_usd=None,
            files_written=files,
        )
