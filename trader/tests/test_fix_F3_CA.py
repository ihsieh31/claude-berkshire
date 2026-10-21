"""F3-CA 修復測試（公司行動 detect／apply）：A3-M7E-01、A3-M7A-07、A3-M7C-03、A3-M7B-04、A3-M7C-07、A3-M7C-08。"""
import json

import pytest

from test_corp_actions import (ALPACA_SPLIT, ASOF, RUN, _engine, ca_rows, cap_file, env, event, hold, order,  # noqa: F401
                               pos, registry, result)
from trader import db
from trader.ledger import corp_actions as ca
from trader.models import JobSpec

PREV = "2026-10-15"


def _bars(pb, pa, T="KO"):
    return {T: {"close": pb}}, {T: {"close": pa}}


# A3-M7E-01：中斷重跑時孤兒工作與重派工作同 run 都跑；第二次模型生效日不同也不得重複拆股
@pytest.mark.parametrize("second_eff", [PREV, ASOF])
def test_m7e01_same_event_applied_once_even_if_model_eff_differs(tmp_path, second_eff):
    def resp(job):
        return result(effective_date=ASOF if job.job_id.endswith(":1") else second_eff)
    e = _engine(tmp_path, {"CORP_ACTION": resp})
    hold(e.conn, "KO", 10, 100.0)
    bp, ba = _bars(100.0, 25.0)
    ev = ca.detect(e.conn, ASOF, PREV, ["KO"], [ALPACA_SPLIT], {}, bp, ba)[0]
    e.engine.add_job(JobSpec(job_id=f"{RUN}:KO:CORP_ACTION:1", run_id=RUN, job_type="CORP_ACTION", ticker="KO",
                             asof=ASOF, deps=[], params={"event": ev}))
    e.engine.add_job(JobSpec(job_id=f"{RUN}:KO:CORP_ACTION:1r2", run_id=RUN, job_type="CORP_ACTION", ticker="KO",
                             asof=ASOF, deps=[], params={"event": dict(ev)}))
    e.engine.run(RUN)
    assert pos(e.conn, "KO") == (40, 25.0)
    assert [r["status"] for r in ca_rows(e.conn)] == ["applied"]


def test_m7e01_apply_dedupes_by_event_id(env):
    hold(env.conn, "KO", 10, 100.0)
    assert ca.apply(env.conn, env.paths, result(), event(), ASOF)["status"] == "applied"
    out = ca.apply(env.conn, env.paths, result(effective_date=PREV), event(), ASOF)
    assert out["status"] == "duplicate" and pos(env.conn, "KO") == (40, 25.0) and len(ca_rows(env.conn)) == 1


# A3-M7A-07：8-K 拒收列生效日≠申報日 → 不重派、不重插異常
def test_m7a07_8k_rejected_not_redispatched(env):
    c = env.conn
    hold(c, "KO", 10, 100.0)
    f8 = {"KO": [{"filingDate": ASOF, "form": "8-K", "items": "5.03", "accessionNumber": "acc1"}]}
    bp, ba = _bars(100.0, 99.0)
    ev = ca.detect(c, ASOF, PREV, ["KO"], [], f8, bp, ba)
    assert len(ev) == 1
    ca.reject(c, result(action_type="其他", effective_date="2026-11-02", needs_human=True), ev[0], ["需要人工"])
    assert ca.detect(c, ASOF, PREV, ["KO"], [], f8, bp, ba) == []
    assert c.execute("SELECT COUNT(*) FROM anomalies WHERE ticker='KO' AND status='open'").fetchone()[0] == 1


def test_m7a07_8k_no_action_not_redispatched(tmp_path):
    e = _engine(tmp_path, {"CORP_ACTION": lambda job: result(action_type="無公司行動", ratio_new_per_old=None)})
    hold(e.conn, "KO", 10, 100.0)
    f8 = {"KO": [{"filingDate": ASOF, "form": "8-K", "items": "8.01", "accessionNumber": "acc9"}]}
    bp, ba = _bars(100.0, 99.0)
    ev = ca.detect(e.conn, ASOF, PREV, ["KO"], [], f8, bp, ba)
    assert len(ev) == 1
    e.engine.add_job(JobSpec(job_id=f"{RUN}:KO:CORP_ACTION:1", run_id=RUN, job_type="CORP_ACTION", ticker="KO",
                             asof=ASOF, deps=[], params={"event": ev[0]}))
    e.engine.run(RUN)
    assert e.conn.execute("SELECT status FROM jobs").fetchone()[0] == "done" and ca_rows(e.conn) == []
    assert ca.detect(e.conn, ASOF, PREV, ["KO"], [], f8, bp, ba) == []


