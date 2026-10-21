"""RM3-4：複審 M3 修復（jobs/deep.py 等）。

- M3_D2-08（D24）：紅隊修正後仍判致命 → NO_ACTION／RED_LIGHT（G9）：登錄表 WATCH、asof+14 重跑、PRICE 不得升買。
- M3-RS3-02／M3-RS4-02（D30）：非持股 FULL_A 填 holding_stop 而 stopped=false → 改為停止結果，不跑 FULL_B。
- M3-RS6-01：FULL_A 未停止卻把 oe／g10／g5 或 starting_grade 留 null、FULL_B 填了 → 最多 WATCH。
- M3-RS6-03：DOSSIER 摘要寫 _dq.json 前清洗價格字句。
- M3_D3-03／M3-RS3-03（D31）：P-29 尾部上限取已驗證摘要 tail_worst_loss_pct；缺 → 警示並取較嚴者。
- M3-RS2-04：HOLD_REVIEW 提示與程式一致（不叫模型做 G8 數量減碼、Q1 制度類減碼由框架產生）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from trader.analysis import writeback as W
from trader.jobs import deep
from trader.paths import get_paths

from test_engine import ASOF, RUN, full_a, full_b, full_responses, make_env, red_team, dossier_ok


def _decisions(env):
    return [tuple(r) for r in env.conn.execute("SELECT signal, reason_code FROM decisions WHERE ticker='KO' "
                                               "ORDER BY rowid").fetchall()]


def _reg(env):
    r = env.conn.execute("SELECT status, data FROM registry WHERE ticker='KO'").fetchone()
    return r["status"], json.loads(r["data"])


def _last_card(env):
    return json.loads(env.conn.execute("SELECT data FROM signals WHERE ticker='KO' ORDER BY rowid DESC").fetchone()[0])


def _types(env):
    return [(r["job_type"], r["status"]) for r in env.rows()]


# ---------------------------------------------------------------- M3_D2-08：紅隊致命 → RED_LIGHT

def test_red_team_fatal_after_fix_is_red_light(tmp_path):
    env = make_env(tmp_path, {**full_responses(), "RED_TEAM": red_team(True)})   # 兩輪都 fatal
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)

    rt = env.by_type("RED_TEAM")
    assert len(rt) == 2 and json.loads(rt[-1]["result"])["final_signal"] == "NO_ACTION"
    assert _decisions(env) == [("NO_ACTION", "RED_LIGHT")]
    card = _last_card(env)
    assert (card["signal"], card["reason_code"], card["stopped_at_gate"]) == ("NO_ACTION", "RED_LIGHT", "G9")
    status, data = _reg(env)
    assert status == "WATCH"   # 延後不是淘汰：留觀察名單，不進 X5
    assert data["last_reason_code"] == "RED_LIGHT"
    assert data["next_full_review_due"] == (date.fromisoformat(ASOF) + timedelta(days=14)).isoformat()

    # 之後 PRICE 快速通道不得升成買進（D24）
    did = W.write_price_result(env.conn, env.paths, "KO", "2026-10-30", "r2", action="BUY", gate1=True,
                               gate2="通過", gate3="通過", cap={"auto_buy_cap": 70.0, "IV_base": 80.0,
                                                               "grade_used": "B"},
                               price=50.0, price_date="2026-10-30", t30=0.047, reason="", notes=[])
    sig = env.conn.execute("SELECT signal FROM decisions WHERE decision_id=?", (did,)).fetchone()[0]
    assert sig not in ("BUY", "ADD")


def test_red_team_number_errors_after_fix_stays_watch(tmp_path):
    rt = {**red_team(False), "number_errors": [{"where": "估值段", "claimed": "IV_base 90", "correct": "IV_base 80",
                                                    "source": "cap_result.json"}], "verdict": "退回"}
    env = make_env(tmp_path, {**full_responses(), "RED_TEAM": rt})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)

    assert json.loads(env.by_type("RED_TEAM")[-1]["result"])["final_signal"] == "WATCH"
    assert _decisions(env) == [("WATCH", "PASS_ALL")]
    assert _reg(env)[1].get("next_full_review_due") != (date.fromisoformat(ASOF) + timedelta(days=14)).isoformat()


# ---------------------------------------------------------------- M3-RS3-02／RS4-02：非持股 holding_stop

def test_non_holding_holding_stop_becomes_stop(tmp_path):
    a = {**full_a(), "holding_stop": {"gate": "G3", "reason_code": "REJECT", "reason": "護城河三測不到 2 強"},
         "rule_ids": ["SEL-061"]}
    env = make_env(tmp_path, {"FULL_A": a, "FULL_B": full_b("WATCH")})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)

    assert _types(env) == [("FULL_A", "done")]   # 不跑 FULL_B
    assert _decisions(env) == [("NO_ACTION", "REJECT")]
    assert _reg(env)[0] == "REJECT"
    log = json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()[0])
    assert any("非持股不適用持股分支" in n for n in log.get("framework_notes") or [])


def test_non_holding_holding_stop_missing_rule_ids_retries_once(tmp_path):
    a = {**full_a(), "holding_stop": {"gate": "G3", "reason_code": "REJECT", "reason": "x"}}   # rule_ids []
    env = make_env(tmp_path, {"FULL_A": a})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)

    assert _types(env) == [("FULL_A", "done"), ("FULL_A", "done")]
    fb = json.loads(env.by_type("FULL_A")[1]["params"])["feedback"]
    assert any("is_holding＝否" in x for x in fb)
    assert _decisions(env) == [("NO_ACTION", "REJECT")]


def test_holding_holding_stop_still_runs_full_b(tmp_path):
    a = {**full_a(), "holding_stop": {"gate": "G3", "reason_code": "REJECT", "reason": "x"}}
    env = make_env(tmp_path, {"FULL_A": a, "FULL_B": full_b("BUY"), "HOLD_REVIEW": {}})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1, params={"holding": True}))
    env.engine.run(RUN)
    assert [t for t, _ in _types(env)][:2] == ["FULL_A", "FULL_B"]   # D30 持股分支不變


# ---------------------------------------------------------------- M3-RS6-01：前半 null、後半有數字

@pytest.mark.parametrize("patch", [
    {"normalized_inputs_null": ("oe_per_share", "g10", "g5")},
    {"normalized_inputs_null": ("g10",)},
    {"starting_grade": None},
])
def test_phase_a_null_inputs_cap_at_watch(tmp_path, patch):
    a = full_a()
    for k in patch.get("normalized_inputs_null", ()):
        a["normalized_inputs"] = {**a["normalized_inputs"], k: None}
    if "starting_grade" in patch:
        a["starting_grade"] = patch["starting_grade"]
    env = make_env(tmp_path, {**full_responses(), "FULL_A": a})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)

    assert "RED_TEAM" not in [t for t, _ in _types(env)]
    assert _decisions(env)[-1][0] == "WATCH"
    assert _last_card(env)["stopped_at_gate"] == "G7"
    log = json.loads(env.conn.execute("SELECT data FROM decisions ORDER BY rowid DESC").fetchone()[0])
    assert any("前半價格盲欄位為 null" in n for n in log["framework_notes"])


def test_blind_gaps_ignores_stopped_and_both_null(tmp_path):
    (tmp_path / "cap_input.json").write_text(json.dumps({"oe": 5.0, "g10": None, "g5": 0.05}), encoding="utf-8")
    pa = {"stopped": False, "starting_grade": "B", "normalized_inputs": {"oe_per_share": 5.0, "g10": None, "g5": 0.05}}
    assert deep._phase_a_blind_gaps(pa, tmp_path, {"certainty_grade": "B"}, {}) == []
    assert deep._phase_a_blind_gaps({**pa, "holding_stop": {"gate": "G3"}, "normalized_inputs": {}}, tmp_path,
                                    {}, {}) == []


# ---------------------------------------------------------------- M3-RS6-03：_dq.json 清洗

def test_dossier_dq_json_is_sanitized(tmp_path):
    dq = {**dossier_ok(),
          "dq_checks": [{"warning": "W1", "result": "數字有誤：正確值見 10-K。當時股價 61.2 美元、市值 120 億。"}],
          "missing": ["Market value determined using the closing market price of $195.09 per share of common stock."]}
    env = make_env(tmp_path, {**full_responses(), "DOSSIER": dq})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)

    text = (env.paths.work / RUN / "KO" / "dossier" / "KO_dq.json").read_text(encoding="utf-8")
    assert "61.2" not in text and "195.09" not in text
    out = json.loads(text)
    assert out["framework_price_lines_removed"] >= 1
    assert out["dq_checks"][0]["warning"] == "W1"


def test_sanitize_dq_keeps_p58_prefix():
    out, _ = deep._sanitize_dq({"missing": ["P-58：浮存金成本湊不滿 5 年"], "dq_checks": []})
    assert out["missing"][0].startswith("P-58")


# ---------------------------------------------------------------- M3-RS3-03：P-29 尾部上限

def test_p29_from_summary_overrides_model():
    card = {"max_weight_pct": {"tail": 50, "single_event": 7.0}}
    notes: list[str] = []
    deep._p29_writeback(card, {"tail_worst_loss_pct": 60}, {"asset_class": "非金融"}, notes)
    assert card["max_weight_pct"]["tail"] == pytest.approx(16.6667, abs=1e-3)
    assert card["max_weight_pct"]["single_event"] == 7.0
    card = {"max_weight_pct": {}}
    deep._p29_writeback(card, {"tail_worst_loss_pct": 10}, {}, [])   # 下限 25%
    assert card["max_weight_pct"]["tail"] == pytest.approx(40.0)


@pytest.mark.parametrize("summary, model, asset, want", [
    ({}, None, "非金融", 33.3333),            # 缺：P-32 非金融 30% 口徑
    ({"tail_worst_loss_pct": "約 60%"}, 50, "非金融", 33.3333),   # 非數字、模型值較寬 → 取較嚴
    ({}, 12.0, "非金融", 12.0),               # 模型值較嚴 → 保留
    ({}, None, None, 20.0),                   # 缺 asset_class 視為金融 50%
])
def test_p29_missing_warns_and_takes_stricter(summary, model, asset, want):
    card = {"max_weight_pct": {"tail": model}}
    notes: list[str] = []
    deep._p29_writeback(card, summary, {"asset_class": asset}, notes)
    assert card["max_weight_pct"]["tail"] == pytest.approx(want, abs=1e-3)
    assert any(n.startswith("警示：P-29") for n in notes)


def test_p29_engine_card_uses_summary(tmp_path):
    env = make_env(tmp_path, {**full_responses(), "FULL_B": {**full_b("BUY"), "tail_worst_loss_pct": 50}})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)
    assert _last_card(env)["max_weight_pct"]["tail"] == pytest.approx(20.0)


# ---------------------------------------------------------------- M3-RS2-04：HOLD_REVIEW 提示

def test_hold_review_prompt_matches_framework():
    text = (get_paths().routing_json.parent / "prompts" / "HOLD_REVIEW.md").read_text(encoding="utf-8")
    assert "由框架計算數量" not in text
    assert "數量減到 G8 勝出的權重為止" not in text
    assert "不自動執行" in text and "不填 sell_category＝制度" in text


def test_docs_red_light():
    root = get_paths().routing_json.parent
    nxt = json.loads(root.joinpath("model_routing.json").read_text(encoding="utf-8"))
    assert "RED_LIGHT" in json.dumps(nxt, ensure_ascii=False)
    skill = (get_paths().skill_deep / "SKILL.md").read_text(encoding="utf-8")
    assert "紅隊複核不過或沒跑完" not in skill
