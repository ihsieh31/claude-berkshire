"""F3-DP：深度鏈與持有回寫（V3-1 的 FG-M2M3-deep、FG-M2-redlight、FG-M3-hold-writeback）＋其他組留下的 deep.py 待辦。
A3-M1D-01 FULL_A screen_context 去 X5；A3-M2B-03（D92）紅燈只重跑一次並帶上次致命路徑；A3-M2C-02 訊號卡紅燈欄落地；
A3-M3B-01／A3-M3D-03 持股 FULL_A 違規停止退回、仍停不推進完整複查日；A3-M3C-03 _grade_after；A3-M3D-01(1) 論點理由遮估值；
A3-M3D-02 Q3c 需 P-08 品質優；A3-M2C-06 deep 層 distress_pass；D93 g_notes 進日誌；內部人解析失敗標缺；G8 _pick 帶 price_to_conservative。"""
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_engine import ASOF, RUN, consistent_card, full_a, full_b, full_responses, make_env, red_team
from trader import db
from trader.analysis import writeback as W
from trader.jobs import deep


def _reg(conn, t="KO"):
    row = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return row["status"], json.loads(row["data"])


def _set_reg(conn, data, status="WATCH", t="KO"):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"ticker": t, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _last(conn):
    r = conn.execute("SELECT d.signal, d.reason_code, d.data AS log, s.data AS card FROM decisions d "
                     "JOIN signals s USING(decision_id) ORDER BY d.rowid DESC LIMIT 1").fetchone()
    return r["signal"], r["reason_code"], json.loads(r["log"]), json.loads(r["card"])


def _plus(d, n):
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------- A3-M1D-01

def test_full_a_screen_context_has_no_x5(tmp_path):
    env = make_env(tmp_path, {})
    sc = {"ticker": "KO", "screen_decision": "PASS", "delete_codes": "X5_近期已淘汰;X2_財報缺", "flag_codes": "F1",
          "price": 60.0}
    job = env.job("KO", "FULL_A", 1, params={"screen_context": sc})
    v = deep.FullAHandler().prompt_values(job, env.engine.ctx, "api")
    shown = json.loads(v["screen_context"].split("```json\n")[1].split("\n```")[0])
    assert shown["delete_codes"] == "X2_財報缺" and "price" not in shown
    assert "X5" not in v["screen_context"]
    assert sc["delete_codes"].startswith("X5_")   # 原物件（decisions.json 追溯）不動
    assert deep.screen_context_for_prompt({"delete_codes": ["X5_a", "X1_b"]})["delete_codes"] == ["X1_b"]


# ---------------------------------------------------------------- A3-M2B-03（D92）

def test_red_light_rerun_once(tmp_path):
    env = make_env(tmp_path, {})
    conn, paths = env.conn, env.paths
    stop = {"stopped": True, "stopped_at_gate": "G9", "reason_code": "RED_LIGHT"}
    W.write_stop_result(conn, paths, "KO", ASOF, "r1", stop)
    assert _reg(conn)[1]["next_full_review_due"] == _plus(ASOF, 14)
    d2 = _plus(ASOF, 14)
    W.write_stop_result(conn, paths, "KO", d2, "r2", stop)
    st, data = _reg(conn)
    assert st == "WATCH" and data["last_reason_code"] == "RED_LIGHT"
    assert data["next_full_review_due"] is None          # 修前：d2+14，無上限
    assert any("連續第二次紅燈" in n and "D92" in n for n in _last(conn)[2]["framework_notes"])
    W.write_stop_result(conn, paths, "KO", _plus(d2, 30), "r3", {**stop, "reason_code": "REJECT"})
    assert "red_light_context" not in _reg(conn)[1]


def test_red_light_engine_stores_fatal_paths_and_second_time_no_rerun(tmp_path):
    env = make_env(tmp_path, {**full_responses(), "RED_TEAM": red_team(True)})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)
    data = _reg(env.conn)[1]
    assert data["next_full_review_due"] == _plus(ASOF, 14)
    assert data["red_light_context"]["fatal_paths"][0]["path"] == "需求崩潰"
    # 第二次（登錄表已是 RED_LIGHT）：直接以 write_full_result 的 RED_LIGHT 結論重寫
    td = env.paths.work / RUN / "KO"
    W.write_full_result(env.conn, env.paths, "KO", _plus(ASOF, 14), "r2", td, "NO_ACTION", [])
    data = _reg(env.conn)[1]
    assert data["last_reason_code"] == "RED_LIGHT" and data.get("next_full_review_due") != _plus(ASOF, 28)


