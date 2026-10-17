"""交易日曆測試：NYSE 規則假日、前後交易日、快取與退回邏輯。"""
import json
from datetime import date

import pytest

from trader.calendar import TradingCalendar, nyse_holidays
from trader.data.http import DataError


def test_holidays_2026_exact_set():
    expected = {
        date(2026, 1, 1),   # 元旦（週四）
        date(2026, 1, 19),  # 馬丁路德金恩日
        date(2026, 2, 16),  # 總統日
        date(2026, 4, 3),   # 耶穌受難日
        date(2026, 5, 25),  # 陣亡將士紀念日
        date(2026, 6, 19),  # 六月節
        date(2026, 7, 3),   # 獨立日 7/4 為週六 → 前週五
        date(2026, 9, 7),   # 勞動節
        date(2026, 11, 26), # 感恩節
        date(2026, 12, 25), # 聖誕節
    }
    assert nyse_holidays(2026) == expected


def test_new_year_saturday_not_observed_on_prior_year():
    # 2028-01-01 為週六：不補 2027-12-31
    assert date(2028, 1, 1) in nyse_holidays(2028)
    assert date(2027, 12, 31) not in nyse_holidays(2027)
    assert TradingCalendar().is_trading_day("2027-12-31") is True


def test_sunday_holiday_observed_monday():
    # 2022-12-25 為週日 → 12/26 補假
    assert date(2022, 12, 26) in nyse_holidays(2022)
    # 2022-01-01 為週六：元旦不補前一年 12/31
    assert date(2022, 1, 1) in nyse_holidays(2022)
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert date(2022, 1, 3) not in nyse_holidays(2022)  # 週一並非假日


def test_juneteenth_only_from_2022():
    assert date(2021, 6, 18) not in nyse_holidays(2021)  # 2021-06-19 為週六，且 2021 尚無六月節
    assert date(2022, 6, 20) in nyse_holidays(2022)      # 2022-06-19 為週日 → 週一補假


def test_next_prev_across_weekend_and_holiday():
    cal = TradingCalendar()
    assert cal.next_trading_day("2026-07-02") == "2026-07-06"   # 7/3 假日、7/4-5 週末
    assert cal.prev_trading_day("2026-07-06") == "2026-07-02"
    assert cal.next_trading_day("2026-01-01") == "2026-01-02"   # 嚴格大於 d
    assert cal.prev_trading_day("2026-01-19") == "2026-01-16"   # MLK 週一 → 上週五
    assert cal.next_trading_day("2026-01-16") == "2026-01-20"   # 跨週末與假日


def test_is_trading_day_basic():
    cal = TradingCalendar()
    assert cal.is_trading_day("2026-03-10") is True   # 週二
    assert cal.is_trading_day("2026-03-14") is False  # 週六
    assert cal.is_trading_day(date(2026, 6, 19)) is False
    assert cal.is_trading_day("2026-06-18") is True


def test_trading_days_inclusive_range():
    cal = TradingCalendar()
    assert cal.trading_days("2026-06-18", "2026-06-22") == ["2026-06-18", "2026-06-22"]
    assert cal.trading_days("2026-12-24", "2026-12-28") == ["2026-12-24", "2026-12-28"]


def test_days_given_limits_in_range_and_falls_back_outside():
    cal = TradingCalendar(days=["2026-03-02", "2026-03-04"], source="alpaca")
    assert cal.is_trading_day("2026-03-02") is True
    assert cal.is_trading_day("2026-03-03") is False   # 範圍內、未列出 → 不交易
    assert cal.is_trading_day("2026-03-04") is True
    # 範圍外（2026-03-09 週一）退回規則
    assert cal.is_trading_day("2026-03-09") is True
    assert cal.is_trading_day("2026-01-19") is False   # 範圍外的規則假日
    assert cal.source == "alpaca"


class _FakeClient:
    def __init__(self, rows=None, error=None):
        self.rows = rows
        self.error = error
        self.calls = 0

    def calendar(self, start, end):
        self.calls += 1
        if self.error:
            raise self.error
        return self.rows


_ROWS = [
    {"date": "2026-03-02", "open": "09:30", "close": "16:00"},
    {"date": "2026-03-04", "open": "09:30", "close": "16:00"},
]


def test_load_from_client_writes_cache(tmp_path):
    cache = tmp_path / "cache" / "calendar.json"
    client = _FakeClient(rows=_ROWS)
    cal = TradingCalendar.load(client, cache, "2026-03-01", "2026-03-31")
    assert client.calls == 1
    assert cal.source == "alpaca"
    assert cal.is_trading_day("2026-03-03") is False
    assert json.loads(cache.read_text(encoding="utf-8")) == _ROWS


def test_load_falls_back_to_cache_on_data_error(tmp_path):
    cache = tmp_path / "calendar.json"
    cache.write_text(json.dumps(_ROWS), encoding="utf-8")
    cal = TradingCalendar.load(_FakeClient(error=DataError("boom")), cache, "2026-03-01", "2026-03-31")
    assert cal.source == "cache"
    assert cal.is_trading_day("2026-03-03") is False
    assert cal.is_trading_day("2026-03-04") is True


def test_load_without_client_uses_cache(tmp_path):
    cache = tmp_path / "calendar.json"
    cache.write_text(json.dumps(_ROWS), encoding="utf-8")
    cal = TradingCalendar.load(None, cache, "2026-03-01", "2026-03-31")
    assert cal.source == "cache"


def test_load_without_cache_falls_back_to_rules(tmp_path):
    cache = tmp_path / "missing.json"
    cal = TradingCalendar.load(_FakeClient(error=DataError("down")), cache, "2026-01-01", "2026-12-31")
    assert cal.source == "rules"
    assert cal.is_trading_day("2026-01-19") is False
    assert cal.is_trading_day("2026-01-20") is True


def test_load_none_client_no_cache_is_rules(tmp_path):
    cal = TradingCalendar.load(None, tmp_path / "none.json", "2026-01-01", "2026-12-31")
    assert cal.source == "rules"
    assert cal.is_trading_day("2026-12-25") is False


def test_load_corrupt_cache_is_rules(tmp_path):
    cache = tmp_path / "bad.json"
    cache.write_text("not json", encoding="utf-8")
    cal = TradingCalendar.load(None, cache, "2026-01-01", "2026-12-31")
    assert cal.source == "rules"
