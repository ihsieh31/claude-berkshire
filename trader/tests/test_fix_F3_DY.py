"""F3-DY（daily 群）回歸測試：V3-3 的 G-daily-order、G-buy-supersede、G-sellall-buy、G-broker-match、G-daily-misc、
G-scheduler，A3-M7E-01 的 daily 派工端，以及 book.adjust 現金＋解除暫停結案、corp_action_job_failed 標籤。全部離線。"""
import json
import os
import signal
import time
from types import SimpleNamespace

import pytest

from test_daily_pipeline import world  # noqa: F401  fixture
from test_daily_pipeline import CARD, D1, D2, D3, FakeAlpaca, _bars, _dec, _reg, _run, _router
from trader import db
from trader.calendar import TradingCalendar
from trader.jobs import approvals
from trader.jobs.engine import Engine
from trader.ledger import book, broker
from trader.ledger import corp_actions as ca
from trader.models import JobSpec
from trader.pipelines import daily as DP
from trader.portfolio import sizing as Z
from trader.report import metrics


def _placed_buys(s, t):
    return [o for o in s.get("orders", []) if o["ticker"] == t and o["side"] == "BUY"]


def _late_buy(conn, did="AAA-20261015-1"):
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B", "target_weight": 0.10})
    _dec(conn, did, "AAA", D1, "BUY", card=CARD)


def _log(conn, did, action, review_type, extra=None, t="AAA", date=D1, reason_code=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "signal": action,
                                  "reason_code": reason_code,
                                  "data": {"ticker": t, "decision_date": date, "action": action,
                                           "review_type": review_type, **(extra or {})},
                                  "created_at": db.now_iso()})


D80_STOP = {"stopped_at_gate": "STEP0", "stop_mode": "A_THESIS_CHECK", "gate1_not_deleted": None,
            "gate2_price_below_cap": None, "gate3_pass": None}


def _orders(conn):
    return [dict(r) for r in conn.execute("SELECT order_id, ticker, side, qty, status, decision_id FROM orders "
                                          "ORDER BY order_id").fetchall()]


# ================================================================ G-daily-order

def test_m7a04_daily_refuses_skip_ahead(world):
    """A3-M7A-04：較早交易日未完成時不可先跑較晚日（否則較早日永遠不能補）。"""
    cfg, paths, conn = world
    _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    s2 = _run(cfg, paths, conn, D2, FakeAlpaca({k: v for k, v in _bars().items() if k != D2}))
    assert "error" in s2                                         # D2 失敗（當日日線未到）
    with pytest.raises(DP.DailyError, match=f"先補 {D2}"):
        _run(cfg, paths, conn, D3, FakeAlpaca(_bars()))
    s2b = _run(cfg, paths, conn, D2, FakeAlpaca(_bars()))        # 較早日仍可補
    assert "error" not in s2b
    s3 = _run(cfg, paths, conn, D3, FakeAlpaca(_bars()))
    assert "error" not in s3


def test_m7a04_first_daily_and_force_not_blocked(world):
    cfg, paths, conn = world
    s = _run(cfg, paths, conn, D3, FakeAlpaca(_bars()))         # 首次 daily：無先前完成日，不擋
    assert "error" not in s


def test_v3303_health_flags_gap(world):
    """A3-V3-3-03：較早 daily 失敗、較晚 daily 完成（既有資料或 --force）→ health 警示。"""
    cfg, paths, conn = world
    ts = db.now_iso()
    db.insert(conn, "runs", {"run_id": f"daily-{D2}", "kind": "daily_close", "asof": D2, "status": "failed",
                             "started_at": ts})
    db.insert(conn, "runs", {"run_id": f"daily-{D3}", "kind": "daily_close", "asof": D3, "status": "done",
                             "started_at": ts, "finished_at": ts})
    h = metrics.health(conn, paths.state)
    assert h["daily_gap"] == D2
    assert h["warning"] and D2 in h["warning"] and "未完成" in h["warning"]


