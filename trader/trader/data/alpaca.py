"""Alpaca 市場資料客戶端：日線、公司行動、新聞、交易日曆與時鐘。

所有網路請求都經 `HttpClient`；金鑰只從建構參數傳入（呼叫端讀環境變數），
並以 `secrets` 交給 HttpClient 遮罩錯誤訊息。
"""
from __future__ import annotations

import json
from datetime import date as _date, timedelta as _timedelta
from typing import Any, Iterator

import pandas as pd

from trader.data.http import DataError, HttpClient

DATA_URL = "https://data.alpaca.markets"
TRADING_URL = "https://paper-api.alpaca.markets"
NY_TZ = "America/New_York"

BAR_COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume", "vwap"]
SYMBOL_BATCH = 100  # bars 端點每次請求最多 100 檔


def normalize_ticker(s: str | None) -> str | None:
    """代號標準形：去空白、大寫、"-" 換成 "."（BRK-B → BRK.B）。"""
    if s is None:
        return None
    return str(s).strip().upper().replace("-", ".")


def _ny_date(ts: str) -> str:
    """時間字串（UTC，例如 2026-03-10T04:00:00Z）→ 紐約日期 YYYY-MM-DD。"""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(NY_TZ).strftime("%Y-%m-%d")


def _div(a: Any, b: Any) -> float | None:
    if a is None or b is None or float(b) == 0:
        return None
    return float(a) / float(b)


def _plus_one(r: Any) -> float | None:
    return None if r is None else 1.0 + float(r)


def _opt_float(x: Any) -> float | None:
    return None if x is None else float(x)


def _date_windows(start: str, end: str, days: int) -> list[tuple[str, str]]:
    """把 [start, end] 切成每段最多 days 天的區間（含頭尾）。"""
    s, e = _date.fromisoformat(start[:10]), _date.fromisoformat(end[:10])
    out = []
    while s <= e:
        w = min(e, s + _timedelta(days=days - 1))
        out.append((s.isoformat(), w.isoformat()))
        s = w + _timedelta(days=1)
    return out or [(start[:10], end[:10])]


def _normalize_action(key: str, it: dict) -> dict:
    """單筆公司行動 → 統一格式；key 為回應的鍵名（如 forward_splits）。"""
    # 鍵名去掉複數 s 即為 type（forward_splits → forward_split）
    base = key[:-1] if key.endswith("s") else key
    g = it.get
    ex_date = g("ex_date")
    ticker: Any = None
    new_ticker: Any = None
    ratio: float | None = None
    cash: float | None = None

    if base in ("forward_split", "reverse_split"):
        ticker = g("symbol")
        ratio = _div(g("new_rate"), g("old_rate"))
    elif base == "cash_dividend":
        ticker = g("symbol")
        cash = _opt_float(g("rate"))
    elif base == "stock_dividend":
        ticker = g("symbol")
        ratio = _plus_one(g("rate"))
    elif base == "spin_off":
        ticker = g("source_symbol")
        new_ticker = g("new_symbol")
    elif base == "cash_merger":
        ticker = g("acquiree_symbol")
        cash = _opt_float(g("rate"))
    elif base in ("stock_merger", "stock_and_cash_merger"):
        ticker = g("acquiree_symbol")
        new_ticker = g("acquirer_symbol")
        ratio = _div(g("acquirer_rate"), g("acquiree_rate"))
        if base == "stock_and_cash_merger":
            cash = _opt_float(g("cash_rate"))
    elif base == "name_change":
        ticker = g("old_symbol")
        new_ticker = g("new_symbol")
    else:
        # 未知鍵：只盡量填代號欄位，比率與現金留空
        ticker = g("symbol") or g("old_symbol") or g("source_symbol") or g("acquiree_symbol")
        new_ticker = g("new_symbol") or g("acquirer_symbol")

    # 併購、改名等通常只有生效日；ex_date 缺時改用 effective_date 或 process_date（防重複入帳的鍵需要日期）
    ex_date = ex_date or g("effective_date") or g("process_date")
    return {
        "ticker": normalize_ticker(ticker),
        "type": base,
        "ex_date": ex_date,
        "process_date": g("process_date"),
        "record_date": g("record_date"),
        "payable_date": g("payable_date"),
        "ratio_new_per_old": ratio,
        "cash_per_share": cash,
        "new_ticker": normalize_ticker(new_ticker),
        "raw": it,
    }