def test_red_light_rerun_prompt_carries_prior_fatal_paths(tmp_path):
    env = make_env(tmp_path, {})
    _set_reg(env.conn, {"last_reason_code": "RED_LIGHT", "red_light_context": {
        "date": "2026-10-02", "fatal_paths": [{"path": "需求崩潰", "probability_note": "低"},
                                               {"path": "KO trades at 25x earnings, valuation stretched"}],
        "reasons": []}})
    ctx = env.engine.ctx
    b = deep.FullBHandler().prompt_values(env.job("KO", "FULL_B", 1), ctx, "api")["trigger"]
    assert "需求崩潰（低）" in b and "25x earnings" in b and "反方角色" in b and "D92" in b
    a = deep.FullAHandler().prompt_values(env.job("KO", "FULL_A", 1), ctx, "api")["trigger"]
    assert "需求崩潰（低）" in a and "25x" not in a and "略" in a   # 價格盲：估值字句整條略
    _set_reg(env.conn, {"last_reason_code": "REJECT"})
    assert "紅燈" not in deep.FullBHandler().prompt_values(env.job("KO", "FULL_B", 1), ctx, "api")["trigger"]


# ---------------------------------------------------------------- A3-M2C-02

def _fb_with_card(**kw):
    fb = full_b("BUY")
    card = {**consistent_card("BUY"), **kw}
    fb["files"] = {**fb["files"], "KO/signal_card.json": json.dumps(card, ensure_ascii=False)}
    return fb


@pytest.mark.parametrize("kw,red", [
    ({"bias_count": {"pro_trade": 3, "pro_sell_or_wait": 0}}, True),
    ({"bias_count": {"pro_trade": 5, "pro_sell_or_wait": 1}}, True),
    ({"bias_count": {"pro_trade": 0, "pro_sell_or_wait": "3"}}, True),
    ({"red_light": True}, True),
    ({"delay_one_cycle": True}, True),
    ({"bias_count": {"pro_trade": 2, "pro_sell_or_wait": 2}, "red_light": False, "delay_one_cycle": False}, False),
])
def test_buy_with_bias3_becomes_red_light(tmp_path, kw, red):
    env = make_env(tmp_path, {**full_responses(), "FULL_B": _fb_with_card(**kw)})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    assert env.engine.run(RUN)["status"] == "done"
    sig, rc, log, card = _last(env.conn)
    types = [r["job_type"] for r in env.rows()]
    if red:
        assert (sig, rc) == ("NO_ACTION", "RED_LIGHT") and "RED_TEAM" not in types
        assert card["stopped_at_gate"] == "G9"
        assert any("P-64" in n and "A3-M2C-02" in n for n in log["framework_notes"])
        st, data = _reg(env.conn)
        assert st == "WATCH" and data["next_full_review_due"] == _plus(ASOF, 14)
        assert data["red_light_context"]["reasons"]
    else:
        assert sig == "BUY" and "RED_TEAM" in types


# ---------------------------------------------------------------- A3-M3B-01／A3-M3D-03

OLD_FULL = "2025-10-01"


def _holding(env):
    env.paths.theses.mkdir(parents=True, exist_ok=True)
    (env.paths.theses / "KO_2025-10-01.md").write_text("# 原論點\n", encoding="utf-8")
    _set_reg(env.conn, {"grade": "B", "thesis_file": "theses/KO_2025-10-01.md", "last_review_date": OLD_FULL,
                        "last_full_review_date": OLD_FULL}, status="HOLDING")
    db.insert(env.conn, "positions", {"ticker": "KO", "qty": 100, "avg_cost": 50.0, "first_buy_date": "2024-01-02",
                                      "updated_at": db.now_iso()})


HOLD = {"ticker": "KO", "decision_date": ASOF, "path": [{"q": "Q3", "answer": "不適用", "evidence": "無估值"}],
        "action": "HOLD", "sell_category": None, "trim_fraction": None, "stop_adding": True, "observe_until": None,
        "falsified_assumption": None, "external_drivers": [], "external_drivers_change_reason": None, "reason": "持有"}


