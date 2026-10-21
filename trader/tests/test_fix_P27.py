"""P27：撮合與委託規則（設計決策 D03 撮合部分、D04、D05、D06、D07、D15、D50；M7-D10-03）。全部離線。"""
import json

import pytest

from test_daily_pipeline import (CARD, D1, D2, D3, FakeAlpaca, _bars, _clients, _dec, _reg, _router, _run,
                                 world)  # noqa: F401 - world 是 fixture
from trader import db
from trader.config import ConfigError, _build_config
from trader.ledger import book, broker
from trader.paths import get_paths
from trader.pipelines import daily as DP
from trader.portfolio import sizing as Z

L0, L1, L2 = "2026-10-01", "2026-10-14", "2026-10-15"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}


@pytest.fixture
def conn(state_dir):
    c = db.connect(get_paths().ensure().db)
    db.migrate(c)
    book.ensure_initial_deposit(c, 100_000, L0)
    yield c
    c.close()


def _bar(o, h, lo, c):
    return {"open": o, "high": h, "low": lo, "close": c}


def _hold(conn, t, qty, avg=50.0, first="2025-01-01", data=None):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": first,
                                  "updated_at": db.now_iso()})
    db.upsert(conn, "registry", {"ticker": t, "status": "HOLDING", "data": data or {"grade": "B"},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _decision(conn, did, t, date, log):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "signal": log.get("action"),
                                  "data": {"decision_date": date, **log}, "created_at": db.now_iso()})


def _pause(conn, t):
    # D88（A3-M7C-01，F3-DY）後 D06 放行全清只限現金股利類暫停；類型未知（detail 空）連 SELL_ALL 也不撮合
    detail = json.dumps({"event": {"ticker": t, "source": "alpaca", "date": "2026-10-01",
                                   "alpaca": {"ticker": t, "type": "cash_dividend"}},
                         "result": {"action_type": "現金股利", "effective_date": "2026-10-01"}})
    db.insert(conn, "anomalies", {"ticker": t, "kind": "corp_action_check_failed", "detail": detail, "status": "open",
                                  "pause_trading": 1, "created_at": db.now_iso()})


# ---------------------------------------------------------------- D07：典型價成交

def test_d07_gap_open_fills_at_typical_price(conn):
    """開盤跳空 60、典型價 (61+49+50)/3＝53.33：市價賣單以典型價減滑價入帳，不是開盤價。"""
    _hold(conn, "AAA", 10)
    broker.place_order(conn, "AAA", "SELL", 10, None, None, L1)
    f = broker.match_orders(conn, L2, {"AAA": _bar(60, 61, 49, 50)}, 10, 0)
    assert f[0]["price"] == pytest.approx(round(160 / 3 * (1 - 10 / 1e4), 4))


def test_d07_limit_buy_min_of_typical_and_limit(conn):
    broker.place_order(conn, "AAA", "BUY", 10, 52.0, None, L1)
    broker.place_order(conn, "BBB", "BUY", 10, 50.0, None, L1)
    f = {x["ticker"]: x for x in broker.match_orders(
        conn, L2, {"AAA": _bar(55, 56, 51, 53), "BBB": _bar(55, 56, 49, 53)}, 0, 0)}
    assert f["AAA"]["price"] == pytest.approx(52.0)        # 典型價 53.33 > 限價 → 限價
    assert f["BBB"]["price"] == pytest.approx(50.0)        # 典型價 52.67 > 限價 50 → 限價；最低價 49 ≤ 50 才成交
    broker.place_order(conn, "CCC", "BUY", 10, 60.0, None, L2)
    g = broker.match_orders(conn, "2026-10-16", {"CCC": _bar(58, 59, 49, 50)}, 0, 0)
    assert g[0]["price"] == pytest.approx(round(158 / 3, 4))   # 典型價 52.67 < 限價 → 典型價


def test_d07_missing_close_not_matched(conn):
    broker.place_order(conn, "AAA", "BUY", 1, 60.0, None, L1)
    assert broker.match_orders(conn, L2, {"AAA": {"open": 50, "high": 51, "low": 49, "close": None}}, 0, 0) == []


# ---------------------------------------------------------------- D03 撮合部分：估值 TRIM 限價＝觸發線、當日有效

def _valuation_trim(conn, t="VAL", line=100.0, date=L1):
    _hold(conn, t, 100, data={"grade": "B", "absurd_price_line": line,
                              "last_trim": {"date": date, "reason_code": "ABSURD", "line": line}})
    log = {"action": "TRIM", "review_type": "HOLD_REVIEW", "sell_category": "估值", "trim_fraction": 0.5}
    _decision(conn, f"{t}-1", t, date, log)
    return {"decision_id": f"{t}-1", "ticker": t, "action": "TRIM", "review_type": "HOLD_REVIEW",
            "log": {"decision_date": date, **log}}


