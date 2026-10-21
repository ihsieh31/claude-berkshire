"""RM6-2（M6 portfolio／build_plan 群 8 條）：
1 M6-D10-01 登錄表 target_weight 含相關群額度／流動性／使用者硬上限夾限；被擋的 ADD 下修 T（現金不足不下修）。
2 M6-RS3-01 THESIS 疑似不重開已過期未結案的 P-50 觀察期。
3 M6-D4-04 switch_candidate 最新決策非 BUY／ADD 時結案。
4 M6-RS4-01 舊資料日完整分析不改 build_plan、不取消較新 BUY 的未成交買單。
5 M6-RS4-02 record_trim_anomalies 舊 asof 不倒退。
6 M6-RS4-03 metrics._latest_log 與 daily._deferred_buys 排除 PORTFOLIO。
7 M6-R2-01 建倉窗外 ADD target_w ≤ add_cap（登錄表降等）。
8 M6-R2-02 holdings_view 以 worse_grade(估值等級, 登錄表等級) 判可否加碼。
"""
import json
import sys
from datetime import date, timedelta

import pandas as pd
import pytest

from trader import db
from trader.analysis import writeback as W
from trader.ledger import book
from trader.paths import get_paths
from trader.pipelines import daily as D
from trader.pipelines.review import export_registry
from trader.portfolio import correlation
from trader.portfolio import holdings_view as H
from trader.portfolio import sizing as S
from trader.report import metrics as M

ASOF = "2026-10-16"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}
GATES_OK = {"gate1": True, "gate2": True, "gate3": True, "gate3_failures": [], "gate2_failures": [],
            "questionnaire_version": "q-v1", "gate3_answers_file": "work/r/AAA/gate3_answers.json",
            "screen_decision": "PASS", "flag_codes": [], "distress_codes": [], "price_date": ASOF,
            "time_zone": "America/New_York"}
BIG_ADV = 1e9


def _card(**kw):
    c = {"target_weight_pct": 10, "certainty_grade": "B", "auto_buy_cap": 60.0, "price_to_conservative": 0.8,
         "value_per_share": {"conservative": 60.0}, "max_weight_pct": {"host": 15, "tail": None},
         "position_plan": {"first_tranche_pct_of_target": 50}}
    c.update(kw)
    return c


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100000.0, "2026-10-01")
    yield conn, paths
    conn.close()


def _pos(conn, t, qty, avg=50.0, first="2025-01-02"):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": first,
                                  "updated_at": db.now_iso()})


def _setreg(conn, t, status="HOLDING", **data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"grade": "B", "auto_buy_cap": 60.0, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _reg(conn, t):
    r = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return r["status"], json.loads(r["data"])


def _dec(conn, did, t, day, log, created=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": day, "run_id": "r0",
                                  "job_id": None, "signal": log.get("action"), "reason_code": None,
                                  "data": {"ticker": t, "decision_date": day, **log},
                                  "created_at": created or db.now_iso()})


def _item(did, t, action="BUY", card=None, review_type=None):
    return {"decision_id": did, "ticker": t, "action": action, "review_type": review_type,
            "log": {**GATES, "action": action, "review_type": review_type}, "card": card or _card()}


def _room(value, status="ok"):
    return lambda *a, **k: {"status": status, "room": value, "reason": "測試群額度", "group": ["X"]}


def _review_due():
    sys.path.insert(0, str(get_paths().skill_deep / "scripts"))
    import review_due
    return review_due


def _px(close=50.0, days=10):
    d0 = date.fromisoformat(ASOF)
    return pd.DataFrame({"date": [d0 - timedelta(days=days - 1 - i) for i in range(days)], "close": [close] * days})


