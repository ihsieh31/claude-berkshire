"""個股走勢彈出視窗：ChartService（區間整理、不快取、退回 IEX）與 /api/charts。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from test_approvals import _env

from trader.dashboard.charts import ChartService
from trader.dashboard.server import create_app

NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)  # 紐約 14:00


def _bar(t, c):
    return {"t": t, "c": c}


class FakeHttp:
    def __init__(self, fail_sip=False):
        self.calls = []
        self.fail_sip = fail_sip

    def get_json(self, url, params):
        self.calls.append(dict(params))
        if self.fail_sip and params["feed"] == "sip":
            raise RuntimeError("sip 不可用")
        syms = params["symbols"].split(",")
        tf = params["timeframe"]
        if tf == "5Min":
            bs = [_bar("2026-10-15T19:55:00Z", 99.0),            # 前一日盤中
                  _bar("2026-10-16T12:00:00Z", 1.0),             # 盤前 08:00 → 排除
                  _bar("2026-10-16T13:30:00Z", 100.0), _bar("2026-10-16T17:55:00Z", 102.0)]
        elif tf == "1Day":
            bs = [_bar("2026-10-14T04:00:00Z", 97.0), _bar("2026-10-15T04:00:00Z", 98.0), _bar("2026-10-16T04:00:00Z", 102.0)]
        elif tf == "30Min":
            bs = [_bar(f"2026-10-{d:02d}T14:00:00Z", float(d)) for d in (8, 9, 12, 13, 14, 15, 16)]
        else:
            bs = [_bar("2016-10-17T04:00:00Z", 50.0), _bar("2026-10-12T04:00:00Z", 100.0)]
        return {"bars": {s: bs for s in syms}, "next_page_token": None}


def test_ranges_and_cache():
    http = FakeHttp()
    cs = ChartService(http, now=lambda: NOW)
    r = cs.get(["AAA", "BBB"], ["1D", "1W", "3M", "10Y"])
    d = r["AAA"]["1D"]
    assert [p[0] for p in d["points"]] == ["09:30", "13:55"]      # 只留最近一日正常盤
    assert d["base"] == 98.0 and d["last"] == 102.0                  # 基準＝前一交易日收盤
    assert abs(d["pct"] - (102 / 98 - 1)) < 1e-9
    assert len(r["AAA"]["1W"]["points"]) == 5                        # 最近 5 個交易日
    assert r["BBB"]["10Y"]["base"] == 50.0 and r["BBB"]["10Y"]["pct"] == 1.0
    assert all(c["adjustment"] == "split" and c["feed"] == "sip" for c in http.calls)
    assert all(c["symbols"] == "AAA,BBB" for c in http.calls)       # 多檔合併一次請求
    n = len(http.calls)
    cs.get(["AAA"], ["3M"])
    assert len(http.calls) == n + 1                                  # 不快取：每次都重抓


def test_falls_back_to_iex():
    http = FakeHttp(fail_sip=True)
    r = ChartService(http, now=lambda: NOW).get(["AAA"], ["3M"])
    assert r["AAA"]["3M"]["last"] == 102.0
    assert [c["feed"] for c in http.calls] == ["sip", "iex"]


def test_endpoint(tmp_path):
    env = _env(tmp_path)
    port = 8787
    app = create_app(env.paths, port, spawn=lambda: None, charts=ChartService(FakeHttp(), now=lambda: NOW))
    c = TestClient(app, base_url=f"http://127.0.0.1:{port}")
    r = c.get("/api/charts", params={"tickers": "aaa,BRK.B", "ranges": "1D,10Y"})
    assert r.status_code == 200 and set(r.json()) == {"AAA", "BRK.B"} and set(r.json()["AAA"]) == {"1D", "10Y"}
    assert c.get("/api/charts", params={"tickers": "AAA", "ranges": "5Y"}).status_code == 400
    assert c.get("/api/charts", params={"tickers": "A;B"}).status_code == 400
    no = TestClient(create_app(env.paths, port, spawn=lambda: None), base_url=f"http://127.0.0.1:{port}")
    assert no.get("/api/charts", params={"tickers": "AAA"}).status_code == 503