class AlpacaClient:
    def __init__(self, key: str, secret: str, feed: str = "iex", http: HttpClient | None = None):
        self.feed = feed
        self.http = http or HttpClient(
            min_interval=0.31,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            secrets=[key, secret],
        )

    # ---- 內部：分頁迭代 ----
    def _pages(self, url: str, params: dict[str, Any]) -> Iterator[dict]:
        """逐頁 GET，跟到 next_page_token 為空為止。"""
        token: str | None = None
        while True:
            p = dict(params)
            if token:
                p["page_token"] = token
            body = self.http.get_json(url, params=p)
            if not isinstance(body, dict):
                raise DataError(f"回應格式不符：{url}")
            yield body
            token = body.get("next_page_token")
            if not token:
                return

    # ---- 日線 ----
    def bars(self, symbols: list[str], start: str, end: str, adjustment: str = "raw",
             timeframe: str = "1Day") -> pd.DataFrame:
        syms = [normalize_ticker(s) for s in symbols]
        syms = [s for s in syms if s]
        rows: list[dict] = []
        for i in range(0, len(syms), SYMBOL_BATCH):
            batch = syms[i:i + SYMBOL_BATCH]
            params = {
                "symbols": ",".join(batch),
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "adjustment": adjustment,
                "feed": self.feed,
                "limit": 10000,
            }
            for body in self._pages(f"{DATA_URL}/v2/stocks/bars", params):
                for sym, bs in (body.get("bars") or {}).items():
                    tk = normalize_ticker(sym)
                    for b in bs or []:
                        rows.append({
                            "ticker": tk,
                            "date": _ny_date(b["t"]),
                            "open": b.get("o"),
                            "high": b.get("h"),
                            "low": b.get("l"),
                            "close": b.get("c"),
                            "volume": b.get("v"),
                            "vwap": b.get("vw"),
                        })
        if not rows:
            return pd.DataFrame(columns=BAR_COLUMNS)
        df = pd.DataFrame(rows, columns=BAR_COLUMNS)
        return df.sort_values(["ticker", "date"]).reset_index(drop=True)

    # ---- 公司行動 ----
    def corporate_actions(self, symbols: list[str], start: str, end: str,
                          types: list[str] | None = None) -> list[dict]:
        syms = [s for s in (normalize_ticker(x) for x in symbols) if s]
        out: list[dict] = []
        seen: set[str] = set()
        # 代號每批 ≤100、日期每段 ≤365 天，避免網址過長與端點的區間限制
        for i in range(0, len(syms), SYMBOL_BATCH):
            for w_start, w_end in _date_windows(start, end, 365):
                params: dict[str, Any] = {
                    "symbols": ",".join(syms[i:i + SYMBOL_BATCH]),
                    "start": w_start,
                    "end": w_end,
                    "limit": 1000,
                }
                if types:
                    params["types"] = ",".join(types)
                for body in self._pages(f"{DATA_URL}/v1/corporate-actions", params):
                    groups = body.get("corporate_actions") or {}
                    for key, items in groups.items():
                        for it in items or []:
                            a = _normalize_action(key, it)
                            k = json.dumps([a["type"], a["ticker"], a["ex_date"], it.get("id")], default=str)
                            if k not in seen:  # 分段邊界可能重複
                                seen.add(k)
                                out.append(a)
        return out

    # ---- 新聞 ----
    def news(self, symbols: list[str], start: str, end: str) -> list[dict]:
        want = [s for s in (normalize_ticker(x) for x in symbols) if s]
        if not want:
            return []
        wanted = set(want)
        params = {
            "symbols": ",".join(want),
            "start": start,
            "end": end,
            "limit": 50,
            "sort": "asc",
        }
        out: list[dict] = []
        for body in self._pages(f"{DATA_URL}/v1beta1/news", params):
            for n in body.get("news") or []:
                created = n.get("created_at")
                date = _ny_date(created) if created else None
                seen: set[str] = set()
                # 每則新聞展開成「請求代號 ∩ 該則代號」的每代號一筆
                for sym in n.get("symbols") or []:
                    tk = normalize_ticker(sym)
                    if tk in wanted and tk not in seen:
                        seen.add(tk)
                        out.append({
                            "id": n.get("id"),
                            "ticker": tk,
                            "date": date,
                            "created_at": created,
                            "headline": n.get("headline"),
                            "source": n.get("source"),
                            "url": n.get("url"),
                            "summary": n.get("summary"),
                        })
        return out

    # ---- 交易日曆與時鐘 ----
    def calendar(self, start: str, end: str) -> list[dict]:
        body = self.http.get_json(f"{TRADING_URL}/v2/calendar", params={"start": start, "end": end})
        if not isinstance(body, list):
            raise DataError("calendar 回應格式不符")
        return [{"date": r.get("date"), "open": r.get("open"), "close": r.get("close")} for r in body]

    def clock(self) -> dict:
        body = self.http.get_json(f"{TRADING_URL}/v2/clock")
        if not isinstance(body, dict):
            raise DataError("clock 回應格式不符")
        return body