def test_group_clamped_new_buy_syncs_registry_target_and_review_due_quiet(env, monkeypatch):
    conn, _ = env
    _setreg(conn, "AAA", status="WATCH", target_weight=0.08,
            target_weight_basis={"decision_id": "AAA-20261010-1", "grade": "B"})
    monkeypatch.setattr(correlation, "buy_room", _room(0.04))
    p = S.plan_buys(conn, ASOF, [_item("AAA-20261016-1", "AAA", card=_card(target_weight_pct=8))],
                    {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["qty"] > 0 and p["target_weight"] == pytest.approx(0.04)
    D._after_place(conn, p, ASOF)
    _, data = _reg(conn, "AAA")
    assert data["build_plan"]["target_weight"] == pytest.approx(0.04)
    assert data["target_weight"] == pytest.approx(0.04)
    assert data["target_weight_basis"]["clamp"]["from"] == pytest.approx(0.08)
    e = next(x for x in export_registry(conn, "all") if x["ticker"] == "AAA")
    e.update({"status": "HOLDING", "weight": 0.04, "last_review_date": "2026-10-10",
              "last_full_review_date": "2026-10-10", "review_price": 50.0})
    act, reasons, _ = _review_due().check(e, [], _px(), ASOF, None)
    assert not any("加碼許可檢查" in r for r in reasons), reasons
    e2 = {**e, "target_weight": 0.08}
    assert any("加碼許可檢查" in r for r in _review_due().check(e2, [], _px(), ASOF, None)[1])


def test_after_place_does_not_raise_registry_target(env, monkeypatch):
    conn, _ = env
    _setreg(conn, "AAA", status="WATCH", target_weight=0.03)
    D._after_place(conn, {"side": "BUY", "action": "BUY", "ticker": "AAA", "target_weight": 0.05,
                          "decision_id": "AAA-1", "notes": []}, ASOF)
    _, data = _reg(conn, "AAA")
    assert data["target_weight"] == pytest.approx(0.03) and "clamp" not in (data.get("target_weight_basis") or {})


def test_blocked_add_by_group_room_lowers_registry_target(env, monkeypatch):
    conn, _ = env
    _pos(conn, "AAA", 100)
    _setreg(conn, "AAA", target_weight=0.10)
    monkeypatch.setattr(correlation, "buy_room", _room(0.0))
    buys = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", action="ADD")], {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)
    assert buys[0]["qty"] == 0 and buys[0]["add_target"] == pytest.approx(5000 / 105000)
    D._suppress_adds(conn, buys, ASOF)
    _, data = _reg(conn, "AAA")
    assert data["target_weight"] == pytest.approx(5000 / 105000, abs=1e-5)
    assert data["target_weight_basis"]["add_suppressed"]["from"] == pytest.approx(0.10)


def test_blocked_add_cash_shortage_does_not_lower_target(env, monkeypatch):
    conn, _ = env
    _pos(conn, "AAA", 100)
    _setreg(conn, "AAA", target_weight=0.10)
    monkeypatch.setattr(correlation, "buy_room", _room(0.0005))
    monkeypatch.setattr(S, "_avail_cash", lambda c, e: 0.0)
    buys = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", action="ADD")], {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)
    assert buys[0]["qty"] == 0 and "add_target" not in buys[0]
    D._suppress_adds(conn, buys, ASOF)
    assert _reg(conn, "AAA")[1]["target_weight"] == pytest.approx(0.10)


def test_full_result_target_includes_user_hard_cap(env, tmp_path):
    conn, paths = env
    d = tmp_path / "td"
    d.mkdir()
    card = {"signal": "BUY", "reason_code": "PASS_ALL", "certainty_grade": "B", "auto_buy_cap": 60.0,
            "target_weight_pct": 10, "value_per_share": {"conservative": 50, "base": 60},
            "position_plan": {"first_tranche_pct_of_target": 50}}
    for n, o in (("signal_card.json", card), ("registry_entry.json", {}), ("decision_log.json", {"action": "BUY"})):
        (d / n).write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text("論點", encoding="utf-8")
    W.write_full_result(conn, paths, "AAA", ASOF, "r", d, "BUY", [], gates=GATES_OK, user_hard_cap=0.05)
    assert _reg(conn, "AAA")[1]["target_weight"] == pytest.approx(0.05)


def test_thesis_suspect_does_not_reopen_expired_p50(env):
    conn, paths = env
    _pos(conn, "AAA", 100)
    _setreg(conn, "AAA", observe_until="2026-09-01", last_full_review_date="2026-08-01",
            last_review_date="2026-09-15")
    did = W.write_thesis_result(conn, paths, "AAA", ASOF, "r", {"verdict": "疑似", "reason": "毛利率續降"})
    _, data = _reg(conn, "AAA")
    assert data["observe_until"] == "2026-09-01"
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])
    assert any("不重開" in n for n in log.get("framework_notes") or [])
    e = next(x for x in export_registry(conn, "all") if x["ticker"] == "AAA")
    assert any("P-50 觀察期到期" in r for r in _review_due().check(e, [], None, ASOF, None)[1])


