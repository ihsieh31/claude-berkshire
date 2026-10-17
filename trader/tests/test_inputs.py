"""fetch_inputs 組裝測試：假客戶端，不連網。"""
import json

import pandas as pd

from trader.config import load_config
from trader.data.inputs import fetch_inputs
from trader.paths import get_paths
from trader import db


class FakeSec:
    stale = []
    warnings = []

    def features(self, tickers, asof):
        return pd.DataFrame([{"ticker": t, "net_income": 1.0} for t in tickers])


class FakeFred:
    def t30(self, asof):
        return 0.047, "2026-10-15"

    def t30_median_20y(self, asof):
        return 0.035


class FakeAlpaca:
    def bars(self, symbols, start, end, adjustment="raw", timeframe="1Day"):
        return pd.DataFrame([{"ticker": s, "date": "2026-10-15", "open": 10, "high": 10, "low": 10,
                              "close": 10.0, "volume": 1, "vwap": 10} for s in symbols])

    def corporate_actions(self, symbols, start, end, types=None):
        return [{"ticker": symbols[0], "type": "cash_dividend", "ex_date": "2026-09-01", "raw": {"x": 1}}]


def _cfg(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("models:\n  strong: {backend: fake, model: f}\n  medium: {backend: fake, model: f}\n"
                 "  weak: {backend: fake, model: f}\n", encoding="utf-8")
    return load_config(p, env_file=tmp_path / "none.env")


def test_fetch_inputs_writes_all_files(tmp_path):
    paths = get_paths(tmp_path / "state").ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    out = tmp_path / "out"
    s = fetch_inputs(_cfg(tmp_path), paths, conn, "2026-10-16", out, tickers=["brk-b", "KO"], freeze_pe=True,
                     clients={"sec": FakeSec(), "alpaca": FakeAlpaca(), "fred": FakeFred()})
    for f in ("features.csv", "prices.csv", "daily.csv", "corporate_actions.json", "rates.json"):
        assert (out / f).is_file(), f
    assert list(pd.read_csv(out / "features.csv").ticker) == ["BRK.B", "KO"]
    assert json.loads((out / "rates.json").read_text())["t30"] == 0.047
    assert "raw" not in json.loads((out / "corporate_actions.json").read_text())[0]
    assert "略過" in s["pe_median"]  # 部分代號不得凍結 P/E 中位
    assert not (paths.cache / "pe_median").exists()


def test_fetch_inputs_skips_missing_sources(tmp_path):
    paths = get_paths(tmp_path / "state").ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    s = fetch_inputs(_cfg(tmp_path), paths, conn, "2026-10-16", tmp_path / "o", tickers=["KO"],
                     clients={"sec": None, "alpaca": None, "fred": FakeFred()})
    assert len(s["skipped"]) == 2 and s["rates"]["t30"] == 0.047
