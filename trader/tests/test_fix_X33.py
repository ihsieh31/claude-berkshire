"""X33（P46 交接）：已作廢決策不當閘門 3／近期完整分析來源；延後買進的「最新決策」略過作廢與 MANUAL；
review --scope all 不匯出 RETIRED。全部離線。"""
from __future__ import annotations

import json

import pytest

from trader import db
from trader.jobs.review_jobs import _gate3_status, _last_full_card
from trader.paths import get_paths
from trader.pipelines.daily import _deferred_buys
from trader.pipelines.review import export_registry

NOW = "2026-10-16T00:00:00+00:00"
VOID = {"voided_at": NOW, "voided_note": "資料包錯"}


@pytest.fixture
def conn(state_dir):
    c = db.connect(get_paths().ensure().db)
    db.migrate(c)
    yield c
    c.close()


def _dec(conn, did, t, date, data, card=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "run_id": None,
                                  "job_id": None, "signal": data.get("action"), "reason_code": None,
                                  "data": data, "created_at": f"{date}T00:00:00+00:00"})
    if card is not None:
        db.insert(conn, "signals", {"decision_id": did, "ticker": t, "signal": card.get("signal"),
                                    "data": card, "created_at": f"{date}T00:00:00+00:00"})


def test_gate3_status_skips_voided(conn):
    _dec(conn, "T-OK", "TT", "2026-10-01", {"gate3_pass": True})
    _dec(conn, "T-BAD", "TT", "2026-10-10", {"gate3_pass": False, **VOID})
    assert _gate3_status(conn, "TT", "2026-10-15") == "通過"


def test_last_full_card_skips_voided(conn):
    _dec(conn, "T-OLD", "TT", "2026-10-01", {"action": "WATCH"}, {"signal": "WATCH", "price": 90, "auto_buy_cap": 100})
    _dec(conn, "T-NEW", "TT", "2026-10-10", {"action": "REJECT", **VOID}, {"signal": "REJECT"})
    assert _last_full_card(conn, "TT", "2026-10-15") == ("2026-10-01", {"signal": "WATCH", "price": 90,
                                                                         "auto_buy_cap": 100})


@pytest.mark.parametrize("newer", [{"action": "HOLD", **VOID}, {"action": "BUY", "review_type": "MANUAL"}])
def test_deferred_buys_skips_voided_and_manual(conn, newer):
    _dec(conn, "T-BUY", "TT", "2026-10-13", {"action": "BUY"})
    _dec(conn, "T-LATER", "TT", "2026-10-14", newer)
    db.insert(conn, "runs", {"run_id": "daily-2026-10-14", "kind": "daily_close", "asof": "2026-10-14",
                             "status": "done", "started_at": NOW, "finished_at": NOW,
                             "summary": json.dumps({"deferred_buys": [{"decision_id": "T-BUY", "ticker": "TT"}]})})
    out = _deferred_buys(conn, "2026-10-15", "daily-2026-10-15", set())
    assert [x["decision_id"] for x in out] == ["T-BUY"]


def test_export_registry_all_skips_retired(conn):
    for t, st in (("AA", "WATCH"), ("BB", "RETIRED"), ("CC", "HOLDING")):
        db.upsert(conn, "registry", {"ticker": t, "status": st, "first_buy_date": None,
                                     "data": {"grade": "B"}, "updated_at": NOW}, ["ticker"])
    assert [e["ticker"] for e in export_registry(conn, "all")] == ["AA", "CC"]