def test_holding_full_a_stop_retried_and_not_advanced(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": HOLD})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    assert env.engine.run(RUN)["status"] == "done"
    fa = env.by_type("FULL_A")
    assert len(fa) == 2                                     # 修前 1（直接 _stop）
    p2 = json.loads(fa[1]["params"])
    assert p2["hold_stop_retry"] == 1 and any("holding_stop" in f for f in p2["feedback"])
    st, data = _reg(env.conn)
    assert st == "HOLDING" and data["last_full_review_date"] == OLD_FULL   # 修前 ASOF
    assert data["next_full_review_due"] == _plus(ASOF, 91)
    hr = env.by_type("HOLD_REVIEW")
    assert len(hr) == 1 and json.loads(hr[0]["params"])["hold_stop_unresolved"] is True
    stop_log = json.loads(env.conn.execute(
        "SELECT data FROM decisions WHERE json_extract(data,'$.review_type') IS NULL").fetchone()[0])
    assert any("A3-M3B-01" in n for n in stop_log["framework_notes"])


def test_holding_full_a_stop_fixed_on_retry_goes_on(tmp_path):
    fixed = {**full_a(stopped=False), "holding_stop": {"gate": "G3", "reason_code": "REJECT", "reason": "測試"}}
    seq = [full_a(stopped=True), fixed]
    env = make_env(tmp_path, {"FULL_A": lambda job: seq[1 if job.params.get("hold_stop_retry") else 0],
                              "FULL_B": full_b("WATCH"), "HOLD_REVIEW": HOLD})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()][:3] == ["FULL_A", "FULL_A", "FULL_B"]


def test_holding_step0_stop_not_retried(tmp_path):
    a = {**full_a(stopped=True), "stopped_at_gate": "STEP0", "reason_code": "MISSING_DATA"}
    env = make_env(tmp_path, {"FULL_A": a, "HOLD_REVIEW": HOLD})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert len(env.by_type("FULL_A")) == 1


# ---------------------------------------------------------------- A3-M3C-03

@pytest.mark.parametrize("key", ["grade_after", "_grade_after"])
def test_hold_review_grade_after_underscore_key(tmp_path, key):
    env = make_env(tmp_path, {})
    _holding(env)
    W.write_hold_review_result(env.conn, env.paths, "KO", ASOF, "r1", {**HOLD, key: "C"}, {"full_stage": "B"})
    assert _reg(env.conn)[1]["grade"] == "C"            # 修前 _grade_after 靜默丟失（仍 B）


# ---------------------------------------------------------------- A3-M3D-01(1)

def test_trigger_text_masks_valuation_reason():
    p = {"thesis_result": {"verdict": "疑似", "reason": "Goldman upgrades KO to Buy; KO trades at 25x earnings, "
                                                       "analyst sees 30% upside"}}
    blind = deep.trigger_text(p)
    assert "疑似" in blind and "Goldman" not in blind and "25x" not in blind and "30%" not in blind
    ok = deep.trigger_text({"thesis_result": {"verdict": "疑似", "reason": "毛利率連 2 季下滑 3.5 個百分點"}})
    assert "毛利率" in ok and "3.5" not in ok and "〔數值略〕" in ok
    seen = deep.trigger_text(p, price_blind=False)   # FULL_B 看得到價格：不遮
    assert "25x earnings" in seen


# ---------------------------------------------------------------- A3-M3D-02

def _seed_hold(conn):
    db.insert(conn, "positions", {"ticker": "KO", "qty": 100, "avg_cost": 50.0, "first_buy_date": "2024-01-02",
                                  "updated_at": db.now_iso()})
    _set_reg(conn, {"first_buy_date": "2024-01-02", "absurd_price_line": 260.0, "grade": "B"}, status="HOLDING")


def seed_full_card(conn, quality, d="2026-10-01", n=1):
    """最近一次完整分析的訊號卡（p08_final.quality）；給 Q3c 測試用。"""
    did = f"KO-{d.replace('-', '')}-{n}"
    db.insert(conn, "decisions", {"decision_id": did, "ticker": "KO", "decision_date": d, "run_id": "x",
                                  "job_id": None, "signal": "WATCH", "reason_code": "TOO_HARD",
                                  "data": {"action": "WATCH"}, "created_at": db.now_iso()})
    db.insert(conn, "signals", {"decision_id": did, "ticker": "KO", "signal": "WATCH",
                                "data": {"p08_final": {"items_with_data": 5, "passed": 4, "rate": 80.0,
                                                       "quality": quality} if quality else None},
                                "created_at": db.now_iso()})


Q3C = [{"q": "Q3", "answer": "是", "evidence": "V 成立"}, {"q": "Q3c", "answer": "是", "evidence": "頂級複利型"}]


