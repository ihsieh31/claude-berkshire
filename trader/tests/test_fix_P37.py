"""P37（M1-D）：輸入覆蓋率門檻、override 不凍結、補跑判斷容許時差、非時點警告、股票池固定、REIT 不算金融、method 寫明母體。不連網。"""
import importlib.util
import json
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
from trader.pipelines import screen as SP

ASOF = "2026-10-15"


class _Sec:
    stale, warnings = [], []

    def __init__(self, no_data=()):
        self.no_data = set(no_data)

    def features(self, tickers, asof):
        return pd.DataFrame([{"ticker": t, "net_income": 1.0,
                              "years_of_data": None if t in self.no_data else 10} for t in tickers])


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


def _cfg(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("models:\n  strong: {backend: fake, model: f}\n  medium: {backend: fake, model: f}\n"
                 "  weak: {backend: fake, model: f}\n", encoding="utf-8")
    return load_config(p, env_file=tmp_path / "none.env")


TICKERS = [f"T{i:03d}" for i in range(40)]


@pytest.fixture
def fenv(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "state").ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    monkeypatch.setattr(I, "load_universe", lambda paths, conn, asof: (list(TICKERS), "download"))
    monkeypatch.setattr(I, "fill_missing_shares", lambda feats, *a, **k: (feats, []))
    monkeypatch.setattr(I, "compute_pe_median", lambda f, p, asof: {"asof": asof, "pe_norm_median": 26.9})
    yield paths, conn
    conn.close()


def _fetch(tmp_path, paths, conn, out="o", no_data=(), **kw):
    return I.fetch_inputs(_cfg(tmp_path), paths, conn, ASOF, tmp_path / out, freeze_pe=True,
                          clients={"sec": _Sec(no_data), "alpaca": _Alpaca(), "fred": _Fred()}, **kw)


# ---------------------------------------------------------------- M1-D7-2
def test_low_coverage_does_not_freeze(tmp_path, fenv):
    paths, conn = fenv
    s = _fetch(tmp_path, paths, conn, no_data=TICKERS[:20])          # 一半代號 SEC 無資料
    assert s["coverage"] == 0.5
    assert isinstance(s["pe_median"], str) and "覆蓋率" in s["pe_median"]
    assert P._read_frozen(P.freeze_path(paths, "2026-10")) is None


def test_high_coverage_freezes_and_records_coverage(tmp_path, fenv):
    paths, conn = fenv
    s = _fetch(tmp_path, paths, conn, no_data=TICKERS[:2])           # 95%
    assert s["coverage"] == 0.95
    fz = P._read_frozen(P.freeze_path(paths, "2026-10"))
    assert fz["pe_norm_median"] == 26.9 and fz["coverage"] == 0.95 and fz["n_tickers"] == 40


def _skill_make():
    p = get_paths().skill_screen / "scripts" / "test_screen.py"
    spec = importlib.util.spec_from_file_location("skill_test_screen", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.make()


@pytest.fixture
def senv(tmp_path, state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    yield paths, conn
    conn.close()


def _write_inputs(d, no_data_frac=0.0):
    d.mkdir(parents=True, exist_ok=True)
    F, Pr = _skill_make()
    F["years_of_data"] = 10.0
    F.loc[F.index[:int(len(F) * no_data_frac)], "years_of_data"] = None
    F.to_csv(d / "features.csv", index=False)
    Pr.to_csv(d / "prices.csv", index=False)


def test_run_screen_low_coverage_fails(tmp_path, senv):
    paths, conn = senv
    _write_inputs(tmp_path / "in", no_data_frac=0.5)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=tmp_path / "in")
    assert "覆蓋率" in s["error"] and "shortlist" not in s
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "failed"


def test_run_screen_good_coverage_ok(tmp_path, senv):
    paths, conn = senv
    _write_inputs(tmp_path / "in", no_data_frac=0.05)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=tmp_path / "in")
    assert "error" not in s and s["coverage"] == 0.95


class _SecAllFail(_Sec):
    """非 403 全失敗（逾時／5xx／DNS）時 sec.features 每列只有 ticker/cik/data_notes：無 years_of_data、net_income 欄。"""
    def features(self, tickers, asof):
        return pd.DataFrame([{"ticker": t, "cik": "0000000001", "data_notes": "抓取失敗：DataError"} for t in tickers])


def test_all_failed_features_coverage_zero_no_freeze(tmp_path, fenv, monkeypatch):
    paths, conn = fenv
    calls = []
    monkeypatch.setattr(I, "compute_pe_median", lambda f, p, asof: calls.append(1) or {"pe_norm_median": 1.0})
    s = I.fetch_inputs(_cfg(tmp_path), paths, conn, ASOF, tmp_path / "o", freeze_pe=True,
                       clients={"sec": _SecAllFail(), "alpaca": _Alpaca(), "fred": _Fred()})
    assert s["coverage"] == 0 and "覆蓋率" in s["pe_median"] and not calls
    assert P._read_frozen(P.freeze_path(paths, "2026-10")) is None


def test_coverage_fallback_to_net_income():
    px = pd.DataFrame({"ticker": ["A", "B"], "price": [1.0, 1.0]})
    assert I.coverage(pd.DataFrame({"ticker": ["A", "B"], "net_income": [1.0, None]}), px, 2) == 0.5
    assert I.coverage(pd.DataFrame({"ticker": ["A", "B"], "data_notes": ["x", "x"]}), px, 2) == 0


def test_run_screen_all_failed_features_fails(tmp_path, senv):
    paths, conn = senv
    d = tmp_path / "in"; d.mkdir()
    F, Pr = _skill_make()
    pd.DataFrame([{"ticker": t, "cik": "1", "data_notes": "抓取失敗：DataError"} for t in F["ticker"]]).to_csv(
        d / "features.csv", index=False)
    Pr.to_csv(d / "prices.csv", index=False)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=d)
    assert s["coverage"] == 0 and "覆蓋率" in s["error"] and "shortlist" not in s
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "failed"


