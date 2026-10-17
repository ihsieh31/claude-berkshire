"""FRED 資料來源測試：全部經 httpx.MockTransport，不連網。"""
from __future__ import annotations

import os
import time

import httpx
import pytest

from trader.data.fred import FredClient
from trader.data.http import DataError, HttpClient

API_URL = "https://api.stlouisfed.org/fred/series/observations"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
KEY = "fake-key-123"


class Server:
    """可切換回應的假 FRED；記錄所有請求。"""

    def __init__(self, api_obs=None, csv_text=None, fail=False):
        self.api_obs = api_obs
        self.csv_text = csv_text
        self.fail = fail
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(500, text="server down")
        if request.url.host == "api.stlouisfed.org":
            return httpx.Response(200, json={"observations": self.api_obs})
        return httpx.Response(200, text=self.csv_text)

    def client(self) -> HttpClient:
        return HttpClient(transport=httpx.MockTransport(self.handler), sleep=lambda s: None)

    def count(self, host: str) -> int:
        return sum(1 for r in self.requests if r.url.host == host)


def age_file(path, hours: float) -> None:
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


API_OBS = [
    {"date": "2024-01-02", "value": "4.1"},
    {"date": "2024-01-03", "value": "."},  # 缺值丟棄
    {"date": "2024-01-04", "value": "4.2"},
]
CSV_TEXT = (
    "observation_date,DGS30\n"
    "2024-01-02,4.1\n"
    "2024-01-03,.\n"
    "2024-01-04,4.2\n"
)


# ---- 兩種來源 ----
def test_api_source_with_key_drops_missing(tmp_path):
    srv = Server(api_obs=API_OBS)
    c = FredClient(KEY, tmp_path, http=srv.client())
    s = c.series("DGS30")
    assert list(s.index) == ["2024-01-02", "2024-01-04"]
    assert list(s.values) == [4.1, 4.2]
    req = srv.requests[0]
    assert req.url.host == "api.stlouisfed.org"
    assert req.url.params["api_key"] == KEY
    assert req.url.params["series_id"] == "DGS30"
    assert req.url.params["observation_start"] == "1977-01-01"


def test_csv_source_without_key_drops_missing(tmp_path):
    srv = Server(csv_text=CSV_TEXT)
    c = FredClient(None, tmp_path, http=srv.client())
    s = c.series("DGS30")
    assert list(s.index) == ["2024-01-02", "2024-01-04"]
    assert list(s.values) == [4.1, 4.2]
    req = srv.requests[0]
    assert req.url.host == "fred.stlouisfed.org"
    assert req.url.params["id"] == "DGS30"
    assert "api_key" not in req.url.params


def test_empty_string_key_means_csv(tmp_path):
    srv = Server(csv_text=CSV_TEXT)
    FredClient("", tmp_path, http=srv.client()).series()
    assert srv.count("fred.stlouisfed.org") == 1


# ---- 快取 ----
def test_cache_hit_then_expiry(tmp_path):
    srv = Server(csv_text=CSV_TEXT)
    c = FredClient(None, tmp_path, http=srv.client(), max_age_hours=20)
    c.series("DGS30")
    c.series("DGS30")
    assert srv.count("fred.stlouisfed.org") == 1  # 快取命中
    age_file(tmp_path / "DGS30.json", hours=21)
    c.series("DGS30")
    assert srv.count("fred.stlouisfed.org") == 2  # 過期重抓


def test_fetch_failure_falls_back_to_stale_cache(tmp_path):
    ok = Server(csv_text=CSV_TEXT)
    FredClient(None, tmp_path, http=ok.client()).series("DGS30")
    age_file(tmp_path / "DGS30.json", hours=21)

    bad = Server(fail=True)
    s = FredClient(None, tmp_path, http=bad.client()).series("DGS30")
    assert list(s.values) == [4.1, 4.2]


def test_fetch_failure_without_cache_raises(tmp_path):
    bad = Server(fail=True)
    with pytest.raises(DataError):
        FredClient(None, tmp_path, http=bad.client()).series("DGS30")


# ---- 時點 ----
def test_t30_excludes_asof_day(tmp_path):
    csv = "observation_date,DGS30\n2026-10-14,4.70\n2026-10-15,4.80\n2026-10-16,4.90\n"
    c = FredClient(None, tmp_path, http=Server(csv_text=csv).client())
    val, d = c.t30("2026-10-16")
    assert d == "2026-10-15"
    assert val == pytest.approx(0.048)


def test_t30_no_data_raises(tmp_path):
    c = FredClient(None, tmp_path, http=Server(csv_text=CSV_TEXT).client())
    with pytest.raises(DataError):
        c.t30("2024-01-02")  # 沒有嚴格早於此日的值


def test_t30_median_20y_window(tmp_path):
    obs = [
        {"date": "2006-10-15", "value": "100"},  # 窗口外（早於 20 年前），不可入中位數
        {"date": "2006-10-16", "value": "3"},
        {"date": "2010-01-01", "value": "5"},
        {"date": "2015-01-01", "value": "."},    # 缺值
        {"date": "2020-01-01", "value": "7"},
        {"date": "2026-10-15", "value": "9"},
        {"date": "2026-10-16", "value": "99"},   # asof 當日，不可用
    ]
    c = FredClient(KEY, tmp_path, http=Server(api_obs=obs).client())
    # 窗口 [2006-10-16, 2026-10-16) 的值 3,5,7,9 → 中位數 6
    assert c.t30_median_20y("2026-10-16") == pytest.approx(0.06)
