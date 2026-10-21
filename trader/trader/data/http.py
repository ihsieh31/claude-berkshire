"""資料層共用 HTTP：限速、重試（429／5xx／連線錯誤）、可注入 transport 供測試；另有快取用的原子寫檔。

秘密只放在 headers／params 由呼叫端提供；錯誤訊息會遮罩 secrets。
帶金鑰標頭的客戶端（Alpaca）應以 follow_redirects=False 建構：httpx 跨網域轉址只剝 Authorization／Cookie，
自訂金鑰標頭會被帶到新主機；不跟隨時 3xx 一律 DataError。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx


class DataError(Exception):
    """資料來源錯誤（重試耗盡、4xx、格式不符）。"""


class SecBlocked(DataError):
    """SEC 以 403 封鎖本機（Request Rate Threshold／Undeclared Automated Tool）。
    同一 HttpClient 之後的請求不再送出、直接拋本例外，避免被封鎖時繼續逐檔打 SEC。"""


# SEC 封鎖頁的固定字樣（限速或 User-Agent 被拒）
_BLOCK_MARKERS = ("Request Rate Threshold", "Undeclared Automated Tool")


def ny_today() -> str:
    """紐約時間的今天（YYYY-MM-DD）：快取 meta 的 fetched_ny_date（D36）。決策日 asof 也是紐約日期。"""
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def atomic_write_text(path, text: str) -> None:
    """先寫同目錄暫存檔再 os.replace：中途被殺不會留下半截快取檔；失敗時刪暫存檔並往上拋。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class HttpClient:
    def __init__(self, min_interval: float = 0.0, retries: int = 3, base_delay: float = 2.0,
                 timeout: float = 60.0, transport: httpx.BaseTransport | None = None,
                 headers: dict[str, str] | None = None, secrets: list[str | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, follow_redirects: bool = True):
        self.min_interval = min_interval
        self.retries = retries
        self.base_delay = base_delay
        self.sleep = sleep
        self.secrets = [s for s in (secrets or []) if s]
        self.follow_redirects = follow_redirects
        self.blocked: str | None = None  # 收到 SEC 封鎖頁後的錯誤訊息；之後的請求直接拋 SecBlocked
        self._client = httpx.Client(timeout=timeout, transport=transport, headers=headers or {},
                                    follow_redirects=follow_redirects)
        self._lock = threading.Lock()
        self._last = 0.0

    def _redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()

    def get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
        """GET；429／5xx／連線錯誤指數退避重試（429 依 Retry-After 秒數）；其他 4xx → DataError；
        SEC 封鎖頁（403）→ SecBlocked 並停用本客戶端；不跟隨轉址時 3xx → DataError。"""
        if self.blocked:
            raise SecBlocked(self.blocked)
        last_err = ""
        for i in range(self.retries + 1):
            self._throttle()
            try:
                r = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                last_err = f"連線失敗（{type(e).__name__}）"
                delay = self.base_delay * (2 ** i)
            else:
                if 300 <= r.status_code < 400 and not self.follow_redirects:
                    raise DataError(self._redact(f"HTTP {r.status_code} 轉址未跟隨：{url}"))
                if r.status_code < 400:
                    return r
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}"
                    ra = r.headers.get("Retry-After")
                    try:
                        delay = float(ra) if ra else self.base_delay * (2 ** i)
                    except ValueError:
                        delay = self.base_delay * (2 ** i)
                else:
                    if r.status_code == 403 and any(m in r.text for m in _BLOCK_MARKERS):
                        self.blocked = self._redact(
                            f"SEC 封鎖（HTTP 403 限速或 User-Agent 被拒）：{url}；本次不再送出請求，"
                            "檢查 SEC_USER_AGENT 後約 10 分鐘再重跑")
                        raise SecBlocked(self.blocked)
                    raise DataError(self._redact(f"HTTP {r.status_code} {url}：{r.text[:300]}"))
            if i < self.retries:
                self.sleep(delay)
        raise DataError(self._redact(f"重試耗盡：{url}（{last_err}）"))

    def get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        r = self.get(url, params=params, headers=headers)
        try:
            return r.json()
        except ValueError as e:
            raise DataError(f"回應不是 JSON：{url}") from e

    def close(self) -> None:
        self._client.close()
