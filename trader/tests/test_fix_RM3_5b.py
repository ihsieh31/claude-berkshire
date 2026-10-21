"""RM3-5b：M3 複審修復（sizing／writeback）。
M3-RS2-01（sizing 側）：C／D 級加碼上限＝0（P-28），與 target_weight 一致。
M3-RS4-01：P-50 登錄表觀察期未到期 → THESIS 完好不解除停止加碼。
M3-R7-01：建倉已完成時，新完整分析 BUY 的框架 T 寫進登錄表 target_weight（不被舊 build_plan 目標棘輪壓住）。"""
import json
from datetime import date, timedelta

import pytest

import test_review_jobs as R
from trader import db
from trader.analysis import writeback
from trader.portfolio import sizing as S

env = R.env


# ---------------- M3-RS2-01（sizing 側） ----------------

@pytest.mark.parametrize("grade", ["C", "D"])
def test_add_cap_zero_for_non_ab_grade(grade):
    assert S._add_cap({"grade": grade}, None) == 0.0
    assert S._add_cap({"grade": grade}, 0.08) == 0.0


@pytest.mark.parametrize("grade,qty0", [("C", True), ("B", False)])
def test_plan_buys_add_blocked_for_c_grade(tmp_path, grade, qty0):
    """登錄表降為 C 的持股（建倉計畫仍有剩餘批次）：ADD 計畫數量 0；B 級對照照常加碼。"""
    import test_portfolio as P
    from trader.ledger import book
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100000.0, "2026-10-01")
    P._pos(conn, "HLD", 100, 50.0, "2026-10-10")
    P._reg(conn, "HLD", "HOLDING", {"grade": grade, "auto_buy_cap": 60.0,
                                    "build_plan": {"target_weight": 0.10, "tranches_left": 3}})
    plans = S.plan_buys(conn, P.ASOF, [P._item("HLD-20261016-1", "HLD", action="ADD", card=P._card())],
                        {"HLD": 50.0}, {"HLD": 1e9}, 10)
    conn.close()
    assert (plans[0]["qty"] == 0) is qty0


def test_add_cap_ab_and_missing_unchanged():
    assert S._add_cap({}, None) == pytest.approx(0.15)   # 缺等級（舊資料）不變
    assert S._add_cap({"grade": "A"}, None) == pytest.approx(0.15)
    assert S._add_cap({"grade": "B"}, None) == pytest.approx(0.10)
    assert S._add_cap({"grade": "A"}, 0.08) == pytest.approx(0.08)


# ---------------- M3-RS4-01 ----------------

def _dec(conn, i, d, data):
    db.insert(conn, "decisions", {"decision_id": f"KO-{i}", "ticker": "KO", "decision_date": d, "run_id": "r",
                                  "job_id": "j", "data": data, "created_at": f"{d}T0{i}:00:00"})


def test_thesis_intact_inside_p50_window_still_blocks(env):
    paths, conn = env
    R._seed_registry(conn, "KO", "HOLDING", {"observe_until": "2027-04-20"})
    _dec(conn, 1, "2026-10-20", {"review_type": "THESIS", "thesis_check": "疑似"})
    _dec(conn, 2, "2026-10-21", {"review_type": "HOLD_REVIEW", "action": "HOLD", "stop_adding": True,
                                 "observe_until": "2027-04-20"})
    assert S.add_blocked(conn, "KO", "2026-11-01")
    _dec(conn, 3, "2027-01-25", {"review_type": "THESIS", "thesis_check": "完好"})
    why = S.add_blocked(conn, "KO", "2027-01-26")
    assert why and "2027-04-20" in why
    # 觀察期過後（登錄表期限已過）THESIS 完好照舊解除
    assert S.add_blocked(conn, "KO", "2027-04-21") is None


def test_hold_review_no_stop_inside_window_blocks(env):
    paths, conn = env
    R._seed_registry(conn, "KO", "HOLDING", {"observe_until": "2027-04-20"})
    _dec(conn, 1, "2026-11-01", {"review_type": "HOLD_REVIEW", "action": "HOLD", "stop_adding": False})
    assert S.add_blocked(conn, "KO", "2026-11-02")


def test_thesis_intact_no_observation_not_blocked(env):
    paths, conn = env
    R._seed_registry(conn, "KO", "HOLDING", {})
    _dec(conn, 1, "2026-11-01", {"review_type": "THESIS", "thesis_check": "完好"})
    assert S.add_blocked(conn, "KO", "2026-11-02") is None


# ---------------- M3-R7-01 ----------------

GATES = {"gate1": True, "gate2": True, "gate3": True, "gate2_failures": [], "gate3_failures": []}
SUMMARY_BUY = {"signal": "BUY", "reason_code": "PASS_ALL", "stopped_at_gate": None, "certainty_grade": "B",
               "intended_weight": {"level": "HIGH", "reason": "G8 優勢 2pp，確定性 B 級"}}
CAP = {"auto_buy_cap": 70.0, "IV_base": 90.0, "grade_used": "B", "price": 60.5}


def _td(tmp_path, name):
    td = tmp_path / name
    td.mkdir()
    files = {"signal_card.json": {"signal": "BUY", "reason_code": "PASS_ALL", "certainty_grade": "B"},
             "registry_entry.json": {"ticker": "KO"}, "decision_log.json": {"action": "BUY"},
             "phaseB.json": SUMMARY_BUY, "cap_result.json": CAP, "cap_input.json": R.CAP_INPUT}
    for k, v in files.items():
        (td / k).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
    (td / "thesis.md").write_text("論點", encoding="utf-8")
    return td


def _seed_held(conn, bp):
    first = (date.fromisoformat(R.ASOF) - timedelta(days=400)).isoformat()
    R._seed_registry(conn, "KO", "HOLDING", {"first_buy_date": first, "weight": 0.05,
                                             "build_plan": {**bp, "started": first}})


def test_completed_build_new_buy_raises_registry_target(env, tmp_path):
    paths, conn = env
    _seed_held(conn, {"target_weight": 0.05, "tranches_left": 0})
    writeback.write_full_result(conn, paths, "KO", R.ASOF, "r1", _td(tmp_path, "t"), "BUY", [], gates=GATES)
    reg = R._reg(conn)["data"]
    assert reg["target_weight"] == pytest.approx(0.10)          # 新框架 T（HIGH × B）
    assert reg["build_plan"]["target_weight"] == pytest.approx(0.05)   # 歷史目標照留
    from trader.jobs.review_jobs import _framework_action
    act, _ = _framework_action(True, True, weight=reg["weight"], target=reg["target_weight"])
    assert act != "HOLD"


def test_build_in_progress_keeps_smaller_target(env, tmp_path):
    paths, conn = env
    _seed_held(conn, {"target_weight": 0.05, "tranches_left": 2})
    writeback.write_full_result(conn, paths, "KO", R.ASOF, "r1", _td(tmp_path, "t"), "BUY", [], gates=GATES)
    assert R._reg(conn)["data"]["target_weight"] == pytest.approx(0.05)
