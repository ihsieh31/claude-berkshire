"""股票池測試：全部使用 MockTransport，不連網。"""
import os
import time

import httpx
import pytest

from trader import db
from trader.data import universe as U
from trader.data.http import DataError, HttpClient
from trader.paths import get_paths


def _csv(n: int, extra: list[str] | None = None) -> str:
    rows = ["Symbol,Name,Sector"]
    rows += [f"T{i:04d},Name {i},Tech" for i in range(n)]
    rows += [f"{s},Name,Tech" for s in (extra or [])]
    return "\n".join(rows) + "\n"


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


def _versions(conn):
    return conn.execute("SELECT source, tickers, sha256 FROM universe_versions ORDER BY version_id").fetchall()


def test_normalize_ticker():
    assert U.normalize_ticker("BRK-B") == "BRK.B"
    assert U.normalize_ticker(" brk-b ") == "BRK.B"
    assert U.normalize_ticker("BF/B") == "BF.B"
    assert U.normalize_ticker("aapl") == "AAPL"


def test_override_takes_priority_and_skips_network(env):
    paths, conn = env
    (paths.state / U.OVERRIDE_NAME).write_text("# 註解\nbrk-b\n\nAAPL  # 蘋果\nAAPL\n", encoding="utf-8")
    tickers, src = U.load_universe(paths, conn, http=_client(_no_net))
    assert src == "override"
    assert tickers == ["AAPL", "BRK.B"]


def test_override_does_not_need_450(env):
    paths, conn = env
    (paths.state / U.OVERRIDE_NAME).write_text("MSFT\n", encoding="utf-8")
    tickers, src = U.load_universe(paths, conn, http=_client(_no_net))
    assert tickers == ["MSFT"] and src == "override"


def test_cache_hit_does_not_download(env):
    paths, conn = env
    cache = paths.cache / "universe" / U.CACHE_NAME
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(_csv(460), encoding="utf-8")
    tickers, src = U.load_universe(paths, conn, http=_client(_no_net))
    assert src == "cache"
    assert len(tickers) == 460


def test_expired_cache_is_refetched(env):
    paths, conn = env
    cache = paths.cache / "universe" / U.CACHE_NAME
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(_csv(460, ["OLD"]), encoding="utf-8")
    old = time.time() - 8 * 86400  # 超過預設 7 天
    os.utime(cache, (old, old))
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=_csv(500, ["BRK-B"]))

    tickers, src = U.load_universe(paths, conn, http=_client(handler))
    assert src == "download"
    assert len(calls) == 1
    assert "BRK.B" in tickers and "OLD" not in tickers
    assert "BRK.B" in U._parse_csv(cache.read_text(encoding="utf-8"))


def test_refresh_flag_forces_download(env):
    paths, conn = env
    cache = paths.cache / "universe" / U.CACHE_NAME
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(_csv(460), encoding="utf-8")
    tickers, src = U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(200, text=_csv(470))), refresh=True)
    assert src == "download" and len(tickers) == 470


def test_download_failure_uses_stale_cache(env):
    paths, conn = env
    cache = paths.cache / "universe" / U.CACHE_NAME
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(_csv(460), encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(cache, (old, old))
    tickers, src = U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(500)))
    assert src == "cache_stale"
    assert len(tickers) == 460


def test_download_failure_without_cache_raises(env):
    paths, conn = env
    with pytest.raises(DataError):
        U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(500)))


def test_too_small_download_is_anomaly_and_falls_back(env):
    paths, conn = env
    cache = paths.cache / "universe" / U.CACHE_NAME
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(_csv(460), encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(cache, (old, old))
    before = cache.read_text(encoding="utf-8")
    tickers, src = U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(200, text=_csv(449))), refresh=True)
    assert src == "cache_stale"
    assert len(tickers) == 460
    assert cache.read_text(encoding="utf-8") == before  # 異常下載不覆寫快取


def test_too_small_download_without_cache_raises(env):
    paths, conn = env
    with pytest.raises(DataError):
        U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(200, text=_csv(100))))


def test_universe_versions_only_on_change(env):
    paths, conn = env
    (paths.state / U.OVERRIDE_NAME).write_text("AAPL\nMSFT\n", encoding="utf-8")
    U.load_universe(paths, conn, http=_client(_no_net))
    U.load_universe(paths, conn, http=_client(_no_net))
    assert len(_versions(conn)) == 1  # 名單未變，不新增

    (paths.state / U.OVERRIDE_NAME).write_text("AAPL\nMSFT\nNVDA\n", encoding="utf-8")
    U.load_universe(paths, conn, http=_client(_no_net))
    rows = _versions(conn)
    assert len(rows) == 2 and rows[1]["source"] == "override"


def test_universe_versions_records_download_change(env):
    paths, conn = env
    U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(200, text=_csv(460))))
    U.load_universe(paths, conn, http=_client(_no_net), refresh=False)  # 快取命中，名單相同
    assert len(_versions(conn)) == 1
    U.load_universe(paths, conn, http=_client(lambda r: httpx.Response(200, text=_csv(461))), refresh=True)
    assert len(_versions(conn)) == 2


def test_backfill_asof_uses_version_of_that_time(env):
    """補跑過去日期：用當時記錄的名單（時點正確）；沒有那麼早的版本 → 目前名單並標 _not_pit。"""
    paths, conn = env
    db.insert(conn, "universe_versions", {"asof": "2026-01-05", "source": "download", "tickers": ["AAA", "BBB"],
                                          "sha256": "x", "created_at": db.now_iso()})
    t, src = U.load_universe(paths, conn, http=_client(_no_net), asof="2026-03-01")
    assert t == ["AAA", "BBB"] and src == "version:2026-01-05"
    (paths.cache / "universe").mkdir(parents=True, exist_ok=True)
    (paths.cache / "universe" / U.CACHE_NAME).write_text(_csv(500), encoding="utf-8")
    t, src = U.load_universe(paths, conn, http=_client(_no_net), asof="2025-06-01")
    assert len(t) == 500 and src == "cache_not_pit"
