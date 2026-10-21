"""F3-OO：V3-2 G1-stop-D80、G2-out-of-order-D86、G3-fullb-downgrade、G4-holdings-view-G8 修復的回歸測試。全部離線。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from trader import db
from trader.analysis import writeback as W
from trader.analysis.writeback import (write_full_result, write_hold_review_result, write_price_result,
                                       write_stop_result, write_thesis_result)
from trader.jobs import engine as E
from trader.paths import get_paths
from trader.portfolio import holdings_view as HV
from trader.portfolio import sizing as Z

from test_daily_pipeline import world  # noqa: F401  fixture

ASOF = "2026-10-16"
NEWER = "2026-10-20"
OLD = "2026-10-01"
BP = {"tranches_left": 2, "target_weight": 0.1}


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield conn, paths
    conn.close()


def _reg_set(conn, t, status, data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"ticker": t, "status": status, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _reg(conn, t):
    r = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return r["status"], json.loads(r["data"])


def _decision(conn, t, d, did, data, signal="WATCH", reason="X", job_id=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": d, "run_id": "r0",
                                  "job_id": job_id, "signal": signal, "reason_code": reason,
                                  "data": {"decision_date": d, **data}, "created_at": db.now_iso()})


def _signal(conn, t, did, as_of, data, signal="WATCH"):
    db.insert(conn, "signals", {"decision_id": did, "ticker": t, "signal": signal,
                                "data": {"as_of": as_of, "ticker": t, "decision_log_id": did, **data},
                                "created_at": db.now_iso()})


def _open_buy(conn, t, oid="OO-o1", did=None):
    row = {"order_id": oid, "ticker": t, "side": "BUY", "status": "open", "limit_price": 50.0,
           "created_date": OLD, "decision_id": did}
    for c in conn.execute("PRAGMA table_info(orders)").fetchall():
        name, typ, notnull, dflt, pk = c[1], (c[2] or "").upper(), c[3], c[4], c[5]
        if name in row or not notnull or dflt is not None or pk:
            continue
        row[name] = db.now_iso() if name.endswith("_at") else (
            1 if any(x in typ for x in ("INT", "REAL", "NUM", "FLOA", "DOUB")) else "x")
    db.insert(conn, "orders", row)
    return oid


def _order_status(conn, oid):
    return conn.execute("SELECT status FROM orders WHERE order_id=?", (oid,)).fetchone()["status"]


def _log(conn, did):
    return json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])


def _card(conn, did):
    return json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()["data"])


def _tdir(tmp_path: Path, ticker="AAPL", signal="BUY", reason="PASS_ALL", grade="B", kill=None, name="td") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    card = {"schema": "berkshire-deep-merged/signal-v1", "ticker": ticker, "as_of": ASOF, "signal": signal,
            "reason_code": reason, "conclusion": {"BUY": "買", "WATCH": "觀察", "NO_ACTION": "不買"}[signal],
            "auto_buy_cap": 150.0, "certainty_grade": grade, "value_per_share": {"conservative": 120.0, "base": 160.0},
            "red_light": False, "decision_log_id": ""}
    entry = {"ticker": ticker, "cik": "0000320193", "status": signal, "grade": grade, "auto_buy_cap": 150.0}
    if kill is not None:
        entry["kill_criteria_due"] = kill
    log = {"decision_id": "", "decision_date": ASOF, "ticker": ticker, "action": signal,
           "screen_decision": "PASS", "gate3_pass": True, "reason_one_liner": "測試"}
    for n, v in (("signal_card.json", card), ("registry_entry.json", entry), ("decision_log.json", log)):
        (d / n).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text("論點內容", encoding="utf-8")
    return d


GATES_PASS = {"review_type": None, "action": "BUY", "gate1_not_deleted": True, "gate2_price_below_cap": True,
              "gate3_pass": True, "gate3_failures": []}


# ================================================================ G1-stop-D80

def test_m5a01_engine_full_b_invalid_is_non_analytic(tmp_path):
    """A3-M5A-01：FULL_B 輸出不合格（G7）→ 模型輸出失敗屬 D80 非分析性：三閘門 null、記 non_analytic_stop，不寫 False。"""
    from test_engine import RUN, full_responses, make_env
    resp = full_responses()
    resp["FULL_B"] = {"bad": 1}
    env = make_env(tmp_path, resp)
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)
    row = env.conn.execute("SELECT data FROM decisions WHERE ticker='KO' ORDER BY rowid DESC LIMIT 1").fetchone()
    log = json.loads(row["data"])
    assert log["stopped_at_gate"] == "G7"
    assert log["gate3_pass"] is None and log["gate2_price_below_cap"] is None
    assert log.get("non_analytic_stop") is True
    assert Z.nonanalytic_stop(log, "MISSING_DATA")


@pytest.mark.parametrize("gate", ["G6", "G7"])
def test_m5a01_non_analytic_flag_keeps_buys_and_gates(env, gate):
    """A3-M5A-01：呼叫端明示 non_analytic（不靠 gate 字串）→ 不撤買單、不歸 0 批次、閘門 null。"""
    conn, paths = env
    _reg_set(conn, "KO", "WATCH", {"grade": "A", "auto_buy_cap": 100.0, "build_plan": BP})
    _decision(conn, "KO", OLD, "KO-20261001-1", GATES_PASS, "BUY")
    oid = _open_buy(conn, "KO", did="KO-20261001-1")
    did = write_stop_result(conn, paths, "KO", ASOF, "r1", {"stopped": True, "stopped_at_gate": gate,
                                                            "reason_code": "MISSING_DATA"}, non_analytic=True)
    log = _log(conn, did)
    assert log["gate3_pass"] is None and log["non_analytic_stop"] is True
    assert _order_status(conn, oid) == "open"
    assert _reg(conn, "KO")[1]["build_plan"] == BP
    assert Z._latest_gate_decision(conn, "KO", ASOF)["decision_id"] == "KO-20261001-1"


def test_m5a01_data_check_failure_stays_analytic(env):
    """A3-M5A-01：D20／資料包建立失敗（資料檢查不過）明示 non_analytic=False → STEP0 仍失敗關閉。"""
    conn, paths = env
    _reg_set(conn, "KO", "WATCH", {"grade": "A", "build_plan": BP})
    oid = _open_buy(conn, "KO")
    did = write_stop_result(conn, paths, "KO", ASOF, "r1", {"stopped": True, "stopped_at_gate": "STEP0",
                                                            "reason_code": "MISSING_DATA"}, non_analytic=False)
    log = _log(conn, did)
    assert log["gate3_pass"] is False and "non_analytic_stop" not in log
    assert _order_status(conn, oid) == "cancelled"


def test_m5a01_deep_missing_hold_passes_flag(monkeypatch):
    """A3-M5A-01：deep._missing_hold 預設 non_analytic=True；DOSSIER 金融前置（D20）與資料包失敗傳 False。"""
    import inspect
    from trader.jobs import deep
    seen = []
    monkeypatch.setattr(deep, "_stop", lambda job, ctx, pa, notes, key=None, hold_unresolved=False, non_analytic=None:
                        seen.append(non_analytic))
    monkeypatch.setattr(deep, "_hold_after_fail", lambda *a, **k: [])
    from types import SimpleNamespace
    deep._missing_hold(SimpleNamespace(ticker="KO", asof=ASOF), None, "x", "G7")
    assert seen == [True]
    src = inspect.getsource(deep.DossierHandler)
    assert "non_analytic=False" in src and src.count("non_analytic=False") >= 2


@pytest.mark.parametrize("gate,flag", [("STEP0", None), ("G7", True)])
def test_m6a01_revalidate_keeps_buy_after_non_analytic_stop(env, gate, flag):
    """A3-M6A-01：D80 非分析性停止之後的 revalidate_open_buys 不撤 write_stop_result 刻意保留的買單
    （STEP0 已由 A3-V3-3-01 修；G6／G7 輸出失敗靠 non_analytic_stop 旗標）。"""
    conn, paths = env
    _reg_set(conn, "KO", "WATCH", {"grade": "A", "auto_buy_cap": 100.0, "build_plan": BP})
    _decision(conn, "KO", OLD, "KO-20261001-1", GATES_PASS, "BUY")
    oid = _open_buy(conn, "KO", did="KO-20261001-1")
    kw = {} if flag is None else {"non_analytic": flag}
    write_stop_result(conn, paths, "KO", "2026-10-05", "r1", {"stopped": True, "stopped_at_gate": gate,
                                                              "reason_code": "MISSING_DATA"}, **kw)
    assert _order_status(conn, oid) == "open"
    assert Z.revalidate_open_buys(conn, "2026-10-06") == []


def test_m6e04_d87_watch_registry_preserved(env):
    """A3-M6E-04／A3-M6A-02（D87）：非持股觀察檔遇非分析性停止 → 以既有登錄表為底，只改 status／last_reason_code／
    last_review_date；估值欄、價格線、等級、T、失效條件保留，不推進 last_full_review_date。"""
    conn, paths = env
    kc = [{"text": "毛利率連兩季下滑", "due": "2027-01-01"}]
    before = {"grade": "B", "auto_buy_cap": 90.0, "review_price": 100.0, "t30_at_valuation": 0.045,
              "cap_input_file": "cap_inputs/AAPL.json", "target_weight": 0.075, "sell_review_above": 160.0,
              "kill_criteria_due": kc, "last_full_review_date": OLD, "last_review_date": OLD}
    _reg_set(conn, "AAPL", "WATCH", before)
    write_stop_result(conn, paths, "AAPL", ASOF, "r1", {"stopped": True, "stopped_at_gate": "STEP0",
                                                        "reason_code": "MISSING_DATA"})
    status, d = _reg(conn, "AAPL")
    assert status == "MISSING_DATA" and d["status"] == "MISSING_DATA"
    for k in ("grade", "auto_buy_cap", "review_price", "t30_at_valuation", "cap_input_file", "target_weight",
              "sell_review_above", "kill_criteria_due"):
        assert d[k] == before[k], k
    assert d["last_full_review_date"] == OLD and d["last_review_date"] == ASOF
    assert d["last_reason_code"] == "MISSING_DATA"


@pytest.mark.parametrize("gate,reason,flag", [("STEP0", "MISSING_DATA", False), ("G1", "TOO_HARD", None),
                                              ("STEP0", "MISSING_DATA", None)])
def test_m6e01_integrity_reject_survives_stop(env, gate, reason, flag):
    """A3-M6E-01：誠信淘汰標記不因之後的 MISSING_DATA／TOO_HARD 停止而清掉（D27）。"""
    conn, paths = env
    _reg_set(conn, "AAPL", "REJECT", {"integrity_reject": True, "reject_basis": "NEVER-04a"})
    kw = {} if flag is None else {"non_analytic": flag}
    write_stop_result(conn, paths, "AAPL", ASOF, "r1", {"stopped": True, "stopped_at_gate": gate,
                                                        "reason_code": reason}, **kw)
    assert _reg(conn, "AAPL")[1].get("integrity_reject") is True


# ================================================================ G2-out-of-order-D86

def _stale_env(tmp_path, newer_rt="THESIS", **extra):
    from test_engine import full_responses, make_env, thesis
    env = make_env(tmp_path, {**full_responses(), "THESIS": thesis("完好"),
                              "HOLD_REVIEW": {"ticker": "KO", "action": "HOLD", "reason": "x", "path": []}})
    _reg_set(env.conn, "KO", "HOLDING", {"grade": "A"})
    _decision(env.conn, "KO", NEWER, "KO-20261020-1", {"review_type": newer_rt, "action": "HOLD",
                                                       "thesis_check": "完好", **extra}, "HOLD")
    return env


@pytest.mark.parametrize("jt", ["THESIS", "HOLD_REVIEW", "DOSSIER"])
def test_m6b04_engine_skips_stale_asof_job(tmp_path, jt):
    """A3-M6B-04（D86）：該檔已有 decision_date > job.asof 的決策 → 工作 skipped（stale_asof），不呼叫模型、不回寫、
    不寫收尾保守結論，run 不算失敗。"""
    from test_engine import RUN
    env = _stale_env(tmp_path)
    env.add(env.job("KO", jt, 1, params={"holding": True}))
    if jt == "DOSSIER":
        env.price_file("KO")
    env.engine.run(RUN)
    rows = env.rows()
    assert [r["status"] for r in rows] == ["skipped"]
    assert rows[0]["error"].startswith(E.STALE_ASOF)
    assert env.conn.execute("SELECT COUNT(*) FROM decisions WHERE ticker='KO'").fetchone()[0] == 1
    assert env.conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN,)).fetchone()[0] == "done"
    assert env.fake.calls == [] if hasattr(env.fake, "calls") else True


def test_m6b04_price_and_placeholders_do_not_trigger(env):
    """D86：較新的 PRICE、佔位列（job_id 非 null）、run 收尾（fallback）、D80 非分析性停止不觸發 stale_asof。"""
    conn, _ = env
    _decision(conn, "KO", NEWER, "KO-20261020-1", {"review_type": "PRICE", "action": "HOLD"})
    _decision(conn, "KO", NEWER, "KO-20261020-2", {"review_type": None, "action": "NO_ACTION"}, job_id="j9")
    _decision(conn, "KO", NEWER, "KO-20261020-3", {"review_type": "HOLD_REVIEW", "action": "HOLD", "fallback": True})
    _decision(conn, "KO", NEWER, "KO-20261020-4", {"review_type": None, "action": "NO_ACTION", "non_analytic_stop": True},
              reason="MISSING_DATA")
    assert E.stale_asof(conn, "KO", ASOF) is None
    _decision(conn, "KO", NEWER, "KO-20261020-5", {"review_type": "THESIS", "action": "HOLD"})
    assert E.stale_asof(conn, "KO", ASOF).startswith(E.STALE_ASOF)
    assert E.stale_asof(conn, "KO", NEWER) is None


def test_m6b01_stale_hold_review_does_not_downgrade_or_reopen(env):
    """A3-M6B-01：舊資料日 HOLD_REVIEW 晚寫入 → 不降登錄表等級、不重開觀察期，只留決策與複查日。"""
    conn, paths = env
    _reg_set(conn, "AAPL", "HOLDING", {"grade": "A", "last_full_review_date": NEWER, "last_review_date": NEWER})
    _decision(conn, "AAPL", NEWER, "AAPL-20261020-1", {"review_type": None, "action": "BUY"}, "BUY")
    did = write_hold_review_result(conn, paths, "AAPL", ASOF, "r1",
                                   {"action": "HOLD", "grade_after": "C", "observe_until": "2026-12-31",
                                    "reason": "x", "path": []})
    _, d = _reg(conn, "AAPL")
    assert d["grade"] == "A" and "observe_until" not in d
    assert any("A3-M6B-01" in n for n in _log(conn, did)["framework_notes"])


def test_m6b01_stale_thesis_suspect_does_not_open_p50(env):
    conn, paths = env
    _reg_set(conn, "AAPL", "HOLDING", {"grade": "A", "last_full_review_date": NEWER})
    write_thesis_result(conn, paths, "AAPL", ASOF, "r1",
                        {"verdict": "疑似", "reason": "x", "kill_criteria_status": []}, {"holding": True})
    assert "observe_until" not in _reg(conn, "AAPL")[1]


def test_m6b02_stale_full_buy_downgraded(env, tmp_path):
    """A3-M6B-02：資料日之後已有較新的完整分析 → 舊資料日 BUY 當 WATCH（不與較新卡拼成可下單決策）。"""
    conn, paths = env
    _reg_set(conn, "AAPL", "WATCH", {"grade": "B", "last_full_review_date": NEWER})
    _decision(conn, "AAPL", NEWER, "AAPL-20261020-1", {"review_type": None, "action": "WATCH"})
    did = write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path))
    assert conn.execute("SELECT signal FROM decisions WHERE decision_id=?", (did,)).fetchone()[0] == "WATCH"
    assert _log(conn, did)["action"] == "WATCH" and _card(conn, did)["signal"] == "WATCH"


def test_m6b03_latest_signal_asof_bound(env):
    """A3-M6B-03：latest_signal／latest_card／_valued_card 依資料日上界；舊資料日 PRICE 不沿用未來的決策卡。"""
    conn, paths = env
    _decision(conn, "AAPL", "2026-10-10", "AAPL-20261010-1", {"review_type": None, "action": "WATCH"})
    _signal(conn, "AAPL", "AAPL-20261010-1", "2026-10-10", {"reason_code": "PASS_ALL", "certainty_grade": "B",
                                                            "value_per_share": {"conservative": 100.0}})
    _decision(conn, "AAPL", NEWER, "AAPL-20261020-1", {"review_type": None, "action": "WATCH"})
    _signal(conn, "AAPL", "AAPL-20261020-1", NEWER, {"reason_code": "GATE3_FAIL", "certainty_grade": "A",
                                                     "value_per_share": {"conservative": 130.0}})
    assert W.latest_signal(conn, "AAPL", "2026-10-12")["data"]["decision_log_id"] == "AAPL-20261010-1"
    assert W.latest_signal(conn, "AAPL")["data"]["decision_log_id"] == "AAPL-20261020-1"
    assert Z.latest_card(conn, "AAPL", "2026-10-12")["decision_log_id"] == "AAPL-20261010-1"
    assert HV._valued_card(conn, "AAPL", "2026-10-12")["decision_log_id"] == "AAPL-20261010-1"
    _reg_set(conn, "AAPL", "WATCH", {"grade": "B"})
    did = write_price_result(conn, paths, "AAPL", "2026-10-12", "r1", action="WATCH", gate1=True, gate2="不通過",
                             gate3="通過", cap={"auto_buy_cap": 90.0}, price=100.0, price_date="2026-10-12",
                             t30=0.045, reason="x", notes=[])
    c = _card(conn, did)
    assert c["carried_from_decision_id"] == "AAPL-20261010-1" and c["reason_code"] == "PASS_ALL"


def test_m6b03_price_handler_reads_asof_bound_card():
    import inspect
    from trader.jobs.review_jobs import PriceHandler
    src = inspect.getsource(PriceHandler)
    assert "latest_signal(ctx.conn, job.ticker, job.asof)" in src and "latest_signal(ctx.conn, T, asof)" in src


def test_m6d05_holdings_view_backfill_note(env):
    """A3-M6D-05：補跑舊資料日的 G8 比較檔標註持股與登錄表為現況。"""
    conn, paths = env
    _decision(conn, "AAPL", NEWER, "AAPL-20261020-1", {"review_type": None, "action": "WATCH"})
    view = HV.holdings_g8(conn, paths, ASOF, {}, None)
    assert "A3-M6D-05" in view["note"] and "現況" in view["note"]
    assert "A3-M6D-05" not in HV.holdings_g8(conn, paths, NEWER, {}, None)["note"]


def test_m6d02_stale_full_keeps_newer_thesis_state(env, tmp_path):
    """A3-M6D-02：較新 THESIS 疑似開了觀察期、H2 降過等級、失效條件已檢查 → 晚寫入的舊資料日完整分析 BUY
    不清觀察期、不把等級升回、不重設 checked_on。"""
    conn, paths = env
    kc = [{"text": "毛利率連兩季下滑", "due": "2026-12-01", "checked_on": NEWER, "status": "未觸發"}]
    _reg_set(conn, "AAPL", "HOLDING", {"grade": "C", "observe_until": "2027-04-20", "kill_criteria_due": kc,
                                       "last_full_review_date": OLD, "weight": 0.05})
    _decision(conn, "AAPL", NEWER, "AAPL-20261020-1", {"review_type": "THESIS", "action": "HOLD",
                                                       "thesis_check": "疑似"}, "HOLD")
    _decision(conn, "AAPL", NEWER, "AAPL-20261020-2", {"review_type": "HOLD_REVIEW", "action": "HOLD",
                                                       "grade_after": "C", "observe_until": "2027-04-20"}, "HOLD")
    write_full_result(conn, paths, "AAPL", ASOF, "r1",
                      _tdir(tmp_path, kill=[{"text": "毛利率連兩季下滑", "due": "2026-11-01"},
                                            {"text": "新條件", "due": "2027-01-01"}]))
    _, d = _reg(conn, "AAPL")
    assert d["observe_until"] == "2027-04-20"
    assert d["grade"] == "C"
    k1 = [k for k in d["kill_criteria_due"] if k["text"] == "毛利率連兩季下滑"]
    assert k1 == kc
    assert any(k["text"] == "新條件" for k in d["kill_criteria_due"])
    assert Z.add_blocked(conn, "AAPL", "2026-11-20") is not None


def test_m6d03_stale_full_after_price_writes_grade(env, tmp_path):
    """A3-M6D-03：只有較新的 PRICE（PRICE 不寫等級／T／cap_inputs）→ 舊資料日完整分析照寫等級；
    load_decisions 附卡取該決策自己的卡。"""
    conn, paths = env
    _reg_set(conn, "AAPL", "WATCH", {"grade": "A", "target_weight": 0.15, "auto_buy_cap": 140.0,
                                     "review_price": 99.0, "last_full_review_date": OLD})
    _decision(conn, "AAPL", NEWER, "AAPL-20261020-1", {"review_type": "PRICE", "action": "WATCH"})
    _signal(conn, "AAPL", "AAPL-20261020-1", NEWER, {"reason_code": "PASS_ALL", "certainty_grade": "A"})
    did = write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, grade="B"))
    _, d = _reg(conn, "AAPL")
    assert d["grade"] == "B"
    assert d["review_price"] == 99.0 and d["auto_buy_cap"] == 140.0   # 價格線仍保留較新的 PRICE
    items = [x for x in Z.load_decisions(conn, ASOF, since="2026-10-15") if x["decision_id"] == did]
    assert items and items[0]["card"]["decision_log_id"] == did




@pytest.mark.parametrize("suspect", [True, False])
def test_m6d01_daily_late_buy(world, suspect):
    from test_daily_pipeline import CARD, D1, D2, FakeAlpaca, _bars, _dec, _reg as _dreg, _run
    cfg, paths, conn = world
    _dreg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B", "target_weight": 0.10})
    if suspect:
        db.insert(conn, "decisions", {"decision_id": "AAA-20261015-9", "ticker": "AAA", "decision_date": D1,
                                      "signal": "WATCH", "data": {"ticker": "AAA", "decision_date": D1, "action": "WATCH",
                                                                  "review_type": "THESIS", "thesis_check": "疑似"},
                                      "created_at": db.now_iso()})
    _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    time.sleep(1.1)
    _dec(conn, "AAA-20261014-1", "AAA", "2026-10-14", "BUY", card=CARD)
    s = _run(cfg, paths, conn, D2, FakeAlpaca(_bars()))
    placed = [o for o in s.get("orders", []) if o["ticker"] == "AAA" and o["side"] == "BUY"]
    if suspect:
        assert not placed, s["buys"]
        assert any("A3-M6D-01" in n for n in s["notes"])
    else:
        assert placed, s["buys"]


# ================================================================ G3-fullb-downgrade

def test_m5b01_fullb_downgrade_blocks_price_add(env, tmp_path):
    """A3-M5B-01：框架 BUY → WATCH（G8 等）→ reason_code FRAMEWORK_HOLD；PRICE ADD → HOLD、BUY → WATCH。閘門值不改。"""
    conn, paths = env
    did = write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path), final_signal="WATCH",
                            notes=["G8 機會成本未勝出，BUY → WATCH"], framework_hold=True,
                            gates={"gate1": True, "gate2": True, "gate3": True, "gate3_failures": [],
                                   "gate2_failures": []})
    assert _card(conn, did)["reason_code"] == "FRAMEWORK_HOLD"
    assert conn.execute("SELECT reason_code FROM decisions WHERE decision_id=?", (did,)).fetchone()[0] == "FRAMEWORK_HOLD"
    assert _reg(conn, "AAPL")[1]["last_reason_code"] == "FRAMEWORK_HOLD"
    assert _log(conn, did)["gate3_pass"] is True
    assert "FRAMEWORK_HOLD" in W.PRICE_NO_BUY_REASONS
    for act, want in (("ADD", "HOLD"), ("BUY", "WATCH")):
        p = write_price_result(conn, paths, "AAPL", ASOF, "r1", action=act, gate1=True, gate2="通過", gate3="通過",
                               cap={"auto_buy_cap": 200.0}, price=100.0, price_date=ASOF, t30=0.045, reason="x",
                               notes=[])
        assert _log(conn, p)["action"] == want


def test_m5b01_engine_g8_downgrade_writes_framework_hold(tmp_path):
    """A3-M5B-01：引擎實跑 G8 明顯輸 → BUY → WATCH，訊號卡與登錄表 reason_code＝FRAMEWORK_HOLD。"""
    from test_engine import RUN, _g8_view, full_responses, make_env
    env = make_env(tmp_path, full_responses())
    _g8_view(env, 0.12)
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)
    card = json.loads(env.conn.execute("SELECT data FROM signals").fetchone()[0])
    assert card["signal"] == "WATCH" and card["stopped_at_gate"] == "G8"
    assert card["reason_code"] == "FRAMEWORK_HOLD"
    assert _reg(env.conn, "KO")[1]["last_reason_code"] == "FRAMEWORK_HOLD"


def test_m5b01_model_watch_keeps_reason(env, tmp_path):
    """模型自己判 WATCH（非框架降級）不改 reason_code。"""
    conn, paths = env
    did = write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, signal="WATCH", reason="PASS_ALL"),
                            final_signal="WATCH")
    assert _card(conn, did)["reason_code"] == "PASS_ALL"


# ================================================================ G4-holdings-view-G8

CAP_INPUT = json.loads((get_paths().skill_deep / "assets" / "cap_calc_example_input.json").read_text(encoding="utf-8"))


def _cap_inputs(paths, t, grade="A"):
    d = paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{t}.json").write_text(json.dumps({**CAP_INPUT, "grade": grade}), encoding="utf-8")


def _cheap_close(conn, paths, t):
    row = HV._valuation(conn, paths, t, float(CAP_INPUT["price"]), None)
    cap = row["auto_buy_cap"]
    assert cap, row
    return cap * 0.5


@pytest.mark.parametrize("patch", [{"last_reason_code": "GATE3_FAIL"}, {"last_reason_code": "RED_LIGHT"},
                                   {"last_reason_code": "FRAMEWORK_HOLD"}, {"auto_buy_cap": None}])
def test_m6a03_watch_row_registry_blocks(env, patch):
    """A3-M6A-03／A3-M6C-01：登錄表 GATE3_FAIL／RED_LIGHT（PRICE 不能升 BUY）或上限 None 的觀察檔不算 G8 可得選項。"""
    conn, paths = env
    _cap_inputs(paths, "PEP")
    _reg_set(conn, "PEP", "WATCH", {"grade": "A", "auto_buy_cap": 999.0})
    close = _cheap_close(conn, paths, "PEP")
    assert HV._watch_row(conn, paths, "PEP", close, ASOF, None, set())["buyable"] is True   # 對照
    _reg_set(conn, "PEP", "WATCH", {"grade": "A", "auto_buy_cap": 999.0, **patch})
    r = HV._watch_row(conn, paths, "PEP", close, ASOF, None, set())
    assert r["buyable"] is False and r["not_buyable"]


def test_m6a04_valuation_uses_worse_grade(env):
    """A3-M6A-04／A3-M6B-09／A3-M6C-02：登錄表等級（HOLD_REVIEW 降等）較 cap_inputs 嚴 → 上限與確定性係數用降後等級；
    sizing.live_g8 同口徑。"""
    conn, paths = env
    _cap_inputs(paths, "PEP", "A")
    _reg_set(conn, "PEP", "WATCH", {"grade": "A"})
    close = _cheap_close(conn, paths, "PEP")
    a = HV._valuation(conn, paths, "PEP", close, None)
    _reg_set(conn, "PEP", "WATCH", {"grade": "C"})
    c = HV._valuation(conn, paths, "PEP", close, None)
    assert a["grade"] == "A" and c["grade"] == "C"
    assert c["certainty_coef"] == HV.CERTAINTY_COEF["C"]
    assert HV._watch_row(conn, paths, "PEP", close, ASOF, None, set())["buyable"] is False
    la = Z.live_g8(paths.state / "cap_inputs", "PEP", close)
    lc = Z.live_g8(paths.state / "cap_inputs", "PEP", close, "C")
    assert la is not None and lc is not None and lc < la
