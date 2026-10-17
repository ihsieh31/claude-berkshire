"""Alpaca 客戶端測試：全部走 httpx.MockTransport，不連網。"""
import json

import httpx
import pytest

from trader.data import alpaca
from trader.data.alpaca import AlpacaClient
from trader.data.http import DataError, HttpClient

KEY = "KEY_TEST_123"
SECRET = "SECRET_TEST_456"


def _client(handler) -> AlpacaClient:
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None,
                      headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET},
                      secrets=[KEY, SECRET])
    return AlpacaClient(KEY, SECRET, http=http)


def _json(body, status=200):
    return httpx.Response(status, json=body)


# ---- bars ----

def test_bars_paginates_two_pages():
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        if "page_token" not in request.url.params:
            return _json({"bars": {"AAPL": [
                {"t": "2026-03-10T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100, "vw": 1.2},
            ]}, "next_page_token": "T2"})
        assert request.url.params["page_token"] == "T2"
        return _json({"bars": {"AAPL": [
            {"t": "2026-03-11T04:00:00Z", "o": 2, "h": 3, "l": 1.5, "c": 2.5, "v": 200, "vw": 2.2},
            {"t": "2026-03-12T04:00:00Z", "o": 3, "h": 4, "l": 2.5, "c": 3.5, "v": 300, "vw": 3.2},
        ]}, "next_page_token": None})

    df = _client(handler).bars(["aapl"], "2026-03-10", "2026-03-12")
    assert len(calls) == 2
    assert calls[0]["symbols"] == "AAPL"
    assert calls[0]["adjustment"] == "raw"
    assert calls[0]["feed"] == "iex"
    assert list(df["date"]) == ["2026-03-10", "2026-03-11", "2026-03-12"]
    assert list(df["close"]) == [1.5, 2.5, 3.5]


def test_bars_batches_over_100_symbols():
    seen_batches = []

    def handler(request):
        seen_batches.append(request.url.params["symbols"].split(","))
        return _json({"bars": {}, "next_page_token": None})

    syms = [f"S{i:04d}" for i in range(150)]
    _client(handler).bars(syms, "2026-01-01", "2026-01-31")
    assert len(seen_batches) == 2
    assert len(seen_batches[0]) == 100
    assert len(seen_batches[1]) == 50
    assert seen_batches[0] + seen_batches[1] == syms


@pytest.mark.parametrize("t, expected", [
    ("2026-03-10T04:00:00Z", "2026-03-10"),   # UTC 04:00 = 紐約 3/10 00:00（EDT）
    ("2026-03-10T13:30:00Z", "2026-03-10"),
    ("2026-03-09T23:00:00Z", "2026-03-09"),   # 紐約仍是 3/9 19:00
])
def test_bar_date_is_new_york_date(t, expected):
    def handler(request):
        return _json({"bars": {"MSFT": [{"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}]},
                      "next_page_token": None})

    df = _client(handler).bars(["MSFT"], "2026-03-01", "2026-03-31")
    assert df.loc[0, "date"] == expected


def test_bars_empty_result_has_all_columns():
    def handler(request):
        return _json({"bars": None, "next_page_token": None})

    df = _client(handler).bars(["AAPL"], "2026-01-01", "2026-01-02")
    assert df.empty
    assert list(df.columns) == ["ticker", "date", "open", "high", "low", "close", "volume", "vwap"]


def test_bars_request_carries_key_headers(monkeypatch):
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("APCA-API-KEY-ID")
        seen["secret"] = request.headers.get("APCA-API-SECRET-KEY")
        return _json({"bars": {}, "next_page_token": None})

    real = alpaca.HttpClient

    def factory(**kw):  # 用真正的預設 headers 建構，只替換 transport 與 sleep
        kw["transport"] = httpx.MockTransport(handler)
        kw["sleep"] = lambda s: None
        return real(**kw)

    monkeypatch.setattr(alpaca, "HttpClient", factory)
    AlpacaClient(KEY, SECRET).bars(["AAPL"], "2026-01-01", "2026-01-02")
    assert seen == {"key": KEY, "secret": SECRET}


# ---- corporate_actions ----

def _ca_body(groups, token=None):
    return {"corporate_actions": groups, "next_page_token": token}


def test_corporate_actions_normalizes_all_known_types():
    groups = {
        "forward_splits": [{"symbol": "NVDA", "new_rate": 4, "old_rate": 1, "ex_date": "2024-06-10",
                            "process_date": "2024-06-07"}],
        "reverse_splits": [{"symbol": "AMC", "new_rate": 1, "old_rate": 10, "ex_date": "2023-01-02"}],
        "cash_dividends": [{"symbol": "KO", "rate": 0.5, "ex_date": "2026-03-13", "payable_date": "2026-04-01"}],
        "stock_dividends": [{"symbol": "XYZ", "rate": 0.1, "ex_date": "2026-02-01"}],
        "spin_offs": [{"source_symbol": "GE", "new_symbol": "GEV", "ex_date": "2024-04-02"}],
        "cash_mergers": [{"acquiree_symbol": "OLD-A", "rate": 25.0, "ex_date": "2026-05-01"}],
        "stock_mergers": [{"acquiree_symbol": "TGT", "acquirer_symbol": "ACQ",
                           "acquiree_rate": 2, "acquirer_rate": 0.5, "ex_date": "2026-06-01"}],
        "stock_and_cash_mergers": [{"acquiree_symbol": "T1", "acquirer_symbol": "T2", "acquiree_rate": 1,
                                    "acquirer_rate": 0.25, "cash_rate": 3.0, "ex_date": "2026-07-01"}],
        "name_changes": [{"old_symbol": "FB", "new_symbol": "META", "effective_date": "2022-06-09"}],
    }

    def handler(request):
        return _json(_ca_body(groups))

    rows = {r["type"]: r for r in _client(handler).corporate_actions(["NVDA"], "2024-01-01", "2026-12-31")}
    assert set(rows) == {"forward_split", "reverse_split", "cash_dividend", "stock_dividend", "spin_off",
                         "cash_merger", "stock_merger", "stock_and_cash_merger", "name_change"}

    assert rows["forward_split"]["ticker"] == "NVDA"
    assert rows["forward_split"]["ratio_new_per_old"] == 4.0
    assert rows["forward_split"]["process_date"] == "2024-06-07"
    assert rows["forward_split"]["record_date"] is None  # 缺欄位為 None

    assert rows["reverse_split"]["ticker"] == "AMC"
    assert rows["reverse_split"]["ratio_new_per_old"] == pytest.approx(0.1)

    assert rows["cash_dividend"]["cash_per_share"] == 0.5
    assert rows["cash_dividend"]["payable_date"] == "2026-04-01"

    assert rows["stock_dividend"]["ratio_new_per_old"] == pytest.approx(1.1)

    assert rows["spin_off"]["ticker"] == "GE"
    assert rows["spin_off"]["new_ticker"] == "GEV"

    assert rows["cash_merger"]["ticker"] == "OLD.A"  # 代號標準化："-" → "."
    assert rows["cash_merger"]["cash_per_share"] == 25.0

    assert rows["stock_merger"]["ticker"] == "TGT"
    assert rows["stock_merger"]["new_ticker"] == "ACQ"
    assert rows["stock_merger"]["ratio_new_per_old"] == pytest.approx(0.25)
    assert rows["stock_merger"]["cash_per_share"] is None

    assert rows["stock_and_cash_merger"]["ratio_new_per_old"] == pytest.approx(0.25)
    assert rows["stock_and_cash_merger"]["cash_per_share"] == 3.0

    nc = rows["name_change"]
    assert nc["ticker"] == "FB" and nc["new_ticker"] == "META"
    assert nc["ex_date"] == "2022-06-09"  # ex_date 缺 → 用 effective_date

    for r in rows.values():
        assert r["raw"] is not None
        assert set(r) >= {"ticker", "type", "ex_date", "process_date", "record_date", "payable_date",
                          "ratio_new_per_old", "cash_per_share", "new_ticker", "raw"}


def test_corporate_actions_unknown_key_and_raw_kept():
    item = {"symbol": "xyz-a", "ex_date": "2026-01-05", "extra": 1}

    def handler(request):
        return _json(_ca_body({"rights_distributions": [item]}))

    rows = _client(handler).corporate_actions(["XYZ.A"], "2026-01-01", "2026-01-31")
    assert len(rows) == 1
    assert rows[0]["type"] == "rights_distribution"
    assert rows[0]["ticker"] == "XYZ.A"
    assert rows[0]["raw"] == item


def test_corporate_actions_paginates_and_sends_types():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if "page_token" not in request.url.params:
            return _json(_ca_body({"cash_dividends": [{"symbol": "A", "rate": 1}]}, token="P2"))
        return _json(_ca_body({"cash_dividends": [{"symbol": "B", "rate": 2}]}, token=None))

    rows = _client(handler).corporate_actions(["A", "B"], "2026-01-01", "2026-02-01",
                                              types=["cash_dividend", "forward_split"])
    assert [r["ticker"] for r in rows] == ["A", "B"]
    assert len(seen) == 2
    assert seen[0]["types"] == "cash_dividend,forward_split"
    assert seen[1]["page_token"] == "P2"


# ---- news ----

def test_news_expands_per_ticker_and_keeps_only_requested():
    news = [{
        "id": 1, "created_at": "2026-03-10T04:00:00Z", "headline": "H", "source": "S",
        "url": "U", "summary": "X", "symbols": ["AAPL", "MSFT", "TSLA"],
    }, {
        "id": 2, "created_at": "2026-03-11T13:00:00Z", "headline": "H2", "source": "S",
        "url": "U2", "summary": "X2", "symbols": ["TSLA"],
    }]

    def handler(request):
        assert request.url.params["symbols"] == "AAPL,MSFT"
        return _json({"news": news, "next_page_token": None})

    out = _client(handler).news(["aapl", "MSFT"], "2026-03-01", "2026-03-31")
    assert [(r["id"], r["ticker"]) for r in out] == [(1, "AAPL"), (1, "MSFT")]
    assert all(r["date"] == "2026-03-10" for r in out)
    assert out[0]["headline"] == "H" and out[0]["url"] == "U"


def test_news_paginates():
    def handler(request):
        if "page_token" not in request.url.params:
            return _json({"news": [{"id": 1, "created_at": "2026-03-10T04:00:00Z", "symbols": ["AAPL"]}],
                          "next_page_token": "N2"})
        return _json({"news": [{"id": 2, "created_at": "2026-03-11T04:00:00Z", "symbols": ["AAPL"]}],
                      "next_page_token": None})

    out = _client(handler).news(["AAPL"], "2026-03-01", "2026-03-31")
    assert [r["id"] for r in out] == [1, 2]


# ---- 錯誤處理 ----

def test_401_raises_data_error_without_secret(monkeypatch):
    def handler(request):
        # 模擬伺服器把金鑰回顯在錯誤內文，確認會被遮罩
        return httpx.Response(401, text=f"unauthorized key={KEY} secret={SECRET}")

    real = alpaca.HttpClient

    def factory(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        kw["sleep"] = lambda s: None
        return real(**kw)

    monkeypatch.setattr(alpaca, "HttpClient", factory)
    client = AlpacaClient(KEY, SECRET)
    with pytest.raises(DataError) as ei:
        client.bars(["AAPL"], "2026-01-01", "2026-01-02")
    msg = str(ei.value)
    assert "401" in msg
    assert KEY not in msg
    assert SECRET not in msg


def test_429_then_success_retries():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="slow down")
        return _json({"bars": {"AAPL": [{"t": "2026-03-10T04:00:00Z", "o": 1, "h": 1, "l": 1,
                                          "c": 1, "v": 1, "vw": 1}]}, "next_page_token": None})

    slept = []
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=slept.append,
                      headers={"APCA-API-KEY-ID": KEY}, secrets=[KEY])
    df = AlpacaClient(KEY, SECRET, http=http).bars(["AAPL"], "2026-03-01", "2026-03-31")
    assert len(attempts) == 2
    assert slept == [1.0]  # 依 Retry-After 等待
    assert len(df) == 1


def test_merger_ex_date_falls_back_to_effective_date_and_windows():
    seen = []

    def handler(req):
        seen.append((req.url.params["start"], req.url.params["end"]))
        body = {"stock_mergers": [{"id": "m1", "acquirer_symbol": "BIG", "acquirer_rate": 0.5,
                                   "acquiree_symbol": "SML", "acquiree_rate": 1, "effective_date": "2025-06-02"}]}
        return _json(_ca_body(body))

    out = _client(handler).corporate_actions(["SML"], "2024-01-01", "2026-06-30")
    assert seen == [("2024-01-01", "2024-12-30"), ("2024-12-31", "2025-12-30"), ("2025-12-31", "2026-06-30")]
    assert len(out) == 1  # 同一事件在多個區段重複出現 → 去重
    assert out[0]["ex_date"] == "2025-06-02" and out[0]["ratio_new_per_old"] == 0.5
