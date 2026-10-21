"""P42：價格來源分工（D34）與時效（M4-D1-17、M4-D4-04、M4-D7-1~5）。不連網。"""
from __future__ import annotations

import dataclasses
import json
import os
import time

import httpx
import pandas as pd
import pytest

from test_daily_pipeline import CARD, D1, FakeAlpaca, FakeSec, _bars, _dec, _reg, _router, world  # noqa: F401
from trader.calendar import TradingCalendar
from trader.config import ConfigError, PROJECT_DIR, load_config
from trader.data import inputs as I
from trader.data import prices_adapter as PA
from trader.data import yahoo as Y
from trader.data.alpaca import AlpacaClient
from trader.data.fred import FredClient
from trader.data.http import DataError, HttpClient
from trader.data.yahoo import YahooClient
from trader.paths import get_paths
from trader.pipelines import daily as DP

NY = "America/New_York"


def _ts(day: str) -> int:
    return int(pd.Timestamp(f"{day} 09:30", tz=NY).timestamp())


# ---- D34：設定與 make_clients ----
def test_config_research_price_source_defaults_and_validation(tmp_path):
    env = tmp_path / "none.env"
    assert load_config(PROJECT_DIR / "config.yaml", env_file=env).data.research_price_source == "alpaca"
    assert load_config(PROJECT_DIR / "config.example.yaml", env_file=env).data.research_price_source == "alpaca"
    hark_yaml = PROJECT_DIR / "config.hark.yaml"   # .gitignore 排除、A 機沒有（X29）；存在時照常嚴格檢查
    if hark_yaml.is_file():
        hark = load_config(hark_yaml, env_file=env).data
        assert hark.research_price_source == "yahoo" and hark.adv_feed == "iex"
    bad = tmp_path / "c.yaml"
    bad.write_text("data: {research_price_source: google}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="research_price_source"):
        load_config(bad, env_file=env)


def _cfg(tmp_path, **data):
    cfg = load_config(PROJECT_DIR / "config.example.yaml", env_file=tmp_path / "none.env")
    return dataclasses.replace(cfg, data=dataclasses.replace(cfg.data, **data))


def test_make_clients_yahoo_never_in_alpaca_slot(tmp_path, state_dir, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    paths = get_paths(state_dir=state_dir).ensure()
    cl = I.make_clients(_cfg(tmp_path, research_price_source="yahoo"), paths)
    assert cl["alpaca"] is None and isinstance(cl["prices"], YahooClient)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    cl = I.make_clients(_cfg(tmp_path, research_price_source="yahoo"), paths)
    assert isinstance(cl["alpaca"], AlpacaClient) and isinstance(cl["prices"], YahooClient)
    cl = I.make_clients(_cfg(tmp_path), paths)
    assert cl["prices"] is cl["alpaca"] and isinstance(cl["alpaca"], AlpacaClient)


# ---- M4-D7-1 ----
def test_yahoo_calendar_raises_dataerror_and_calendar_falls_back(tmp_path):
    with pytest.raises(DataError):
        YahooClient().calendar("2026-10-01", "2026-10-31")
    cal = TradingCalendar.load(YahooClient(), tmp_path / "cal.json", "2026-10-01", "2026-10-31")
    assert cal.source == "rules"


def test_daily_rejects_yahoo_client():
    with pytest.raises(DP.DailyError, match="Yahoo"):
        DP.run_daily(None, None, None, D1, clients={"alpaca": YahooClient(), "sec": FakeSec()})


# ---- M4-D7-2 ----
def test_alpaca_bars_feed_override_in_params():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    c = AlpacaClient("k", "s", feed="iex", http=http)
    c.bars(["AAA"], "2026-10-01", "2026-10-02")
    c.bars(["AAA"], "2026-10-01", "2026-10-02", feed="sip")
    assert [p["feed"] for p in seen] == ["iex", "sip"]


class FeedAlpaca(FakeAlpaca):
    feed = "iex"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.feeds = []

    def bars(self, symbols, start, end, adjustment="raw", feed=None):
        self.feeds.append(feed)
        return super().bars(symbols, start, end, adjustment)


def test_daily_notes_adv_source_and_adv_feed_override(world):
    cfg, paths, conn = world
    alp = FeedAlpaca(_bars())
    s = DP.run_daily(cfg, paths, conn, D1, clients={"alpaca": alp, "sec": FakeSec(), "fred": None},
                     skip_review=True, router=_router(cfg, paths), dry_run=True)
    assert "ADV 來源＝iex" in s["notes"] and alp.feeds == [None]
    cfg2 = dataclasses.replace(cfg, data=dataclasses.replace(cfg.data, adv_feed="sip"))
    alp2 = FeedAlpaca(_bars())
    s2 = DP.run_daily(cfg2, paths, conn, D1, clients={"alpaca": alp2, "sec": FakeSec(), "fred": None},
                      skip_review=True, router=_router(cfg2, paths), dry_run=True)
    assert "ADV 來源＝sip" in s2["notes"] and alp2.feeds == [None, "sip"]


# ---- D34：下單紀錄記研究價 ----
def test_order_record_has_research_px_and_gap(world):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B", "review_price": 45.5})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    s = DP.run_daily(cfg, paths, conn, D1, clients={"alpaca": FakeAlpaca(_bars()), "sec": FakeSec(), "fred": None},
                     skip_review=True, router=_router(cfg, paths))
    assert "error" not in s, s
    o = s["orders"][0]
    assert o["research_px"] == 45.5 and o["research_gap"] == pytest.approx(o["limit_price"] / 45.5 - 1, abs=1e-4)
    assert s["research_price_source"] == "alpaca"


# ---- M4-D7-3 ----
class Sec8K(FakeSec):
    def __init__(self):
        self.calls = []
        self.stale = ["sub_CIK0000000001.json"]

    def cik_for(self, ticker):
        return "0000000001"

    def filings(self, cik, asof, since=None, forms=None, max_age_hours=None):
        self.calls.append((forms, max_age_hours))
        return [{"form": "8-K/A", "filingDate": D1, "items": "5.03", "accessionNumber": "a-1", "url": ""}]


def test_daily_8k_refetch_includes_amendments_and_notes_stale(world, monkeypatch):
    cfg, paths, conn = world
    db_pos = {"ticker": "AAA", "qty": 10, "avg_cost": 50.0, "first_buy_date": "2026-10-01", "updated_at": "x"}
    from trader import db
    db.insert(conn, "positions", db_pos)
    seen = {}

    def detect(conn, date, prev, tickers, alpaca_actions, filings_8k, *a, **k):
        seen.update(filings_8k)
        return []
    monkeypatch.setattr(DP.corp_actions, "detect", detect)
    sec = Sec8K()
    s = DP.run_daily(cfg, paths, conn, D1, clients={"alpaca": FakeAlpaca(_bars()), "sec": sec, "fred": None},
                     skip_review=True, router=_router(cfg, paths))
    assert sec.calls and all(c == (["8-K", "8-K/A"], 0) for c in sec.calls)
    assert [f["form"] for f in seen["AAA"]] == ["8-K/A"]
    assert any(n.startswith("SEC 抓取失敗改用舊快取") for n in s["notes"])


# ---- M4-D4-04 ----
def test_screen_prices_stale_price_marked_and_dropped():
    days = pd.bdate_range("2026-01-02", "2026-09-15").strftime("%Y-%m-%d")
    raw = pd.DataFrame({"ticker": "OLD", "date": days, "close": 10.0})
    out = PA.screen_prices(raw, raw.copy(), [], "2026-10-15")
    r = out.iloc[0]
    assert r["price_src"] == "過舊：2026-09-15" and pd.isna(r["price"])
    fresh = pd.DataFrame({"ticker": "NEW", "date": pd.bdate_range("2026-01-02", "2026-10-14").strftime("%Y-%m-%d"),
                          "close": 10.0})
    assert PA.screen_prices(fresh, fresh.copy(), [], "2026-10-15").iloc[0]["price_src"] == "alpaca"


# ---- M4-D7-4 ----
class BarsClient:
    feed = "alpaca"

    def __init__(self):
        self.ends = []

    def bars(self, symbols, start, end, adjustment="raw"):
        self.ends.append(end)
        days = [d for d in ("2026-10-14", "2026-10-15") if d <= end]
        return pd.DataFrame({"ticker": "AAA", "date": days, "open": 1.0, "high": 1.0, "low": 1.0,
                             "close": [10.0, 11.0][:len(days)], "volume": 1.0, "vwap": None})

    def corporate_actions(self, symbols, start, end, types=None):
        return []


@pytest.mark.parametrize("now,excluded", [("2026-10-15 12:00", True), ("2026-10-15 16:20", False),
                                          ("2026-10-16 09:00", False)])
def test_fetch_price_inputs_excludes_intraday_bar(monkeypatch, now, excluded):
    monkeypatch.setattr(PA, "ny_now", lambda: pd.Timestamp(now, tz=NY))
    c = BarsClient()
    px = PA.fetch_price_inputs(c, ["AAA"], "2026-10-15")
    assert set(c.ends) == {"2026-10-14" if excluded else "2026-10-15"}
    assert px["prices"].iloc[0]["price_date"] == ("2026-10-14" if excluded else "2026-10-15")
    assert bool(px["notes"]) is excluded and (not excluded or px["notes"][0].startswith("盤中排除"))


def test_last_close_ny():
    assert PA.last_close_ny(pd.Timestamp("2026-10-15 12:00", tz=NY)) == pd.Timestamp("2026-10-14 16:15", tz=NY)
    assert PA.last_close_ny(pd.Timestamp("2026-10-15 16:15", tz=NY)) == pd.Timestamp("2026-10-15 16:15", tz=NY)


# ---- P42R：asof 晚於紐約今天時的盤中排除；DST 切換日的 16:15 ----
class LateBarsClient(BarsClient):
    def bars(self, symbols, start, end, adjustment="raw"):
        self.ends.append(end)
        days = [d for d in ("2026-10-16", "2026-10-19", "2026-10-20") if d <= end]
        return pd.DataFrame({"ticker": "AAA", "date": days, "open": 1.0, "high": 1.0, "low": 1.0,
                             "close": [10.0, 11.0, 12.0][:len(days)], "volume": 1.0, "vwap": None})


def test_fetch_price_inputs_asof_after_ny_today_cut_to_ny_yesterday(monkeypatch):
    monkeypatch.setattr(PA, "ny_now", lambda: pd.Timestamp("2026-10-19 13:00", tz=NY))
    c = LateBarsClient()
    px = PA.fetch_price_inputs(c, ["AAA"], "2026-10-20")
    assert set(c.ends) == {"2026-10-18"}          # 不含 10-20、也不含 10-19 盤中半根
    assert px["prices"].iloc[0]["price_date"] == "2026-10-16"
    assert "daily" not in px   # 拆股調整日線已不抓（M4-R3-01）
    assert px["notes"] and px["notes"][0].startswith("盤中排除") and "2026-10-19" in px["notes"][0]


def test_fetch_price_inputs_asof_after_ny_today_after_close_not_cut(monkeypatch):
    monkeypatch.setattr(PA, "ny_now", lambda: pd.Timestamp("2026-10-19 16:20", tz=NY))
    c = LateBarsClient()
    px = PA.fetch_price_inputs(c, ["AAA"], "2026-10-20")
    assert set(c.ends) == {"2026-10-20"} and px["notes"] == []


@pytest.mark.parametrize("now,expect", [
    ("2026-11-01 17:00", "2026-11-01 16:15"),   # 夏令結束日（EST）
    ("2026-11-01 12:00", "2026-10-31 16:15"),   # 前一天仍是 EDT
    ("2026-11-02 09:00", "2026-11-01 16:15"),
    ("2026-03-08 10:00", "2026-03-07 16:15"),   # 夏令開始日（前一天 EST）
    ("2026-03-08 16:15", "2026-03-08 16:15"),
    ("2026-03-09 00:30", "2026-03-08 16:15"),
])
def test_last_close_ny_dst_transition_days(now, expect):
    got = PA.last_close_ny(pd.Timestamp(now, tz=NY))
    assert got == pd.Timestamp(expect, tz=NY)
    assert (got.hour, got.minute) == (16, 15)


def test_yahoo_cache_fetched_before_close_not_reused_after(tmp_path, monkeypatch):
    f = tmp_path / "AAA.json"
    f.write_text(json.dumps({"cached": True}), encoding="utf-8")
    mt = pd.Timestamp("2026-10-15 15:00", tz=NY).timestamp()
    os.utime(f, (mt, mt))
    monkeypatch.setattr(Y.time, "time", lambda: pd.Timestamp("2026-10-15 17:00", tz=NY).timestamp())
    monkeypatch.setattr(Y, "last_close_ny", lambda: pd.Timestamp("2026-10-15 16:15", tz=NY))
    monkeypatch.setattr(Y.time, "sleep", lambda s: None)

    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(Y.urllib.request, "urlopen", boom)
    assert YahooClient(tmp_path, min_interval=0)._chart("AAA") is None      # 不沿用收盤前快取 → 重抓（此處失敗）
    monkeypatch.setattr(Y, "last_close_ny", lambda: pd.Timestamp("2026-10-14 16:15", tz=NY))
    assert YahooClient(tmp_path, min_interval=0)._chart("AAA") == {"cached": True}


# ---- M4-D1-17 與合股標人工 ----
def _chart():
    days = ["2026-03-02", "2026-06-29", "2026-09-01"]
    return {"timestamp": [_ts(d) for d in days],
            "indicators": {"quote": [{"open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3, "close": [1.0] * 3,
                                      "volume": [1] * 3}], "adjclose": [{"adjclose": [1.0] * 3}]},
            "events": {"splits": {"a": {"date": _ts("2026-06-29"), "numerator": 1907.0, "denominator": 2000.0},
                                  "b": {"date": _ts("2026-09-01"), "numerator": 2.0, "denominator": 1.0}},
                       "dividends": {"x": {"date": _ts("2026-03-02"), "amount": 0.5},
                                     "y": {"date": _ts("2026-09-02"), "amount": 0.3}}}}


def test_yahoo_dividend_restored_to_paid_amount_and_merged_factor_needs_human():
    c = YahooClient(cache_dir=None)
    c._mem["HON"] = _chart()
    acts = c.corporate_actions(["HON"], "2026-01-01", "2026-10-15")
    div = {a["ex_date"]: a["cash_per_share"] for a in acts if a["type"] == "cash_dividend"}
    assert div["2026-03-02"] == pytest.approx(0.5 * 0.9535 * 2.0)   # 之後的係數全部乘回
    assert div["2026-09-02"] == pytest.approx(0.3)
    spin = [a for a in acts if a["type"] == "spin_off"]
    assert len(spin) == 1 and "needs_human" in spin[0]["raw"]
    split = [a for a in acts if a["type"] == "forward_split"]
    assert len(split) == 1 and "needs_human" not in split[0]["raw"]


# ---- M4-D7-5 ----
def test_fred_stale_cache_fallback_records_warning(tmp_path):
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="down")),
                      sleep=lambda s: None)
    (tmp_path / "DGS30.json").write_text(json.dumps([["2026-09-01", 4.5]]), encoding="utf-8")
    old = time.time() - 48 * 3600
    os.utime(tmp_path / "DGS30.json", (old, old))
    f = FredClient(None, tmp_path, http=http)
    assert f.t30("2026-10-15") == (0.045, "2026-09-01")
    assert f.warnings and "舊快取" in f.warnings[0]


