"""RM4-4：M4-R5-02（舊格式 rates.json 缺 D29 鍵 → analyze 補抓／標缺）、M4-R3-01（刪無讀者的拆股調整日線與兩輸出）。

假客戶端，不連網。
"""
import json

import pandas as pd

import test_engine as TE
from test_fix_P40 import _Fred3m
from test_inputs import FakeAlpaca, FakeSec, _cfg
from trader import db
from trader.config import load_config
from trader.data import prices_adapter as PA
from trader.data.inputs import fetch_inputs
from trader.jobs.engine import Engine
from trader.paths import get_paths
from trader.pipelines import analyze as AP

ASOF = TE.ASOF
OLD_RATES = {"t30": 0.0552, "t30_date": "2026-10-14", "t30_20y_median": 0.0337}   # 修前格式（只有 t30 三鍵）


def _env(tmp_path, rates):
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    sd = paths.state / "screens" / ASOF
    (sd / "inputs").mkdir(parents=True)
    (sd / "summary.json").write_text(json.dumps({"to_analyze": [{"ticker": "KO"}]}), encoding="utf-8")
    (sd / "handoff_noprice.json").write_text(json.dumps(
        {"core_shortlist": [{"ticker": "KO", "screen_decision": "PASS"}], "special_handoff": []}), encoding="utf-8")
    pd.DataFrame([{"ticker": "KO", "price": 60.5, "price_date": "2026-10-15"}]).to_csv(sd / "inputs" / "prices.csv",
                                                                                       index=False)
    (sd / "inputs" / "rates.json").write_text(json.dumps(rates), encoding="utf-8")
    return cfg, paths, conn


def _run(tmp_path, monkeypatch, rates, fred):
    cfg, paths, conn = _env(tmp_path, rates)
    monkeypatch.setattr(Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})
    monkeypatch.setattr(AP, "fetch_inputs", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不應重抓價格")))
    out = AP.run_analyze(cfg, paths, conn, ASOF, clients={"sec": object(), "fred": fred})
    p = json.loads(conn.execute("SELECT params FROM jobs WHERE job_type='DOSSIER'").fetchone()["params"])
    conn.close()
    return out, p["rates"]


# ---------------------------------------------------------------- M4-R5-02

def test_old_format_rates_backfilled_from_fred_keeping_screen_t30(tmp_path, monkeypatch):
    out, r = _run(tmp_path, monkeypatch, OLD_RATES, _Fred3m())
    assert r["t30"] == 0.0552 and r["t30_date"] == "2026-10-14"        # 初選的 t30 不換（口徑一致）
    assert r["t3m"] == 0.039 and r["t3m_date"] == "2026-10-14"
    assert (r["t30_mean_5y"], r["t30_mean_10y"], r["t3m_mean_5y"], r["t3m_mean_10y"]) == (0.035, 0.03, 0.025, 0.015)
    assert "t30_extra_error" not in r and "t3m_extra_error" not in r
    assert set(out["rates_backfilled"]) == {"t30_mean_5y", "t30_mean_10y", "t3m", "t3m_date", "t3m_mean_5y",
                                            "t3m_mean_10y"}


def test_old_format_rates_without_fred_marked_missing(tmp_path, monkeypatch):
    out, r = _run(tmp_path, monkeypatch, OLD_RATES, None)
    assert r["t30"] == 0.0552 and "t3m" not in r
    assert "舊格式" in r["t30_extra_error"] and "舊格式" in r["t3m_extra_error"]   # 資料包利率段會寫「缺：…」
    assert out["rates_backfilled"] == []


def test_current_format_rates_not_refetched(tmp_path, monkeypatch):
    full = {**OLD_RATES, "t30_mean_5y": 0.04, "t30_mean_10y": 0.033, "t3m_extra_error": "DataError: x"}

    class NoFred:
        def __getattr__(self, name):
            raise AssertionError("完整格式（含 *_extra_error）不應重抓")

    out, r = _run(tmp_path, monkeypatch, full, NoFred())
    assert r["t30_mean_10y"] == 0.033 and r["t3m_extra_error"] == "DataError: x"
    assert out["rates_backfilled"] == []


# ---------------------------------------------------------------- M4-R3-01

class _Client:
    def __init__(self):
        self.adjs = []

    def bars(self, symbols, start, end, adjustment="raw"):
        self.adjs.append(adjustment)
        return pd.DataFrame([{"ticker": "AAA", "date": "2026-10-15", "open": 1.0, "high": 1.0, "low": 1.0,
                              "close": 10.0, "volume": 1.0, "vwap": None}])

    def corporate_actions(self, symbols, start, end):
        return []


def test_fetch_price_inputs_no_split_adjusted_pull():
    c = _Client()
    px = PA.fetch_price_inputs(c, ["AAA"], ASOF)
    assert c.adjs == ["raw", "all"] and "daily" not in px
    assert callable(PA.daily_closes)   # review 仍用 daily_closes


def test_fetch_inputs_no_daily_or_corporate_actions_files(tmp_path):
    paths = get_paths(tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    out = tmp_path / "out"
    out.mkdir()
    for f in ("daily.csv", "corporate_actions.json"):   # 舊版殘留檔重跑時清掉
        (out / f).write_text("old", encoding="utf-8")
    fetch_inputs(_cfg(tmp_path), paths, conn, "2026-10-16", out, tickers=["KO"],
                 clients={"sec": FakeSec(), "alpaca": FakeAlpaca(), "fred": _Fred3m()})
    assert (out / "prices.csv").is_file() and (out / "rates.json").is_file()
    assert not (out / "daily.csv").exists() and not (out / "corporate_actions.json").exists()
    conn.close()
