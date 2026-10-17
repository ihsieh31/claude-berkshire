"""公司行動（CONTRACTS_M5 §B）測試。

不連網；模型用 FakeRunner；資料寫入真的 SQLite（每個測試獨立 state 目錄）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader import db
from trader.config import load_config
from trader.jobs.engine import Engine
from trader.jobs.handlers import HANDLERS
from trader.jobs.corp_jobs import CorpActionHandler
from trader.ledger import corp_actions as ca
from trader.llm.fake import FakeRunner
from trader.llm.router import Router
from trader.models import JobSpec
from trader.paths import get_paths

ASOF = "2026-10-16"
NOW = "2026-10-16T00:00:00Z"


# ---------------------------------------------------------------- 夾具與建構函式

@pytest.fixture
def env(tmp_path):
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    return SimpleNamespace(paths=paths, conn=conn)


def hold(conn, T, qty, avg, first="2026-01-02"):
    conn.execute("INSERT INTO positions(ticker, qty, avg_cost, first_buy_date, updated_at) VALUES (?,?,?,?,?)",
                 (T, qty, avg, first, NOW))
    conn.commit()


def registry(conn, T, status, data):
    conn.execute("INSERT INTO registry(ticker, status, first_buy_date, data, updated_at) VALUES (?,?,?,?,?)",
                 (T, status, None, json.dumps(data), NOW))
    conn.commit()


def order(conn, oid, T, side, qty, limit):
    conn.execute(
        "INSERT INTO orders(order_id, ticker, side, qty, limit_price, status, decision_id, created_date, updated_at) "
        "VALUES (?,?,?,?,?, 'open', 'd1', '2026-10-15', ?)", (oid, T, side, qty, limit, NOW))
    conn.commit()


def cap_file(paths, T, data):
    p = paths.state / "cap_inputs" / f"{T}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def event(T="KO", pb=100.0, pa=25.0, source="alpaca", alp=None, date=ASOF):
    return {"event_id": f"{T}:{source}:{date}:1", "ticker": T, "source": source, "date": date,
            "text": "原文", "alpaca": alp, "url": None, "price_before": pb, "date_before": "2026-10-15",
            "price_after": pa, "date_after": date}


def result(**kw):
    base = {"ticker": "KO", "action_type": "拆股", "effective_date": ASOF, "pay_date": None,
            "ratio_new_per_old": 4, "cash_per_share": None, "fractional_handling": "不適用",
            "new_ticker": None, "needs_human": False, "evidence": "原文", "reason": "測試"}
    base.update(kw)
    return base


def total(conn, kind):
    return conn.execute("SELECT COALESCE(SUM(amount),0) FROM cash_ledger WHERE kind=?", (kind,)).fetchone()[0]


def pos(conn, T):
    r = conn.execute("SELECT qty, avg_cost FROM positions WHERE ticker=?", (T,)).fetchone()
    return None if r is None else (r["qty"], r["avg_cost"])


def ca_rows(conn):
    return conn.execute("SELECT ticker, action_type, effective_date, status FROM corporate_actions ORDER BY ca_id").fetchall()


ALPACA_SPLIT = {"ticker": "KO", "type": "forward_split", "ex_date": ASOF, "ratio_new_per_old": 4.0,
                "cash_per_share": None, "new_ticker": None, "raw": {"id": "a1"}, "payable_date": None}


# ---------------------------------------------------------------- 覆核

def test_verify_forward_split_passes(env):
    hold(env.conn, "KO", 10, 100.0)
    assert ca.verify(result(), event(), ca.position_of(env.conn, "KO")) == []


def test_reverse_split_with_cash_in_lieu_passes(env):
    hold(env.conn, "KO", 27, 10.0)
    res = result(action_type="合股", ratio_new_per_old=0.1, fractional_handling="現金了結")
    assert ca.verify(res, event(pb=10.0, pa=100.0), ca.position_of(env.conn, "KO")) == []


def test_ratio_disagrees_with_alpaca_is_rejected(env):
    hold(env.conn, "KO", 10, 100.0)
    errs = ca.verify(result(ratio_new_per_old=3), event(alp=ALPACA_SPLIT), ca.position_of(env.conn, "KO"))
    assert any("Alpaca" in e for e in errs)


def test_ratio_disagrees_with_price_ratio_is_rejected(env):
    hold(env.conn, "KO", 10, 100.0)
    errs = ca.verify(result(), event(pb=100.0, pa=100.0), ca.position_of(env.conn, "KO"))
    assert any("前後價格" in e for e in errs)


def test_missing_prices_cannot_verify_ratio(env):
    errs = ca.verify(result(), event(pb=None, pa=None), None)
    assert any("缺前後價格" in e for e in errs)


@pytest.mark.parametrize("res", [
    result(needs_human=True),
    result(action_type="分拆", ratio_new_per_old=None, new_ticker="SPC"),
    result(action_type="併購換股", ratio_new_per_old=None, new_ticker="ACQ"),
    result(action_type="其他"),
])
def test_needs_human_types_return_human_error(env, res):
    hold(env.conn, "KO", 10, 100.0)
    errs = ca.verify(res, event(), ca.position_of(env.conn, "KO"))
    assert len(errs) == 1 and errs[0].startswith("需要人工")


def test_fraction_with_unknown_handling_is_rejected(env):
    hold(env.conn, "KO", 27, 10.0)
    res = result(action_type="合股", ratio_new_per_old=0.1, fractional_handling="不明")
    errs = ca.verify(res, event(pb=10.0, pa=100.0), ca.position_of(env.conn, "KO"))
    assert any("零股" in e for e in errs)


def test_discarding_fraction_breaks_market_value_conservation(env):
    hold(env.conn, "KO", 7, 10.0)
    res = result(action_type="合股", ratio_new_per_old=0.1, fractional_handling="無條件捨去")
    errs = ca.verify(res, event(pb=10.0, pa=100.0), ca.position_of(env.conn, "KO"))
    assert any("市值不守恆" in e for e in errs)


def test_cash_dividend_requires_positive_cash(env):
    res = result(action_type="現金股利", ratio_new_per_old=None, cash_per_share=0)
    assert any("現金" in e for e in ca.verify(res, event(pb=None, pa=None), None))


# ---------------------------------------------------------------- 入帳：比例類

def test_forward_split_adjusts_position(env):
    hold(env.conn, "KO", 10, 100.0)
    out = ca.apply(env.conn, env.paths, result(), event(), ASOF)
    assert out["status"] == "applied"
    assert pos(env.conn, "KO") == (40, 25.0)
    assert [tuple(r) for r in ca_rows(env.conn)] == [("KO", "拆股", ASOF, "applied")]


def test_reverse_split_cash_in_lieu_and_avg_cost(env):
    hold(env.conn, "KO", 27, 10.0)
    res = result(action_type="合股", ratio_new_per_old=0.1, fractional_handling="現金了結")
    ca.apply(env.conn, env.paths, res, event(pb=10.0, pa=100.0), ASOF)
    qty, avg = pos(env.conn, "KO")
    assert qty == 2 and avg == pytest.approx(135.0)
    assert total(env.conn, "corp_action_cash") == pytest.approx(70.0)


def test_reverse_split_to_zero_shares_cashes_out_and_sets_watch(env):
    hold(env.conn, "KO", 3, 10.0)
    registry(env.conn, "KO", "HOLDING", {"cost": 10.0})
    res = result(action_type="合股", ratio_new_per_old=0.1, fractional_handling="現金了結")
    ca.apply(env.conn, env.paths, res, event(pb=10.0, pa=100.0), ASOF)
    assert pos(env.conn, "KO") is None
    assert total(env.conn, "corp_action_cash") == pytest.approx(30.0)
    assert env.conn.execute("SELECT status FROM registry WHERE ticker='KO'").fetchone()[0] == "WATCH"


def test_orders_and_registry_and_cap_inputs_stay_in_sync(env):
    hold(env.conn, "KO", 10, 100.0)
    order(env.conn, "KO-buy-1", "KO", "BUY", 10, 100.0)
    order(env.conn, "KO-sell-1", "KO", "SELL", 10, None)
    order(env.conn, "KO-buy-2", "KO", "BUY", 3, 5.0)
    registry(env.conn, "KO", "HOLDING", {"auto_buy_cap": 80.0, "cost": 100.0, "note": "keep"})
    p = cap_file(env.paths, "KO", {"ticker": "KO", "oe": 4.0, "norm_eps": 4.0, "iv_cons": 80.0,
                                   "price": 100.0, "insurance": {"tbv_ps": 8.0, "float_ps": 2.0}})
    ca.apply(env.conn, env.paths, result(), event(), ASOF)

    orders = {r["order_id"]: (r["qty"], r["limit_price"], r["status"]) for r in env.conn.execute(
        "SELECT order_id, qty, limit_price, status FROM orders")}
    assert orders["KO-buy-1"] == (40, 25.0, "open")
    assert orders["KO-sell-1"] == (40, None, "open")
    assert orders["KO-buy-2"] == (12, 1.25, "open")

    data = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])
    assert data == {"auto_buy_cap": 20.0, "cost": 25.0, "note": "keep"}

    cap = json.loads(p.read_text(encoding="utf-8"))
    assert cap["oe"] == 1.0 and cap["norm_eps"] == 1.0 and cap["iv_cons"] == 20.0 and cap["price"] == 25.0
    assert cap["insurance"] == {"tbv_ps": 2.0, "float_ps": 0.5} and cap["ticker"] == "KO"


def test_order_that_rounds_to_zero_shares_is_cancelled(env):
    hold(env.conn, "KO", 10, 10.0)
    order(env.conn, "KO-buy-z", "KO", "BUY", 3, 5.0)
    res = result(action_type="合股", ratio_new_per_old=0.1, fractional_handling="現金了結")
    ca.apply(env.conn, env.paths, res, event(pb=10.0, pa=100.0), ASOF)
    status, reason = env.conn.execute(
        "SELECT status, cancel_reason FROM orders WHERE order_id='KO-buy-z'").fetchone()
    assert status == "cancelled" and reason


def test_failed_transaction_rolls_back_everything(env):
    hold(env.conn, "KO", 10, 100.0)
    p = cap_file(env.paths, "KO", {"oe": 4.0})
    p.write_text("{not json", encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(json.JSONDecodeError):
        ca.apply(env.conn, env.paths, result(), event(), ASOF)
    assert pos(env.conn, "KO") == (10, 100.0)
    assert ca_rows(env.conn) == []
    assert total(env.conn, "corp_action_cash") == 0
    assert p.read_bytes() == before


# ---------------------------------------------------------------- 入帳：股利、代號變更、防重複

def test_cash_dividend_receivable_then_paid_once(env):
    hold(env.conn, "KO", 100, 50.0)
    res = result(action_type="現金股利", ratio_new_per_old=None, cash_per_share=0.5, pay_date="2026-11-01")
    assert ca.verify(res, event(pb=None, pa=None), ca.position_of(env.conn, "KO")) == []
    ca.apply(env.conn, env.paths, res, event(pb=None, pa=None), ASOF)
    assert total(env.conn, "dividend_receivable") == pytest.approx(50.0)
    assert total(env.conn, "dividend") == 0

    assert ca.settle_dividends(env.conn, "2026-10-31") == []
    paid = ca.settle_dividends(env.conn, "2026-11-01")
    assert paid == [{"ticker": "KO", "effective_date": ASOF, "pay_date": "2026-11-01", "amount": 50.0}]
    assert total(env.conn, "dividend") == pytest.approx(50.0)
    assert total(env.conn, "dividend_receivable") == pytest.approx(0.0)
    assert ca.settle_dividends(env.conn, "2026-11-02") == []


def test_cash_dividend_without_pay_date_uses_inferred_date(env):
    """M2：缺發放日 → 除息日後 DEFAULT_PAY_LAG_DAYS 天入現金，並標「推論」。"""
    hold(env.conn, "KO", 100, 50.0)
    res = result(action_type="現金股利", ratio_new_per_old=None, cash_per_share=0.5)
    out = ca.apply(env.conn, env.paths, res, event(pb=None, pa=None), ASOF)
    assert out["status"] == "applied"
    data = json.loads(env.conn.execute("SELECT data FROM corporate_actions WHERE action_type='現金股利'").fetchone()[0])
    inferred = (date.fromisoformat(ASOF) + timedelta(days=ca.DEFAULT_PAY_LAG_DAYS)).isoformat()
    assert data["pay_date"] == inferred and data["pay_date_inferred"] is True

    assert ca.settle_dividends(env.conn, (date.fromisoformat(inferred) - timedelta(days=1)).isoformat()) == []
    paid = ca.settle_dividends(env.conn, inferred)
    assert paid[0]["pay_date_inferred"] is True and paid[0]["amount"] == 50.0
    assert total(env.conn, "dividend") == pytest.approx(50.0)
    assert total(env.conn, "dividend_receivable") == pytest.approx(0.0)


def test_legacy_dividend_row_without_pay_date_is_settled(env):
    """M2：修正前入帳、data 沒有 pay_date 的舊列也會在推論日期入現金。"""
    hold(env.conn, "KO", 100, 50.0)
    ca._cash(env.conn, ASOF, 50.0, "dividend_receivable", "CA:KO:現金股利:" + ASOF, "除息應收")
    ca._ca_insert(env.conn, ticker="KO", atype="現金股利", eff=ASOF, ratio=None, cash=0.5, new_ticker=None,
                  status="applied", source="x", data={"amount": 50.0, "paid": False, "pay_date": None,
                                                      "ref": "CA:KO:現金股利:" + ASOF})
    env.conn.commit()
    late = (date.fromisoformat(ASOF) + timedelta(days=ca.DEFAULT_PAY_LAG_DAYS)).isoformat()
    assert len(ca.settle_dividends(env.conn, late)) == 1
    assert total(env.conn, "dividend") == pytest.approx(50.0)


def test_ticker_change_moves_position_order_registry_and_cap(env):
    hold(env.conn, "KO", 10, 50.0)
    order(env.conn, "KO-buy-1", "KO", "BUY", 5, 40.0)
    registry(env.conn, "KO", "HOLDING", {"cost": 50.0})
    cap_file(env.paths, "KO", {"oe": 4.0})
    res = result(action_type="代號變更", ratio_new_per_old=None, new_ticker="KOX")
    out = ca.apply(env.conn, env.paths, res, event(pb=None, pa=None), ASOF)
    assert out["status"] == "applied"
    assert pos(env.conn, "KO") is None and pos(env.conn, "KOX") == (10, 50.0)
    assert env.conn.execute("SELECT ticker FROM orders WHERE order_id='KO-buy-1'").fetchone()[0] == "KOX"
    assert env.conn.execute("SELECT ticker FROM registry").fetchall()[0][0] == "KOX"
    assert not (env.paths.state / "cap_inputs" / "KO.json").exists()
    assert (env.paths.state / "cap_inputs" / "KOX.json").is_file()


def test_ticker_change_to_existing_ticker_is_conflict_without_writes(env):
    hold(env.conn, "KO", 10, 50.0)
    registry(env.conn, "KOX", "WATCH", {})
    res = result(action_type="代號變更", ratio_new_per_old=None, new_ticker="KOX")
    out = ca.apply(env.conn, env.paths, res, event(pb=None, pa=None), ASOF)
    assert out["status"] == "conflict"
    assert pos(env.conn, "KO") == (10, 50.0) and ca_rows(env.conn) == []


def test_same_effective_date_is_not_applied_twice(env):
    hold(env.conn, "KO", 10, 100.0)
    ca.apply(env.conn, env.paths, result(), event(), ASOF)
    out = ca.apply(env.conn, env.paths, result(), event(), ASOF)
    assert out["status"] == "duplicate"
    assert pos(env.conn, "KO") == (40, 25.0)


def test_spin_off_books_nothing_pending_human(env):
    hold(env.conn, "KO", 10, 100.0)
    res = result(action_type="分拆", ratio_new_per_old=None, cash_per_share=0.25, new_ticker="SPC")
    errs = ca.verify(res, event(), ca.position_of(env.conn, "KO"))
    assert errs and errs[0].startswith("需要人工")
    ca.reject(env.conn, res, event(), errs)
    assert pos(env.conn, "KO") == (10, 100.0)
    assert total(env.conn, "corp_action_cash") == 0          # 需人工：不自動入帳，避免與未調整的股數重複計價
    row = env.conn.execute("SELECT status FROM corporate_actions WHERE ticker='KO'").fetchone()
    assert row[0] == "pending_human"
    pause = env.conn.execute("SELECT pause_trading, status FROM anomalies WHERE ticker='KO'").fetchone()
    assert (pause[0], pause[1]) == (1, "open")


def test_reject_without_cash_books_nothing_and_pauses(env):
    hold(env.conn, "KO", 10, 100.0)
    res = result(ratio_new_per_old=3)
    errs = ca.verify(res, event(alp=ALPACA_SPLIT), ca.position_of(env.conn, "KO"))
    ca.reject(env.conn, res, event(alp=ALPACA_SPLIT), errs)
    assert pos(env.conn, "KO") == (10, 100.0)
    assert total(env.conn, "corp_action_cash") == 0
    assert env.conn.execute("SELECT status FROM corporate_actions").fetchone()[0] == "anomaly"
    kind, pause = env.conn.execute("SELECT kind, pause_trading FROM anomalies").fetchone()
    assert (kind, pause) == ("corp_action_check_failed", 1)


# ---------------------------------------------------------------- 偵測

BARS_PREV = {"KO": {"close": 100.0}, "XYZ": {"close": 100.0}, "AAA": {"close": 50.0}}
BARS_TODAY = {"KO": {"close": 25.0}, "XYZ": {"close": 50.0}, "AAA": {"close": 51.0}}


def test_detect_alpaca_event_within_window(env):
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [ALPACA_SPLIT], {}, BARS_PREV, BARS_TODAY)
    assert len(evs) == 1
    e = evs[0]
    assert e["source"] == "alpaca" and e["event_id"] == "KO:alpaca:2026-10-16:a1"
    assert (e["price_before"], e["price_after"], e["date_before"], e["date_after"]) == (100.0, 25.0, "2026-10-15", ASOF)


def test_detect_ignores_ex_date_outside_window(env):
    old = dict(ALPACA_SPLIT, ex_date="2026-10-15")  # 不在 (prev, date] 內，不算 Alpaca 事件
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [old], {}, BARS_PREV, BARS_TODAY)
    assert [e["source"] for e in evs] == ["gap"]  # 價格跳空仍會被偵測到


def test_detect_8k_items(env):
    f = {"KO": [{"form": "8-K", "filingDate": ASOF, "items": "5.02,8.01", "accessionNumber": "0001-26-1", "url": "u"},
                {"form": "8-K", "filingDate": ASOF, "items": "2.02,9.01", "accessionNumber": "0001-26-2"}]}
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [], f, BARS_PREV, BARS_TODAY)
    assert [(e["source"], e["event_id"], e["url"]) for e in evs] == [("8k", "KO:8k:2026-10-16:0001-26-1", "u")]


def test_detect_price_gap_without_known_event(env):
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["XYZ", "AAA"], [], {}, BARS_PREV, BARS_TODAY)
    assert [(e["ticker"], e["source"]) for e in evs] == [("XYZ", "gap")]
    assert evs[0]["event_id"] == "XYZ:gap:2026-10-16:1"


def test_detect_gap_suppressed_when_known_event_exists(env):
    bars_t = {"KO": {"close": 40.0}}  # -60%，但已有 Alpaca 事件 → 不另建跳空事件
    evs = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [ALPACA_SPLIT], {}, BARS_PREV, bars_t)
    assert [e["source"] for e in evs] == ["alpaca"]


def test_detect_skips_already_registered_action(env):
    hold(env.conn, "KO", 10, 100.0)
    ev = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [ALPACA_SPLIT], {}, BARS_PREV, BARS_TODAY)
    ca.apply(env.conn, env.paths, result(), ev[0], ASOF)
    assert ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [ALPACA_SPLIT], {}, BARS_PREV, BARS_TODAY) == []


def test_same_day_split_and_dividend_both_kept(env):
    """去重鍵含類型：同日拆股已入帳，同日的股利事件仍要偵測、入帳。"""
    hold(env.conn, "KO", 10, 100.0)
    div = {**ALPACA_SPLIT, "type": "cash_dividend", "ratio_new_per_old": None, "cash_per_share": 0.5,
           "raw": {"id": "a2"}}
    ev = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [ALPACA_SPLIT, div], {}, BARS_PREV, BARS_TODAY)
    split_ev = next(e for e in ev if e["event_id"].endswith(":a1"))
    ca.apply(env.conn, env.paths, result(), split_ev, ASOF)
    left = ca.detect(env.conn, ASOF, "2026-10-15", ["KO"], [ALPACA_SPLIT, div], {}, BARS_PREV, BARS_TODAY)
    assert [e["event_id"] for e in left] == ["KO:alpaca:" + ASOF + ":a2"]
    assert ca._ca_exists(env.conn, "KO", ASOF, "拆股") and not ca._ca_exists(env.conn, "KO", ASOF, "現金股利")


# ---------------------------------------------------------------- 處理器與整條流程

def test_handler_registered_under_same_key():
    assert isinstance(HANDLERS["CORP_ACTION"], CorpActionHandler)


def test_prompt_values_come_from_ledger(env):
    hold(env.conn, "KO", 10, 100.0)
    order(env.conn, "KO-buy-1", "KO", "BUY", 5, 40.0)
    job = JobSpec(job_id="r1:KO:CORP_ACTION:1", run_id="r1", job_type="CORP_ACTION", ticker="KO", asof=ASOF,
                  params={"event": event(alp=ALPACA_SPLIT)})
    ctx = SimpleNamespace(conn=env.conn, paths=env.paths, work_dir=env.paths.work)
    vals = CorpActionHandler().prompt_values(job, ctx, "fake")
    assert vals["shares"] == "10" and vals["cost"] == "100.0000"
    assert vals["open_orders"] == "BUY 5 股 限價 40.0"
    assert vals["price_before"] == "100.0" and vals["date_after"] == ASOF and vals["event_text"] == "原文"


ASOF_ENGINE = ASOF
RUN = "r1"


def _engine(tmp_path, responses):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "models:\n"
        "  strong: {backend: fake, model: fake, concurrency: 2}\n"
        "  medium: {backend: fake, model: fake, concurrency: 2}\n"
        "  weak: {backend: fake, model: fake, concurrency: 2}\n"
        "budget:\n  max_llm_calls_per_run: 50\n  max_input_tokens_per_run: 20000000\n",
        encoding="utf-8")
    config = load_config(cfg, env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    router = Router(config, paths.routing_json, dry_run=True, fake=FakeRunner(responses=responses))
    engine = Engine(conn, router, paths, config, sleep=lambda s: None)
    engine.start_run(RUN, "daily", ASOF)
    return SimpleNamespace(conn=conn, paths=paths, engine=engine)


def _add_ca_job(e, ev):
    spec = JobSpec(job_id=f"{RUN}:KO:CORP_ACTION:1", run_id=RUN, job_type="CORP_ACTION", ticker="KO",
                   asof=ASOF, deps=[], params={"event": ev})
    assert e.engine.add_job(spec)


def test_engine_flow_applies_verified_split(tmp_path):
    e = _engine(tmp_path, {"CORP_ACTION": result()})
    hold(e.conn, "KO", 10, 100.0)
    _add_ca_job(e, event(alp=ALPACA_SPLIT))
    e.engine.run(RUN)
    st = e.conn.execute("SELECT status FROM jobs WHERE job_type='CORP_ACTION'").fetchone()[0]
    assert st == "done"
    assert pos(e.conn, "KO") == (40, 25.0)
    assert (e.paths.work / RUN / "KO" / "corp_action.json").is_file()
    assert e.conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == 0


def test_engine_flow_rejects_mismatch_and_pauses(tmp_path):
    e = _engine(tmp_path, {"CORP_ACTION": result(ratio_new_per_old=3)})
    hold(e.conn, "KO", 10, 100.0)
    _add_ca_job(e, event(alp=ALPACA_SPLIT))
    e.engine.run(RUN)
    assert pos(e.conn, "KO") == (10, 100.0)
    pause = e.conn.execute("SELECT pause_trading, status FROM anomalies WHERE ticker='KO'").fetchone()
    assert (pause[0], pause[1]) == (1, "open")
    assert e.conn.execute("SELECT status FROM corporate_actions").fetchone()[0] == "anomaly"


# ---------------------------------------------------------------- 無公司行動（8-K 一般公告）

def test_none_type_from_8k_is_no_op(env):
    hold(env.conn, "KO", 100, 50.0)
    ev = event(source="8k", pb=50.0, pa=51.0)
    r = result(action_type="無公司行動", ratio_new_per_old=None, needs_human=False)
    assert ca.verify(r, ev, ca.position_of(env.conn, "KO")) == []
    out = ca.apply(env.conn, env.paths, r, ev, ASOF)
    assert out["status"] == "no_action"
    assert ca_rows(env.conn) == [] and pos(env.conn, "KO") == (100, 50.0)
    assert "KO" not in __import__("trader.ledger.book", fromlist=["paused"]).paused(env.conn)


def test_none_type_from_alpaca_or_gap_is_rejected(env):
    for src in ("alpaca", "gap"):
        errs = ca.verify(result(action_type="無公司行動", ratio_new_per_old=None), event(source=src), None)
        assert errs and "不可判為無公司行動" in errs[0]


def test_none_type_with_numbers_is_contradiction(env):
    errs = ca.verify(result(action_type="無公司行動", ratio_new_per_old=2), event(source="8k"), None)
    assert any("自相矛盾" in e for e in errs)


def test_pre_script_attaches_8k_text(env):
    class Sec:
        def filing_text(self, url):
            return "Item 8.01 Other Events. The Board declared a quarterly dividend."

        def exhibit_text(self, cik, acc, typ):
            return "Press release body"
    ev = event(source="8k")
    ev["url"] = "https://www.sec.gov/Archives/edgar/data/21344/000002134426000010/ko-8k.htm"
    ev["text"] = "8-K 項目 8.01，申報日 2026-10-16"
    job = JobSpec(job_id="d:KO:CORP_ACTION:1", run_id="d", job_type="CORP_ACTION", ticker="KO", asof=ASOF,
                  params={"event": ev})
    ctx = SimpleNamespace(sec=Sec(), conn=env.conn, paths=env.paths)
    CorpActionHandler().pre_script(job, ctx)
    t = job.params["event"]["text"]
    assert "quarterly dividend" in t and "Press release body" in t
    CorpActionHandler().pre_script(job, ctx)                      # 重跑不重複附加
    assert job.params["event"]["text"].count("quarterly dividend") == 1


def test_registered_actions_listed_for_prompt():
    from trader.jobs.corp_jobs import _registered
    c = db.connect(":memory:")
    db.migrate(c)
    db.insert(c, "corporate_actions", {"ticker": "APH", "action_type": "拆股", "effective_date": "2026-09-03",
                                       "ratio_new_per_old": 2.0, "status": "applied", "source": "alpaca",
                                       "data": "{}", "created_at": db.now_iso()})
    db.insert(c, "corporate_actions", {"ticker": "APH", "action_type": "現金股利", "effective_date": "2025-01-03",
                                       "cash_per_share": 0.2, "status": "applied", "source": "alpaca",
                                       "data": "{}", "created_at": db.now_iso()})
    txt = _registered(c, "APH", "2026-09-04")
    assert "2026-09-03 拆股" in txt and "2025-01-03" not in txt
    assert _registered(c, "ZZZ", "2026-09-04") == "無"


def test_8k_only_split_uses_tighter_price_ratio_tolerance(env):
    hold(env.conn, "KO", 10, 100.0)
    pos = ca.position_of(env.conn, "KO")
    # 比例 4、價格比 100/27＝3.70（差 7.4%）：8-K 只靠價格比 → 8% 內通過；差 10% → 退回
    assert not any("價格比" in e for e in ca.verify(result(), event(source="8k", pa=27.0), pos))
    assert any("價格比" in e for e in ca.verify(result(), event(source="8k", pa=27.8), pos))
    # 有 Alpaca 比例交叉核對時維持寬容差
    assert not any("價格比" in e for e in ca.verify(result(), event(alp=ALPACA_SPLIT, pa=27.8), pos))
