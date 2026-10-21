"""Claude Code 後端：以 `claude -p` 子程序執行一次工作。提示經 stdin 傳入，cwd 為工作沙箱。"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..jobs.isolation import (CLAUDE_CONFIG_NAMES, PRICED_SUFFIX, IsolationError, check_sandbox, deny_roots,
                              priced_root, settings_json)
from ..models import PRICE_ISOLATED_JOBS, JobSpec, LLMResult, TierConfig
from .base import FatalLLMError, LLMTimeout, TransportError, extract_json, write_raw


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """沙箱內所有檔案的 (mtime_ns, size)，用來偵測執行期間新增或修改的檔案。"""
    snap: dict[str, tuple[int, int]] = {}
    for p in root.rglob("*"):
        if p.is_file():
            st = p.stat()
            snap[p.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
    return snap


def _clear_claude_config(sandbox: Path) -> None:
    """刪除沙箱內 Claude Code 會載入的專案設定／記憶檔（前次嘗試可能被注入 hooks 等）。每次呼叫前執行。
    沙箱是 {root}/{run}/{ticker}/_X_n 結構時，往上到沙箱根（含）各層也清（CLAUDE.md 會往上層載入）；沙箱根以外不動。"""
    sb = Path(sandbox).resolve()
    for p in (d / name for d in ([sb] if _sandbox_roots(sb) is None else [sb, *sb.parents[:3]])
              for name in CLAUDE_CONFIG_NAMES):
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()


def _child_env() -> dict[str, str]:
    """子程序環境：移除交易／資料／API 金鑰等秘密變數（ANTHROPIC_／CLAUDE_ 開頭者為 claude 自身登入所需，保留）。"""
    from ..config import secret_env_names  # 延遲匯入：llm 層執行期不依賴 config 模組載入順序
    drop = secret_env_names()
    return {k: v for k, v in os.environ.items()
            if k not in drop or k.startswith(("ANTHROPIC_", "CLAUDE_"))}


_LIVE: set[subprocess.Popen] = set()   # 執行中的 claude 行程（各自一個行程群組）
_LIVE_LOCK = threading.Lock()


def _killpg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_in_group(cmd: list[str], *, input: str | None = None, timeout: float | None = None,
                 cancelled: threading.Event | None = None, **kw: Any) -> subprocess.CompletedProcess:
    """subprocess.run 的替代（M5_D4-02）：子行程開新的 session／行程群組（終端機 Ctrl-C 不會直接打斷它，
    由引擎收尾），逾時或任何例外（含 KeyboardInterrupt）時 killpg 整個群組（claude 衍生的孫行程一併結束），再拋原例外。
    cancelled：檢查與啟動在 cancel_running 同一把鎖下，已取消就不再啟動新行程（M5-RS4-02）。"""
    kw.pop("capture_output", None)
    with _LIVE_LOCK:
        if cancelled is not None and cancelled.is_set():
            raise TransportError("claude 呼叫已取消（行程中斷），不再啟動")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, **kw)
        _LIVE.add(proc)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except BaseException:
        _killpg(proc)
        proc.wait()
        raise
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def cancel_running(cancelled: threading.Event | None = None) -> None:
    """結束所有執行中的 claude 行程群組（引擎被中斷時呼叫；被結束的呼叫以非零結束碼回報）。
    cancelled 給定時在同一把鎖下設定：之後的 run_in_group 不再啟動新行程。"""
    with _LIVE_LOCK:
        if cancelled is not None:
            cancelled.set()
        procs = list(_LIVE)
    for p in procs:
        _killpg(p)


def _usage(data: Any) -> tuple[int, int, float | None]:
    """claude JSON 的用量：(輸入 token（含快取建立／讀取）, 輸出 token, total_cost_usd)。"""
    usage = (data.get("usage") if isinstance(data, dict) else None) or {}

    def _int(key: str) -> int:
        try:
            return int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    cost = data.get("total_cost_usd") if isinstance(data, dict) else None
    return (_int("input_tokens") + _int("cache_creation_input_tokens") + _int("cache_read_input_tokens"),
            _int("output_tokens"), cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)


def _sandbox_roots(sandbox: Path) -> tuple[Path, Path, bool] | None:
    """沙箱 {root}/{run_id}/{ticker}/_{JOB}_{n} → (隔離根, priced 根, 沙箱是否在 priced 根)；不是此結構則 None。"""
    sb = Path(sandbox).resolve()
    if not sb.name.startswith("_") or len(sb.parents) < 4:
        return None
    root = sb.parents[2]
    if root.name.endswith(PRICED_SUFFIX):
        return root.with_name(root.name[: -len(PRICED_SUFFIX)]), root, True
    return root, priced_root(root), False


class ClaudeCodeRunner:
    backend = "claude_code"

    def __init__(self, tier: TierConfig, runner_fn: Callable[..., Any] = run_in_group,
                 denies: list[Path] | None = None):
        self.tier = tier
        self.runner_fn = runner_fn  # 測試時注入假函式，不會真的呼叫 claude
        # 模型不得讀寫的目錄（家目錄、暫存、專案、state…）；沙箱必須在這些目錄之外
        self.denies = list(denies) if denies is not None else deny_roots()
        self._cancelled = threading.Event()   # cancel() 後本 Runner 不再啟動 claude（同 hark_queue，M5-RS4-02）

    def cancel(self) -> None:
        cancel_running(self._cancelled)

    def command(self, sandbox: Path | None = None, job_type: str | None = None) -> list[str]:
        """sandbox／job_type 給定時：隔離工作封鎖 priced 根、看價格工作封鎖隔離根（兩邊互不可讀寫）。"""
        denies = list(self.denies)
        roots = _sandbox_roots(sandbox) if sandbox is not None else None
        if roots is not None:
            iso_root, pr_root, in_priced = roots
            if job_type in PRICE_ISOLATED_JOBS:
                denies.append(pr_root)          # 隔離工作：兄弟看價格沙箱（*_price.json、G8 比較檔）讀不到
            elif in_priced:
                denies.append(iso_root)         # 看價格工作：不得讀寫隔離工作的沙箱（例如塞價格檔進去）
        return [
            self.tier.claude_bin, "-p",
            "--model", self.tier.model,
            "--effort", self.tier.effort,
            "--output-format", "json",
            "--allowedTools", "Read,Write,Glob,Grep",
            "--disallowedTools", "Bash,WebFetch,WebSearch",
            "--permission-mode", "acceptEdits",
            # 隔離：不載入使用者層設定（hooks、外掛）、不載入 MCP、不保存 session。
            # 沙箱內的專案層設定每次呼叫前清除、且模型不得寫入；--settings 另以 disableAllHooks 停用 hooks
            "--setting-sources", "project,local",
            "--strict-mcp-config",
            "--no-session-persistence",
            # 沙箱外讀寫封鎖（Read 規則同時涵蓋 Glob／Grep）
            "--settings", settings_json(denies),
        ]

    def run(self, job: JobSpec) -> LLMResult:
        if job.sandbox is None:
            raise FatalLLMError(f"工作 {job.job_id} 沒有沙箱，無法以 claude_code 執行")
        sandbox = Path(job.sandbox)
        try:
            check_sandbox(sandbox, self.denies)
        except IsolationError as e:
            raise FatalLLMError(str(e)) from e
        # 驗證重試與傳輸重試都沿用同一沙箱：每次呼叫前清掉可能被注入的專案設定（M5-D8-01）
        _clear_claude_config(sandbox)
        before = _snapshot(sandbox)
        write_raw(job.log_dir, job, "prompt.txt", job.prompt_text)

        t0 = time.monotonic()
        try:
            proc = self.runner_fn(
                self.command(sandbox, job.job_type),
                input=job.prompt_text,
                cwd=str(sandbox),
                timeout=self.tier.timeout_s,
                capture_output=True,
                text=True,
                env=_child_env(),
                **({"cancelled": self._cancelled} if self.runner_fn is run_in_group else {}),
            )
        except FileNotFoundError as e:
            raise FatalLLMError(f"找不到 claude 執行檔：{self.tier.claude_bin}") from e
        except subprocess.TimeoutExpired as e:
            raise LLMTimeout(f"claude 逾時（{self.tier.timeout_s}s）") from e
        latency = time.monotonic() - t0

        stdout = proc.stdout or ""
        raw_path = write_raw(job.log_dir, job, "response.json", stdout)
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None
        input_tokens, output_tokens, cost = _usage(data)

        def _fail(msg: str) -> TransportError:   # 失敗呼叫仍帶回已花的用量（M5-D1-07）
            return TransportError(msg, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
                                  raw_path=raw_path)

        # 不論 stderr 內容是限流、過載或其他原因，皆為傳輸錯誤；是否重試由 call_with_retry 依類別決定（D47）
        if proc.returncode != 0:
            if proc.stderr:   # 完整 stderr 另存（錯誤訊息只截 500 字，M5-D5-08）
                write_raw(job.log_dir, job, "stderr.txt", proc.stderr)
            err = (proc.stderr or "")[:500]
            raise _fail(f"claude 結束碼 {proc.returncode}：{err}")
        if data is None:
            raise _fail("claude 輸出不是合法 JSON")
        if not isinstance(data, dict):
            raise _fail("claude 輸出的 JSON 不是物件")
        if data.get("is_error"):
            raise _fail(f"claude 回報錯誤：{str(data.get('result') or '')[:500]}")

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
            cost_usd=cost,
            files_written=files_written,
        )
