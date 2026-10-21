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
    """子程序或 HTTP 失敗。retry_after：伺服器要求的等待秒數（429）。
    失敗呼叫若仍有用量（例如 claude 回報 is_error 時的 usage／total_cost_usd），帶在 input_tokens／output_tokens／
    cost_usd，raw_path 為已存的原始回應；引擎據此逐次記入 llm_calls（M5-D1-07）。"""

    def __init__(self, msg: str, retry_after: float | None = None, input_tokens: int = 0, output_tokens: int = 0,
                 cost_usd: float | None = None, raw_path: Path | None = None):
        super().__init__(msg)
        self.retry_after = retry_after
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.raw_path = raw_path


class LLMTimeout(TransportError):
    """模型呼叫逾時（設計決策 D47：最多重試 1 次）。"""


class FatalLLMError(LLMError):
    """不可重試（設定錯、金鑰缺、401/403、找不到 claude）。"""


class Runner(Protocol):
    backend: str
    # 可續跑（通用屬性，M5_D9-02）：行程中斷後，同一工作以同一沙箱、同一提示再呼叫 run 會接上上次的作答
    # （例如外部代理依佇列檔作答的後端）。True 時引擎續跑不重建沙箱、不重跑前置腳本；未宣告視為 False。
    resumable: bool

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


# 不含裸 credit（A3-M5C-02）：claude is_error 的訊息會帶模型 result 文字，「credit facility」等財報字樣不得當成額度；
# 「credit balance is too low」（帳戶餘額不足）退避幾分鐘也不會好，不重試
QUOTA_PAT = re.compile(r"usage limit|quota|rate.?limit|overloaded|too many requests", re.I)
QUOTA_BASE_DELAY = 60.0   # 額度類錯誤的退避起點：60、120、240 秒（共 7 分鐘）；訂閱額度重置要數小時，更長只會卡住 daily，
                          # 真正的保險是 daily 把失敗的公司行動事件留到下次重派（ledger.corp_actions.record_job_failure）


TIMEOUT_RETRIES = 1      # 設計決策 D47：逾時最多重試 1 次；其他非額度類傳輸錯誤不重試


def is_quota_error(e: TransportError) -> bool:
    """額度類錯誤（429 帶 retry_after，或訊息含限流／額度／過載字樣）：照 retries 退避重試。"""
    return e.retry_after is not None or bool(QUOTA_PAT.search(str(e)))


def call_with_retry(runner: Runner, job: JobSpec, retries: int = 3, base_delay: float = 2.0,
                    sleep: Callable[[float], None] = time.sleep, failed: list | None = None,
                    stop: Any = None, before_retry: Callable[[], None] | None = None) -> LLMResult:
    """傳輸失敗的重試（設計決策 D47）：額度類錯誤指數退避最多 retries 次（429 依 retry_after）；逾時最多重試
    TIMEOUT_RETRIES 次；其他傳輸錯誤與 FatalLLMError 不重試。
    failed：給定時，每個「已重試過去」的 TransportError 依序附加在這裡，供呼叫端逐次記帳（M5-D1-07）；
    最後拋出的錯誤不附加（由呼叫端從例外取得）。stop：threading.Event，已設定時不再重試（M5_D9-03）；
    退避等待用 stop.wait（中斷立即醒來；注入假 sleep 的測試仍用 sleep），醒來 stop 已設定即拋出該錯誤、不再送出（M5-RS4-02）。
    before_retry：每次重試送出前呼叫（例如還原沙箱內會被追加的檔，M5-D1-09）。"""
    timeouts = 0
    for i in range(retries + 1):
        try:
            return runner.run(job)
        except TransportError as e:
            if isinstance(e, LLMTimeout):
                timeouts += 1
                again = timeouts <= TIMEOUT_RETRIES
            else:
                again = is_quota_error(e)
            if i == retries or not again or (stop is not None and stop.is_set()):
                raise
            if e.retry_after is not None:
                delay = e.retry_after
            else:
                delay = max(base_delay, QUOTA_BASE_DELAY if QUOTA_PAT.search(str(e)) else 0.0) * (2 ** i)
            if stop is not None and sleep is time.sleep:
                stop.wait(delay)
            else:
                sleep(delay)
            if stop is not None and stop.is_set():
                raise
            if failed is not None:
                failed.append(e)
            if before_retry is not None:
                before_retry()
    raise AssertionError("unreachable")


def write_raw(log_dir: Path | None, job: JobSpec, suffix: str, content: str) -> Path | None:
    """存原始提示／回應：{log_dir}/{job_id 安全化}_a{attempt}_{UTC 時間 %Y%m%dT%H%M%SZ}.{suffix}；
    同名已存在（同一秒）加 _2、_3…，不覆寫舊檔（重跑、續跑、傳輸重試都留下各自的紀錄，M3_D9-01）。"""
    if log_dir is None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{raw_stem(job)}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    p = log_dir / f"{stem}.{suffix}"
    k = 2
    while p.exists():
        p = log_dir / f"{stem}_{k}.{suffix}"
        k += 1
    p.write_text(content, encoding="utf-8")
    return p


def raw_stem(job: JobSpec, attempt: int | None = None) -> str:
    """write_raw 檔名的前綴：{job_id 安全化}_a{attempt}。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", job.job_id)
    return f"{safe}_a{job.attempt if attempt is None else attempt}"


def latest_raw(log_dir: Path | None, job: JobSpec, suffix: str, attempt: int | None = None) -> Path | None:
    """write_raw 留下的該工作、該次嘗試最新的檔（依檔名的時間戳與同秒序號）；沒有則 None。"""
    if log_dir is None or not Path(log_dir).is_dir():
        return None
    pat = re.compile(re.escape(raw_stem(job, attempt)) + r"_(\d{8}T\d{6}Z)(?:_(\d+))?\." + re.escape(suffix))
    best = None
    for p in Path(log_dir).iterdir():
        m = pat.fullmatch(p.name)
        if m is None:
            continue
        key = (m.group(1), int(m.group(2) or 1))
        if best is None or key > best[0]:
            best = (key, p)
    return best[1] if best else None