# ================================================================ G-buy-supersede

@pytest.mark.parametrize("rt,act,extra,rc", [
    ("TRIAGE", "WATCH", {"thesis_check": "不適用"}, None),
    ("THESIS", "WATCH", {"thesis_check": "完好"}, None),
    (None, "NO_ACTION", D80_STOP, "MISSING_DATA"),                 # A3-V3-3-01：D80 非分析性停止
])
def test_m7d02_later_non_superseding_log_keeps_buy(world, rt, act, extra, rc):
    """A3-M7D-02／A3-V3-3-01：窗口內 BUY 之後的 TRIAGE 日誌、THESIS 完好、D80 停止不吞掉未下單 BUY。"""
    cfg, paths, conn = world
    _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    time.sleep(1.1)   # db.now_iso 秒精度：late_after 為嚴格大於
    _late_buy(conn)
    _log(conn, "AAA-20261015-2", act, rt, extra, reason_code=rc)
    s = _run(cfg, paths, conn, D2, FakeAlpaca(_bars()))
    assert _placed_buys(s, "AAA"), s["buys"]


def test_m7d02_later_full_watch_still_cancels_buy(world):
    cfg, paths, conn = world
    _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    time.sleep(1.1)
    _late_buy(conn)
    _log(conn, "AAA-20261015-2", "WATCH", None, {"gate3_pass": False})
    s = _run(cfg, paths, conn, D2, FakeAlpaca(_bars()))
    assert not _placed_buys(s, "AAA")


def test_supersedes_buy_rules():
    assert not Z.supersedes_buy({"review_type": "TRIAGE", "action": "WATCH"})
    assert not Z.supersedes_buy({"review_type": "PRICE", "action": "WATCH"})
    assert not Z.supersedes_buy({"review_type": "THESIS", "action": "WATCH", "thesis_check": "完好"})
    assert Z.supersedes_buy({"review_type": "THESIS", "action": "WATCH", "thesis_check": "疑似"})
    assert Z.supersedes_buy({"review_type": None, "action": "WATCH"})
    assert Z.supersedes_buy({"review_type": None, "action": "NO_ACTION", "stopped_at_gate": "G3"}, "TOO_HARD")
    assert not Z.supersedes_buy({"review_type": None, "action": "NO_ACTION", **D80_STOP}, "MISSING_DATA")


def test_v3301_user_reject_does_not_cancel_open_buy(world):
    """A3-V3-3-01：同意頁拒絕強模型（D80 STEP0 MISSING_DATA 停止日誌）→ 隔天重核不撤已掛買單。"""
    cfg, paths, conn = world
    _late_buy(conn)
    s1 = _run(cfg, paths, conn, D1, FakeAlpaca(_bars(d2_low=50.5)))
    oid = _placed_buys(s1, "AAA")[0]["order_id"]
    time.sleep(1.1)
    db.insert(conn, "approvals", {"ticker": "AAA", "run_id": "review-2026-10-15-watch", "asof": D1,
                                  "job_id": "j1", "job_type": "FULL_A", "tier": "strong", "holding": 0,
                                  "reason": "x", "status": "pending", "created_at": db.now_iso()})
    aid = conn.execute("SELECT approval_id FROM approvals").fetchone()[0]
    approvals.reject(conn, paths, [aid], note="本月額度不足")
    assert Z.revalidate_open_buys(conn, D2) == []
    # 對照：委託後的完整分析 WATCH 仍撤單
    time.sleep(1.1)
    _log(conn, "AAA-20261016-1", "WATCH", None, {"gate3_pass": False}, date=D2)
    assert [c["order_id"] for c in Z.revalidate_open_buys(conn, D2)] == [oid]


