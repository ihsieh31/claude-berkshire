"""Hark 佇列後端：把一次模型工作交給外部代理（Hark 子代理）作答。

用途：在沒有 `claude` CLI 的環境（例如 Hark 雲端工作區）以 Hark 子代理扮演強／中／弱模型。
框架其餘部分（沙箱、價格隔離、輸出驗證、gate3_check、cap_calc 覆核、寫回）完全不變。

協定（檔案交換，可在行程被中斷後續跑）：
  {queue}/{job_key}/request.json   工作描述（job_id、job_type、ticker、tier、sandbox、建立時間）
  {queue}/{job_key}/prompt.txt     與 claude_code 後端相同的完整提示（stdin 內容）
  {queue}/{job_key}/response.txt   外部代理的「最後回覆」全文（等同 claude -p 的 result 欄）
  {queue}/{job_key}/out/            提示要求「寫進工作目錄」的檔案，一律寫在這裡（相對路徑不變）；
                                   取用回覆時由本後端複製進沙箱（jobs retry、重跑時沙箱會重建，寫在沙箱會遺失）
  {queue}/{job_key}/meta.json      選填：{"model": "...", "input_tokens": n, "output_tokens": n}
外部代理只能「讀」sandbox 目錄、「寫」out/ 與 response.txt；寫完 response.txt 即代表完成（最後才寫）。
response.txt 請先寫 response.txt.tmp 再改名（原子出現）；本後端讀到抽不出 JSON 時會再等一輪重讀一次（A3-M5E-02）。

job_key＝job_id 安全化＋_a{attempt}；同一 attempt 已有 response.txt → 直接取用（續跑不重問）。
佇列 prompt.txt 與這次的提示不同（jobs retry、修好資料包後重跑）→ 舊目錄改名 {job_key}.retired-<UTC 時間> 後重新派題（M5-D1-04）。
代理式提示只列檔名，所以 request.json 另記派題時沙箱每個檔的 sha256（inputs）；記錄過的輸入檔不見或內容變了
（同名資料包改了內容）也照上面退休重派。與 out/ 同路徑的檔（取答時複製進沙箱）等於 out/ 版本也算沒變；
沙箱多出的檔不比（檔名清單已在提示裡）；舊格式 request.json 沒有 inputs 時不檢查（RM5-4）。
沒有 meta.json 時 token 記 0、cost_usd 記 None（不以字數估算冒充實際用量，M5-D5-05）。
佇列目錄由環境變數 HARK_QUEUE_DIR 指定（預設 {sandbox 根目錄}/../hark-queue）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..jobs.isolation import IsolationError, check_sandbox, deny_roots
from ..models import JobSpec, LLMResult, TierConfig
from .base import FatalLLMError, TransportError, extract_json, write_raw

POLL_S = 5.0


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _sandbox_inputs(sandbox: Path) -> dict[str, str]:
    """沙箱每個檔的相對路徑 → sha256（派題時記入 request.json）。"""
    return {p.relative_to(sandbox).as_posix(): _file_sha(p) for p in sorted(sandbox.rglob("*")) if p.is_file()}


def _inputs_changed(qd: Path, sandbox: Path) -> bool:
    """派題時記錄的輸入檔是否不見或內容不同（RM5-4）。舊格式（無 inputs）或讀不到 → 不檢查。"""
    try:
        rec = json.loads((qd / "request.json").read_text(encoding="utf-8")).get("inputs")
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(rec, dict):
        return False
    for rel, h in rec.items():
        p = sandbox / rel
        if not p.is_file():
            return True
        cur = _file_sha(p)
        if cur != h and not ((qd / "out" / rel).is_file() and _file_sha(qd / "out" / rel) == cur):
            return True
    return False


def job_key(job: JobSpec) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", job.job_id) + f"_a{job.attempt}"


def queue_dir(sandbox: Path | None = None) -> Path:
    env = os.environ.get("HARK_QUEUE_DIR")
    if env:
        return Path(env)
    if sandbox is None:
        raise FatalLLMError("未設定 HARK_QUEUE_DIR")
    # sandbox＝{root}/{run_id}/{ticker}/_{JOB}_{n} → {root}/../hark-queue
    return Path(sandbox).resolve().parents[3] / "hark-queue"


class HarkQueueRunner:
    backend = "hark_queue"
    resumable = True   # 佇列檔在行程中斷後仍在：續跑沿用原沙箱與提示（Runner.resumable，M5_D9-02）

    def __init__(self, tier: TierConfig, denies: list[Path] | None = None,
                 sleep: Callable[[float], None] | None = None, poll_s: float = POLL_S):
        self.tier = tier
        self.denies = list(denies) if denies is not None else deny_roots()
        self._cancelled = threading.Event()
        self.sleep = sleep or self._cancelled.wait   # 預設：等候輪詢間隔，cancel() 時立即醒來
        self.poll_s = poll_s

    def cancel(self) -> None:
        """引擎被中斷（Ctrl-C／SIGTERM）時呼叫：等候中的呼叫立即結束，行程可乾淨退出；佇列檔保留，續跑時接上。"""
        self._cancelled.set()

    def run(self, job: JobSpec) -> LLMResult:
        if job.sandbox is None:
            raise FatalLLMError(f"工作 {job.job_id} 沒有沙箱，無法以 hark_queue 執行")
        sandbox = Path(job.sandbox)
        try:
            check_sandbox(sandbox, self.denies)
        except IsolationError as e:
            raise FatalLLMError(str(e)) from e
        qd = queue_dir(sandbox) / job_key(job)
        qd.mkdir(parents=True, exist_ok=True)
        req = qd / "request.json"
        resp = qd / "response.txt"
        old = qd / "prompt.txt"
        if old.exists() and (_sha(old.read_text(encoding="utf-8")) != _sha(job.prompt_text)
                             or _inputs_changed(qd, sandbox)):
            # 同一 job_key 的新提示（retry 時 attempt 歸 0）或同名輸入檔內容變了：舊題目與答覆退休，
            # 不得重播舊答案、不得把舊 out/ 蓋進新沙箱（M5-D1-04、RM5-4）
            qd.rename(qd.with_name(f"{qd.name}.retired-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"))
            qd.mkdir(parents=True)
        # 每次呼叫都存原始提示（含接回既有佇列目錄）：引擎續跑只認本次執行 started_at 之後寫的提示（A3-M5E-02）
        write_raw(job.log_dir, job, "prompt.txt", job.prompt_text)
        if not req.exists():
            (qd / "prompt.txt").write_text(job.prompt_text, encoding="utf-8")
            req.write_text(json.dumps({
                "job_id": job.job_id, "job_type": job.job_type, "ticker": job.ticker, "asof": job.asof,
                "tier": self.tier.name, "tier_model": self.tier.model, "effort": self.tier.effort,
                "sandbox": str(sandbox.resolve()), "attempt": job.attempt, "inputs": _sandbox_inputs(sandbox),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, ensure_ascii=False, indent=1), encoding="utf-8")

        t0 = time.monotonic()
        deadline = t0 + self.tier.timeout_s
        while not resp.exists():
            if self._cancelled.is_set():
                raise TransportError(f"hark_queue 等候已取消（行程中斷）：{qd.name}")
            if time.monotonic() > deadline:
                raise TransportError(f"hark_queue 等候逾時（{self.tier.timeout_s}s）：{qd.name}")
            self.sleep(self.poll_s)
        time.sleep(0.5)  # 讓寫入者完成（未用 .tmp 改名的寫入者）
        text = resp.read_text(encoding="utf-8")
        if extract_json(text) is None:   # 可能讀到寫一半的檔：再等一輪重讀一次（A3-M5E-02；中斷時立即醒來）
            self._cancelled.wait(self.poll_s)
            text = resp.read_text(encoding="utf-8")
        latency = time.monotonic() - t0
        raw_path = write_raw(job.log_dir, job, "response.json",
                             json.dumps({"backend": "hark_queue", "result": text}, ensure_ascii=False))
        meta = {}
        if (qd / "meta.json").exists():
            try:
                meta = json.loads((qd / "meta.json").read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        files_written = []
        out = qd / "out"
        if out.is_dir():
            import shutil
            from .base import _safe_rel
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    rel = _safe_rel(p.relative_to(out).as_posix())
                    dest = sandbox / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(p, dest)
                    files_written.append(str(rel))
        return LLMResult(
            text=text,
            parsed_json=extract_json(text),
            input_tokens=int(meta.get("input_tokens") or 0),
            output_tokens=int(meta.get("output_tokens") or 0),
            model=str(meta.get("model") or f"hark-subagent({self.tier.model})"),
            latency_s=latency,
            raw_path=raw_path,
            cost_usd=None,
            files_written=files_written,
        )