@pytest.mark.parametrize("quality,want", [(None, "HOLD"), ("可", "HOLD"), ("優", "TRIM")])
def test_q3c_requires_p08_quality(tmp_path, quality, want):
    env = make_env(tmp_path, {})
    _seed_hold(env.conn)
    if quality is not None:
        seed_full_card(env.conn, quality)
    tr = {**HOLD, "path": Q3C, "action": "TRIM", "sell_category": "估值", "trim_fraction": 0.25}
    did = W.write_hold_review_result(env.conn, env.paths, "KO", ASOF, "r", tr, price=80.0, iv_base=60.0)
    log = json.loads(env.conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()[0])
    assert log["action"] == want
    if want == "HOLD":
        assert any("A3-M3D-02" in n for n in log["framework_notes"])


# ---------------------------------------------------------------- A3-M2C-06（deep 層）

def test_deep_oe_check_passes_distress_pass(tmp_path):
    from test_fix_P19 import _table_md
    md = _table_md([100.0] * 5 + [100.0, 10.0, 20.0, 200.0, 100.0], [50.0] * 10, [40.0] * 10, [10000.0] * 10,
                   [10.0] * 10)
    td = tmp_path / "KO"
    (td / "dossier").mkdir(parents=True)
    (td / "dossier" / "KO.md").write_text(md, encoding="utf-8")
    (td / "dossier" / "_peers.json").write_text(json.dumps({"shock_years": [2022, 2023, 2024]}), encoding="utf-8")
    job = SimpleNamespace(ticker="KO", params={})
    ni = {"oe_per_share": 10.0, "excluded_shock_years": [2022, 2023, 2024]}
    on = deep._oe_check(job, td, {"normalized_inputs": {**ni, "distress_pass": True}})
    assert "4A 三問全過" in on["mechanical"]["basis"] and on["diffs"] == []
    assert "4A 三問全過" in json.loads((td / "oe_check.json").read_text(encoding="utf-8"))["mechanical"]["basis"]
    off = deep._oe_check(job, td, {"normalized_inputs": {**ni, "distress_pass": "true"}})   # 只認 true
    assert "4A" not in off["mechanical"]["basis"]


# ---------------------------------------------------------------- D93 g_notes 進日誌

def test_oe_g_notes_reach_decision_log(tmp_path, monkeypatch):
    def fake(job, td, pa):
        chk = {"mechanical": {"E": 5.0}, "model": {"g10": 0.07}, "diffs": [],
               "g_notes": ["g10：模型 0.07，機械版 0（只記註不退回，D93）"], "retried": False}
        (td / "oe_check.json").write_text(json.dumps(chk, ensure_ascii=False), encoding="utf-8")
        return chk
    monkeypatch.setattr(deep, "_oe_check", fake)
    env = make_env(tmp_path, {**full_responses(), "FULL_B": full_b("WATCH")})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)
    notes = _last(env.conn)[2]["framework_notes"]
    assert any(n.startswith("D26 機械版業主盈餘對照") and "g_notes" in n and "D93" in n for n in notes)


# ---------------------------------------------------------------- 內部人解析失敗

def test_insiders_failed_parse_marked_missing(tmp_path, monkeypatch):
    from trader.analysis import insiders
    monkeypatch.setattr(insiders, "build_insider_section", lambda *a, **k: {
        "markdown": "\n## 內部人交易\n", "stats": {"filings": 5, "parsed": 3, "failed": 2}, "summary": {}})
    td = tmp_path / "KO"
    (td / "dossier").mkdir(parents=True)
    status: list[str] = []
    deep.DossierHandler()._insiders(SimpleNamespace(ticker="KO", asof=ASOF, params={}), None, "1", td, status)
    assert "缺：內部人申報 2 份解析失敗" in status
    monkeypatch.setattr(insiders, "build_insider_section", lambda *a, **k: {
        "markdown": "x", "stats": {"filings": 5, "parsed": 5, "failed": 0}, "summary": {}})
    status = []
    deep.DossierHandler()._insiders(SimpleNamespace(ticker="KO", asof=ASOF, params={}), None, "1", td, status)
    assert status == []


# ---------------------------------------------------------------- holdings_view _pick

def test_g8_pick_carries_price_to_conservative():
    from trader.portfolio.holdings_view import summarize
    rows = [{"ticker": "PEP", "grade": "A", "expected_return": 0.1, "adjusted_expected_return": 0.09,
             "addable": True, "price_to_conservative": 0.8}]
    watch = [{"ticker": "MCD", "grade": "B", "expected_return": 0.12, "adjusted_expected_return": 0.09,
              "buyable": True, "price_to_conservative": 0.7}]
    s = summarize(rows, 1, watch)
    assert s["best_addable"]["price_to_conservative"] == 0.8 and s["best_watch"]["price_to_conservative"] == 0.7