def test_d03_valuation_trim_limit_is_trigger_line(conn):
    d = _valuation_trim(conn, line=100.004)
    p = Z.plan_sells(conn, L1, [d], {"VAL": 105.0})[0]
    assert p["qty"] == 50 and p["limit_price"] == pytest.approx(100.01) and "當日有效" in p["reason"]


def test_d03_business_trim_and_sell_all_stay_market(conn):
    _hold(conn, "BIZ", 100)
    log = {"action": "TRIM", "review_type": "HOLD_REVIEW", "sell_category": "業務", "trim_fraction": 0.5}
    p = Z.plan_sells(conn, L1, [{"decision_id": "B1", "ticker": "BIZ", "action": "TRIM", "log": log,
                                 "review_type": "HOLD_REVIEW"}], {"BIZ": 50.0})[0]
    assert p["qty"] == 50 and p["limit_price"] is None


def test_d03_valuation_trim_without_line_not_placed(conn):
    d = _valuation_trim(conn)
    conn.execute("UPDATE registry SET data=? WHERE ticker='VAL'", (json.dumps({"grade": "B"}),))
    conn.commit()
    p = Z.plan_sells(conn, L1, [d], {"VAL": 105.0})[0]
    assert p["qty"] == 0 and "觸發線" in p["reason"]


