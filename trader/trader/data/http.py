"""資料層共用 HTTP：限速、重試（429／5xx／連線錯誤）、可注入 transport 供測試。

秘密只放在 headers／params 由呼叫端提供；錯誤訊息會遮罩 secrets。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import httpx


class DataError(Exception):
    """資料來源錯誤（重試耗盡、4xx、格式不符）。"""


class HttpClient:
    def __init__(self, min_interval: float = 0.0, retries: int = 3, base_delay: float = 2.0,
                 timeout: float = 60.0, transport: httpx.BaseTransport | None = None,
                 headers: dict[str, str] | None = None, secrets: list[str | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval = min_interval
        self.retries = retries
        self.base_delay = base_delay
        self.sleep = sleep
        self.secrets = [s for s in (secrets or []) if s]
        self._client = httpx.Client(timeout=timeout, transport=transport, headers=headers or {},
                                    follow_redirects=True)
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
        """GET；429／5xx／連線錯誤指數退避重試（429 依 Retry-After 秒數）；其他 4xx → DataError。"""
        last_err = ""
        for i in range(self.retries + 1):
            self._throttle()
            try:
                r = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                last_err = f"連線失敗（{type(e).__name__}）"
                delay = self.base_delay * (2 ** i)
            else:
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
