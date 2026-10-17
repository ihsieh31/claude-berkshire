"""Claude Code 後端：以 `claude -p` 子程序執行一次工作。提示經 stdin 傳入，cwd 為工作沙箱。"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ..jobs.isolation import IsolationError, check_sandbox, deny_roots, settings_json
from ..models import JobSpec, LLMResult, TierConfig
from .base import FatalLLMError, TransportError, extract_json, write_raw


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """沙箱內所有檔案的 (mtime_ns, size)，用來偵測執行期間新增或修改的檔案。"""
    snap: dict[str, tuple[int, int]] = {}
    for p in root.rglob("*"):
        if p.is_file():
            st = p.stat()
            snap[p.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
    return snap


class ClaudeCodeRunner:
    backend = "claude_code"

    def __init__(self, tier: TierConfig, runner_fn: Callable[..., Any] = subprocess.run,
                 denies: list[Path] | None = None):
        self.tier = tier
        self.runner_fn = runner_fn  # 測試時注入假函式，不會真的呼叫 claude
        # 模型不得讀寫的目錄（家目錄、暫存、專案、state…）；沙箱必須在這些目錄之外
        self.denies = list(denies) if denies is not None else deny_roots()

    def command(self) -> list[str]:
        return [
            self.tier.claude_bin, "-p",
            "--model", self.tier.model,
            "--effort", self.tier.effort,
            "--output-format", "json",
            "--allowedTools", "Read,Write,Glob,Grep",
            "--disallowedTools", "Bash,WebFetch,WebSearch",
            "--permission-mode", "acceptEdits",
            # 隔離：不載入使用者層設定（hooks、外掛）、不載入 MCP、不保存 session
            "--setting-sources", "project,local",
            "--strict-mcp-config",
            "--no-session-persistence",
            # 沙箱外讀寫封鎖（Read 規則同時涵蓋 Glob／Grep）
            "--settings", settings_json(self.denies),
        ]

    def run(self, job: JobSpec) -> LLMResult:
        if job.sandbox is None:
            raise FatalLLMError(f"工作 {job.job_id} 沒有沙箱，無法以 claude_code 執行")
        sandbox = Path(job.sandbox)
        try:
            check_sandbox(sandbox, self.denies)
        except IsolationError as e:
            raise FatalLLMError(str(e)) from e
        before = _snapshot(sandbox)
        write_raw(job.log_dir, job, "prompt.txt", job.prompt_text)

        t0 = time.monotonic()
        try:
            proc = self.runner_fn(
                self.command(),
                input=job.prompt_text,
                cwd=str(sandbox),
                timeout=self.tier.timeout_s,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as e:
            raise FatalLLMError(f"找不到 claude 執行檔：{self.tier.claude_bin}") from e
        except subprocess.TimeoutExpired as e:
            raise TransportError(f"claude 逾時（{self.tier.timeout_s}s）") from e
        latency = time.monotonic() - t0

        stdout = proc.stdout or ""
        raw_path = write_raw(job.log_dir, job, "response.json", stdout)

        # 不論 stderr 內容是限流、過載或其他原因，皆視為可重試的傳輸錯誤
        if proc.returncode != 0:
            err = (proc.stderr or "")[:500]
            raise TransportError(f"claude 結束碼 {proc.returncode}：{err}")

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise TransportError("claude 輸出不是合法 JSON") from e
        if not isinstance(data, dict):
            raise TransportError("claude 輸出的 JSON 不是物件")
        if data.get("is_error"):
            raise TransportError(f"claude 回報錯誤：{str(data.get('result') or '')[:500]}")

        usage = data.get("usage") or {}

        def _int(key: str) -> int:
            return int(usage.get(key) or 0)

        input_tokens = _int("input_tokens") + _int("cache_creation_input_tokens") + _int("cache_read_input_tokens")
        output_tokens = _int("output_tokens")
        text = str(data.get("result") or "")
        parsed = extract_json(text)

        after = _snapshot(sandbox)
        files_written = sorted(rel for rel, sig in after.items() if before.get(rel) != sig)

        return LLMResult(
            text=text,
            parsed_json=parsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=self.tier.model,
            latency_s=latency,
            raw_path=raw_path,
            cost_usd=data.get("total_cost_usd"),
            files_written=files_written,
        )