def test_override_hint_mentions_universe_file(tmp_path, fenv, monkeypatch):
    paths, conn = fenv
    monkeypatch.setattr(I, "load_universe", lambda paths, conn, asof: (list(TICKERS), "override"))
    s = _fetch(tmp_path, paths, conn)
    assert "override" in s["pe_median"] and I.UNIVERSE_FILE in s["pe_median"]


# ---------------------------------------------------------------- M1-D7-5
def test_override_universe_does_not_freeze(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "state").ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    monkeypatch.setattr(I, "fill_missing_shares", lambda feats, *a, **k: (feats, []))
    monkeypatch.setattr(I, "compute_pe_median", lambda f, p, asof: {"asof": asof, "pe_norm_median": 26.9})
    (paths.state / U.OVERRIDE_NAME).write_text("\n".join(TICKERS), encoding="utf-8")
    s = _fetch(tmp_path, paths, conn)
    assert s["universe_source"] == "override"
    assert isinstance(s["pe_median"], str) and "override" in s["pe_median"]
    assert P._read_frozen(P.freeze_path(paths, "2026-10")) is None


# ---------------------------------------------------------------- M1-D7-3
def _no_net(request):
    raise AssertionError(f"不應連網：{request.url}")


def _uenv(tmp_path):
    paths = get_paths(tmp_path / "state", root=tmp_path).ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    db.insert(conn, "universe_versions", {"asof": "2020-01-01", "source": "download", "tickers": ["OLD"],
                                          "sha256": "x", "created_at": db.now_iso()})
    (paths.cache / "universe").mkdir(parents=True, exist_ok=True)
    rows = ["Symbol,Name"] + [f"T{i:04d},N" for i in range(500)]
    (paths.cache / "universe" / U.CACHE_NAME).write_text("\n".join(rows) + "\n", encoding="utf-8")
    return paths, conn, HttpClient(transport=httpx.MockTransport(_no_net), sleep=lambda s: None, retries=0)


def test_recent_asof_is_not_backfill(tmp_path):
    """從台灣跑或美東晚上才輪到：asof＝UTC 昨天，仍走快取並記新版，不回舊版本。"""
    paths, conn, http = _uenv(tmp_path)
    yday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    t, src = U.load_universe(paths, conn, http=http, asof=yday)
    assert src == "cache" and len(t) == 500
    assert conn.execute("SELECT source FROM universe_versions ORDER BY version_id DESC").fetchone()["source"] == "cache"