def test_d03_valuation_trim_below_line_not_filled_and_expires(conn):
    """收盤低於觸發線時估值 TRIM 不成交，且當日作廢（回落即停），隔天不再撮合。"""
    _valuation_trim(conn)
    oid = broker.place_order(conn, "VAL", "SELL", 50, 100.0, "VAL-1", L1)
    assert broker.match_orders(conn, L2, {"VAL": _bar(98, 99.5, 97, 98)}, 0, 0) == []
    r = conn.execute("SELECT status FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert r["status"] == "expired"
    assert broker.match_orders(conn, "2026-10-16", {"VAL": _bar(101, 102, 100, 101)}, 0, 0) == []
    assert book.positions(conn)["VAL"]["qty"] == 100


# ---------------------------------------------------------------- D06：暫停中 SELL_ALL 放行

def test_d06_paused_sell_all_fills_but_buy_and_trim_do_not(conn):
    _hold(conn, "PSD", 100)
    _decision(conn, "PSD-SA", "PSD", L1, {"action": "SELL_ALL", "review_type": "THESIS"})
    _decision(conn, "PSD-TR", "PSD", L1, {"action": "TRIM", "review_type": "HOLD_REVIEW", "sell_category": "業務"})
    tr = broker.place_order(conn, "PSD", "SELL", 10, None, "PSD-TR", L1)
    _pause(conn, "PSD")
    sa = broker.place_order(conn, "PSD", "SELL", 100, None, "PSD-SA", L1)     # 全清照常下單
    with pytest.raises(broker.BrokerError):
        broker.place_order(conn, "PSD", "BUY", 1, 10.0, None, L1)
    with pytest.raises(broker.BrokerError):
        broker.place_order(conn, "PSD", "SELL", 10, None, "PSD-TR", L1)
    f = broker.match_orders(conn, L2, {"PSD": _bar(50, 51, 49, 50)}, 0, 0)
    assert [x["order_id"] for x in f] == [sa] and f[0]["qty"] == 100
    assert conn.execute("SELECT status FROM orders WHERE order_id=?", (tr,)).fetchone()["status"] == "open"


# ---------------------------------------------------------------- D04：限價＝上限×0.9999；窗口自 build_plan.started

def test_d04_limit_is_cap_floor_and_qty_from_close():
    assert Z.buy_limit(50.0, 60.0, 10) == 59.99
    assert Z.buy_limit(50.0, 100.0, 10) == 99.99
    assert Z.buy_limit(50.0, 0.01, 10) is None                  # 捨去到 0：無合法限價
    assert Z.ref_price(50.0, 10) == pytest.approx(50.05)


def test_d04_small_rise_below_cap_next_tranche_still_placed(world):
    """首批限價＝59.99；隔日小漲到 52（仍低於上限 60）→ 首批照樣成交，同日後續批次照常下單。"""
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    bars = _bars()
    bars[D2]["AAA"] = (52.0, 53.0, 51.5, 52.0, 1_000_000)
    alp = FakeAlpaca(bars)
    s1 = _run(cfg, paths, conn, D1, alp)
    assert s1["orders"][0]["limit_price"] == 59.99
    s2 = _run(cfg, paths, conn, D2, alp)
    assert "error" not in s2, s2
    assert len(s2["fills"]) == 1
    adds = [o for o in s2["orders"] if o["action"] == "ADD"]
    assert len(adds) == 1 and adds[0]["qty"] > 0 and adds[0]["limit_price"] == 59.99
    assert adds[0]["created_date"] == D2 and adds[0]["review_type"] == Z.TRANCHE


def _build(conn, started, first="2026-10-14"):
    _hold(conn, "BLD", 100, first=first,
          data={"grade": "B", "auto_buy_cap": 60.0,
                "build_plan": {"target_weight": 0.10, "tranches_left": 2, "started": started}})
    _decision(conn, "BLD-1", "BLD", "2026-10-10", {"review_type": None, "action": "BUY", **GATES})


def test_d04_window_starts_at_build_plan_started(conn):
    _build(conn, started="2026-09-20")      # 首批下單 26 曆日前（首次成交雖在 10-14）→ 窗口到期
    plans, cancels = Z.plan_tranches(conn, "2026-10-16", {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert plans == [] and "首批下單日" in cancels[0]["reason"]


def test_d04_window_within_started(conn):
    _build(conn, started="2026-10-13")
    plans, cancels = Z.plan_tranches(conn, "2026-10-16", {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert cancels == [] and plans[0]["qty"] > 0


# ---------------------------------------------------------------- M7-D10-03：建倉批次不套冷靜期

def test_m7_d10_03_tranche_not_cooled_even_if_gate_decision_is_price(conn):
    _build(conn, started="2026-10-13")
    _decision(conn, "BLD-2", "BLD", "2026-10-15", {"review_type": "PRICE", "action": "HOLD", **GATES})
    plans, _ = Z.plan_tranches(conn, "2026-10-16", {"BLD": 50.0}, {"BLD": 1e9}, 10)
    p = plans[0]
    assert p["qty"] > 0 and p["review_type"] == Z.TRANCHE and p["decision_id"] == "BLD-2"
    out = {"plans_dry_run": [], "orders": [], "rejected": []}
    DP._place(conn, p, "2026-10-16", "2026-10-19", False, out)
    assert out["plans_dry_run"][0]["created_date"] == "2026-10-16"


def test_p27r_tranche_self_check_includes_slippage(conn):
    """P27R：上限 60、收盤 59.97、滑價 10bps → 59.97×1.001＝60.03 ≥ 60：批次單不下（與 plan_buys、D04 同口徑）。"""
    _build(conn, started="2026-10-13")
    _decision(conn, "BLD-2", "BLD", "2026-10-15", {"review_type": "PRICE", "action": "HOLD", **GATES})
    plans, cancels = Z.plan_tranches(conn, "2026-10-16", {"BLD": 59.97}, {"BLD": 1e9}, 10)
    assert cancels == [] and plans[0]["qty"] == 0 and "含滑價" in plans[0]["reason"]
    assert book.open_orders(conn) == []


def test_p27r_tranche_with_price_gate_not_cancelled_by_recheck_and_reason_neutral(conn):
    """P27R：閘門決策為 PRICE 的批次單，以同一收盤重核不撤；若隔日前收盤已不低於上限才撤，理由不寫「冷靜單」。"""
    _build(conn, started="2026-10-13")
    _decision(conn, "BLD-2", "BLD", "2026-10-15", {"review_type": "PRICE", "action": "HOLD", **GATES})
    plans, _ = Z.plan_tranches(conn, "2026-10-16", {"BLD": 59.9}, {"BLD": 1e9}, 10)   # 59.9×1.001＝59.96 < 60
    out = {"plans_dry_run": [], "orders": [], "rejected": []}
    oid = DP._place(conn, plans[0], "2026-10-16", "2026-10-19", True, out)
    assert oid and out["orders"][0]["created_date"] == "2026-10-16"
    assert broker.recheck_pending(conn, "2026-10-19", {"BLD": 59.9}, 10) == []
    cs = broker.recheck_pending(conn, "2026-10-19", {"BLD": 59.97}, 10)
    assert [c["order_id"] for c in cs] == [oid]
    r = conn.execute("SELECT status, cancel_reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert r["status"] == "cancelled" and "上限重核" in r["cancel_reason"] and "冷靜單" not in r["cancel_reason"]


# ---------------------------------------------------------------- D05：撮合前以前一日收盤重核

def test_d05_cooling_buy_cancelled_when_price_back_above_cap(world):
    """PRICE 觸發的 BUY 冷靜一日；冷靜日收盤 61 已高於上限 60 → 撮合前取消，不成交。"""
    cfg, paths, conn = world
    _reg(conn, "BBB", "WATCH", {"cik": "0000000002", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "BBB-20261015-1", "BBB", D1, "BUY", review_type="PRICE", card=CARD)
    bars = _bars()
    bars[D2]["BBB"] = (59.0, 61.5, 58.0, 61.0, 1_000_000)
    bars[D3]["BBB"] = (59.0, 60.0, 50.0, 55.0, 1_000_000)     # 撮合日最低價已低於限價：不重核就會成交
    alp = FakeAlpaca(bars)
    s1 = _run(cfg, paths, conn, D1, alp)
    oid = s1["orders"][0]["order_id"]
    _run(cfg, paths, conn, D2, alp)
    s3 = _run(cfg, paths, conn, D3, alp)
    assert "error" not in s3, s3
    assert s3["fills"] == [] and [c["order_id"] for c in s3["recheck_cancels"]] == [oid]
    r = conn.execute("SELECT status, cancel_reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert r["status"] == "cancelled" and "上限重核" in r["cancel_reason"]


def test_d05_cooling_buy_kept_when_still_below_cap(conn):
    db.upsert(conn, "registry", {"ticker": "CCC", "status": "WATCH", "data": {"auto_buy_cap": 60.0},
                                 "updated_at": db.now_iso()}, key=["ticker"])
    _decision(conn, "CCC-1", "CCC", L1, {"action": "BUY", "review_type": "PRICE", **GATES})
    broker.place_order(conn, "CCC", "BUY", 10, 59.99, "CCC-1", L2)
    assert broker.recheck_pending(conn, "2026-10-16", {"CCC": 55.0}, 10) == []
    assert book.open_orders(conn)[0]["status"] == "open"


def test_d05_late_valuation_trim_cancelled_when_close_below_line(conn):
    """等同意數日後才完成的估值 TRIM：撮合前一日收盤已回到觸發線下 → 取消。"""
    _valuation_trim(conn, date="2026-10-09")
    oid = broker.place_order(conn, "VAL", "SELL", 50, 100.0, "VAL-1", L1)
    cs = broker.recheck_pending(conn, L2, {"VAL": 99.0}, 10)
    assert [c["order_id"] for c in cs] == [oid]
    r = conn.execute("SELECT status, cancel_reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert r["status"] == "cancelled" and "觸發線" in r["cancel_reason"]
    oid2 = broker.place_order(conn, "VAL", "SELL", 50, 100.0, "VAL-1", L2)
    assert broker.recheck_pending(conn, "2026-10-16", {"VAL": 100.0}, 10) == []
    assert book.open_orders(conn)[0]["order_id"] == oid2


def test_d05_paused_orders_untouched(conn):
    _valuation_trim(conn)
    broker.place_order(conn, "VAL", "SELL", 50, 100.0, "VAL-1", L1)
    _pause(conn, "VAL")
    assert broker.recheck_pending(conn, L2, {}, 10) == []


# ---------------------------------------------------------------- D15：計畫現金水位 6%

def test_d15_avail_cash_uses_plan_level(conn):
    assert Z._avail_cash(conn, 100_000.0) == pytest.approx(100_000 - 6_000)
    assert Z._plan_margin(100_000.0, 1.2) == pytest.approx(0.0)
    assert Z._plan_margin(100_000.0, 1.5) == pytest.approx(1_500.0)


def test_d15_cash_between_floor_and_plan_level_stops_new_buys_only(conn):
    """現金 5.5%（高於硬下限 5%、低於計畫 6%）：不新買；不產生任何賣單。"""
    conn.execute("DELETE FROM cash_ledger")
    conn.commit()
    book.ensure_initial_deposit(conn, 5_500, L0)
    _hold(conn, "BIG", 1890, avg=50.0, first="2026-10-01", data={"grade": "A", "auto_buy_cap": 80.0})   # 94,500
    item = {"decision_id": "NEW-1", "ticker": "NEW", "action": "BUY", "review_type": None,
            "log": GATES, "card": {**CARD}}
    db.upsert(conn, "registry", {"ticker": "NEW", "status": "WATCH", "data": {"auto_buy_cap": 60.0, "grade": "B"},
                                 "updated_at": db.now_iso()}, key=["ticker"])
    p = Z.plan_buys(conn, L1, [item], {"NEW": 50.0, "BIG": 50.0}, {"NEW": 1e12}, 10)[0]
    assert p["qty"] == 0
    assert all(x["qty"] == 0 for x in Z.plan_sells(conn, L1, [], {"BIG": 50.0}))   # 現金跌破計畫水位不會引發賣出
    # 同一情境改用 1.0（只看硬下限）時可買：證明是計畫水位擋下
    q = Z.plan_buys(conn, L1, [item], {"NEW": 50.0, "BIG": 50.0}, {"NEW": 1e12}, 10, cash_plan_buffer=1.0)[0]
    assert q["qty"] > 0


def test_d15_config_key_default_and_validation(tmp_path):
    import yaml
    import test_engine as TE
    raw = yaml.safe_load(TE._write_config(tmp_path).read_text(encoding="utf-8"))
    assert _build_config(raw, None).account.cash_plan_buffer == 1.2
    assert _build_config({**raw, "account": {"cash_plan_buffer": 1.5}}, None).account.cash_plan_buffer == 1.5
    with pytest.raises(ConfigError, match="cash_plan_buffer"):
        _build_config({**raw, "account": {"cash_plan_buffer": 0.9}}, None)


# ---------------------------------------------------------------- D50：觸發檢查失敗只停該檔

def _review_with_failures(conn, tickers, date):
    def fake(cfg, paths, c, asof, scope, run_id, clients, router):
        for i, t in enumerate(tickers):
            db.insert(c, "jobs", {"job_id": f"{run_id}:{t}:PRICE:{i}", "run_id": run_id, "job_type": "PRICE",
                                  "ticker": t, "asof": asof, "status": "failed", "created_at": db.now_iso()})
        return {"run_id": run_id, "counts": {}, "skipped": [],
                "engine": {"status": "failed" if tickers else "done", "failed": len(tickers), "skipped": 0}}
    return fake


def _two_buys(conn):
    for t, cik in (("AAA", "0000000001"), ("BBB", "0000000002")):
        _reg(conn, t, "WATCH", {"cik": cik, "auto_buy_cap": 60.0, "grade": "B"})
        _dec(conn, f"{t}-20261015-1", t, D1, "BUY", card=CARD)


def _daily(cfg, paths, conn, date, monkeypatch, failed):
    monkeypatch.setattr(DP, "run_review", _review_with_failures(conn, failed, date))
    return DP.run_daily(cfg, paths, conn, date, clients=_clients(FakeAlpaca(_bars())), skip_review=False,
                        router=_router(cfg, paths), now=DP.datetime(2026, 10, 20, 12, 0, tzinfo=DP.timezone.utc))


def test_d50_failure_blocks_only_that_ticker(world, monkeypatch):
    cfg, paths, conn = world
    _two_buys(conn)
    s = _daily(cfg, paths, conn, D1, monkeypatch, ["AAA"])
    assert "error" not in s, s
    assert s["review"]["ok"] is True and s["review"]["failed_tickers"] == ["AAA"]
    assert [o["ticker"] for o in s["orders"]] == ["BBB"]
    assert [x["ticker"] for x in s["deferred_buys"]] == ["AAA"]
    assert any("AAA" in n and "其他照常" in n for n in s["notes"])


def test_d50_three_failed_tickers_block_all(world, monkeypatch):
    cfg, paths, conn = world
    _two_buys(conn)
    s = _daily(cfg, paths, conn, D1, monkeypatch, ["X1", "X2", "X3"])
    assert s["review"]["ok"] is False and s["orders"] == []
    assert sorted(x["ticker"] for x in s["deferred_buys"]) == ["AAA", "BBB"]


def test_d50_unattributed_failure_blocks_all(world, monkeypatch):
    cfg, paths, conn = world
    _two_buys(conn)
    monkeypatch.setattr(DP, "run_review", lambda *a, **k: {"run_id": "review-x", "counts": {}, "skipped": [],
                                                           "engine": {"status": "failed", "failed": 1}})
    s = DP.run_daily(cfg, paths, conn, D1, clients=_clients(FakeAlpaca(_bars())), skip_review=False,
                     router=_router(cfg, paths), now=DP.datetime(2026, 10, 20, 12, 0, tzinfo=DP.timezone.utc))
    assert s["review"]["ok"] is False and s["orders"] == []


def test_d50_streak_anomaly_after_three_daily_failures(world, monkeypatch):
    cfg, paths, conn = world
    for d in (D1, D2):
        _daily(cfg, paths, conn, d, monkeypatch, ["ZZZ"])
        assert conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='trigger_fail_streak'").fetchone()[0] == 0
    _daily(cfg, paths, conn, D3, monkeypatch, ["ZZZ"])
    rows = conn.execute("SELECT ticker, pause_trading FROM anomalies WHERE kind='trigger_fail_streak'").fetchall()
    assert [tuple(r) for r in rows] == [("ZZZ", 0)]