def test_m7d02_deferred_buy_survives_later_triage(world):
    """A3-M7D-02：上次延後的 BUY，之後只有 TRIAGE 日誌 → 仍併回。"""
    cfg, paths, conn = world
    _late_buy(conn)
    ts = db.now_iso()
    db.insert(conn, "runs", {"run_id": f"daily-{D1}", "kind": "daily_close", "asof": D1, "status": "done",
                             "started_at": ts, "finished_at": ts,
                             "summary": {"deferred_buys": [{"decision_id": "AAA-20261015-1", "ticker": "AAA",
                                                            "action": "BUY"}]}})
    _log(conn, "AAA-20261016-1", "HOLD", "TRIAGE", {"thesis_check": "不適用"}, date=D2)
    got = DP._deferred_buys(conn, D2, f"daily-{D2}", set())
    assert [x["decision_id"] for x in got] == ["AAA-20261015-1"]
    _log(conn, "AAA-20261016-2", "WATCH", None, {"gate3_pass": False}, date=D2)
    assert DP._deferred_buys(conn, D2, f"daily-{D2}", set()) == []


def test_m7b05_switch_candidate_not_closed_by_triage(world):
    """A3-M7B-05：換股候選不因較新的 TRIAGE 日誌結案；完整分析 WATCH 才結案。"""
    cfg, paths, conn = world
    for i in range(Z.MAX_HOLDINGS):
        db.insert(conn, "positions", {"ticker": f"H{i}", "qty": 1, "avg_cost": 1.0, "first_buy_date": "2025-01-01",
                                      "updated_at": db.now_iso()})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    db.insert(conn, "anomalies", {"ticker": "AAA", "kind": "switch_candidate", "status": "open", "pause_trading": 0,
                                  "detail": {"date": D1}, "created_at": db.now_iso()})
    _log(conn, "AAA-20261016-1", "WATCH", "TRIAGE", {"thesis_check": "不適用"}, date=D2)
    DP._switch_candidates(conn, [], D2)
    st = lambda: conn.execute("SELECT status FROM anomalies WHERE kind='switch_candidate'").fetchone()[0]  # noqa: E731
    assert st() == "open"
    _log(conn, "AAA-20261016-2", "WATCH", None, {"gate3_pass": False}, date=D2)
    DP._switch_candidates(conn, [], D2)
    assert st() == "resolved"


# ================================================================ G-sellall-buy

def test_x101_sell_all_then_buy_same_window_only_sell(world):
    """A3-X1-01／X2-01：同窗口 SELL_ALL 後的 BUY 不下單（全清中，買回走新建倉）；D2 後無殘股。"""
    cfg, paths, conn = world
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 100, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    _reg(conn, "AAA", "HOLDING", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "SELL_ALL", review_type="THESIS")
    _dec(conn, "AAA-20261015-2", "AAA", D1, "BUY", card=CARD)
    alp = FakeAlpaca(_bars())
    s1 = _run(cfg, paths, conn, D1, alp)
    assert "error" not in s1, s1
    assert [o["side"] for o in _orders(conn)] == ["SELL"]
    assert any("全清中" in (p.get("reason") or "") for p in s1["buys"])
    _run(cfg, paths, conn, D2, alp)
    assert "AAA" not in book.positions(conn)


def test_x101_open_sell_all_blocks_tranche_and_add(world):
    cfg, paths, conn = world
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 100, "avg_cost": 50.0, "first_buy_date": D1,
                                  "updated_at": db.now_iso()})
    _reg(conn, "AAA", "HOLDING", {"cik": "1", "auto_buy_cap": 60.0, "grade": "B",
                                  "build_plan": {"target_weight": 0.1, "tranches_left": 2, "started": D1}})
    _dec(conn, "AAA-SA", "AAA", D1, "SELL_ALL", review_type="THESIS")
    broker.place_order(conn, "AAA", "SELL", 100, None, "AAA-SA", D1)
    closes, adv = {"AAA": 50.0}, {"AAA": 1e9}
    tr, _ = Z.plan_tranches(conn, D2, closes, adv, 5)
    assert all(p["qty"] == 0 for p in tr) and any("全清中" in p["reason"] for p in tr)
    _dec(conn, "AAA-ADD", "AAA", D2, "ADD", card=CARD)
    buys = Z.plan_buys(conn, D2, Z.load_decisions(conn, D2), closes, adv, 5)
    assert [p["qty"] for p in buys] == [0] and "全清中" in buys[0]["reason"]


