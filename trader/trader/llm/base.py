"""Runner 介面、錯誤類別、JSON 抽取、檔案落地、傳輸重試。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from ..models import JobSpec, LLMResult


class LLMError(Exception):
    """模型層錯誤基底。"""


class TransportError(LLMError):
    """子程序或 HTTP 失敗，可重試。retry_after：伺服器要求的等待秒數（429）。"""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class FatalLLMError(LLMError):
    """不可重試（設定錯、金鑰缺、401/403、找不到 claude）。"""


class Runner(Protocol):
    backend: str

    def run(self, job: JobSpec) -> LLMResult:
        """執行一次。失敗拋 TransportError（可重試）或 FatalLLMError。不負責驗證 schema。"""
        ...


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.S)


def extract_json(text: str) -> dict[str, Any] | None:
    """從模型文字抽出單一 JSON 物件；容忍 ```json 包裹與前後雜訊。抽不到回 None。"""
    if not text:
        return None
    s = text.strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    for block in reversed(_FENCE.findall(s)):
        try:
            v = json.loads(block)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    dec = json.JSONDecoder()
    best = None
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            v, end = dec.raw_decode(s, i)
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict) and (best is None or end - i > best[0]):
            best = (end - i, v)
    return best[1] if best else None


def _safe_rel(name: str) -> PurePosixPath:
    p = PurePosixPath(name.replace("\\", "/"))
    if not name or p.is_absolute() or ".." in p.parts or p.parts[:1] == ("~",):
        raise ValueError(f"不安全的檔名：{name!r}")
    return p


def materialize_files(parsed: dict[str, Any] | None, sandbox: Path | None) -> list[str]:
    """API 模式：模型把檔案放在 parsed['files'] = {檔名: 內容}。寫入沙箱，並把 files 換成檔名清單。

    內容若是 dict/list 則以 JSON 寫出。files 已是清單（Claude Code 模式自己寫檔）則不動。
    """
    if not parsed or not isinstance(parsed.get("files"), dict):
        return []
    if sandbox is None:
        raise ValueError("輸出含 files 但工作沒有沙箱")
    names = []
    for name, content in parsed["files"].items():
        rel = _safe_rel(name)
        dest = sandbox / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=1)
        dest.write_text(content, encoding="utf-8")
        names.append(str(rel))
    parsed["files"] = sorted(names)
    return sorted(names)


QUOTA_PAT = re.compile(r"usage limit|quota|rate.?limit|credit|overloaded|too many requests", re.I)
QUOTA_BASE_DELAY = 60.0   # 額度類錯誤的退避起點：60、120、240 秒（共 7 分鐘）；訂閱額度重置要數小時，更長只會卡住 daily，
                          # 真正的保險是 daily 把失敗的公司行動事件留到下次重派（ledger.corp_actions.record_job_failure）


def call_with_retry(runner: Runner, job: JobSpec, retries: int = 3, base_delay: float = 2.0,
                    sleep: Callable[[float], None] = time.sleep) -> LLMResult:
    """傳輸失敗指數退避重試（最多 retries 次額外嘗試）；429 依 retry_after。FatalLLMError 不重試。"""
    for i in range(retries + 1):
        try:
            return runner.run(job)
        except TransportError as e:
            if i == retries:
                raise
            if e.retry_after is not None:
                delay = e.retry_after
            else:
                delay = max(base_delay, QUOTA_BASE_DELAY if QUOTA_PAT.search(str(e)) else 0.0) * (2 ** i)
            sleep(delay)
    raise AssertionError("unreachable")


def write_raw(log_dir: Path | None, job: JobSpec, suffix: str, content: str) -> Path | None:
    """存原始提示／回應：{log_dir}/{job_id 安全化}_a{attempt}.{suffix}"""
    if log_dir is None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", job.job_id)
    p = log_dir / f"{safe}_a{job.attempt}.{suffix}"
    p.write_text(content, encoding="utf-8")
    return p
