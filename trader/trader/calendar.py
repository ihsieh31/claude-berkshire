"""NYSE 交易日曆：規則假日、Alpaca 日曆（含快取）三層退回。

優先序：Alpaca 日曆（有給定 days 且日期在其範圍內）→ 規則假日。
"""
from __future__ import annotations

import functools
import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from trader.data.http import DataError

if TYPE_CHECKING:  # 只供型別標註，避免循環匯入
    from trader.data.alpaca import AlpacaClient


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """該月第 n 個指定星期幾（weekday：週一=0 … 週日=6）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """該月最後一個指定星期幾。"""
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """復活節（Anonymous Gregorian 演算法）。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """週六假日 → 前週五；週日假日 → 後週一。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@functools.lru_cache(maxsize=None)
def _holidays_cached(year: int) -> frozenset[date]:
    days: set[date] = set()
    # 元旦：週六不補前一年 12/31，只處理週日 → 週一
    ny = date(year, 1, 1)
    days.add(ny + timedelta(days=1) if ny.weekday() == 6 else ny)
    days.add(_nth_weekday(year, 1, 0, 3))          # 馬丁路德金恩日：1 月第 3 個週一
    days.add(_nth_weekday(year, 2, 0, 3))          # 總統日：2 月第 3 個週一
    days.add(_easter(year) - timedelta(days=2))    # 耶穌受難日：復活節前週五
    days.add(_last_weekday(year, 5, 0))            # 陣亡將士紀念日：5 月最後一個週一
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))     # 六月節（Juneteenth）
    days.add(_observed(date(year, 7, 4)))          # 獨立日
    days.add(_nth_weekday(year, 9, 0, 1))          # 勞動節：9 月第 1 個週一
    days.add(_nth_weekday(year, 11, 3, 4))         # 感恩節：11 月第 4 個週四
    days.add(_observed(date(year, 12, 25)))        # 聖誕節
    return frozenset(days)


def nyse_holidays(year: int) -> set[date]:
    """指定年度的 NYSE 規則假日（已套用週末補假）。"""
    return set(_holidays_cached(year))


def _to_date(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


class TradingCalendar:
    def __init__(self, days: list[str] | None = None, source: str = "rules"):
        # days 給定（來自 Alpaca 或快取）時只認這些日；範圍外的日期退回規則
        self.source = source
        self._days: list[str] = sorted(set(days)) if days else []
        self._set: set[str] = set(self._days)

    @classmethod
    def load(cls, client: "AlpacaClient | None", cache_path: Path, start: str, end: str) -> "TradingCalendar":
        """優先抓 Alpaca 日曆並存快取；抓取失敗或無 client 時讀快取；再不行退回規則。"""
        cache_path = Path(cache_path)
        if client is not None:
            try:
                rows = client.calendar(start, end)
                days = [r["date"] for r in rows]
                if not days:   # 回空清單＝資料來源暫時故障：不可覆寫好的快取（A3-M7C-04）
                    raise DataError(f"Alpaca 交易日曆 {start}～{end} 回空")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                return cls(days, source="alpaca")
            except DataError:
                pass  # 落到快取
        cached = cls._read_cache(cache_path)
        if cached is not None:
            return cls(cached, source="cache")
        return cls(source="rules")

    @staticmethod
    def _read_cache(cache_path: Path) -> list[str] | None:
        try:
            rows = json.loads(cache_path.read_text(encoding="utf-8"))
            days = [r["date"] for r in rows]
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return days or None

    # ---- 查詢 ----
    def _in_source_range(self, d: date) -> bool:
        if not self._days:
            return False
        return self._days[0] <= d.isoformat() <= self._days[-1]

    def is_trading_day(self, d: str | date) -> bool:
        d = _to_date(d)
        if d.weekday() >= 5:
            return False
        if self._in_source_range(d):
            return d.isoformat() in self._set
        return d not in nyse_holidays(d.year)

    def next_trading_day(self, d: str | date) -> str:
        cur = _to_date(d) + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur.isoformat()

    def prev_trading_day(self, d: str | date) -> str:
        cur = _to_date(d) - timedelta(days=1)
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur.isoformat()

    def trading_days(self, start: str | date, end: str | date) -> list[str]:
        cur, last = _to_date(start), _to_date(end)
        out: list[str] = []
        while cur <= last:
            if self.is_trading_day(cur):
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out