class StaleFred:
    warnings = ["DGS30 抓取失敗（DataError），改用舊快取"]

    def t30(self, asof):
        return 0.045, "2026-09-01"

    def t30_median_20y(self, asof):
        return 0.04


def test_fetch_inputs_marks_stale_rates(tmp_path, state_dir):
    cfg = _cfg(tmp_path)
    paths = get_paths(state_dir=state_dir).ensure()
    s = I.fetch_inputs(cfg, paths, None, "2026-10-15", tmp_path / "out", tickers=["AAA"],
                       clients={"alpaca": None, "sec": None, "fred": StaleFred()})
    assert s["rates"]["stale"] is True and "2026-09-01" in s["rates"]["warning"]
    assert s["rates"]["fred_warnings"] == StaleFred.warnings


def test_fetch_inputs_uses_research_client_and_records_source(tmp_path, state_dir, monkeypatch):
    monkeypatch.setattr(PA, "ny_now", lambda: pd.Timestamp("2026-10-16 09:00", tz=NY))
    cfg = _cfg(tmp_path)
    paths = get_paths(state_dir=state_dir).ensure()

    class Fred(StaleFred):
        warnings = []

        def t30(self, asof):
            return 0.045, "2026-10-14"
    rc = BarsClient()
    rc.feed = "yahoo"
    s = I.fetch_inputs(cfg, paths, None, "2026-10-15", tmp_path / "out", tickers=["AAA"],
                       clients={"alpaca": None, "prices": rc, "sec": None, "fred": Fred()})
    assert s["price_source"] == "yahoo" and s["prices_ok"] == 1 and "stale" not in s["rates"]


def test_fetch_inputs_price_source_alpaca_label(tmp_path, state_dir, monkeypatch):
    monkeypatch.setattr(PA, "ny_now", lambda: pd.Timestamp("2026-10-16 09:00", tz=NY))
    cfg = _cfg(tmp_path)
    paths = get_paths(state_dir=state_dir).ensure()

    class Fred(StaleFred):
        warnings = []

        def t30(self, asof):
            return 0.045, "2026-10-14"
    rc = BarsClient()
    rc.feed = "iex"
    s = I.fetch_inputs(cfg, paths, None, "2026-10-15", tmp_path / "out", tickers=["AAA"],
                       clients={"alpaca": rc, "prices": rc, "sec": None, "fred": Fred()})
    assert s["price_source"] == "alpaca(iex)"