def test_old_asof_uses_version(tmp_path):
    paths, conn, http = _uenv(tmp_path)
    old = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
    t, src = U.load_universe(paths, conn, http=http, asof=old)
    assert t == ["OLD"] and src == "version:2020-01-01"


# ---------------------------------------------------------------- M1-D6-04
def test_not_pit_universe_warns(tmp_path, senv, monkeypatch):
    paths, conn = senv

    def fake_fetch(cfg, paths, conn, asof, outdir, **kw):
        _write_inputs(outdir)
        return {"universe_source": "cache_not_pit", "n_tickers": 120}
    monkeypatch.setattr(SP, "fetch_inputs", fake_fetch)
    s = SP.run_screen(None, paths, conn, ASOF)
    assert "error" not in s and "非時點" in s["warning"] and "cache_not_pit" in s["warning"]


# ---------------------------------------------------------------- M1-D9-04
def test_universe_file_written_then_reused(tmp_path, fenv, monkeypatch):
    paths, conn = fenv
    s1 = _fetch(tmp_path, paths, conn)
    f = tmp_path / "o" / I.UNIVERSE_FILE
    assert f.is_file() and s1["universe_sha256"] == U.universe_sha256(TICKERS)
    f.write_text("# source=download\nAAA\nBBB\nCCC\n", encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("不應呼叫 load_universe")
    monkeypatch.setattr(I, "load_universe", boom)
    s2 = _fetch(tmp_path, paths, conn)
    assert s2["n_tickers"] == 3 and s2["universe_source"] == "pinned:download"
    assert s2["universe_sha256"] == U.universe_sha256(["AAA", "BBB", "CCC"])


def test_run_screen_rerun_keeps_universe_file(tmp_path, senv, monkeypatch):
    paths, conn = senv
    seen = []

    def fake_fetch(cfg, paths, conn, asof, outdir, **kw):
        seen.append((outdir / I.UNIVERSE_FILE).is_file())
        if not seen[-1]:
            (outdir).mkdir(parents=True, exist_ok=True)
            (outdir / I.UNIVERSE_FILE).write_text("# source=download\nAAA\n", encoding="utf-8")
        _write_inputs(outdir)
        return {"universe_source": "download", "n_tickers": 120}
    monkeypatch.setattr(SP, "fetch_inputs", fake_fetch)
    assert "error" not in SP.run_screen(None, paths, conn, ASOF)
    assert "error" not in SP.run_screen(None, paths, conn, ASOF, force=True)
    assert seen == [False, True]


# ---------------------------------------------------------------- D20／D70
def _synth(n=40, fin=20, reit=10):
    rows, px = [], []
    for i in range(n):
        price = 10.0 + i
        mc = price * 1_000_000
        rows.append({"ticker": f"T{i:03d}", "shares_cover": 1_000_000.0, "shares_cover_filed": "2025-01-01",
                     "shares": 1_000_000.0, "shares_filed_date": "2025-01-01",
                     "net_income": mc / 15.0, "ni5_mean": mc / (20.0 + i), "fcf": mc / 15.0,
                     "is_financial": 1 if i < fin else 0, "sic": 6798 if i < reit else (6020 if i < fin else 3571)})
        px.append({"ticker": f"T{i:03d}", "price": price, "price_date": ASOF})
    return pd.DataFrame(rows), pd.DataFrame(px)


def test_reit_not_counted_as_financial():
    f, p = _synth(n=80, fin=50, reit=10)
    r = P.compute_pe_median(f, p, ASOF)
    assert r["n_fin"] == 40                       # 50 檔金融中 10 檔 REIT 剔除
    assert r["fin_pe_norm_median"] == pytest.approx(pd.Series([20.0 + i for i in range(10, 50)]).median())


def test_method_names_population():
    f, p = _synth()
    assert "S&P 500" in P.compute_pe_median(f, p, ASOF)["method"]