def test_thesis_suspect_after_closed_p50_opens_new(env):
    conn, paths = env
    _pos(conn, "AAA", 100)
    _setreg(conn, "AAA", observe_until="2026-09-01", last_full_review_date="2026-09-10")
    W.write_thesis_result(conn, paths, "AAA", ASOF, "r", {"verdict": "疑似", "reason": "x"})
    assert _reg(conn, "AAA")[1]["observe_until"] > ASOF


def _full_book(conn):
    for i in range(S.MAX_HOLDINGS):
        _pos(conn, f"H{i:02d}", 10)


def _switch(conn, t="NEW"):
    db.insert(conn, "anomalies", {"ticker": t, "kind": "switch_candidate",
                                  "detail": json.dumps({"date": "2026-10-15", "first_date": "2026-10-15"}),
                                  "status": "open", "pause_trading": 0, "created_at": db.now_iso()})


def _switch_status(conn, t="NEW"):
    return conn.execute("SELECT status FROM anomalies WHERE ticker=? AND kind='switch_candidate'", (t,)).fetchone()[0]


def test_switch_candidate_resolved_when_latest_decision_watch(env):
    conn, _ = env
    _full_book(conn)
    _dec(conn, "NEW-20261015-1", "NEW", "2026-10-15", {"action": "BUY"}, created="2026-10-15T01:00:00")
    _dec(conn, "NEW-20261016-1", "NEW", ASOF, {"action": "WATCH", "review_type": "PRICE"}, created="2026-10-16T01:00:00")
    _switch(conn)
    D._switch_candidates(conn, [], ASOF)
    assert _switch_status(conn) == "resolved"


def test_switch_candidate_kept_when_latest_decision_buy(env):
    conn, _ = env
    _full_book(conn)
    _dec(conn, "NEW-20261016-1", "NEW", ASOF, {"action": "BUY"})
    _switch(conn)
    D._switch_candidates(conn, [], ASOF)
    assert _switch_status(conn) == "open"


def test_stale_full_watch_keeps_build_plan_and_open_buy(env, tmp_path):
    conn, paths = env
    _dec(conn, "AAA-20261001-1", "AAA", "2026-10-01", {"action": "BUY", **GATES})
    _pos(conn, "AAA", 100, first="2026-10-02")
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "HOLDING", "data": {
        "grade": "B", "auto_buy_cap": 60.0, "first_buy_date": "2026-10-02", "target_weight": 0.10,
        "build_plan": {"target_weight": 0.10, "tranches_left": 2, "days_above_cap": 0}},
        "updated_at": db.now_iso()}, key=["ticker"])
    _dec(conn, "AAA-20261020-1", "AAA", "2026-10-20", {"action": "BUY", "review_type": "PRICE"})
    db.insert(conn, "orders", {"order_id": "o1", "ticker": "AAA", "side": "BUY", "qty": 10, "limit_price": 50.0,
                               "status": "open", "decision_id": "AAA-20261020-1", "created_date": "2026-10-20",
                               "cancel_reason": None, "updated_at": db.now_iso()})
    d = tmp_path / "td"
    d.mkdir()
    card = {"signal": "WATCH", "reason_code": "PASS_ALL", "certainty_grade": "B", "auto_buy_cap": 60.0,
            "target_weight_pct": 10, "value_per_share": {"conservative": 50, "base": 60}}
    for n, o in (("signal_card.json", card), ("registry_entry.json", {}), ("decision_log.json", {"action": "WATCH"})):
        (d / n).write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text("論點", encoding="utf-8")
    W.write_full_result(conn, paths, "AAA", "2026-10-15", "r", d, "WATCH", [], gates=GATES_OK)
    bp = _reg(conn, "AAA")[1]["build_plan"]
    assert bp["tranches_left"] == 2 and bp["target_weight"] == pytest.approx(0.10)
    assert conn.execute("SELECT status FROM orders WHERE order_id='o1'").fetchone()[0] == "open"