def test_x102_rerun_after_manual_cancel_does_not_replace(world):
    """A3-X1-02：人工撤單後同日重跑，同一 BUY 決策不再重掛。"""
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    alp = FakeAlpaca(_bars())
    s1 = _run(cfg, paths, conn, D1, alp)
    broker.cancel_manual(conn, s1["orders"][0]["order_id"], "不想買", D1)
    _run(cfg, paths, conn, D1, alp)
    assert [o for o in _orders(conn) if o["status"] == "open"] == []


def _ledger(state_dir):
    from trader.paths import get_paths
    paths = get_paths(state_dir=state_dir).ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, 100_000, "2026-10-01")
    return c


def test_x202_sell_all_sells_all_held(state_dir):
    """A3-X2-02：SELL_ALL 委託 100 股、撮合前帳上 150 股 → 賣 150（全清不留殘股）。"""
    c = _ledger(state_dir)
    db.insert(c, "positions", {"ticker": "AAA", "qty": 100, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                               "updated_at": db.now_iso()})
    _dec(c, "AAA-SA", "AAA", D1, "SELL_ALL", review_type="THESIS")
    oid = broker.place_order(c, "AAA", "SELL", 100, None, "AAA-SA", D1)
    c.execute("UPDATE positions SET qty=150 WHERE ticker='AAA'")
    c.commit()
    f = broker.match_orders(c, D2, {"AAA": {"open": 50, "high": 51, "low": 49, "close": 50}}, 0, 0)
    assert [(x["order_id"], x["qty"]) for x in f] == [(oid, 150)]
    assert "AAA" not in book.positions(c)
    c.close()


# ================================================================ G-broker-match

def _ca_fail(c, t, alp_type, date=D1):
    ev = {"event_id": f"{t}:alpaca:{date}:1", "ticker": t, "source": "alpaca", "date": date,
          "alpaca": {"ticker": t, "type": alp_type, "ex_date": date}}
    ca.record_job_failure(c, ev, "j1", "TransportError")


@pytest.mark.parametrize("alp_type,ev_date,fills", [("forward_split", D1, False), ("cash_dividend", D1, True),
                                                     ("forward_split", "2026-10-30", True)])
def test_m7c01_d88_split_pause_holds_sell_all(state_dir, alp_type, ev_date, fills):
    """A3-M7C-01（D88）：比例類公司行動工作失敗且事件日 ≤ 撮合日 → SELL_ALL 不撮合、委託保留；現金股利照 D06 放行。"""
    c = _ledger(state_dir)
    db.insert(c, "positions", {"ticker": "KO", "qty": 100, "avg_cost": 100.0, "first_buy_date": "2025-01-01",
                               "updated_at": db.now_iso()})
    _dec(c, "KO-S", "KO", D1, "SELL_ALL", review_type="THESIS")
    oid = broker.place_order(c, "KO", "SELL", 100, None, "KO-S", D1)
    _ca_fail(c, "KO", alp_type, ev_date)
    f = broker.match_orders(c, D2, {"KO": {"open": 50, "high": 51, "low": 49, "close": 50}}, 0, 0)
    if fills:
        assert [x["order_id"] for x in f] == [oid]
    else:
        assert f == [] and [o["order_id"] for o in book.open_orders(c)] == [oid]
        assert book.positions(c)["KO"]["qty"] == 100
    c.close()


def test_m7d05_broker_doc_matches_behavior():
    assert "連 SELL_ALL 全清也擋" not in (broker.__doc__ or "")
    assert "撮合延到解除" in (broker.__doc__ or "")


