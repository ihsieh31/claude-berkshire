"""持股完整複查（HOLD_REVIEW，H2 賣出決策樹）：框架覆核、引擎流程、回寫與下單規劃。全部 FakeRunner。"""
from __future__ import annotations

import json

from test_engine import ASOF, RUN, full_a, full_b, make_env

from trader import db
from trader.jobs.hold_jobs import check, sell_tree_text
from trader.paths import get_paths
from trader.portfolio import sizing as S


def hold(action="HOLD", cat=None, frac=None, stop=True, named=None):
    return {"ticker": "KO", "decision_date": ASOF,
            "path": [{"q": "Q0", "answer": "否", "evidence": "非價格理由"},
                     {"q": "Q2", "answer": "是", "evidence": "測試"}],
            "action": action, "sell_category": cat, "trim_fraction": frac, "stop_adding": stop,
            "observe_until": "2027-04-16" if action == "HOLD" else None,
            "falsified_assumption": named, "reason": "測試"}


def _holding(env, qty=100, first="2024-01-02"):
    env.paths.theses.mkdir(parents=True, exist_ok=True)
    (env.paths.theses / "KO_2024-01-02.md").write_text("# 原論點\n", encoding="utf-8")
    data = {"ticker": "KO", "status": "HOLDING", "grade": "B", "auto_buy_cap": 55.0, "cost": 50.0,
            "first_buy_date": first, "thesis_file": "theses/KO_2024-01-02.md",
            "kill_criteria_due": [{"text": "原失效條件", "due": "2027-01-01"}]}
    db.insert(env.conn, "registry", {"ticker": "KO", "status": "HOLDING", "data": json.dumps(data),
                                     "updated_at": db.now_iso()})
    db.insert(env.conn, "positions", {"ticker": "KO", "qty": qty, "avg_cost": 50.0, "first_buy_date": first,
                                      "updated_at": db.now_iso()})


def _last_log(env):
    r = env.conn.execute("SELECT data FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()
    return json.loads(r["data"])


# ---------------------------------------------------------------- 覆核規則

def test_check_rules():
    ok = dict(iv_base=50.0, price=60.0, held_days=1000)
    assert check(hold(), **ok) == ([], False)
    assert check(hold("SELL_ALL", "業務", named="假設 A 被 10-Q 否定"), **ok) == ([], False)
    assert check(hold("SELL_ALL", "業務"), **ok)[0]                          # 未具名
    assert check(hold("TRIM", "業務", 0.5, named="x"), **ok)[0]              # 業務類不得部分賣
    assert check(hold("TRIM", "估值", 0.25), **ok) == ([], False)
    assert check(hold("TRIM", "估值", 1.5), **ok)[0]                         # 比例不合法
    assert check(hold("SELL_ALL", "估值"), iv_base=50.0, price=45.0, held_days=1000)[0]   # 價格 < IV_base
    assert check(hold("SELL_ALL", "估值"), iv_base=None, price=60.0, held_days=1000)[0]   # 無 IV
    assert check(hold("SELL_ALL", "估值"), iv_base=50.0, price=60.0, held_days=200)[0]    # Q5 未滿 2 年
    assert check(hold("SELL_ALL", "SWITCH"), **ok) == ([], True)            # 轉人工


def test_sell_tree_text_is_rulebook_section_9():
    txt = sell_tree_text(get_paths().skill_deep)
    assert txt.startswith("## 9. 賣出決策樹") and "Q6 機會成本" in txt and "## 10" not in txt


# ---------------------------------------------------------------- 引擎流程

def test_holding_full_a_stop_goes_to_hold_review_and_keeps_registry(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": hold()})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()] == ["FULL_A", "HOLD_REVIEW"]
    log = _last_log(env)
    assert log["action"] == "HOLD" and log["review_type"] == "HOLD_REVIEW" and log["stop_adding"] is True
    reg = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])
    assert reg["auto_buy_cap"] == 55.0 and reg["thesis_file"] == "theses/KO_2024-01-02.md"   # 未被清空
    assert reg["observe_until"] == "2027-04-16"
    assert list(env.paths.theses.glob("KO_2024-01-02_addendum_*.md"))                       # H2 寫增補


