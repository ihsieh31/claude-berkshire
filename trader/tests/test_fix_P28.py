"""P28：公司行動覆核與入帳（M7-C＋M3-E）回歸測試。

M7-D8S-01／M7-D1-06／M3-D8-01（來源旁證、類型對照、新代號）、M7-D1-01（重派後結案 job_failed）、
M7-D1-02（同日同類型第二筆 Alpaca 事件轉人工）、M7-D1-07（零股現金了結的平均成本）、
M7-D1-08（代號變更同步 signals／decisions）、M7-D2-06（暫停中的比例／代號類行動轉人工、異常跟新代號）、
M7-D5-07（入帳留改前改後）、M7-D9-02（Alpaca 回看 14 曆日、事件日後有成交轉人工）。
不連網；資料寫入暫存 SQLite。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trader import db
from trader.jobs.corp_jobs import CorpActionHandler
from trader.ledger import book
from trader.ledger import corp_actions as ca
from trader.models import JobSpec
from trader.paths import get_paths
from trader.portfolio import sizing

ASOF = "2026-10-16"
NOW = "Fri 2026-10-16 8:00 AM CST (UTC+08:00)"


@pytest.fixture
def env(tmp_path):
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100000, "2026-01-02")
    return SimpleNamespace(paths=paths, conn=conn)


def hold(conn, T, qty, avg, first="2026-01-02"):
    conn.execute("INSERT INTO positions(ticker, qty, avg_cost, first_buy_date, updated_at) VALUES (?,?,?,?,?)",
                 (T, qty, avg, first, NOW))
    conn.commit()


def ev(T="KO", source="alpaca", alp=None, pb=60.0, pa=60.0, date=ASOF, seq="1"):
    return {"event_id": f"{T}:{source}:{date}:{seq}", "ticker": T, "source": source, "date": date, "text": "原文",
            "alpaca": alp, "url": None, "price_before": pb, "date_before": "2026-10-15", "price_after": pa,
            "date_after": date}


def res(**kw):
    b = {"ticker": "KO", "action_type": "現金股利", "effective_date": ASOF, "pay_date": "2026-11-01",
         "ratio_new_per_old": None, "cash_per_share": 0.5, "fractional_handling": "不適用",
         "new_ticker": None, "needs_human": False, "evidence": "x", "reason": "x"}
    b.update(kw)
    return b


DIV = {"ticker": "KO", "type": "cash_dividend", "ex_date": ASOF, "ratio_new_per_old": None, "cash_per_share": 0.5,
       "new_ticker": None, "payable_date": "2026-11-01", "raw": {"id": "d1"}}
NAME = {"ticker": "KO", "type": "name_change", "ex_date": ASOF, "ratio_new_per_old": None, "cash_per_share": None,
        "new_ticker": "KOX", "raw": {"id": "n1"}}
SPLIT = {"ticker": "KO", "type": "forward_split", "ex_date": ASOF, "ratio_new_per_old": 2.0, "cash_per_share": None,
         "new_ticker": None, "raw": {"id": "s1"}}


def open_anoms(conn, kind=None):
    q = "SELECT ticker, kind FROM anomalies WHERE status='open'" + (" AND kind=?" if kind else "")
    return [tuple(r) for r in conn.execute(q, (kind,) if kind else ())]


def run_post(env, result, event, T="KO"):
    job = JobSpec(job_id=f"d:{T}:CORP_ACTION:1", run_id="d", job_type="CORP_ACTION", ticker=T, asof=ASOF,
                  params={"event": event})
    ctx = SimpleNamespace(conn=env.conn, paths=env.paths, work_dir=env.paths.work)
    return CorpActionHandler().post(job, result, ctx)


# ---------------------------------------------------------------- M7-D8S-01／M7-D1-06／M3-D8-01

@pytest.mark.parametrize("source", ["gap", "8k"])
def test_cash_dividend_without_alpaca_is_rejected(env, source):
    hold(env.conn, "KO", 100, 60.0)
    errs = ca.verify(res(cash_per_share=500.0), ev(source=source), ca.position_of(env.conn, "KO"))
    assert any("Alpaca 旁證" in e for e in errs)


def test_alpaca_name_change_judged_as_dividend_is_rejected(env):
    hold(env.conn, "KO", 100, 60.0)
    errs = ca.verify(res(cash_per_share=500.0), ev(alp=NAME), ca.position_of(env.conn, "KO"))
    assert any("類型與 Alpaca 不符" in e for e in errs)


def test_unmapped_alpaca_type_goes_human(env):
    alp = {**DIV, "type": "cash_merger", "cash_per_share": 50.0}
    errs = ca.verify(res(cash_per_share=50.0), ev(alp=alp), None)
    assert any("類型與 Alpaca 不符" in e for e in errs)


def test_matching_alpaca_dividend_passes(env):
    hold(env.conn, "KO", 100, 60.0)
    assert ca.verify(res(), ev(alp=DIV), ca.position_of(env.conn, "KO")) == []


def test_ticker_change_must_match_alpaca_new_ticker(env):
    hold(env.conn, "KO", 100, 60.0)
    r = res(action_type="代號變更", cash_per_share=None, pay_date=None, new_ticker="PEP")
    assert any("新代號與 Alpaca 不符" in e for e in ca.verify(r, ev(alp=NAME), None))
    assert ca.verify(dict(r, new_ticker="kox"), ev(alp=NAME), None) == []          # 正規化後相同即可
    assert any("生效日" in e for e in ca.verify(dict(r, new_ticker="KOX", effective_date="2026-10-20"),
                                                ev(alp=NAME), None))


@pytest.mark.parametrize("source", ["gap", "8k"])
def test_ticker_change_without_alpaca_is_rejected_and_not_applied(env, source):
    hold(env.conn, "KO", 100, 60.0)
    r = res(action_type="代號變更", cash_per_share=None, pay_date=None, new_ticker="MSFT")
    e = ev(source=source)
    assert any("Alpaca 旁證" in x for x in ca.verify(r, e, None))
    run_post(env, r, e)
    assert "KO" in book.positions(env.conn) and "MSFT" not in book.positions(env.conn)
    assert "KO" in book.paused(env.conn)


def test_ticker_change_bad_format(env):
    r = res(action_type="代號變更", cash_per_share=None, pay_date=None, new_ticker="abc def")
    assert any("格式" in e for e in ca.verify(r, ev(alp={**NAME, "new_ticker": "ABC DEF"}), None))


def test_gap_dividend_end_to_end_keeps_cash(env):
    hold(env.conn, "KO", 100, 60.0)
    c0 = book.cash(env.conn)
    run_post(env, res(cash_per_share=500.0), ev(source="gap"))
    assert book.receivables(env.conn) == 0 and book.cash(env.conn) == c0
    assert "KO" in book.paused(env.conn)


# ---------------------------------------------------------------- M7-D1-01

@pytest.mark.parametrize("filing_items, result, expect_paused", [
    ("8.01", res(ticker="ABC", action_type="無公司行動", effective_date=None, cash_per_share=None, pay_date=None), False),
    ("5.03", res(ticker="ABC", action_type="拆股", effective_date="2026-10-15", ratio_new_per_old=2,
                 cash_per_share=None, pay_date=None), False),
])
def test_retried_event_resolves_job_failed(env, filing_items, result, expect_paused):
    hold(env.conn, "ABC", 10, 100.0)
    f = {"ABC": [{"filingDate": ASOF, "form": "8-K", "items": filing_items, "accessionNumber": "0001"}]}
    e = ca.detect(env.conn, ASOF, "2026-10-15", ["ABC"], [], f, {"ABC": {"close": 100}}, {"ABC": {"close": 50}})[0]
    ca.record_job_failure(env.conn, e, "job1", "timeout")
    assert "ABC" in book.paused(env.conn)
    pend = ca.unresolved_failed_events(env.conn)
    assert [x["event_id"] for x in pend] == [e["event_id"]]
    run_post(env, result, pend[0], T="ABC")
    assert open_anoms(env.conn, ca.JOB_FAILED_KIND) == []
    assert ("ABC" in book.paused(env.conn)) is expect_paused
    assert ca.unresolved_failed_events(env.conn) == []


def test_retried_event_rejected_resolves_job_failed_but_keeps_reject_pause(env):
    hold(env.conn, "KO", 100, 60.0)
    e = ev(source="gap")
    ca.record_job_failure(env.conn, e, "job1", "timeout")
    run_post(env, res(cash_per_share=500.0), e)
    assert open_anoms(env.conn, ca.JOB_FAILED_KIND) == []
    assert open_anoms(env.conn) == [("KO", "corp_action_check_failed")]


# ---------------------------------------------------------------- M7-D1-02

def test_second_same_day_dividend_goes_human(env):
    hold(env.conn, "XYZ", 100, 50.0)
    acts = [{**DIV, "ticker": "XYZ", "cash_per_share": 0.5, "raw": {"id": "a1"}},
            {**DIV, "ticker": "XYZ", "cash_per_share": 2.0, "raw": {"id": "a2"}}]
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["XYZ"], acts, {}, {}, {})
    assert len(evs) == 2
    for e, cps in zip(evs, (0.5, 2.0)):
        run_post(env, res(ticker="XYZ", cash_per_share=cps), e, T="XYZ")
    assert book.receivables(env.conn) == pytest.approx(50.0)
    assert "XYZ" in book.paused(env.conn)
    # 回看窗口內隔日不再重派第二筆（已有異常）
    assert ca.detect(env.conn, "2026-10-19", ASOF, ["XYZ"], acts, {}, {}, {}) == []


def test_same_event_twice_is_still_duplicate(env):
    hold(env.conn, "KO", 100, 60.0)
    assert ca.apply(env.conn, env.paths, res(), ev(alp=DIV), ASOF)["status"] == "applied"
    assert ca.apply(env.conn, env.paths, res(), ev(alp=DIV), ASOF)["status"] == "duplicate"


# ---------------------------------------------------------------- M7-D1-07

def test_reverse_split_cash_in_lieu_avg_cost_proportional(env):
    hold(env.conn, "RS", 105, 10.0)
    e = ev(T="RS", alp={"ratio_new_per_old": 0.1, "type": "reverse_split"}, pb=10.0, pa=100.0)
    r = res(ticker="RS", action_type="合股", ratio_new_per_old=0.1, cash_per_share=None, pay_date=None,
            fractional_handling="現金了結")
    assert ca.verify(r, e, ca.position_of(env.conn, "RS")) == []
    ca.apply(env.conn, env.paths, r, e, ASOF)
    p = book.positions(env.conn)["RS"]
    assert p["qty"] == 10 and p["avg_cost"] == pytest.approx(100.0)


# ---------------------------------------------------------------- M7-D1-08／M7-D2-06

def _seed_card(conn, T):
    card = {"ticker": T, "as_of": "2026-10-01", "decision_log_id": f"{T}-20261001-1"}
    db.insert(conn, "signals", {"decision_id": f"{T}-20261001-1", "ticker": T, "signal": "BUY",
                                "data": json.dumps(card), "created_at": NOW})
    db.insert(conn, "decisions", {"decision_id": f"{T}-20261001-1", "ticker": T, "decision_date": "2026-10-01",
                                  "run_id": "r", "job_id": "j", "signal": "BUY", "reason_code": None,
                                  "data": json.dumps({"gate1": True, "gate2": "通過", "gate3_pass": True}),
                                  "created_at": NOW})


def test_ticker_change_moves_signals_decisions_and_open_anomalies(env):
    hold(env.conn, "KO", 100, 60.0)
    _seed_card(env.conn, "KO")
    db.insert(env.conn, "anomalies", {"ticker": "KO", "kind": "unregistered_holding", "detail": "{}",
                                      "status": "open", "pause_trading": 0, "created_at": NOW})
    r = res(action_type="代號變更", cash_per_share=None, pay_date=None, new_ticker="KOX")
    e = ev(alp=NAME)
    assert ca.verify(r, e, None) == []
    assert ca.apply(env.conn, env.paths, r, e, ASOF)["status"] == "applied"
    assert sizing.latest_card(env.conn, "KOX")["decision_log_id"] == "KO-20261001-1"
    assert sizing._latest_gate_decision(env.conn, "KOX", ASOF)["decision_id"] == "KO-20261001-1"
    assert open_anoms(env.conn) == [("KOX", "unregistered_holding")]
    data = json.loads(env.conn.execute("SELECT data FROM corporate_actions").fetchone()[0])
    assert data["old_ticker"] == "KO"


def test_ticker_change_while_paused_goes_human(env):
    hold(env.conn, "KO", 100, 60.0)
    db.insert(env.conn, "anomalies", {"ticker": "KO", "kind": "manual_hold", "detail": "{}", "status": "open",
                                      "pause_trading": 1, "created_at": NOW})
    r = res(action_type="代號變更", cash_per_share=None, pay_date=None, new_ticker="KOX")
    run_post(env, r, ev(alp=NAME))
    assert "KO" in book.positions(env.conn) and "KOX" not in book.positions(env.conn)
    assert book.paused(env.conn) == {"KO"}


def test_split_while_paused_goes_human_but_dividend_still_books(env):
    hold(env.conn, "KO", 100, 60.0)
    db.insert(env.conn, "anomalies", {"ticker": "KO", "kind": "manual_hold", "detail": "{}", "status": "open",
                                      "pause_trading": 1, "created_at": NOW})
    r = res(action_type="拆股", ratio_new_per_old=2.0, cash_per_share=None, pay_date=None)
    out = ca.apply(env.conn, env.paths, r, ev(alp=SPLIT, pb=60.0, pa=30.0), ASOF)
    assert out["status"] == "conflict" and book.positions(env.conn)["KO"]["qty"] == 100
    assert ca.apply(env.conn, env.paths, res(), ev(alp=DIV, seq="d"), ASOF)["status"] == "applied"


def test_own_job_failed_pause_does_not_block_retried_split(env):
    hold(env.conn, "KO", 100, 60.0)
    e = ev(alp=SPLIT, pb=60.0, pa=30.0)
    ca.record_job_failure(env.conn, e, "j", "timeout")
    r = res(action_type="拆股", ratio_new_per_old=2.0, cash_per_share=None, pay_date=None)
    run_post(env, r, e)
    assert book.positions(env.conn)["KO"]["qty"] == 200 and book.paused(env.conn) == set()


# ---------------------------------------------------------------- M7-D5-07

def test_ratio_apply_records_before_after(env):
    hold(env.conn, "KO", 61, 60.0)
    env.conn.execute("INSERT INTO orders(order_id, ticker, side, qty, limit_price, status, decision_id, created_date, "
                     "updated_at) VALUES ('o1','KO','SELL',30,80.0,'open','d1','2026-10-15',?)", (NOW,))
    env.conn.commit()
    r = res(action_type="拆股", ratio_new_per_old=2.0, cash_per_share=None, pay_date=None)
    ca.apply(env.conn, env.paths, r, ev(alp=SPLIT, pb=60.0, pa=30.0), ASOF)
    data = json.loads(env.conn.execute("SELECT data FROM corporate_actions").fetchone()[0])
    assert data["position_before"]["qty"] == 61 and data["position_after"] == {"qty": 122, "avg_cost": 30.0}
    assert data["orders"] == [{"order_id": "o1", "qty_before": 30, "qty_after": 60, "limit_before": 80.0,
                               "limit_after": 40.0, "cancelled": False}]


# ---------------------------------------------------------------- M7-D9-02

def test_detect_late_alpaca_event_within_lookback(env):
    late = {**DIV, "ex_date": "2026-10-15"}
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [late], {}, {"KO": {"close": 60}}, {"KO": {"close": 61}})
    assert [e["event_id"] for e in evs] == ["KO:alpaca:2026-10-15:d1"]
    assert evs[0]["price_before"] is None and "晚到" in evs[0]["text"]
    too_old = {**DIV, "ex_date": "2026-10-02"}
    assert ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [too_old], {}, {}, {}) == []


def test_late_event_with_fills_after_event_goes_human(env):
    hold(env.conn, "KO", 100, 60.0)
    env.conn.execute("INSERT INTO orders(order_id, ticker, side, qty, limit_price, status, decision_id, created_date, "
                     "updated_at) VALUES ('o1','KO','BUY',10,70.0,'filled','d1','2026-10-14',?)", (NOW,))
    env.conn.execute("INSERT INTO fills(order_id, ticker, side, qty, price, commission, fill_date) "
                     "VALUES ('o1','KO','BUY',10,60.0,0,'2026-10-15')")
    env.conn.commit()
    e = ev(alp={**DIV, "ex_date": "2026-10-15"}, date="2026-10-15")
    r = res(effective_date="2026-10-15")
    assert ca.verify(r, e, ca.position_of(env.conn, "KO")) == []
    run_post(env, r, e)
    assert book.receivables(env.conn) == 0 and "KO" in book.paused(env.conn)


# ---------------------------------------------------------------- P28R：回看事件不壓掉今日跳空、來源比對

def test_booked_dividend_within_lookback_does_not_suppress_today_gap(env):
    """14 天內已入帳的股利不得壓掉今天的 ≥40% 跳空（Alpaca 漏記拆股時唯一的安全網）。"""
    hold(env.conn, "KO", 100, 60.0)
    div = {**DIV, "ex_date": "2026-10-08"}
    e = ca.detect(env.conn, "2026-10-08", "2026-10-07", ["KO"], [div], {}, {}, {})[0]
    assert ca.apply(env.conn, env.paths, res(effective_date="2026-10-08"), e, "2026-10-08")["status"] == "applied"
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [div], {}, {"KO": {"close": 100}}, {"KO": {"close": 25}})
    assert [x["event_id"] for x in evs] == ["KO:gap:2026-10-16:1"]


def test_late_split_and_today_gap_both_returned(env):
    late = {**SPLIT, "ex_date": "2026-10-15", "raw": {"id": "a1"}}
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [late], {}, {"KO": {"close": 100}}, {"KO": {"close": 25}})
    assert [x["event_id"] for x in evs] == ["KO:alpaca:2026-10-15:a1", "KO:gap:2026-10-16:1"]


def test_same_day_split_returns_only_alpaca(env):
    today = {**SPLIT, "raw": {"id": "a1"}}
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [today], {}, {"KO": {"close": 100}}, {"KO": {"close": 25}})
    assert [x["event_id"] for x in evs] == ["KO:alpaca:2026-10-16:a1"]


def test_alpaca_event_without_type_goes_human(env):
    hold(env.conn, "KO", 100, 60.0)
    alp = {k: v for k, v in DIV.items() if k != "type"}
    errs = ca.verify(res(), ev(alp=alp), ca.position_of(env.conn, "KO"))
    assert any("缺類型" in x for x in errs)
    assert any("缺類型" in x for x in ca.verify(res(), ev(alp=None), ca.position_of(env.conn, "KO")))


def test_alpaca_event_after_same_day_8k_row_is_duplicate(env):
    """M7-D1-02 只和 Alpaca 來源的既有列比 event_id：同日 8-K 先入帳同一行動 → duplicate，不轉人工。"""
    hold(env.conn, "KO", 100, 60.0)
    r = res(action_type="拆股", ratio_new_per_old=2.0, cash_per_share=None, pay_date=None)
    assert ca.apply(env.conn, env.paths, r, ev(source="8k", pb=60.0, pa=30.0), ASOF)["status"] == "applied"
    assert ca.apply(env.conn, env.paths, r, ev(alp=SPLIT, pb=60.0, pa=30.0, seq="s1"), ASOF)["status"] == "duplicate"
    assert book.positions(env.conn)["KO"]["qty"] == 200
