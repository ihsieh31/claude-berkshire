"""RM4-6：review_due 認 NT 延遲申報通知（M4-R4S4-02）；review pipeline 不把 stale 收盤當現價傳給工作（M4-R4S4-03）。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

import test_review_pipeline as TRP
from test_review_pipeline import env, no_engine  # noqa: F401 - pytest 夾具
from trader.pipelines.review import run_review

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("review_due_rm46", ROOT / "berkshire-deep-merged" / "scripts" / "review_due.py")
RD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RD)

ASOF = "2026-10-16"
LAST = "2026-09-01"


def _px(close=100.0):
    d = pd.bdate_range(end=ASOF, periods=60)
    return pd.DataFrame({"date": list(d.date), "close": [close] * len(d)})


def _entry(status="HOLDING", **kw):
    e = {"ticker": "X", "status": status, "last_review_date": LAST, "last_full_review_date": LAST,
         "review_price": 100, "cost": 100, "weight": 0.05, "target_weight": 0.05, "first_buy_date": "2025-01-01"}
    e.update(kw)
    return e


def test_nt_10k_in_window_triggers_thesis_for_holding():
    det = {}
    action, reasons, _ = RD.check(_entry(), [{"form": "NT 10-K", "filingDate": "2026-10-01", "items": ""}],
                                  _px(), ASOF, None, detail=det)
    assert action == "THESIS"
    assert any("延遲申報" in w and "NT 10-K 2026-10-01" in w and "W14" in w for w in reasons)


def test_nt_10q_before_last_review_ignored():
    action, reasons, _ = RD.check(_entry(), [{"form": "NT 10-Q", "filingDate": "2026-08-15", "items": ""}],
                                  _px(), ASOF, None)
    assert action == "NONE"
    assert not any("延遲申報" in w for w in reasons)


def test_nt_suppressed_for_reject():
    det = {}
    action, _, _ = RD.check(_entry(status="REJECT"), [{"form": "NT 10-K", "filingDate": "2026-10-01", "items": ""}],
                            _px(), ASOF, None, detail=det)
    assert action == "NONE"
    assert any("延遲申報" in s for s in det["suppressed"])


class StaleAlpaca(TRP.FakeAlpaca):
    """FUL 的收盤停在決策日前 20 個交易日（停牌／資料中斷）。"""

    def bars(self, symbols, start, end, adjustment="raw"):
        df = super().bars(symbols, start, end, adjustment)
        cut = pd.bdate_range(end=end, periods=21)[0].strftime("%Y-%m-%d")
        return df[(df["ticker"] != "FUL") | (df["date"] <= cut)].reset_index(drop=True)


def test_stale_price_not_passed_as_current_price(env, no_engine):  # noqa: F811
    cfg, paths, conn, tmp = env
    TRP._seed(conn)
    clients = TRP._clients()
    clients["alpaca"] = StaleAlpaca({t: TRP.CLOSE for t in TRP.SEED})
    s = run_review(cfg, paths, conn, ASOF, clients=clients, offline_filings=TRP._offline(tmp))
    assert "error" not in s, s
    due = json.loads((paths.work / s["run_id"] / "due.json").read_text(encoding="utf-8"))
    rows = due["results"]
    assert [r for r in rows if r["ticker"] == "FUL"][0]["price_status"] == "stale"
    p = json.loads([r for r in TRP._jobs(conn, s["run_id"]) if r["ticker"] == "FUL"][0]["params"])
    assert p["price"] is None and p["price_date"] is None
    assert "過期" in p["price_meta"]["source"]
    assert s["price_stale_not_passed"] == ["FUL"]
    # 未過期的檔照傳現價
    q = json.loads([r for r in TRP._jobs(conn, s["run_id"]) if r["ticker"] == "THS"][0]["params"])
    assert q["price"] == TRP.CLOSE
