"""F09（M1-D9-03）：股票池快取與 P/E 凍結檔原子寫入；半截／不合格快取不被當成完整資料。全離線。"""
import os
import time

import httpx
import pytest

from trader import db
from trader.data import pe_median as P
from trader.data import universe as U
from trader.data.http import DataError, HttpClient
from trader.paths import get_paths


def _csv(n: int) -> str:
    return "Symbol,Name,Sector\n" + "".join(f"T{i:04d},Name {i},Tech\n" for i in range(n))


def _client(handler) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, retries=1)


def _no_net(request):
    raise AssertionError(f"不應連網：{request.url}")


@pytest.fixture
def env(tmp_path):
    paths = get_paths(tmp_path / "state", root=tmp_path)
    paths.ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    return paths, conn


def _cache(paths, text: str, age_days: float = 0):
    cache = paths.cache / "universe" / U.CACHE_NAME
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(cache, (old, old))
    return cache


def _boom(*a, **k):
    raise KeyboardInterrupt("模擬寫到一半被殺")


# ---- 股票池 ----

def test_fresh_truncated_cache_is_refetched(env):
    """新鮮快取只有 170 檔（半截）→ 不採用，重抓並以完整名單覆寫快取。"""
    paths, conn = env
    cache = _cache(paths, _csv(500)[: len(_csv(170))])
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, text=_csv(500))

    tickers, src = U.load_universe(paths, conn, http=_client(handler))
    assert src == "download" and len(tickers) == 500 and len(calls) == 1
    assert len(U._parse_csv(cache.read_text(encoding="utf-8"))) == 500
    rows = conn.execute("SELECT source FROM universe_versions").fetchall()
    assert [r["source"] for r in rows] == ["download"]  # 半截名單沒被記成一版


def test_fresh_unparseable_cache_is_refetched(env):
    """新鮮快取連標頭都不完整（缺 Symbol 欄）→ 視同沒有快取，重抓而不是拋錯。"""
    paths, conn = env
    _cache(paths, "Sym")
    tickers, src = U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(200, text=_csv(480))))
    assert src == "download" and len(tickers) == 480


def test_truncated_stale_cache_with_download_failure_raises(env):
    """下載失敗且舊快取只有 170 檔 → 拋 DataError，不默默用縮小的股票池。"""
    paths, conn = env
    _cache(paths, _csv(170), age_days=30)
    with pytest.raises(DataError, match="舊快取"):
        U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(500)))
    assert conn.execute("SELECT COUNT(*) FROM universe_versions").fetchone()[0] == 0


def test_unparseable_stale_cache_with_download_failure_raises_data_error(env):
    paths, conn = env
    _cache(paths, "garbage\x00", age_days=30)
    with pytest.raises(DataError):
        U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(500)))


def test_good_stale_cache_still_used(env):
    """回歸：合格舊快取（≥ MIN_UNIVERSE）在下載失敗時照用。"""
    paths, conn = env
    _cache(paths, _csv(U.MIN_UNIVERSE), age_days=30)
    tickers, src = U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(500)))
    assert src == "cache_stale" and len(tickers) == U.MIN_UNIVERSE


def test_universe_cache_write_is_atomic(env, monkeypatch):
    """寫快取中途被殺 → 舊快取完整保留、不留暫存檔。"""
    paths, conn = env
    cache = _cache(paths, _csv(460), age_days=30)
    before = cache.read_text(encoding="utf-8")
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(KeyboardInterrupt):
        U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(200, text=_csv(500))))
    assert cache.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in cache.parent.iterdir()) == [U.CACHE_NAME]


# ---- P/E 凍結檔 ----

@pytest.fixture
def paths(tmp_path):
    p = get_paths(tmp_path / "state", root=tmp_path)
    p.ensure()
    return p


def test_freeze_write_is_atomic(paths, monkeypatch):
    """寫凍結檔中途被殺 → 不留下凍結檔（下次會重算），也不留暫存檔。"""
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(KeyboardInterrupt):
        P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-01", "pe_norm_median": 30.0})
    d = P.freeze_path(paths, "2026-10").parent
    assert not d.exists() or list(d.iterdir()) == []


def test_freeze_truncated_file_is_recomputed(paths):
    path = P.freeze_path(paths, "2026-10")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"asof": "2026-10-01", "pe_norm_med', encoding="utf-8")
    calls = []

    def compute():
        calls.append(1)
        return {"asof": "2026-10-16", "pe_norm_median": 31.0}

    out = P.freeze_monthly(paths, "2026-10", compute)
    assert calls == [1] and out["pe_norm_median"] == 31.0
    assert P.freeze_monthly(paths, "2026-10", compute)["pe_norm_median"] == 31.0  # 修好後照常凍結
    assert calls == [1]


def test_get_frozen_truncated_file_falls_back(paths):
    """當月凍結檔半截 → 不拋 JSONDecodeError，退回上月；上月也沒有 → None。"""
    bad = P.freeze_path(paths, "2026-10")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('{"asof": "2026-10-0', encoding="utf-8")
    assert P.get_frozen(paths, "2026-10-20") is None
    P.freeze_monthly(paths, "2026-09", lambda: {"asof": "2026-09-01", "pe_norm_median": 25.0})
    assert P.get_frozen(paths, "2026-10-20")["pe_norm_median"] == 25.0