# ================================================================ G-daily-misc

def test_m7b06_expire_stale_uses_alpaca_calendar(world, monkeypatch):
    """A3-M7B-06：approvals.expire_stale 用 daily 的 Alpaca 交易日（含臨時休市），不用純規則日曆。"""
    cfg, paths, conn = world
    seen = {}

    def fake(conn_, asof, days=approvals.EXPIRE_TRADING_DAYS, cal=None):
        seen["cal"] = cal
        return []
    monkeypatch.setattr(DP.approvals, "expire_stale", fake)
    _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    cal = seen["cal"]
    assert cal is not None and cal.source == "alpaca"


def test_m7b06_closure_day_not_counted(state_dir):
    c = _ledger(state_dir)
    db.insert(c, "approvals", {"ticker": "AAA", "run_id": "r", "asof": "2026-10-09", "job_id": "j", "job_type": "FULL_A",
                               "tier": "strong", "holding": 0, "reason": "x", "status": "pending",
                               "created_at": db.now_iso()})
    days = ["2026-10-12", "2026-10-13", "2026-10-15", "2026-10-16"]   # 10-14 臨時休市
    assert approvals.expire_stale(c, "2026-10-16", cal=TradingCalendar(days, source="alpaca")) == []
    c.close()


def test_m7c04_empty_calendar_keeps_cache(tmp_path):
    """A3-M7C-04：Alpaca 日曆回空清單 → 不覆寫快取，改用快取。"""
    p = tmp_path / "cal.json"
    good = [{"date": "2026-10-15"}, {"date": "2026-10-16"}]
    p.write_text(json.dumps(good), encoding="utf-8")

    class C:
        def calendar(self, s, e):
            return []
    cal = TradingCalendar.load(C(), p, "2026-10-01", "2026-10-31")
    assert cal.source == "cache" and json.loads(p.read_text()) == good


def _fake_engine_run(monkeypatch, ran):
    def run(self, run_id):
        for r in self.conn.execute("SELECT job_id FROM jobs WHERE run_id=? AND status='pending'", (run_id,)).fetchall():
            ran.append(r["job_id"])
            self.conn.execute("UPDATE jobs SET status='done' WHERE job_id=?", (r["job_id"],))
        self.conn.commit()
        return {"status": "done"}
    monkeypatch.setattr(DP.Engine, "run", run)


def test_m7c06_watch_cash_dividend_not_dispatched(world, monkeypatch):
    """A3-M7C-06：觀察檔（無持股、無委託）的 Alpaca 現金股利不派 CORP_ACTION；持股的照派。"""
    cfg, paths, conn = world
    ran: list[str] = []
    _fake_engine_run(monkeypatch, ran)
    _reg(conn, "ZZZ", "WATCH", {"cik": "2"})
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 10, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    _reg(conn, "AAA", "HOLDING", {"cik": "1", "grade": "B"})
    div = lambda t: {"ticker": t, "type": "cash_dividend", "ex_date": D1, "ratio_new_per_old": None,  # noqa: E731
                     "cash_per_share": 0.5, "new_ticker": None, "raw": {"id": f"d-{t}"}, "payable_date": None}
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars(), actions=[div("ZZZ"), div("AAA")]))
    assert [e["ticker"] for e in s["corp_events"]] == ["AAA"]
    assert len(ran) == 1


