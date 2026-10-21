"""X10（P44 查核後小項）：舊 corr_group_trim 異常缺 first_date 時取舊 date；相關群 none／ok 時的 warning 也記入下單計畫 notes。"""
import json

import pytest

from trader import db
from trader.ledger import book
from trader.portfolio import correlation as C
from trader.portfolio import sizing as S

ASOF = "2026-10-16"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}
VOCAB = C.load_vocab()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    book.ensure_initial_deposit(c, 90000.0, "2026-10-01")
    yield c
    c.close()


def _pos(conn, t, qty, avg, first="2026-09-01"):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": first,
                                  "updated_at": db.now_iso()})


def _reg(conn, t, status, **data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"ticker": t, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


# ---------------------------------------------------------------- 舊異常缺 first_date

def test_old_trim_anomaly_without_first_date_keeps_its_original_date(conn):
    for t in ("B1", "B2"):
        _reg(conn, t, "HOLDING", grade="B", asset_class="銀行", external_drivers=["consumer_credit"])
        _pos(conn, t, 150, 100.0)
    rep = C.portfolio_groups(conn, {"B1": 100.0, "B2": 100.0}, VOCAB)
    assert rep["trim_recommendations"]
    t0 = rep["trim_recommendations"][0]["ticker"]
    # P44 之前的舊版異常：detail 只有 date（＝首次記錄日），沒有 first_date
    db.insert(conn, "anomalies", {"ticker": t0, "kind": "corr_group_trim",
                                  "detail": json.dumps({"date": "2026-10-05", "ticker": t0}),
                                  "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
    C.record_trim_anomalies(conn, rep, ASOF)
    d = json.loads(conn.execute("SELECT detail FROM anomalies WHERE kind='corr_group_trim' AND ticker=? "
                                "AND status='open'", (t0,)).fetchone()["detail"])
    assert d["date"] == ASOF and d["first_date"] == "2026-10-05"


# ---------------------------------------------------------------- none／ok 時 warning 進下單計畫 notes

def test_plan_buys_notes_warning_when_holding_drivers_unknown(conn):
    _pos(conn, "OLDH", 100, 100.0)
    _reg(conn, "OLDH", "HOLDING", grade="B", asset_class="非金融")          # 持股沒有外部變數判定
    card = {"target_weight_pct": 10, "certainty_grade": "B", "auto_buy_cap": 60.0, "price_to_conservative": 0.8,
            "value_per_share": {"conservative": 60.0}, "max_weight_pct": {"host": 15},
            "position_plan": {"first_tranche_pct_of_target": 50}, "external_drivers": ["oil_price_down"]}
    _reg(conn, "OIL", "WATCH", grade="B", asset_class="非金融", external_drivers=["oil_price_down"])
    item = {"decision_id": "OIL-20261016-1", "ticker": "OIL", "action": "BUY", "review_type": None,
            "log": {**GATES, "action": "BUY"}, "card": card}
    room = C.buy_room(conn, "OIL", {"OLDH": 100.0, "OIL": 50.0}, VOCAB, None)
    assert room["status"] in ("none", "ok") and "OLDH" in room["warning"]
    plans = S.plan_buys(conn, ASOF, [item], {"OLDH": 100.0, "OIL": 50.0}, {"OIL": 1e9}, 0)
    p = next(p for p in plans if p["ticker"] == "OIL")
    assert p["qty"] > 0 and any("OLDH" in n and "外部變數未知" in n for n in p["notes"])


def test_plan_tranches_notes_warning_when_holding_drivers_unknown(conn):
    _pos(conn, "OLDH", 100, 100.0)
    _reg(conn, "OLDH", "HOLDING", grade="B", asset_class="非金融")
    _pos(conn, "BBB", 20, 50.0, first="2026-10-10")
    _reg(conn, "BBB", "HOLDING", grade="B", asset_class="非金融", external_drivers=["housing_starts"],
         auto_buy_cap=60.0, build_plan={"target_weight": 0.10, "tranches_left": 2, "days_above_cap": 0})
    db.insert(conn, "decisions", {"decision_id": "BBB-20261010-1", "ticker": "BBB", "decision_date": "2026-10-10",
                                  "signal": "BUY", "data": {**GATES, "action": "BUY"}, "created_at": db.now_iso()})
    plans, cancels = S.plan_tranches(conn, ASOF, {"OLDH": 100.0, "BBB": 50.0}, {"BBB": 1e9}, 0)
    assert not cancels
    p = next(p for p in plans if p["ticker"] == "BBB")
    assert p["qty"] > 0 and any("OLDH" in n and "外部變數未知" in n for n in p["notes"])
