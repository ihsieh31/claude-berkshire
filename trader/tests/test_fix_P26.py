"""P26：設計決策 D13（意願權重高／中／低）、D29（FULL_B 只能降級、失效條件只增不刪）、D31（決策欄位取驗證過的摘要）。"""
import json

import pytest

from trader.analysis import writeback as W
from trader.jobs import validate as V
from trader.paths import get_paths
from trader.portfolio import sizing as S

from test_fix_F05 import ASOF, CIN, GATES_OK, _cap, _reg, _td, env  # noqa: F401  (env 為 fixture)
from test_jobs_io import GOOD

SCHEMAS = V.load_schemas(get_paths().schemas_json)
REASON = "G8 本標的勝國庫券約 2pp、確定性等級 B"
PA = {"starting_grade": "A", "downgrades": [{"step": "G5", "rule": "SEL-093", "reason": "資本配置淨分 <0"}],
      "thesis_draft": {"kill_criteria": [{"text": "市占跌破 20%", "due": "2027-06-30"},
                                         {"text": "毛利率連兩年 <30%", "due": "2027-12-31"}]}}


def _fb(**kw):
    o = json.loads(json.dumps(GOOD["FULL_B"]))
    o.update({"signal": "BUY", "reason_code": "PASS_ALL", "stopped_at_gate": None,
              "intended_weight": {"level": "MEDIUM", "reason": REASON}})
    o.update(kw)
    return o


# ---------------------------------------------------------------- validate（D13、D31 _CROSS）

def test_full_b_buy_needs_intended_weight_with_g8_reason():
    assert V.check("FULL_B", _fb(), SCHEMAS)[1] == []
    errs = V.check("FULL_B", _fb(intended_weight={"level": None, "reason": None}), SCHEMAS)[1]
    assert any("intended_weight.level" in e for e in errs)
    errs = V.check("FULL_B", _fb(intended_weight={"level": "HIGH", "reason": "護城河很寬"}), SCHEMAS)[1]
    assert any("intended_weight.reason" in e for e in errs)
    errs = V.check("FULL_B", _fb(intended_weight={"level": "HIGH"}), SCHEMAS)[1]
    assert any("intended_weight.reason" in e for e in errs)
    errs = V.check("FULL_B", _fb(intended_weight={"level": "50%", "reason": REASON}), SCHEMAS)[1]
    assert any("不在允許選項內" in e for e in errs)


def test_full_b_buy_must_pass_all_and_not_stop():
    errs = V.check("FULL_B", _fb(reason_code="TOO_HARD"), SCHEMAS)[1]
    assert any("PASS_ALL" in e for e in errs)
    errs = V.check("FULL_B", _fb(stopped_at_gate="G8"), SCHEMAS)[1]
    assert any("stopped_at_gate" in e for e in errs)


# ---------------------------------------------------------------- validate（D29，ref＝phaseA）

def test_phase_a_grade_counts_downgrades():
    assert V.phase_a_grade(PA) == "B"
    assert V.phase_a_grade({"starting_grade": "B", "downgrades": [1, 2, 3]}) == "D"
    assert V.phase_a_grade({"starting_grade": None}) is None


def test_full_b_grade_cannot_exceed_full_a():
    """A3-M3D-04：高於 FULL_A 結束等級不報錯（報錯兩次整鏈 MISSING_DATA），validate 壓回並記 grade_notes。"""
    out, errs = V.check("FULL_B", _fb(certainty_grade="A"), SCHEMAS, ref=PA)
    assert errs == [] and out["certainty_grade"] == "B" and out["certainty_grade_model"] == "A"
    assert any("高於上限 B" in n for n in out["grade_notes"])
    for g in ("B", "C"):
        out, errs = V.check("FULL_B", _fb(certainty_grade=g), SCHEMAS, ref=PA)
        assert errs == [] and out["certainty_grade"] == g and "grade_notes" not in out
    out, errs = V.check("FULL_B", _fb(certainty_grade="A"), SCHEMAS)   # 沒給 ref：不比
    assert errs == [] and out["certainty_grade"] == "A"


def test_missing_kill_criteria_ignores_whitespace():
    assert V.missing_kill_criteria(PA, ["市占跌破 20% ", "新條件"]) == ["毛利率連兩年 <30%"]
    assert V.missing_kill_criteria(PA, ["市占跌破20%", "毛利率連兩年<30%"]) == []


# ---------------------------------------------------------------- sizing（D13）