def test_m7e03_sigterm_outside_engine_marks_failed(world, monkeypatch):
    """A3-M7E-03：daily 非引擎階段收到 SIGTERM → runs['daily-D'] 為 failed（有 error_stage），不停在 running。"""
    cfg, paths, conn = world
    hits = []
    old = signal.signal(signal.SIGTERM, lambda s, f: hits.append(s))   # 代替預設的「直接終止」
    try:
        monkeypatch.setattr(DP.Z, "tick_build_state",
                            lambda *a, **k: os.kill(os.getpid(), signal.SIGTERM))
        with pytest.raises(KeyboardInterrupt):
            _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
        r = conn.execute("SELECT status, summary FROM runs WHERE run_id=?", (f"daily-{D1}",)).fetchone()
        assert r["status"] == "failed" and json.loads(r["summary"])["error_stage"]
        assert signal.getsignal(signal.SIGTERM) is not None and not hits
    finally:
        signal.signal(signal.SIGTERM, old)


def test_x203_days_above_cap_uses_ref_price(state_dir):
    """A3-X2-03：收盤 59.98、上限 60、滑價 5bps → 含滑價不低於上限，days_above_cap 累加（與加碼許可同口徑）。"""
    c = _ledger(state_dir)
    db.insert(c, "positions", {"ticker": "AAA", "qty": 10, "avg_cost": 50.0, "first_buy_date": D1,
                               "updated_at": db.now_iso()})
    _reg(c, "AAA", "HOLDING", {"auto_buy_cap": 60.0, "build_plan": {"tranches_left": 2, "days_above_cap": 3}})
    Z.tick_build_state(c, D2, {"AAA": 59.98}, 5)
    bp = json.loads(c.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()[0])["build_plan"]
    assert bp["days_above_cap"] == 4
    c.close()


# ================================================================ A3-M7E-01 daily 派工端

def _ev(t="AAA", date=D1):
    return {"event_id": f"{t}:alpaca:{date}:x1", "ticker": t, "source": "alpaca", "date": date,
            "alpaca": {"ticker": t, "type": "forward_split", "ex_date": date, "ratio_new_per_old": 2.0},
            "text": "split", "url": None, "price_before": None, "date_before": None, "price_after": None,
            "date_after": date}


@pytest.mark.parametrize("orphan_run,reuse", [(f"daily-{D1}:ca", True), ("daily-2026-10-14:ca", False)])
def test_m7e01_orphan_job_not_duplicated(world, monkeypatch, orphan_run, reuse):
    """A3-M7E-01：中斷留下的同事件 pending CORP_ACTION 工作：同一 ca run → 只重跑它，不另派 r2；
    其他 run 的孤兒 → 標 skipped（不再被續跑），今天重派一個。同事件只會有一個工作執行。"""
    cfg, paths, conn = world
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 10, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    _reg(conn, "AAA", "HOLDING", {"cik": "1", "grade": "B"})
    ev = _ev()
    eng = Engine(conn, _router(cfg, paths), paths, cfg)
    eng.start_run(orphan_run, "corp_action", D1)
    oj = f"{orphan_run.split(':')[0]}:AAA:CORP_ACTION:1"
    eng.add_job(JobSpec(job_id=oj, run_id=orphan_run, job_type="CORP_ACTION", ticker="AAA", asof=D1,
                        params={"event": ev}))
    ran: list[str] = []
    _fake_engine_run(monkeypatch, ran)
    monkeypatch.setattr(DP.corp_actions, "detect", lambda *a, **k: [dict(ev)])
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    assert "error" not in s, s.get("error")
    assert len(ran) == 1
    jobs = {r["job_id"]: r["status"] for r in conn.execute(
        "SELECT job_id, status FROM jobs WHERE json_extract(params, '$.event.event_id')=?", (ev["event_id"],))}
    if reuse:
        assert ran == [oj] and jobs == {oj: "done"}
    else:
        assert ran != [oj] and jobs[oj] == "skipped" and sorted(jobs.values()) == ["done", "skipped"]


