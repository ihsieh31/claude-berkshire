"""RM1-4（M1-R7-01）：override 名單記進 universe_versions 後，補跑不得把它當成時點正確的成分股版本。不連網。"""
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import pytest

from trader import db
from trader.config import load_config
from trader.data import inputs as I
from trader.data import pe_median as P
from trader.data import universe as U
from trader.data.http import HttpClient
from trader.paths import get_paths

TODAY = datetime.now(timezone.utc).date()
OLD = (TODAY - timedelta(days=10)).isoformat()      # override 那一版的 asof
ASOF = (TODAY - timedelta(days=6)).isoformat()      # 補跑日（早於今天 ≥ BACKFILL_LAG_DAYS）
SMALL = [f"S{i:03d}" for i in range(40)]


def _no_net(request):
    raise AssertionError(f"不應連網：{request.url}")


def _client():
    return HttpClient(transport=httpx.MockTransport(_no_net), sleep=lambda s: None, retries=1)


@pytest.fixture
def env(tmp_path):
    paths = get_paths(tmp_path / "state", root=tmp_path).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    # 曾用過 40 檔 override → 記一版 source=override，改成 10 天前
    (paths.state / U.OVERRIDE_NAME).write_text("\n".join(SMALL) + "\n", encoding="utf-8")
    U.load_universe(paths, conn, http=_client())
    conn.execute("UPDATE universe_versions SET asof = ? WHERE source = 'override'", (OLD,))
    conn.commit()
    (paths.state / U.OVERRIDE_NAME).unlink()
    # 目前的正式名單（合格快取 500 檔）
    (paths.cache / "universe").mkdir(parents=True, exist_ok=True)
    rows = ["Symbol,Name,Sector"] + [f"T{i:04d},N,Tech" for i in range(500)]
    (paths.cache / "universe" / U.CACHE_NAME).write_text("\n".join(rows) + "\n", encoding="utf-8")
    yield paths, conn
    conn.close()


def test_backfill_skips_override_version(env):
    paths, conn = env
    t, src = U.load_universe(paths, conn, http=_client(), asof=ASOF)
    assert src == "cache_not_pit" and len(t) == 500     # 修前：version:{OLD}、40 檔


def test_backfill_uses_older_real_version_not_override(env):
    paths, conn = env
    older = (TODAY - timedelta(days=30)).isoformat()
    db.insert(conn, "universe_versions", {"asof": older, "source": "download", "tickers": ["AAA", "BBB"],
                                          "sha256": "x", "created_at": db.now_iso()})
    t, src = U.load_universe(paths, conn, http=_client(), asof=ASOF)
    assert t == ["AAA", "BBB"] and src == f"version:{older}"


class _Sec:
    stale, warnings = [], []

    def features(self, tickers, asof):
        return pd.DataFrame([{"ticker": t, "net_income": 1.0, "years_of_data": 10} for t in tickers])


class _Alpaca:
    def bars(self, symbols, start, end, adjustment="raw", timeframe="1Day"):
        return pd.DataFrame([{"ticker": s, "date": ASOF, "open": 10, "high": 10, "low": 10,
                              "close": 10.0, "volume": 1, "vwap": 10} for s in symbols])

    def corporate_actions(self, symbols, start, end, types=None):
        return []


class _Fred:
    def t30(self, asof):
        return 0.047, ASOF

    def t30_median_20y(self, asof):
        return 0.035


def test_fetch_inputs_backfill_does_not_freeze_override_list(tmp_path, env, monkeypatch):
    paths, conn = env
    monkeypatch.setattr(I, "fill_missing_shares", lambda feats, *a, **k: (feats, []))
    monkeypatch.setattr(I, "compute_pe_median", lambda f, p, asof: {"asof": asof, "pe_norm_median": 26.9})
    cfg_p = tmp_path / "c.yaml"
    cfg_p.write_text("models:\n  strong: {backend: fake, model: f}\n  medium: {backend: fake, model: f}\n"
                     "  weak: {backend: fake, model: f}\n", encoding="utf-8")
    cfg = load_config(cfg_p, env_file=tmp_path / "none.env")
    s = I.fetch_inputs(cfg, paths, conn, ASOF, tmp_path / "o", freeze_pe=True,
                       clients={"sec": _Sec(), "alpaca": _Alpaca(), "fred": _Fred()})
    assert s["universe_source"] == "cache_not_pit" and s["n_tickers"] == 500
    fz = P._read_frozen(P.freeze_path(paths, ASOF[:7]))
    assert fz is None or fz["n_tickers"] == 500          # 絕不以 40 檔 override 名單凍結