def test_non_holding_full_a_stop_has_no_hold_review(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True)})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()] == ["FULL_A"]


def test_holding_full_b_watch_then_business_sell_all(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": full_b("WATCH"),
                              "HOLD_REVIEW": hold("SELL_ALL", "業務", named="假設 A 被 10-Q 否定")})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()] == ["FULL_A", "FULL_B", "HOLD_REVIEW"]
    assert _last_log(env)["action"] == "SELL_ALL"
    # Q3「更好的用途」：HOLD_REVIEW 沙箱有 G8 比較檔，本持股放在 self、不在比較對象內
    from pathlib import Path
    g8 = json.loads((Path(env.by_type("HOLD_REVIEW")[0]["sandbox"]) / "portfolio" / "holdings_g8.json")
                    .read_text(encoding="utf-8"))
    assert g8["self"]["ticker"] == "KO" and g8["holdings"] == [] and g8["excluded_self"] == "KO"
    reg = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])
    assert reg["thesis_file"] == "theses/KO_2024-01-02.md" and reg["first_buy_date"] == "2024-01-02"
    plans = S.plan_sells(env.conn, ASOF, S.load_decisions(env.conn, ASOF), {"KO": 60.0})
    assert plans[0]["qty"] == 100 and plans[0]["action"] == "SELL_ALL"


def test_unverified_sell_twice_becomes_hold_and_anomaly(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": hold("SELL_ALL", "估值")})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()] == ["FULL_A", "HOLD_REVIEW", "HOLD_REVIEW"]   # 退回一次
    log = _last_log(env)
    assert log["action"] == "HOLD" and log["stop_adding"] is True and "覆核兩次未通過" in log["framework_notes"][0]
    a = env.conn.execute("SELECT kind, status FROM anomalies").fetchone()
    assert (a["kind"], a["status"]) == ("hold_review_needs_human", "open")


def test_switch_category_goes_to_human(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": hold("SELL_ALL", "SWITCH")})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert _last_log(env)["action"] == "HOLD"
    assert env.conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == 1


# ---------------------------------------------------------------- 下單規劃

def test_trim_fraction_and_stop_adding(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": hold("TRIM", "估值", 0.25)})
    _holding(env)
    env.price_file("KO")
    # 估值類需要 IV_base：放一份 cap_result（價格 60.5 ≥ IV_base 50）
    (env.paths.work / RUN / "KO" / "cap_result.json").write_text(json.dumps({"IV_base": 50.0}), encoding="utf-8")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    log = _last_log(env)
    assert log["action"] == "TRIM" and log["trim_fraction"] == 0.25 and log["stop_adding"] is True
    plans = S.plan_sells(env.conn, ASOF, S.load_decisions(env.conn, ASOF), {"KO": 60.0})
    assert plans[0]["qty"] == 25
    assert S.add_blocked(env.conn, "KO", ASOF)


def test_add_blocked_lifted_only_by_newer_review(tmp_path):
    env = make_env(tmp_path, {})
    conn = env.conn

    def dec(n, log):
        db.insert(conn, "decisions", {"decision_id": f"KO-20261016-{n}", "ticker": "KO", "decision_date": ASOF,
                                      "run_id": "x", "job_id": None, "signal": log.get("action"),
                                      "reason_code": None, "data": json.dumps(log), "created_at": db.now_iso()})
    dec(1, {"action": "HOLD", "review_type": "HOLD_REVIEW", "stop_adding": True, "observe_until": "2026-01-01"})
    assert S.add_blocked(conn, "KO", ASOF)                 # 觀察期滿也不自動恢復
    dec(2, {"action": "HOLD", "review_type": "THESIS", "thesis_check": "完好"})
    assert S.add_blocked(conn, "KO", ASOF) is None
