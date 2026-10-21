"""P24R（P24 返工；設計決策 D03 回寫與 review_due）：

- review_due 荒謬級重新觸發為邊緣觸發（只認 max(上次減碼日, 上次複查日) 之後的線下收盤），只認本持有期的 last_trim；
- review_due 判定的「回到線下」證據（absurd_rearm）經 review.py 觸發參數帶進 PRICE → HOLD_REVIEW，回寫端認同一份證據。
（review_due.check 本身的單元測試在 skill 的 scripts/test_review_due.py。）
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

import test_engine as TE
import test_fix_P24 as P
import test_review_jobs as TR
import test_review_pipeline as RPT
from trader import db
from trader.analysis import writeback as W
from trader.config import load_config
from trader.paths import get_paths
from trader.pipelines.review import export_registry, run_review

ASOF = RPT.ASOF                     # 2026-10-16
DIP = ("2026-10-05", "2026-10-06", "2026-10-07")


class DipAlpaca(RPT.FakeAlpaca):
    """30 個交易日收盤 101；DIP 三天 99.5（荒謬級線 100 以下；漲跌不到 4%，不觸發單日 ±4%）。"""

    def bars(self, symbols, start, end, adjustment="raw"):
        rows = [{"ticker": t, "date": d.strftime("%Y-%m-%d"),
                 "close": 99.5 if d.strftime("%Y-%m-%d") in DIP else 101.0}
                for t in symbols for d in pd.bdate_range(end=end, periods=30)]
        return pd.DataFrame(rows, columns=["ticker", "date", "close"])


def _seed_abs(conn, data: dict, first_buy: str) -> None:
    base = {"cik": "0000000009", "last_full_review_date": "2026-09-01", "cost": 50, "absurd_price_line": 100,
            "t30_at_valuation": 0.047}
    db.upsert(conn, "registry", {"ticker": "ABS", "status": "HOLDING", "data": {**base, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])
    db.insert(conn, "positions", {"ticker": "ABS", "qty": 100, "avg_cost": 50.0, "first_buy_date": first_buy,
                                  "updated_at": db.now_iso()})


def _run(env):
    cfg, paths, conn, tmp = env
    (paths.state / "cap_inputs").mkdir(parents=True, exist_ok=True)
    (paths.state / "cap_inputs" / "ABS.json").write_text("{}", encoding="utf-8")
    clients = {"alpaca": DipAlpaca({}), "fred": RPT.FakeFred(), "sec": RPT.FakeSec({})}
    s = run_review(cfg, paths, conn, ASOF, clients=clients, offline_filings=RPT._offline(tmp, {"ABS": []}))
    assert "error" not in s, s
    rows = [r for r in RPT._jobs(conn, s["run_id"]) if r["ticker"] == "ABS"]
    return s, rows


@pytest.fixture
def renv(tmp_path, state_dir):
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=state_dir).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield cfg, paths, conn, tmp_path
    conn.close()


def test_rearm_evidence_carried_into_price_params(renv, monkeypatch):
    """減碼後跌破期間（10/05–10/07）沒有任何決策紀錄、之後站回線上：review_due 觸發 PRICE，證據進 params。"""
    monkeypatch.setattr(RPT.Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})
    _seed_abs(renv[2], {"last_review_date": "2026-10-01", "review_price": 101,
                        "last_trim": {"date": "2026-10-01", "reason_code": "ABSURD", "line": 100.0}}, "2025-01-02")
    s, rows = _run(renv)
    assert [r["job_type"] for r in rows] == ["PRICE"], s
    ev = json.loads(rows[0]["params"])["absurd_rearm"]
    assert ev == {"date": "2026-10-05", "close": 99.5, "line": 100.0, "last_trim_date": "2026-10-01"}


def test_review_after_dip_does_not_retrigger(renv, monkeypatch):
    """上次複查（10/08）已在跌破之後：之後沒再跌破 → 不重新觸發（邊緣觸發）。"""
    monkeypatch.setattr(RPT.Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})
    _seed_abs(renv[2], {"last_review_date": "2026-10-08", "review_price": 101,
                        "last_trim": {"date": "2026-10-01", "reason_code": "ABSURD", "line": 100.0}}, "2025-01-02")
    _, rows = _run(renv)
    assert rows == []


def test_previous_holding_period_trim_ignored(renv, monkeypatch):
    """前一持有期的荒謬級減碼（早於帳本建倉日；登錄表 data 沒有 first_buy_date）不影響新持有期：照一般邊緣觸發。"""
    monkeypatch.setattr(RPT.Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})
    _seed_abs(renv[2], {"last_review_date": "2026-10-08", "review_price": 95,
                        "last_trim": {"date": "2026-09-01", "reason_code": "ABSURD", "line": 100.0}}, "2026-09-15")
    assert export_registry(renv[2], "holdings")[0]["first_buy_date"] == "2026-09-15"   # 帳本建倉日帶給 review_due
    _, rows = _run(renv)
    assert [r["job_type"] for r in rows] == ["PRICE"]
    assert json.loads(rows[0]["params"])["absurd_rearm"] is None


# ---------------------------------------------------------------- PRICE → HOLD_REVIEW → 回寫

EV = {"date": "2026-10-16", "close": 250.0, "line": 260.0, "last_trim_date": "2026-10-15"}


def test_price_passes_rearm_to_hold_review(env):
    paths, conn = env
    P._set_absurd_line(paths, 50.0)
    TR._seed_registry(conn, "KO", "HOLDING", {"first_buy_date": "2020-01-01"})
    _, nxt = TR._run_price(paths, conn, TR._price_params(absurd_rearm=EV), TR._price_result())
    assert [j.job_type for j in nxt] == ["HOLD_REVIEW"] and nxt[0].params["absurd_rearm"] == EV


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield paths, conn
    conn.close()


def _trimmed(conn):
    P._seed(conn, last_trim={"date": "2026-10-15", "reason_code": "ABSURD", "line": 260.0, "fraction": 0.5,
                             "shares": 50, "peak_shares": 100})


def test_second_absurd_trim_passes_with_rearm_evidence(env):
    """跌破期間沒有決策紀錄：只靠 review_due 的證據（params.absurd_rearm），第二次荒謬級減碼可通過回寫。"""
    paths, conn = env
    _trimmed(conn)
    did = W.write_hold_review_result(conn, paths, "KO", "2026-10-20", "r", P._hr(0.5), {"absurd_rearm": EV},
                                     price=300.0, iv_base=60.0)
    assert P._log(conn, did)["action"] == "TRIM" and P._reg_data(conn)["last_trim"]["date"] == "2026-10-20"


@pytest.mark.parametrize("ev", [None, dict(EV, date="2026-10-15"), dict(EV, close=261.0), dict(EV, date="2026-10-21")])
def test_rearm_evidence_must_be_after_trim_and_below_line(env, ev):
    """沒有證據、證據不晚於上次減碼、不在線下、晚於決策日 → 仍只減一次（HOLD）。"""
    paths, conn = env
    _trimmed(conn)
    did = W.write_hold_review_result(conn, paths, "KO", "2026-10-20", "r", P._hr(0.5), {"absurd_rearm": ev},
                                     price=300.0, iv_base=60.0)
    assert P._log(conn, did)["action"] == "HOLD"