@pytest.mark.parametrize("level,grade,want", [("HIGH", "A", 0.15), ("MEDIUM", "B", 0.075), ("LOW", "B", 0.05),
                                              ("HIGH", "C", 0.0)])
def test_target_weight_from_intended_level(level, grade, want):
    card = {"certainty_grade": grade, "intended_weight": {"level": level, "reason": REASON},
            "target_weight_pct": 15}   # 模型自填的目標權重不採用
    assert S.target_weight(card, None)[0] == pytest.approx(want)


def test_target_weight_intended_still_min_with_other_caps():
    card = {"certainty_grade": "A", "intended_weight": {"level": "HIGH"}, "max_weight_pct": {"tail": 8}}
    assert S.target_weight(card, None)[0] == pytest.approx(0.08)


# ---------------------------------------------------------------- writeback（D31、D29、D13）

def _phase_b(td, **kw):
    s = {"signal": "BUY", "reason_code": "PASS_ALL", "certainty_grade": "A",
         "intended_weight": {"level": "MEDIUM", "reason": REASON}}
    s.update(kw)
    (td / "phaseB.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def test_writeback_uses_summary_and_full_a_grade(env, tmp_path):
    conn, paths = env
    cin = dict(CIN, grade="A")
    card = {"signal": "BUY", "reason_code": "PASS_ALL", "certainty_grade": "A", "auto_buy_cap": 60.0,
            "target_weight_pct": 15, "position_plan": {"target_weight_pct": 15, "first_tranche_pct_of_target": 50}}
    td = _td(tmp_path, card=card, cin=cin, cap=_cap(tmp_path, cin), phase_a=PA,
             entry={"kill_criteria_due": [{"text": "毛利率連兩年 <30%", "due": "2027-12-31"},
                                          {"text": "新增：主要客戶流失", "due": "2027-03-31"}]})
    _phase_b(td)
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "BUY", [], gates=GATES_OK)
    status, data = _reg(conn, "AAA")
    sc = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()["data"])
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])
    # D29：後半寫 A，前半結束是 B → 以 B 寫回；目標權重 MEDIUM×B＝7.5%
    assert sc["certainty_grade"] == "B" and data["grade"] == "B"
    assert data["target_weight"] == pytest.approx(0.075)
    assert sc["intended_weight"] == {"level": "MEDIUM", "reason": REASON}
    assert sc["target_weight_pct"] == pytest.approx(7.5) and sc["position_plan"]["target_weight_pct"] == pytest.approx(7.5)
    # 失效條件：FULL_A 全部在前，模型新增的接在後
    assert [k["text"] for k in data["kill_criteria_due"]] == ["市占跌破 20%", "毛利率連兩年 <30%", "新增：主要客戶流失"]
    assert any("漏列 FULL_A 失效條件" in n for n in log["framework_notes"])
    assert any("FULL_A 結束等級" in n for n in log["framework_notes"])


def test_writeback_ignores_card_self_written_intended_weight(env, tmp_path):
    conn, paths = env
    card = {"signal": "WATCH", "reason_code": "TOO_HARD", "certainty_grade": "B", "target_weight_pct": 5,
            "intended_weight": {"level": "HIGH", "reason": "自填"}}
    td = _td(tmp_path, card=card, cin=CIN, cap=_cap(tmp_path))
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [], gates=GATES_OK)
    sc = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()["data"])
    assert "intended_weight" not in sc
    assert _reg(conn, "AAA")[1]["target_weight"] == pytest.approx(0.05)   # 舊卡：沿用 target_weight_pct


def test_writeback_summary_reason_code_wins(env, tmp_path):
    conn, paths = env
    td = _td(tmp_path, card={"signal": "WATCH", "reason_code": "PASS_ALL", "certainty_grade": "B",
                             "target_weight_pct": 5}, cin=CIN, cap=_cap(tmp_path))
    _phase_b(td, signal="WATCH", reason_code="TOO_HARD", certainty_grade="B")
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [], gates=GATES_OK)
    row = conn.execute("SELECT reason_code FROM decisions WHERE decision_id=?", (did,)).fetchone()
    assert row["reason_code"] == "TOO_HARD"


# ---------------------------------------------------------------- 提示文字（D29、D61、M3_D2-09）