def test_record_trim_anomalies_old_asof_does_not_roll_back(env):
    conn, _ = env
    detail = {"date": "2026-10-20", "first_date": "2026-10-18", "ticker": "AAA", "trim_weight": 0.03}
    db.insert(conn, "anomalies", {"ticker": "AAA", "kind": "corr_group_trim", "detail": json.dumps(detail),
                                  "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
    n = correlation.record_trim_anomalies(conn, {"trim_recommendations": [{"ticker": "BBB", "trim_weight": 0.01}]},
                                          "2026-10-15")
    rows = conn.execute("SELECT ticker, status, detail FROM anomalies WHERE kind='corr_group_trim'").fetchall()
    assert n == 0 and len(rows) == 1
    assert rows[0]["status"] == "open" and json.loads(rows[0]["detail"]) == detail
    correlation.record_trim_anomalies(conn, {"trim_recommendations": []}, "2026-10-21")
    assert conn.execute("SELECT status FROM anomalies WHERE ticker='AAA'").fetchone()[0] == "resolved"


def test_portfolio_decision_does_not_shadow_deferred_add_or_dashboard_log(env):
    conn, _ = env
    _dec(conn, "AAA-20261015-1", "AAA", "2026-10-15",
         {"action": "ADD", "review_type": "PRICE", "reason_one_liner": "論點一句話", **GATES},
         created="2026-10-15T01:00:00")
    _dec(conn, "AAA-20261015-2", "AAA", "2026-10-15",
         {"action": "TRIM", "review_type": "PORTFOLIO", "reason_one_liner": "硬上限減碼"},
         created="2026-10-15T02:00:00")
    db.insert(conn, "runs", {"run_id": "d1", "kind": "daily_close", "asof": "2026-10-15", "status": "done",
                             "summary": json.dumps({"deferred_buys": [{"decision_id": "AAA-20261015-1",
                                                                       "ticker": "AAA"}]})})
    out = D._deferred_buys(conn, ASOF, "d2", set())
    assert [o["decision_id"] for o in out] == ["AAA-20261015-1"] and out[0]["action"] == "ADD"
    assert M._latest_log(conn, "AAA").get("reason_one_liner") == "論點一句話"


def test_add_outside_window_capped_by_registry_grade(env):
    conn, _ = env
    _pos(conn, "AAA", 100)
    _setreg(conn, "AAA", grade="B")
    card = _card(certainty_grade="A", target_weight_pct=15, max_weight_pct={"host": 15})
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", action="ADD", card=card)], {"AAA": 50.0},
                    {"AAA": BIG_ADV}, 10)[0]
    assert p["action"] == "ADD" and p["qty"] > 0
    assert p["target_weight"] <= S.GRADE_CAP["B"] + 1e-12
    assert p["qty"] == int((0.10 * 105000 - 5000) // 50.05)


def test_holdings_view_uses_worse_of_valuation_and_registry_grade(env):
    conn, paths = env
    _pos(conn, "AAA", 100, first="2026-10-02")
    d = paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "AAA.json").write_text(json.dumps({"oe": 5.0, "g10": 0.07, "g5": 0.06, "grade": "B", "long_rate": 0.047,
                                            "long_rate_20y_med": 0.035, "price": 60.0, "norm_eps": 5.2,
                                            "largecap_pe_median": 19.0, "financial": False,
                                            "incr_roic_gt_rd": True, "roe": 0.22, "eps_cagr5": 0.06}),
                                encoding="utf-8")
    px = {"AAA": {"price": 50.0, "price_date": ASOF}}
    v = H.holdings_g8(conn, paths, ASOF, px, None)
    assert v["holdings"][0]["addable"] is True
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "HOLDING", "data": {"grade": "C"},
                                 "updated_at": db.now_iso()}, key=["ticker"])
    r = H.holdings_g8(conn, paths, ASOF, px, None)["holdings"][0]
    assert r["addable"] is False and any("等級非 A／B" in n for n in r["not_addable"])
