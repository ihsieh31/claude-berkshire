"""個股走勢（網頁彈出視窗用）：1D／1W／3M／6M／10Y 五種區間，取自 Alpaca，記憶體快取。

- 資料源固定用 SIP（免費方案有 15 分鐘延遲；IEX 只有 2020 年後的歷史，不夠 10 年）；SIP 失敗才退回 IEX。
- 價格只做分拆還原（adjustment=split），與看盤軟體一致，最新點即實際成交價。
- 1D：最近一個交易日正常盤 5 分鐘線，基準＝前一交易日收盤；1W：最近 5 個交易日正常盤 30 分鐘線；
  3M／6M：日線；10Y：週線。後四者基準＝區間第一點。
- 同一區間的多檔代號合併成一次 Alpaca 請求。不快取：每次請求都向 Alpaca 取最新資料
  （網頁只在開啟／重新整理時預載一次，存在該頁記憶體，關掉網頁即消失）。只讀，不寫任何 state。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
RANGES = ("1D", "1W", "3M", "6M", "10Y")
# 區間 → (timeframe, 往前抓幾天)
SPEC = {
    "1D": ("5Min", 6),
    "1W": ("30Min", 10),
    "3M": ("1Day", 92),
    "6M": ("1Day", 183),
    "10Y": ("1Week", 3653),
}
MAX_TICKERS = 60


def _regular(ts: datetime) -> bool:
    m = ts.hour * 60 + ts.minute
    return 9 * 60 + 30 <= m < 16 * 60


class ChartService:
    def __init__(self, http: Any, now: Callable[[], datetime] | None = None):
        """http：有 get_json(url, params=…) 的客戶端（AlpacaClient.http）。"""
        self.http = http
        self.now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------ Alpaca
    def _fetch(self, tickers: list[str], timeframe: str, start: datetime) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {t: [] for t in tickers}
        err: Exception | None = None
        for feed in ("sip", "iex"):
            try:
                params = {"symbols": ",".join(tickers), "timeframe": timeframe, "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "adjustment": "split", "feed": feed, "limit": 10000}
                token = None
                while True:
                    p = dict(params, **({"page_token": token} if token else {}))
                    body = self.http.get_json(BARS_URL, params=p)
                    for sym, bs in (body.get("bars") or {}).items():
                        if sym in out:
                            out[sym].extend(bs or [])
                    token = body.get("next_page_token")
                    if not token:
                        return out
            except Exception as e:  # noqa: BLE001 — 換資料源再試一次
                err = e
                out = {t: [] for t in tickers}
        raise RuntimeError(f"Alpaca 走勢資料讀取失敗：{err}")

    # ------------------------------------------------------------ 整理
    @staticmethod
    def _series(rng: str, bars: list[dict], daily: list[dict] | None = None) -> dict:
        rows = []
        for b in bars:
            c = b.get("c")
            if not isinstance(c, (int, float)):
                continue
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(NY)
            rows.append((ts, float(c)))
        base = None
        if rng in ("1D", "1W"):
            rows = [r for r in rows if _regular(r[0])]
            days = sorted({r[0].date() for r in rows})
            keep = days[-1:] if rng == "1D" else days[-5:]
            rows = [r for r in rows if r[0].date() in keep]
            if rng == "1D" and keep:
                prev = [b for b in (daily or []) if b["t"][:10] < keep[0].isoformat() and isinstance(b.get("c"), (int, float))]
                base = float(prev[-1]["c"]) if prev else None
        fmt = {"1D": "%H:%M", "1W": "%m/%d %H:%M"}.get(rng, "%Y-%m-%d")
        pts = [[ts.strftime(fmt), round(c, 4)] for ts, c in rows]
        if not pts:
            return {"points": [], "base": None, "last": None, "change": None, "pct": None}
        if base is None:
            base = pts[0][1]
        last = pts[-1][1]
        return {"points": pts, "base": base, "last": last, "change": round(last - base, 4),
                "pct": (last / base - 1) if base else None, "session": rows[-1][0].date().isoformat()}

    def _load(self, rng: str, tickers: list[str]) -> dict[str, dict]:
        tf, days = SPEC[rng]
        now = self.now()
        bars = self._fetch(tickers, tf, now - timedelta(days=days))
        daily = self._fetch(tickers, "1Day", now - timedelta(days=12)) if rng == "1D" else {}
        return {t: self._series(rng, bars.get(t, []), daily.get(t)) for t in tickers}

    # ------------------------------------------------------------ 對外
    def get(self, tickers: list[str], ranges: list[str]) -> dict[str, dict[str, Any]]:
        tickers = list(dict.fromkeys(tickers))[:MAX_TICKERS]
        res: dict[str, dict[str, Any]] = {t: {} for t in tickers}
        for rng in ranges:
            try:
                for t, data in self._load(rng, tickers).items():
                    res[t][rng] = data
            except Exception as e:  # noqa: BLE001 — 其餘區間照常回
                for t in tickers:
                    res[t][rng] = {"error": str(e)[:300]}
        return res