def test_m7e01_orphan_in_same_ca_run_runs_even_if_not_redetected(world, monkeypatch):
    cfg, paths, conn = world
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 10, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    _reg(conn, "AAA", "HOLDING", {"cik": "1", "grade": "B"})
    eng = Engine(conn, _router(cfg, paths), paths, cfg)
    eng.start_run(f"daily-{D1}:ca", "corp_action", D1)
    oj = f"daily-{D1}:AAA:CORP_ACTION:1"
    eng.add_job(JobSpec(job_id=oj, run_id=f"daily-{D1}:ca", job_type="CORP_ACTION", ticker="AAA", asof=D1,
                        params={"event": _ev()}))
    ran: list[str] = []
    _fake_engine_run(monkeypatch, ran)
    monkeypatch.setattr(DP.corp_actions, "detect", lambda *a, **k: [])
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    assert ran == [oj] and [e["event_id"] for e in s["corp_events"]] == [_ev()["event_id"]]


# ================================================================ G-scheduler

def test_m7a08_watch_success_resolves_task_failed(world):
    """A3-M7A-08：watch 成功 → 結案 watch 的 scheduler_task_failed；提示改為自動再試（D81）。"""
    from trader.scheduler import Scheduler
    cfg, paths, conn = world
    for task in ("watch:2026-10-01", "screen:2026-09-30"):
        db.insert(conn, "anomalies", {"ticker": None, "kind": "scheduler_task_failed", "status": "open",
                                      "pause_trading": 0, "detail": {"task": task}, "created_at": db.now_iso()})
    st: dict = {}
    Scheduler._done(SimpleNamespace(log=lambda *_: None), conn, st, "watch:2026-10-01",
                    ["review", "--scope", "watch", "--asof", D3])
    rows = {json.loads(r["detail"])["task"]: r["status"] for r in conn.execute(
        "SELECT detail, status FROM anomalies WHERE kind='scheduler_task_failed'")}
    assert rows == {"watch:2026-10-01": "resolved", "screen:2026-09-30": "open"}
    h = metrics.anomaly_hint({"anomaly_id": 1, "kind": "scheduler_task_failed", "detail": {"task": "watch:x"}})
    assert "自動再試" in h and "reset" not in h.split("自動再試")[0]


# ================================================================ 順手：book.adjust 現金＋解除暫停、標籤

def test_adjust_cash_with_unpause_resolves_job_failed(state_dir):
    """同一指令 --ticker X＋cash_delta＋--unpause X＝人工已入帳（例：股利）→ 結案 corp_action_job_failed，不再重派。"""
    c = _ledger(state_dir)
    db.insert(c, "positions", {"ticker": "KO", "qty": 100, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                               "updated_at": db.now_iso()})
    _ca_fail(c, "KO", "cash_dividend")
    r = book.adjust(c, date=D2, ticker="KO", cash_delta=50.0, unpause="KO", note="人工入帳股利")
    assert r.get("resolved_job_failed") and not r.get("unpaused_only")
    assert ca.unresolved_failed_events(c) == []
    assert c.execute("SELECT status FROM anomalies WHERE kind='corp_action_job_failed'").fetchone()[0] == "resolved"
    c.close()


def test_adjust_cash_other_ticker_does_not_resolve(state_dir):
    c = _ledger(state_dir)
    _ca_fail(c, "KO", "cash_dividend")
    r = book.adjust(c, date=D2, cash_delta=50.0, unpause="KO", note="只調現金")   # 未指定 --ticker KO
    assert r.get("unpaused_only") and not r.get("resolved_job_failed")
    c.close()


def test_ca_job_failed_label_reflects_unpause(state_dir):
    c = _ledger(state_dir)
    _ca_fail(c, "KO", "forward_split")
    meaning = lambda: [r for r in metrics.anomalies(c)["open"] if r["kind"] == "corp_action_job_failed"][0]["meaning"]  # noqa: E731
    assert "暫停" in meaning()
    book.adjust(c, date=D2, unpause="KO", note="先恢復交易")
    m = meaning()
    assert "已解除暫停" in m and "該檔暫停交易" not in m
    assert "該檔暫停交易；" not in metrics.ANOMALY_KINDS["corp_action_job_failed"][1]
    c.close()
