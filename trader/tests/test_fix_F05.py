"""F05（M6 B1）：回寫的閘門與登錄表白名單。

M6_D7_02／M6-D10-02／M6-D6-03／M6-D8-04／M3_D7-03／M5_D7_01／M5_D7_03：三閘門以框架判定寫回日誌。
M6_D7_01／M6-D6-04：完整分析停止時三閘門寫未過。M6-D1-01／M6-D6-06／M6-D8-03／M3-D10-02：登錄表白名單。
M6-D1-03：schema 包裝形狀拆包。M6_D7_04／M6-D6-05／M3_D7-04：訊號卡等級對帳。M6-D10-01／M6-D5-04／M8-D2-12：目標權重框架值。
M6-D5-02：日誌來源欄。M6-D10-05：review_type。M6-D9-01：未成交買單。M6-D9-05：停止帶入 P-31。
M6-D6-07／M7-D10-01：舊資料日不覆寫。M6-D4-05：建倉計畫目標。設計決策 D01／D03 的回寫部分、D30 hold_flags。
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from trader import db
from trader.analysis import writeback as W
from trader.analysis.checks import run_cap_calc
from trader.ledger import book
from trader.paths import get_paths
from trader.pipelines.review import export_registry
from trader.portfolio import sizing as S

import test_engine as TE

ASOF = "2026-10-16"
GATES_OK = {"gate1": True, "gate2": True, "gate3": True, "gate3_failures": [], "gate2_failures": [],
            "questionnaire_version": "q-v1", "gate3_answers_file": "work/r/AAA/gate3_answers.json",
            "screen_decision": "PASS", "flag_codes": [], "distress_codes": [], "price_date": ASOF,
            "time_zone": "America/New_York"}
CIN = {"oe": 5.0, "g10": 0.07, "g5": 0.06, "grade": "B", "long_rate": 0.047, "long_rate_20y_med": 0.035,
       "price": 60.0, "norm_eps": 5.2, "largecap_pe_median": 19.0, "financial": False, "incr_roic_gt_rd": True,
       "roe": 0.22, "eps_cagr5": 0.06}


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100000.0, "2026-10-01")
    yield conn, paths
    conn.close()


def _cap(tmp_path, cin=CIN):
    f = tmp_path / "cin_for_cap.json"
    f.write_text(json.dumps(cin), encoding="utf-8")
    return run_cap_calc(f)


def _td(tmp_path, name="td", card=None, entry=None, log=None, cin=None, cap=None, phase_a=None):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    base_card = {"signal": "BUY", "reason_code": "PASS_ALL", "certainty_grade": "B", "auto_buy_cap": 60.0,
                 "target_weight_pct": 10, "value_per_share": {"conservative": 50, "base": 60},
                 "position_plan": {"first_tranche_pct_of_target": 50}}
    for n, o in (("signal_card.json", card or base_card), ("registry_entry.json", entry or {}),
                 ("decision_log.json", log or {"action": "BUY"})):
        (d / n).write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text("論點", encoding="utf-8")
    if cin is not None:
        (d / "cap_input.json").write_text(json.dumps(cin), encoding="utf-8")
    if cap is not None:
        (d / "cap_result.json").write_text(json.dumps(cap), encoding="utf-8")
    if phase_a is not None:
        (d / "phaseA.json").write_text(json.dumps(phase_a, ensure_ascii=False), encoding="utf-8")
    return d


def _log(conn, did):
    return json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])


def _reg(conn, t):
    r = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return r["status"], json.loads(r["data"])


def _card(conn, did):
    return json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()["data"])


def _order(conn, oid, t="AAA", side="BUY"):
    db.insert(conn, "orders", {"order_id": oid, "ticker": t, "side": side, "qty": 10, "limit_price": 50.0,
                               "status": "open", "decision_id": None, "created_date": "2026-10-10",
                               "cancel_reason": None, "updated_at": db.now_iso()})


# ---------------------------------------------------------------- M6_D7_02：閘門以框架判定寫回

@pytest.mark.parametrize("model_gates", [
    {},
    {"gate1_not_deleted": "PASS", "gate2_price_below_cap": "是", "gate3_pass": "通過"},
    {"gate1_not_deleted": "true | false", "gate2_price_below_cap": "true | false", "gate3_pass": "true | false"},
])
def test_framework_gates_override_model_log_and_buy_goes_through(env, tmp_path, model_gates):
    conn, paths = env
    td = _td(tmp_path, log={"action": "BUY", **model_gates})
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "BUY", [], gates=GATES_OK)
    log = _log(conn, did)
    assert (log["gate1_not_deleted"], log["gate2_price_below_cap"], log["gate3_pass"]) == (True, True, True)
    assert log["gate3_failures"] == []
    abg = _card(conn, did)["auto_buy_gates"]   # 訊號卡閘門欄與日誌一致（M5_D7_01）
    assert (abg["gate1_not_deleted"], abg["gate2_price_below_cap"], abg["gate3_questionnaire_pass"]) == (True, True, True)
    assert abg["gate3_failures"] == [] and abg["gate3_answers_file"] == GATES_OK["gate3_answers_file"]
    decs = S.load_decisions(conn, ASOF, since="2026-10-15")
    plans = S.plan_buys(conn, ASOF, decs, {"AAA": 50.0}, {"AAA": 1e9}, 10)
    assert plans[0]["qty"] > 0, plans


def test_model_gate3_true_but_framework_false_is_not_pass(env, tmp_path):
    """M5_D7_03：模型日誌 gate3_pass=true、框架 gate3 不過 → 日誌 False，PRICE 的 _gate3_status 回不通過。"""
    from trader.jobs.review_jobs import _gate3_status
    conn, paths = env
    td = _td(tmp_path, log={"action": "WATCH", "gate1_not_deleted": True, "gate2_price_below_cap": True,
                            "gate3_pass": True})
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [],
                              gates={**GATES_OK, "gate3": False, "gate3_failures": ["C3 未作答"]})
    log = _log(conn, did)
    assert log["gate3_pass"] is False and log["gate3_failures"] == ["C3 未作答"]
    assert _card(conn, did)["auto_buy_gates"]["gate3_questionnaire_pass"] is False
    assert any("gate3_pass 以框架判定為準" in n for n in log["framework_notes"])
    assert _gate3_status(conn, "AAA", "2026-10-20") == "不通過"


def test_full_dag_writes_framework_gates_and_source_fields(tmp_path):
    """端到端：模型日誌只有 action，框架寫入三閘門、問卷版本、作答檔、價格、價格日、時區、目標權重（M6-D5-02）。"""
    env = TE.make_env(tmp_path, TE.full_responses())
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    assert env.engine.run(TE.RUN)["status"] == "done"
    log = json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()["data"])
    assert log["action"] == "BUY"
    assert (log["gate1_not_deleted"], log["gate2_price_below_cap"], log["gate3_pass"]) == (True, True, True)
    assert log["questionnaire_version"] is not None
    assert (env.paths.state / log["gate3_answers_file"]).is_file()
    # 價格以價格檔 60.5 為準（P19：框架覆寫 cap_input.price；舊值 60.0 是模型抄寫的 CAP_INPUT）
    assert log["price"] == 60.5 and log["price_date"] is not None and log["time_zone"]
    assert log["grade"] == "B" and log["review_type"] is None
    for k in ("screen_decision", "flag_codes", "distress_codes", "target_weight"):
        assert k in log


# ---------------------------------------------------------------- M6_D7_01：停止日誌三閘門未過

def _seed_holding(conn, bp=None, data=None):
    db.insert(conn, "decisions", {"decision_id": "AAA-20261001-1", "ticker": "AAA", "decision_date": "2026-10-01",
                                  "signal": "BUY", "data": {"action": "BUY", "gate1_not_deleted": True,
                                                            "gate2_price_below_cap": True, "gate3_pass": True},
                                  "created_at": db.now_iso()})
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 100, "avg_cost": 50, "first_buy_date": "2026-10-02",
                                  "updated_at": db.now_iso()})
    d = {"grade": "B", "auto_buy_cap": 60.0, "first_buy_date": "2026-10-02",
         "build_plan": bp or {"target_weight": 0.10, "tranches_left": 2, "days_above_cap": 0}, **(data or {})}
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "HOLDING", "data": d, "updated_at": db.now_iso()},
              key=["ticker"])


def test_stop_result_blocks_tranches_and_price_gate3(env):
    from trader.jobs.review_jobs import _gate3_status
    conn, paths = env
    _seed_holding(conn)
    W.write_stop_result(conn, paths, "AAA", "2026-10-10", "r2", {"reason_code": "REJECT", "stopped_at_gate": "G3"})
    g = S._latest_gate_decision(conn, "AAA", "2026-10-12")
    assert g["decision_id"] == "AAA-20261010-1"
    assert g["log"]["gate2_price_below_cap"] is False and g["log"]["gate3_pass"] is False
    assert g["log"]["hold_flags"] == {"stopped_at_gate": "G3", "reason_code": "REJECT"}   # D30
    assert _gate3_status(conn, "AAA", "2026-10-12") == "不通過"
    plans, _ = S.plan_tranches(conn, "2026-10-12", {"AAA": 52.0}, {"AAA": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "最近閘門紀錄未全過" in plans[0]["reason"]
    _, data = _reg(conn, "AAA")
    assert data["auto_buy_cap"] == 60.0 and data["build_plan"]["tranches_left"] == 2   # 既有資料保留（閘門擋加碼）


# ---------------------------------------------------------------- M6-D1-01／M6-D1-03：登錄表白名單、拆包

def test_registry_whitelist_and_framework_values(env, tmp_path):
    sys.path.insert(0, str(get_paths().skill_deep / "scripts"))
    import review_due
    conn, paths = env
    cap = _cap(tmp_path)
    entry = {"kill_criteria_due": ["營業利益率連兩年 <15%", {"text": "毛利率 <30%", "due": 20270331}],
             "review_price": "70.4", "build_plan": {"target_weight": 0.5, "tranches_left": 3},
             "auto_buy_cap": 999.0, "target_weight": 0.5, "cost": 1.0, "schema": "x", "next_full_review_due": "2027-03-01"}
    td = _td(tmp_path, card={"signal": "WATCH", "reason_code": "PASS_ALL", "certainty_grade": "B"},
             entry=entry, cin=CIN, cap=cap)
    W.write_full_result(conn, paths, "KCS", "2026-10-12", "r1", td, gates=GATES_OK)
    _, data = _reg(conn, "KCS")
    assert data["kill_criteria_due"] == [{"text": "營業利益率連兩年 <15%", "due": None},
                                         {"text": "毛利率 <30%", "due": None}]
    assert data["review_price"] == 60.0 and data["t30_at_valuation"] == 0.047
    assert data["auto_buy_cap"] == cap["auto_buy_cap"]
    assert "build_plan" not in data and "cost" not in data and "schema" not in data
    assert data["next_full_review_due"] == "2027-03-01"
    e = export_registry(conn, "all")[0]
    review_due.check(e, [], None, "2026-10-16", 0.046)   # 不崩


def test_kill_criteria_prefer_phase_a_and_scrub_prices(env, tmp_path):
    conn, paths = env
    pa = {"thesis_draft": {"kill_criteria": [{"text": "市占跌破 20%。股價跌破 50 美元時再看", "due": "2027-06-30"}]}}
    td = _td(tmp_path, entry={"kill_criteria_due": [{"text": "模型版", "due": "2027-01-01"}]}, phase_a=pa)
    W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [], gates=GATES_OK)
    kc = _reg(conn, "AAA")[1]["kill_criteria_due"]
    assert kc[0]["due"] == "2027-06-30" and kc[0]["text"].startswith("市占跌破 20%") and "50 美元" not in kc[0]["text"]


def test_schema_wrapper_shapes_unwrapped(env, tmp_path):
    conn, paths = env
    entry = {"schema": "berkshire-deep-merged/registry-v1",
             "entries": [{"ticker": "AAA", "kill_criteria_due": [{"text": "毛利率 <30%", "due": "2027-03-31"}]}]}
    log = {"schema": "berkshire-deep-merged/decision-log-v1", "fields": {"action": "BUY", "reason_one_liner": "x"}}
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r4", _td(tmp_path, entry=entry, log=log), "BUY", [],
                              gates=GATES_OK)
    _, data = _reg(conn, "AAA")
    assert data["kill_criteria_due"] == [{"text": "毛利率 <30%", "due": "2027-03-31"}]
    assert "entries" not in data and "schema" not in data
    lg = _log(conn, did)
    assert "fields" not in lg and lg["reason_one_liner"] == "x" and lg["gate3_pass"] is True


def test_review_type_forced_null(env, tmp_path):
    conn, paths = env
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", _td(tmp_path, log={"action": "BUY", "review_type": "PRICE"}),
                              "BUY", [], gates=GATES_OK)
    assert _log(conn, did)["review_type"] is None


# ---------------------------------------------------------------- M6_D7_04／M6-D10-01：等級與目標權重

def test_card_grade_synced_and_target_weight_from_framework(env, tmp_path):
    conn, paths = env
    cin = {"oe": 5.0, "g10": 0.15, "g5": 0.02, "grade": "B", "long_rate": 0.045, "long_rate_20y_med": 0.035,
           "price": 60.0}
    cap = _cap(tmp_path, cin)
    card = {"signal": "BUY", "reason_code": "PASS_ALL", "certainty_grade": "A", "auto_buy_cap": cap["auto_buy_cap"],
            "value_per_share": {"conservative": cap["IV_cons"], "base": cap["IV_base"]},
            "target_weight_pct": 20, "max_weight_pct": {"grade": 15}, "position_plan": {"first_tranche_pct_of_target": 50}}
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", _td(tmp_path, card=card, entry={"target_weight": 0.3},
                                                                 cin=cin, cap=cap), "BUY", [], gates=GATES_OK)
    assert _card(conn, did)["certainty_grade"] == "B"
    _, data = _reg(conn, "AAA")
    assert data["grade"] == "B" and data["target_weight"] == pytest.approx(0.10)
    assert data["target_weight_basis"]["decision_id"] == did
    pl = S.plan_buys(conn, ASOF, S.load_decisions(conn, ASOF, since="2026-10-15"), {"AAA": 50.0}, {"AAA": 1e9}, 10)
    assert pl[0]["target_weight"] == pytest.approx(0.10)
    # review_due：權重 0.10 已達框架 T，不再觸發加碼
    sys.path.insert(0, str(get_paths().skill_deep / "scripts"))
    import review_due
    e = {**data, "status": "HOLDING", "weight": 0.10, "ticker": "AAA"}
    px = pd.DataFrame({"date": [date(2026, 10, 15), date(2026, 10, 16)], "close": [50.0, 50.0]})
    _, reasons, _ = review_due.check(e, [], px, ASOF, None)
    assert not any("未達目標" in str(r) for r in reasons)


# ---------------------------------------------------------------- M6-D9-01／M6-D9-05：未成交買單、停止帶入

def test_open_buy_kept_then_cancelled_on_stop(env):
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "auto_buy_cap": 60.0, "sell_review_above": 70.0, "thesis_file": "theses/AAA_2026-10-01.md",
        "build_plan": {"target_weight": 0.1, "tranches_left": 2}}, "updated_at": db.now_iso()}, key=["ticker"])
    _order(conn, "o1")
    W.write_stop_result(conn, paths, "AAA", ASOF, "r", {"reason_code": "REJECT", "stopped_at_gate": "G3"})
    o = conn.execute("SELECT status, cancel_reason FROM orders WHERE order_id='o1'").fetchone()
    assert o["status"] == "cancelled" and "非 BUY" in o["cancel_reason"]
    st, data = _reg(conn, "AAA")
    assert st == "REJECT" and data["build_plan"]["tranches_left"] == 0 and data["sell_review_above"] == 70.0


def test_open_buy_cancelled_on_full_watch_but_kept_on_buy(env, tmp_path):
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "build_plan": {"target_weight": 0.1, "tranches_left": 2}}, "updated_at": db.now_iso()}, key=["ticker"])
    _order(conn, "o1")
    W.write_full_result(conn, paths, "AAA", ASOF, "r", _td(tmp_path, "a"), "BUY", [], gates=GATES_OK)
    assert conn.execute("SELECT status FROM orders WHERE order_id='o1'").fetchone()[0] == "open"
    assert _reg(conn, "AAA")[1]["build_plan"]["tranches_left"] == 2
    W.write_full_result(conn, paths, "AAA", ASOF, "r", _td(tmp_path, "b"), "WATCH", [], gates=GATES_OK)
    assert conn.execute("SELECT status FROM orders WHERE order_id='o1'").fetchone()[0] == "cancelled"
    assert _reg(conn, "AAA")[1]["build_plan"]["tranches_left"] == 0


def test_stop_keeps_drivers_for_non_holding(env):
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "cik": "123", "asset_class": "非金融", "external_drivers": ["oil"], "external_drivers_determined_on": "2026-09-01",
        "external_drivers_basis": [{"driver": "oil"}], "corr_links": {"BBB": {"variables": ["oil"]}},
        "thesis_file": "theses/AAA_2026-09-01.md", "auto_buy_cap": 60.0}, "updated_at": db.now_iso()}, key=["ticker"])
    W.write_stop_result(conn, paths, "AAA", ASOF, "r", {"reason_code": "TOO_HARD", "stopped_at_gate": "G1"})
    _, data = _reg(conn, "AAA")
    assert data["external_drivers"] == ["oil"] and data["corr_links"] and data["cik"] == "123"
    assert data["thesis_file"] == "theses/AAA_2026-09-01.md" and data["auto_buy_cap"] is None


# ---------------------------------------------------------------- M6-D6-07：舊資料日不覆寫

def _decision(conn, did, day, review_type, job_id=None, extra=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": "AAA", "decision_date": day, "run_id": "r0",
                                  "job_id": job_id, "signal": "WATCH", "reason_code": None,
                                  "data": {"review_type": review_type, **(extra or {})}, "created_at": db.now_iso()})


def test_full_after_triage_and_thesis_still_writes_new_valuation(env, tmp_path):
    """M6-D6-07 返工：TRIAGE（touch_review）／THESIS 只推遲 last_review_date，之後才完成的完整分析（資料日較早）
    仍寫入新上限、等級、目標權重與 cap_inputs；不合格 PRICE 的佔位列（job_id 非空）也不擋。"""
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01", "auto_buy_cap": 30.0,
        "grade": "C", "target_weight": None, "review_price": 40.0}, "updated_at": db.now_iso()}, key=["ticker"])
    _decision(conn, "AAA-20260901-1", "2026-09-01", None)                      # 舊的完整分析（早於資料日）
    W.touch_review(conn, "AAA", "2026-10-16")                                   # TRIAGE 判不需深入
    W.write_thesis_result(conn, paths, "AAA", "2026-10-17", "r1", {"verdict": "完好", "reason": "x"},
                          {"price": 61.0, "price_date": "2026-10-17"})          # THESIS
    _decision(conn, "PRICE-job-1", "2026-10-17", "PRICE", job_id="PRICE-job-1", extra={"errors": ["壞"]})
    assert _reg(conn, "AAA")[1]["last_review_date"] == "2026-10-17"
    cap = _cap(tmp_path)
    did = W.write_full_result(conn, paths, "AAA", "2026-10-15", "r", _td(tmp_path, cin=CIN, cap=cap), "BUY", [],
                              gates=GATES_OK)
    _, data = _reg(conn, "AAA")
    card = _card(conn, did)
    assert data["auto_buy_cap"] == cap["auto_buy_cap"] == card["auto_buy_cap"] and data["auto_buy_cap"] != 30.0
    assert data["grade"] == card["certainty_grade"] == "B"
    assert data["target_weight"] == pytest.approx(0.10) and data["target_weight_basis"]["decision_id"] == did
    # review_price 保留較新 THESIS（10-17）的 61（F05R2）；其餘照新估值寫
    assert data["review_price"] == 61.0 and data["t30_at_valuation"] == CIN["long_rate"]
    assert json.loads((paths.state / "cap_inputs" / "AAA.json").read_text(encoding="utf-8")) == CIN
    assert data["last_review_date"] == "2026-10-17" and data["last_full_review_date"] == "2026-10-15"
    assert not any("未覆寫" in n for n in _log(conn, did).get("framework_notes") or [])


def _sold_out_row(conn, sold="2026-10-15"):
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "last_review_date": "2026-10-01", "auto_buy_cap": None, "t30_at_valuation": None, "build_plan": None,
        "sold_out_at": sold, "grade": "B"}, "updated_at": db.now_iso()}, key=["ticker"])


def test_full_result_not_later_than_sold_out_keeps_cap_empty_and_buy_as_watch(env, tmp_path):
    """check-F08R 併入：資料日不晚於賣光日的完整分析不回寫 auto_buy_cap／t30，BUY 當 WATCH、不建倉。"""
    conn, paths = env
    _sold_out_row(conn)
    did = W.write_full_result(conn, paths, "AAA", "2026-10-14", "r", _td(tmp_path, cin=CIN, cap=_cap(tmp_path)),
                              "BUY", [], gates=GATES_OK)
    status, data = _reg(conn, "AAA")
    assert data["auto_buy_cap"] is None and data["t30_at_valuation"] is None and data["sold_out_at"] == "2026-10-15"
    assert status == "WATCH"
    log = _log(conn, did)
    assert log["action"] == "WATCH" and _card(conn, did)["signal"] == "WATCH"
    assert conn.execute("SELECT signal FROM decisions WHERE decision_id=?", (did,)).fetchone()["signal"] == "WATCH"
    assert any("上限不回寫" in n and "需新的完整分析" in n for n in log["framework_notes"])
    decs = S.load_decisions(conn, "2026-10-16", since="2026-10-14")
    plans = [p for p in S.plan_buys(conn, "2026-10-16", decs, {"AAA": 30.0}, {"AAA": 1e9}, 10) if p["qty"] > 0]
    assert plans == []


def test_full_result_after_sold_out_writes_cap(env, tmp_path):
    conn, paths = env
    _sold_out_row(conn)
    cap = _cap(tmp_path)
    did = W.write_full_result(conn, paths, "AAA", "2026-10-16", "r", _td(tmp_path, cin=CIN, cap=cap), "BUY", [],
                              gates=GATES_OK)
    _, data = _reg(conn, "AAA")
    assert data["auto_buy_cap"] == cap["auto_buy_cap"] and data["t30_at_valuation"] == CIN["long_rate"]
    assert _log(conn, did)["action"] == "BUY"


def _price(conn, paths, asof, price):
    return W.write_price_result(conn, paths, "AAA", asof, "r", action="WATCH", gate1=True, gate2="不通過",
                                gate3="通過", cap={"auto_buy_cap": 40.0, "IV_base": 50.0}, price=price,
                                price_date=asof, t30=0.047, reason="x", notes=[])


def test_older_price_does_not_roll_back_review_date_or_price(env):
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "last_review_date": "2026-10-20", "review_price": 70.0}, "updated_at": db.now_iso()}, key=["ticker"])
    _price(conn, paths, "2026-10-15", 61.0)   # 10-20 只是 TRIAGE 推遲：review_price 照寫，複查日不往回
    _, data = _reg(conn, "AAA")
    assert data["last_review_date"] == "2026-10-20" and data["review_price"] == 61.0
    _price(conn, paths, "2026-10-21", 75.0)   # 寫過價格線的 PRICE（10-21）
    _price(conn, paths, "2026-10-18", 66.0)   # 較舊的 PRICE 不蓋回 review_price
    _, data = _reg(conn, "AAA")
    assert data["last_review_date"] == "2026-10-21" and data["review_price"] == 75.0


def test_older_price_after_newer_full_keeps_lines_and_t30(env, tmp_path):
    """F05R2（check-F05R E1）：較新的完整分析（10-20）之後才寫回資料日 10-18 的 PRICE → 上限、兩條賣出線、t30、
    review_price 都保留完整分析的值；複查日不往回改；日誌記原因。"""
    conn, paths = env
    W.write_full_result(conn, paths, "AAA", "2026-10-20", "r", _td(tmp_path, cin=CIN, cap=_cap(tmp_path)), "WATCH", [],
                        gates=GATES_OK)
    ks = ("auto_buy_cap", "sell_review_above", "absurd_price_line", "t30_at_valuation", "review_price",
          "last_review_date")
    before = {k: _reg(conn, "AAA")[1].get(k) for k in ks}
    assert before["auto_buy_cap"] != 40.0 and before["sell_review_above"] != 50.0
    did = W.write_price_result(conn, paths, "AAA", "2026-10-18", "r", action="WATCH", gate1=True, gate2="不通過",
                               gate3="通過", cap={"auto_buy_cap": 40.0, "IV_base": 50.0, "absurd_price_line": 999.0},
                               price=66.0, price_date="2026-10-18", t30=0.051, reason="x", notes=[])
    assert {k: _reg(conn, "AAA")[1].get(k) for k in ks} == before
    assert any("之後已有寫過價格線的決策" in n for n in _log(conn, did)["framework_notes"])


def test_older_price_after_split_keeps_per_share_fields(env):
    """F05R2（check-F05R E2）：拆股（10-16）已換算每股欄位後，才寫回資料日 10-15 的 PRICE（拆股前每股價算的 cap）
    → 每股欄位（上限、兩條賣出線、review_price）不蓋回，t30 照寫。"""
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "last_review_date": "2026-10-10", "auto_buy_cap": 30.0, "sell_review_above": 25.0, "absurd_price_line": 100.0,
        "review_price": 29.0, "t30_at_valuation": 0.04}, "updated_at": db.now_iso()}, key=["ticker"])
    db.insert(conn, "corporate_actions", {"ticker": "AAA", "action_type": "拆股", "effective_date": "2026-10-16",
                                          "ratio_new_per_old": 2.0, "status": "applied", "created_at": db.now_iso()})
    did = W.write_price_result(conn, paths, "AAA", "2026-10-15", "r", action="WATCH", gate1=True, gate2="不通過",
                               gate3="通過", cap={"auto_buy_cap": 40.0, "IV_base": 50.0, "absurd_price_line": 200.0},
                               price=61.0, price_date="2026-10-15", t30=0.047, reason="x", notes=[])
    d = _reg(conn, "AAA")[1]
    assert (d["auto_buy_cap"], d["sell_review_above"], d["absurd_price_line"], d["review_price"]) == \
        (30.0, 25.0, 100.0, 29.0)
    assert d["t30_at_valuation"] == 0.047 and d["last_review_date"] == "2026-10-15"
    assert any("每股欄位已換算" in n for n in _log(conn, did)["framework_notes"])


def test_older_thesis_and_triage_do_not_roll_back(env):
    """F05R2（check-F05R E4）：較舊資料日的 THESIS／TRIAGE 不把 last_review_date 往回改；THESIS 不蓋回較新的 review_price。"""
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "last_review_date": "2026-10-10", "review_price": 70.0}, "updated_at": db.now_iso()}, key=["ticker"])
    _price(conn, paths, "2026-10-20", 72.0)
    did = W.write_thesis_result(conn, paths, "AAA", "2026-10-15", "r", {"verdict": "完好", "reason": "x"},
                                {"price": 61.0, "price_date": "2026-10-15"})
    d = _reg(conn, "AAA")[1]
    assert d["last_review_date"] == "2026-10-20" and d["review_price"] == 72.0
    assert any("review_price 保留較新值" in n for n in _log(conn, did)["framework_notes"])
    W.touch_review(conn, "AAA", "2026-10-12")
    assert _reg(conn, "AAA")[1]["last_review_date"] == "2026-10-20"
    # 較新的帶價格 THESIS（10-22）之後，較舊的 PRICE（10-21）也不蓋回 review_price
    W.write_thesis_result(conn, paths, "AAA", "2026-10-22", "r", {"verdict": "完好", "reason": "x"},
                          {"price": 80.0, "price_date": "2026-10-22"})
    _price(conn, paths, "2026-10-21", 75.0)
    d = _reg(conn, "AAA")[1]
    assert d["review_price"] == 80.0 and d["last_review_date"] == "2026-10-22"


def test_older_hold_review_does_not_roll_back_review_dates(env):
    conn, paths = env
    _seed_ko(conn, "2024-01-02")
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "data": {
        "first_buy_date": "2024-01-02", "grade": "B", "last_review_date": "2026-10-20",
        "last_full_review_date": "2026-10-19"}, "updated_at": db.now_iso()}, key=["ticker"])
    W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _hold("HOLD", None), price=60.0, iv_base=60.0)
    d = _reg(conn, "KO")[1]
    assert d["last_review_date"] == "2026-10-20" and d["last_full_review_date"] == "2026-10-19"


def test_stale_full_result_keeps_newer_stop_status_and_reject_basis(env, tmp_path):
    """F05R2（check-F05R E3）：較新的停止結果（10-20，REJECT）之後才寫回資料日 10-15 的完整分析 → 狀態與
    reject_basis 保留較新值（不被改成 WATCH），上限仍空。"""
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "REJECT", "data": {
        "last_review_date": "2026-10-20", "auto_buy_cap": None, "grade": None, "reject_basis": "G3 不過"},
        "updated_at": db.now_iso()}, key=["ticker"])
    _decision(conn, "AAA-20261020-1", "2026-10-20", None, extra={"action": "NO_ACTION", "stopped_at_gate": "G3"})
    did = W.write_full_result(conn, paths, "AAA", "2026-10-15", "r", _td(tmp_path, cin=CIN, cap=_cap(tmp_path)),
                              "BUY", [], gates=GATES_OK)
    st, d = _reg(conn, "AAA")
    assert st == "REJECT" and d["status"] == "REJECT" and d["reject_basis"] == "G3 不過"
    assert d.get("auto_buy_cap") is None
    assert any("狀態保留較新決策" in n for n in _log(conn, did)["framework_notes"])


def test_full_result_after_only_newer_triage_writes_status(env, tmp_path):
    """護欄：資料日之後只有 TRIAGE（touch_review，不寫決策）→ 完整分析照常寫狀態。"""
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "REJECT", "data": {
        "last_review_date": "2026-09-01", "reject_basis": "舊"}, "updated_at": db.now_iso()}, key=["ticker"])
    W.touch_review(conn, "AAA", "2026-10-20")
    W.write_full_result(conn, paths, "AAA", "2026-10-15", "r", _td(tmp_path, cin=CIN, cap=_cap(tmp_path)),
                        "BUY", [], gates=GATES_OK)
    st, d = _reg(conn, "AAA")
    assert st == "WATCH" and d["status"] == "WATCH" and "reject_basis" not in d
    assert d["last_review_date"] == "2026-10-20"


def test_stale_full_result_keeps_newer_registry_state(env, tmp_path):
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "last_review_date": "2026-10-20", "auto_buy_cap": 50.0, "review_price": 55.0, "grade": "B"},
        "updated_at": db.now_iso()}, key=["ticker"])
    db.insert(conn, "signals", {"decision_id": "AAA-20261020-1", "ticker": "AAA", "signal": "WATCH",
                                "data": {"as_of": "2026-10-20", "signal": "WATCH"}, "created_at": db.now_iso()})
    _decision(conn, "AAA-20261020-1", "2026-10-20", "PRICE")   # 10-20 的 PRICE 寫過價格線
    capf = paths.state / "cap_inputs" / "AAA.json"
    capf.parent.mkdir(parents=True, exist_ok=True)
    capf.write_text('{"keep": 1}', encoding="utf-8")
    td = _td(tmp_path, cin=CIN, cap=_cap(tmp_path))
    did = W.write_full_result(conn, paths, "AAA", "2026-10-15", "r", td, "BUY", [], gates=GATES_OK)
    _, data = _reg(conn, "AAA")
    assert data["last_review_date"] == "2026-10-20" and data["auto_buy_cap"] == 50.0 and data["review_price"] == 55.0
    assert data["last_full_review_date"] == "2026-10-15"
    # A3-M6D-03（F3-OO）：只有較新的 PRICE（PRICE 不寫等級／T／cap_inputs）→ 價格線保留較新值，cap_inputs 照本次分析換上
    assert json.loads(capf.read_text(encoding="utf-8")) != {"keep": 1}
    assert data["cap_input_file"] == "cap_inputs/AAA.json"
    assert any("之後已有較新的 PRICE 決策 AAA-20261020-1" in n for n in _log(conn, did)["framework_notes"])
    assert S.latest_card(conn, "AAA")["as_of"] == "2026-10-20"
    assert W.latest_signal(conn, "AAA")["data"]["as_of"] == "2026-10-20"


def test_split_after_asof_keeps_per_share_fields(env, tmp_path):
    """M7-D10-01：拆股（資料日後）已換算的每股欄位與 cap_inputs 不被舊資料日蓋回；等級、目標權重照新結果寫。"""
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "last_review_date": "2026-10-10", "auto_buy_cap": 30.0, "sell_review_above": 35.0, "absurd_price_line": 200.0,
        "review_price": 29.0, "grade": "C", "target_weight": None, "t30_at_valuation": 0.04},
        "updated_at": db.now_iso()}, key=["ticker"])
    db.insert(conn, "corporate_actions", {"ticker": "AAA", "action_type": "拆股", "effective_date": "2026-10-16",
                                          "ratio_new_per_old": 2.0, "status": "applied", "created_at": db.now_iso()})
    did = W.write_full_result(conn, paths, "AAA", "2026-10-15", "r",
                              _td(tmp_path, cin=CIN, cap=_cap(tmp_path)), "BUY", [], gates=GATES_OK)
    _, data = _reg(conn, "AAA")
    assert (data["auto_buy_cap"], data["sell_review_above"], data["absurd_price_line"], data["review_price"]) == \
        (30.0, 35.0, 200.0, 29.0)
    assert data["grade"] == "B" and data["target_weight"] == pytest.approx(0.10)
    assert data["target_weight_basis"]["decision_id"] == did
    assert not (paths.state / "cap_inputs" / "AAA.json").exists()
    assert any("每股欄位已換算" in n for n in _log(conn, did)["framework_notes"])


# ---------------------------------------------------------------- M6-D4-05：持股建倉計畫

def test_holding_build_plan_target_capped_and_zeroed_when_not_buy(env, tmp_path):
    conn, paths = env
    _seed_holding(conn, bp={"target_weight": 0.15, "tranches_left": 2})
    W.write_full_result(conn, paths, "AAA", ASOF, "r", _td(tmp_path, "a"), "BUY", [], gates=GATES_OK)
    bp = _reg(conn, "AAA")[1]["build_plan"]
    assert bp["target_weight"] <= 0.10 and bp["tranches_left"] == 2
    W.write_full_result(conn, paths, "AAA", ASOF, "r", _td(tmp_path, "b"), "WATCH", [], gates=GATES_OK)
    assert _reg(conn, "AAA")[1]["build_plan"]["tranches_left"] == 0


# ---------------------------------------------------------------- review.export_registry 雙保險

def test_export_registry_drops_bad_fields(env):
    conn, _ = env
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "HOLDING", "data": {
        "kill_criteria_due": ["x", {"text": "y", "due": "2027-01-01"}, {"text": "z", "due": 20270101}],
        "review_price": "70.4", "t30_at_valuation": 0.04, "last_review_date": "昨天"}, "updated_at": db.now_iso()},
        key=["ticker"])
    notes: list[str] = []
    e = export_registry(conn, "all", notes)[0]
    assert e["kill_criteria_due"] == [{"text": "y", "due": "2027-01-01"}]
    assert "review_price" not in e and e["t30_at_valuation"] == 0.04 and "last_review_date" not in e
    assert len(notes) == 3


# ---------------------------------------------------------------- sizing：不再退回訊號卡閘門

def test_gate_failures_does_not_fall_back_to_card():
    card = {"auto_buy_gates": {"gate1_not_deleted": True, "gate2_price_below_cap": True,
                               "gate3_questionnaire_pass": True}}
    assert len(S.gate_failures({}, card)) == 3
    assert S.gate_failures({"gate1_not_deleted": True, "gate2_price_below_cap": "通過", "gate3_pass": True}) == []


# ---------------------------------------------------------------- 設計決策 D01／D03（回寫部分）

def _hold(action, cat, frac=None, path=None):
    return {"action": action, "sell_category": cat, "trim_fraction": frac, "stop_adding": False,
            "observe_until": None, "falsified_assumption": None, "path": path or [], "reason": "測試"}


Q3C = [{"q": "Q3", "answer": "是", "evidence": "V 成立"}, {"q": "Q3c", "answer": "是", "evidence": "頂級複利型"}]


def _seed_ko(conn, first, absurd=None):
    db.insert(conn, "positions", {"ticker": "KO", "qty": 100, "avg_cost": 50.0, "first_buy_date": first,
                                  "updated_at": db.now_iso()})
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "data": {
        "first_buy_date": first, "absurd_price_line": absurd, "grade": "B"}, "updated_at": db.now_iso()},
        key=["ticker"])


def test_first_year_valuation_trim_becomes_hold_and_stops_adding(env):
    conn, paths = env
    _seed_ko(conn, (date.fromisoformat(ASOF) - timedelta(days=100)).isoformat())
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _hold("TRIM", "估值", 0.5), price=80.0, iv_base=60.0)
    log = _log(conn, did)
    assert log["action"] == "HOLD" and log["stop_adding"] is True and log["trim_fraction"] is None
    assert S.plan_sells(conn, ASOF, S.load_decisions(conn, ASOF), {"KO": 80.0}) == [] or \
        all(p["qty"] == 0 for p in S.plan_sells(conn, ASOF, S.load_decisions(conn, ASOF), {"KO": 80.0}))
    assert S.add_blocked(conn, "KO", ASOF)


@pytest.mark.parametrize("price,frac,path,want,code,note", [
    (80.0, 0.5, Q3C, 0.25, "VALUATION", "3c 頂級複利型"),       # 3c：每次至多 1/4
    (300.0, 0.3, [], 0.5, "ABSURD", "荒謬級"),                  # 荒謬級：至少 1/2
    (300.0, 0.3, Q3C, 0.5, "ABSURD", "荒謬級"),                 # 3c 的荒謬級仍至少 1/2
])
def test_valuation_trim_fraction_clamped_and_recorded(env, price, frac, path, want, code, note):
    conn, paths = env
    _seed_ko(conn, "2024-01-02", absurd=260.0)
    import test_fix_P24
    test_fix_P24.seed_p08(conn)   # A3-M3D-02：頂級複利型須 P-08 品質優
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _hold("TRIM", "估值", frac, path),
                                     price=price, iv_base=60.0)
    log = _log(conn, did)
    assert log["trim_fraction"] == want
    assert any(note in n for n in log.get("framework_notes") or []), log.get("framework_notes")
    lt = _reg(conn, "KO")[1]["last_trim"]
    assert lt["reason_code"] == code and lt["date"] == ASOF and lt["shares"] == int(100 * want)


def test_ordinary_valuation_trim_without_g8_becomes_hold(env):
    """D03（F05R2）：普通／良好企業的估值 TRIM（非荒謬級、非 Q3c）數量須由 G8 算出；目前無 G8 數量來源 → HOLD＋停止加碼，
    不記 last_trim。"""
    conn, paths = env
    _seed_ko(conn, "2024-01-02", absurd=260.0)
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _hold("TRIM", "估值", 0.5), price=80.0, iv_base=60.0)
    log = _log(conn, did)
    assert log["action"] == "HOLD" and log["trim_fraction"] is None and log["stop_adding"] is True
    assert any("G8" in n and "HOLD" in n for n in log["framework_notes"])
    assert "last_trim" not in _reg(conn, "KO")[1]
    assert S.add_blocked(conn, "KO", ASOF)