def test_prompts_split_and_popstats_wording():
    prompts = get_paths().schemas_json.parent / "prompts"
    b = (prompts / "FULL_B.md").read_text(encoding="utf-8")
    a = (prompts / "FULL_A.md").read_text(encoding="utf-8")
    assert "重跑前半" not in b and "前半不會重跑" in b and "只能再降" in b
    assert "覆寫訊號卡 `max_weight_pct.correlated_group`" not in b and "intended_weight" in b
    assert "P-03／P-04" in a and "MISSING_DATA" in a and "母體統計" in a


# ---------------------------------------------------------------- P26R：理由檢查正規化（D13）、訊號取摘要（D31）

@pytest.mark.parametrize("reason", ["g8 優勢 2pp，確定性 B", "G８ 勝出 2pp、確定性等級 B", "G8 勝出 2pp，B 級",
                                    "G8 勝出 2pp，Ｂ級"])
def test_full_b_intended_reason_normalized(reason):
    assert V.check("FULL_B", _fb(intended_weight={"level": "MEDIUM", "reason": reason}), SCHEMAS)[1] == []


@pytest.mark.parametrize("reason", ["勝國庫券 2pp，B 級", "護城河寬、確定性 A", "G8 勝出 2pp"])
def test_full_b_intended_reason_still_rejects(reason):
    errs = V.check("FULL_B", _fb(intended_weight={"level": "MEDIUM", "reason": reason}), SCHEMAS)[1]
    assert any("intended_weight.reason" in e for e in errs)


def test_writeback_summary_signal_overrides_card(env, tmp_path):
    conn, paths = env
    td = _td(tmp_path, card={"signal": "BUY", "reason_code": "PASS_ALL", "stopped_at_gate": None, "conclusion": "買",
                             "certainty_grade": "B", "auto_buy_cap": 60.0, "target_weight_pct": 5},
             cin=CIN, cap=_cap(tmp_path))
    _phase_b(td, signal="WATCH", reason_code="TOO_HARD", stopped_at_gate="G8", certainty_grade="B",
             intended_weight={"level": None, "reason": None})
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, None, [], gates=GATES_OK)
    sc = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()["data"])
    row = conn.execute("SELECT data, reason_code FROM decisions WHERE decision_id=?", (did,)).fetchone()
    log = json.loads(row["data"])
    assert sc["signal"] == "WATCH" and sc["reason_code"] == "TOO_HARD" == row["reason_code"]
    assert sc["stopped_at_gate"] == "G8" and sc["conclusion"] == "觀察"
    assert log["action"] == "WATCH"
    assert any("signal 以驗證過的摘要為準" in n for n in log["framework_notes"])
    assert _reg(conn, "AAA")[0] == "WATCH"


def test_full_a_prompt_downgrade_rows_are_real():
    a = (get_paths().schemas_json.parent / "prompts" / "FULL_A.md").read_text(encoding="utf-8")
    assert "`downgrades` 每列代表實際降一級" in a and "不列" in a


# ---------------------------------------------------------------- P26R2：摘要 stopped_at_gate 為 null 不抹掉框架閘

def test_writeback_null_summary_gate_keeps_framework_g8(env, tmp_path):
    """deep.post 因 G8 把 BUY 降 WATCH 並在卡上寫 G8；摘要（BUY、閘 null）不得把 G8 抹成 null。"""
    conn, paths = env
    td = _td(tmp_path, card={"signal": "BUY", "reason_code": "PASS_ALL", "stopped_at_gate": "G8",
                             "certainty_grade": "B", "auto_buy_cap": 60.0, "target_weight_pct": 5},
             cin=CIN, cap=_cap(tmp_path))
    _phase_b(td, stopped_at_gate=None, certainty_grade="B")
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [], gates=GATES_OK)
    sc = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()["data"])
    assert sc["stopped_at_gate"] == "G8" and sc["signal"] == "WATCH"


def test_apply_summary_gate_rules():
    def run(card_gate, sum_gate, final, sig="WATCH"):
        card = {"signal": "BUY", "reason_code": "PASS_ALL", "stopped_at_gate": card_gate}
        W._apply_summary(card, {"signal": sig, "stopped_at_gate": sum_gate}, {}, {}, [], final)
        return card["stopped_at_gate"]
    assert run("G8", None, None) == "G8"            # 摘要 null 不覆寫
    assert run(None, "G9", None) == "G9"            # 摘要有值、無框架降級 → 以摘要為準
    assert run(None, "G9", "WATCH") == "G9"         # final_signal 與摘要同 → 以摘要為準
    assert run("G11", "G9", "NO_ACTION") == "G11"   # 框架降成與摘要不同的訊號 → 保留卡上（框架）閘