# A3-M7C-03：8-K 窗口放寬（since_8k < prev）時 date_before 仍是前一交易日（與 price_before 同日）
def test_m7c03_widened_8k_window_keeps_prev_date_before(env):
    f8 = {"KO": [{"filingDate": "2026-10-13", "form": "8-K", "items": "3.03", "accessionNumber": "acc2"}]}
    bp, ba = _bars(100.0, 99.0)
    ev = ca.detect(env.conn, ASOF, PREV, ["KO"], [], f8, bp, ba, since_8k="2026-10-10")
    assert len(ev) == 1 and ev[0]["date"] == "2026-10-13"
    assert ev[0]["date_before"] == PREV and ev[0]["price_before"] == 100.0


# A3-M7B-04：代號變更連同未結同意、未結工作與 cap_input_file 一起改
def test_m7b04_ticker_change_moves_approvals_jobs_and_cap_input_file(env):
    c = env.conn
    hold(c, "KO", 10, 50.0)
    registry(c, "KO", "HOLDING", {"cost": 50.0, "cap_input_file": "cap_inputs/KO.json"})
    cap_file(env.paths, "KO", {"oe": 4.0})
    now = db.now_iso()
    for run, st in (("ra", "pending"), ("rb", "done")):
        c.execute("INSERT INTO approvals(ticker, run_id, asof, job_id, job_type, status, created_at) "
                  "VALUES ('KO', ?, ?, ?, 'FULL_A', ?, ?)", (run, ASOF, f"{run}:KO:FULL_A", st, now))
    for jid, st in (("j-pend", "pending"), ("j-done", "done")):
        c.execute("INSERT INTO jobs(job_id, run_id, job_type, ticker, asof, status, created_at) "
                  "VALUES (?, 'r', 'FULL_A', 'KO', ?, ?, ?)", (jid, ASOF, st, now))
    c.commit()
    res = result(action_type="代號變更", ratio_new_per_old=None, new_ticker="KOX")
    alp = {"ticker": "KO", "type": "name_change", "ex_date": ASOF, "ratio_new_per_old": None, "cash_per_share": None,
           "new_ticker": "KOX", "raw": {"id": "n1"}, "payable_date": None}
    assert ca.apply(c, env.paths, res, event(pb=None, pa=None, alp=alp), ASOF)["status"] == "applied"
    assert dict(c.execute("SELECT run_id, ticker FROM approvals").fetchall()) == {"ra": "KOX", "rb": "KO"}
    assert dict(c.execute("SELECT job_id, ticker FROM jobs").fetchall()) == {"j-pend": "KOX", "j-done": "KO"}
    data = json.loads(c.execute("SELECT data FROM registry WHERE ticker='KOX'").fetchone()[0])
    assert data["cap_input_file"] == "cap_inputs/KOX.json"
    assert (env.paths.state / data["cap_input_file"]).is_file()


# A3-M7C-07：現金股利發放日以 Alpaca 為準，模型不同時另記
def test_m7c07_alpaca_pay_date_wins(env):
    c = env.conn
    hold(c, "KO", 10, 100.0)
    alp = {"ticker": "KO", "type": "cash_dividend", "ex_date": ASOF, "ratio_new_per_old": None,
           "cash_per_share": 0.5, "new_ticker": None, "raw": {"id": "d1"}, "payable_date": "2026-12-01"}
    res = result(action_type="現金股利", ratio_new_per_old=None, cash_per_share=0.5, pay_date="2026-10-17")
    ev = event(pb=100.0, pa=99.5, alp=alp)
    assert ca.verify(res, ev, {"qty": 10, "avg_cost": 100.0}) == []
    assert ca.apply(c, env.paths, res, ev, ASOF)["status"] == "applied"
    assert ca.settle_dividends(c, "2026-10-19") == []
    data = json.loads(c.execute("SELECT data FROM corporate_actions").fetchone()[0])
    assert data["pay_date"] == "2026-12-01" and data["pay_date_model"] == "2026-10-17"
    assert [p["pay_date"] for p in ca.settle_dividends(c, "2026-12-01")] == ["2026-12-01"]


# A3-M7C-08：0 股準時拆股，前一日規劃、created_date＝今日的冷靜 BUY 照比例換算
def test_m7c08_zero_position_on_time_split_rescales_cooling_buy(env):
    c = env.conn
    registry(c, "KO", "WATCH", {"auto_buy_cap": 120.0})
    c.execute("INSERT INTO orders(order_id, ticker, side, qty, limit_price, status, decision_id, created_date, "
              "updated_at) VALUES ('KO-BUY-1','KO','BUY',10,100.0,'open','d1',?, ?)", (ASOF, db.now_iso()))
    c.commit()
    split2 = {**ALPACA_SPLIT, "ratio_new_per_old": 2.0}
    r = ca.apply(c, env.paths, result(ratio_new_per_old=2), event(pb=100.0, pa=50.0, alp=split2), ASOF)
    o = dict(c.execute("SELECT qty, limit_price FROM orders WHERE order_id='KO-BUY-1'").fetchone())
    cap = json.loads(c.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])["auto_buy_cap"]
    assert r["status"] == "applied" and o == {"qty": 20, "limit_price": 50.0} and cap == 60.0
