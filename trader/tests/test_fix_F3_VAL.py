"""F3-VAL：V3-1 的 FG-M3-validate、FG-M3-routing-docs、FG-M1-docs。
程式項（validate）修前失敗、修後通過；文件項以文字檢查鎖住。"""
import copy
import json
import re
from pathlib import Path

from trader.jobs import validate as V

from test_prompt_contract import ASOF, SAMPLES

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = V.load_schemas(REPO / "routing" / "output_schemas.json")
ROUTING = json.loads((REPO / "routing" / "model_routing.json").read_text(encoding="utf-8"))
PROMPTS = REPO / "routing" / "prompts"
SCREEN = REPO / "berkshire-screen-merged"
EXPECT = {"ticker": "KO", "decision_date": ASOF}
REASON = "G8 本標的勝國庫券約 2pp、確定性等級 B"


def _fa(**kw):
    o = copy.deepcopy(SAMPLES["FULL_A"])
    o.update(kw)
    return o


def _fb(**kw):
    o = copy.deepcopy(SAMPLES["FULL_B"])
    o.update({"signal": "BUY", "reason_code": "PASS_ALL", "stopped_at_gate": None, "certainty_grade": "A",
              "intended_weight": {"level": "MEDIUM", "reason": REASON}})
    o.update(kw)
    return o


def _errs(jt, o, **kw):
    return V.check(jt, o, SCHEMAS, **kw)[1]


# ---------------------------------------------------------------- A3-M2B-08 上限型降級 max_grade

def test_phase_a_grade_max_grade_cap():
    # A 級＋盈餘不穩定（最高 C）→ C（舊版只降一級到 B，FULL_B 仍可 BUY）
    pa = {"starting_grade": "A", "downgrades": [{"driver_id": "循環性", "max_grade": "C"}]}
    assert V.phase_a_grade(pa) == "C"
    # 上限型列不另逐級降：A＋資本配置（降一級）＋優勢在人（最高 B）→ B
    pa = {"starting_grade": "A", "downgrades": [{"driver_id": "資本配置"}, {"driver_id": "優勢在人", "max_grade": "B"}]}
    assert V.phase_a_grade(pa) == "B"
    # 逐級降得比上限更低時取較嚴
    pa = {"starting_grade": "B", "downgrades": [{"driver_id": "資本配置"}, {"driver_id": "誘因"},
                                                {"driver_id": "優勢在人", "max_grade": "B"}]}
    assert V.phase_a_grade(pa) == "D"
    # 沒有 max_grade 的舊卡照舊逐級
    assert V.phase_a_grade({"starting_grade": "A", "downgrades": [{"driver_id": "x"}]}) == "B"


def test_full_a_max_grade_enum_checked():
    bad = _fa(downgrades=[{"step": "G3", "rule": "P-10", "driver_id": "循環性", "reason": "x", "max_grade": "最高C"}])
    assert any("max_grade" in e for e in _errs("FULL_A", bad, expect=EXPECT))
    ok = _fa(downgrades=[{"step": "G3", "rule": "P-10", "driver_id": "循環性", "reason": "x", "max_grade": "C"}])
    assert _errs("FULL_A", ok, expect=EXPECT) == []


def test_max_grade_documented():
    row = SCHEMAS["FULL_A"]["downgrades"][0]
    assert "_max_grade" in row and "max_grade" not in row   # 選填說明鍵
    assert "max_grade" in (PROMPTS / "FULL_A.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- A3-M2C-01 看價後必降（P-26a、P-08 最終）

PA_A = {"starting_grade": "A", "downgrades": []}


def test_full_b_must_downgrade_on_negative_capalloc():
    out, errs = V.check("FULL_B", _fb(capital_allocation_net_after_price=-3), SCHEMAS, ref=PA_A)
    assert errs == [] and out["certainty_grade"] == "B" and out["certainty_grade_model"] == "A"
    assert any("P-26a" in n for n in out["grade_notes"])
    # 前半已因資本配置降過（D18）→ 不重複降：上限＝前半結束等級 B
    pa = {"starting_grade": "A", "downgrades": [{"driver_id": "資本配置"}]}
    out, _ = V.check("FULL_B", _fb(certainty_grade="B", capital_allocation_net_after_price=-3), SCHEMAS, ref=pa)
    assert out["certainty_grade"] == "B" and "grade_notes" not in out
    # 淨分 ≥0 或 null → 不降
    for v in (0, 2, None):
        out, _ = V.check("FULL_B", _fb(capital_allocation_net_after_price=v), SCHEMAS, ref=PA_A)
        assert out["certainty_grade"] == "A"
    # 模型已自行降級 → 不再動
    out, _ = V.check("FULL_B", _fb(certainty_grade="B", capital_allocation_net_after_price=-3), SCHEMAS, ref=PA_A)
    assert out["certainty_grade"] == "B" and "grade_notes" not in out


def test_full_b_must_downgrade_on_p08_final():
    card = {"p08_final": {"items_with_data": 5, "passed": 1, "rate": 20, "quality": "降級"}}
    assert V.after_price_grade_cap(PA_A, {}, card)[0] == "B"
    assert V.after_price_grade_cap({"starting_grade": "A", "downgrades": [{"driver_id": "品質加分"}]}, {}, card)[0] == "B"
    assert V.after_price_grade_cap(PA_A, {}, {"p08_final": {"quality": "可"}})[0] == "A"
    # 兩個獨立驅動因素各降一級（D18）
    assert V.after_price_grade_cap(PA_A, {"capital_allocation_net_after_price": -1}, card)[0] == "C"


# ---------------------------------------------------------------- A3-M3A-02／A3-M3C-07 reason_code 與 signal

def test_full_b_watch_needs_pass_all():
    for code in ("REJECT", "TOO_HARD", "MISSING_DATA", "RED_LIGHT"):
        errs = _errs("FULL_B", _fb(signal="WATCH", reason_code=code, stopped_at_gate="G9"))
        assert any("WATCH" in e and "PASS_ALL" in e for e in errs), code
    assert _errs("FULL_B", _fb(signal="WATCH", reason_code="PASS_ALL", stopped_at_gate="G9")) == []


def test_full_b_no_action_needs_stop_code():
    errs = _errs("FULL_B", _fb(signal="NO_ACTION", reason_code="PASS_ALL", stopped_at_gate="G7"))
    assert any("NO_ACTION" in e for e in errs)
    assert _errs("FULL_B", _fb(signal="NO_ACTION", reason_code="REJECT", stopped_at_gate="G7")) == []


def test_full_b_prompt_watch_g9_uses_pass_all():
    t = (PROMPTS / "FULL_B.md").read_text(encoding="utf-8")
    line = next(x for x in t.splitlines() if "⑤ 不成立" in x)
    assert "PASS_ALL" in line
    assert "只配 NO_ACTION" in t


# ---------------------------------------------------------------- A3-M3A-04 driver_id 必填

def test_full_a_requires_driver_id():
    for d in ("", " ", "\u3000", None):
        row = {"step": "G3", "rule": "P-10", "driver_id": d, "reason": "x"}
        errs = _errs("FULL_A", _fa(downgrades=[row]), expect=EXPECT)
        assert any("driver_id" in e and "非空" in e for e in errs), repr(d)
    # NFKC 後比重複：全形與半形同一個 driver
    rows = [{"step": "G3", "rule": "P-10", "driver_id": "ＲＯＥ", "reason": "x"},
            {"step": "G4", "rule": "P-11", "driver_id": "ROE ", "reason": "y"}]
    assert any("重複" in e for e in _errs("FULL_A", _fa(downgrades=rows), expect=EXPECT))


# ---------------------------------------------------------------- A3-M3A-05 未用的字串規則加註

def test_unused_string_rules_annotated():
    src = Path(V.__file__).read_text(encoding="utf-8")
    assert re.search(r"# 5\. decision_id 格式：目前無 schema 使用", src)
    assert re.search(r'if base == "小數或 0":\s+# 目前無 schema 使用', src)
    txt = json.dumps(SCHEMAS, ensure_ascii=False)
    assert "{YYYYMMDD}" not in txt and "小數或 0" not in txt


# ---------------------------------------------------------------- A3-M3B-04／A3-M3C-04 holding_stop 型別與列舉

def test_full_a_holding_stop_must_be_dict_with_enums():
    assert any("holding_stop" in e for e in _errs("FULL_A", _fa(holding_stop="G3 不過"), expect=EXPECT))
    bad_gate = {"gate": "G9", "reason_code": "REJECT", "reason": "x"}
    assert any("holding_stop.gate" in e for e in _errs("FULL_A", _fa(holding_stop=bad_gate), expect=EXPECT))
    bad_code = {"gate": "G3", "reason_code": "PASS_ALL", "reason": "x"}
    assert any("holding_stop.reason_code" in e for e in _errs("FULL_A", _fa(holding_stop=bad_code), expect=EXPECT))
    for ok in (None, "null", {}, {"gate": "G3", "reason_code": "REJECT", "reason": "護城河弱"},
               {"gate": "產業", "reason_code": "TOO_HARD", "reason": "x"}):
        assert _errs("FULL_A", _fa(holding_stop=ok), expect=EXPECT) == [], ok


# ---------------------------------------------------------------- A3-M3C-05 期限須晚於決策日

def test_kill_due_must_be_after_decision():
    kc = [{"text": "營收連兩年衰退", "due": "2020-01-01"}, {"text": "淨利率跌破 5%", "due": ASOF}]
    errs = _errs("FULL_A", _fa(thesis_draft={"one_liner": "x", "key_assumptions": ["a", "b", "c"], "kill_criteria": kc}),
                 expect=EXPECT)
    assert sum("須晚於決策日" in e for e in errs) == 2
    assert _errs("FULL_A", _fa(), expect=EXPECT) == []
    # 停止時不檢查（不得因此落成 MISSING_DATA）
    stopped = _fa(stopped=True, stopped_at_gate="G1", reason_code="TOO_HARD", rule_ids=["SEL-051"],
                  thesis_draft={"one_liner": "", "key_assumptions": [], "kill_criteria": kc})
    assert _errs("FULL_A", stopped, expect=EXPECT) == []


def test_thesis_next_due_must_be_after_decision():
    def th(nd):
        o = copy.deepcopy(SAMPLES["THESIS"])
        o["kill_criteria_status"][0]["next_due"] = nd
        return o
    assert any("next_due" in e for e in _errs("THESIS", th("2020-01-01"), expect=EXPECT))
    assert any("next_due" in e for e in _errs("THESIS", th(ASOF), expect=EXPECT))
    assert any("next_due" in e for e in _errs("THESIS", th("下季"), expect=EXPECT))
    for ok in ("2027-03-31", None, "null"):
        assert _errs("THESIS", th(ok), expect=EXPECT) == [], ok
    assert "須晚於決策日" in (PROMPTS / "THESIS.md").read_text(encoding="utf-8")
    assert "須晚於決策日" in (PROMPTS / "FULL_A.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- A3-M3D-04 升級不報錯、壓回

def test_full_b_upgrade_capped_not_error():
    pa = {"starting_grade": "A", "downgrades": [{"driver_id": "資本配置"}]}
    out, errs = V.check("FULL_B", _fb(certainty_grade="A"), SCHEMAS, ref=pa)
    assert errs == []
    assert out["certainty_grade"] == "B" and out["certainty_grade_model"] == "A" and out["grade_notes"]
    assert "壓回" in (PROMPTS / "FULL_B.md").read_text(encoding="utf-8").splitlines()[2]


# ---------------------------------------------------------------- A3-M3B-03 框架不採用的欄改選填

def test_unadopted_fields_optional():
    for k in ("decision_id", "target_weight", "first_tranche_weight"):
        assert k not in SCHEMAS["FULL_B"] and f"_{k}" in SCHEMAS["FULL_B"]
    o = _fb()
    for k in ("decision_id", "target_weight", "first_tranche_weight"):
        o.pop(k, None)
    assert _errs("FULL_B", o) == []
    p = copy.deepcopy(SAMPLES["PRICE"])
    p.pop("price", None)
    assert "price" not in SCHEMAS["PRICE"] and "_price" in SCHEMAS["PRICE"]
    assert _errs("PRICE", p) == []


# ---------------------------------------------------------------- A3-M3C-08 routing 文件

def test_routing_docs_match_handlers():
    jobs = ROUTING["jobs"]
    assert jobs["PRICE"]["batch"].startswith("1")
    assert "3–5" not in jobs["PRICE"]["batch"]
    price_in = " ".join(jobs["PRICE"]["inputs"])
    for n in ("cap_result.json", "last_signal_card.json", "registry_entry.json", "portfolio_state.json", "thesis/"):
        assert n in price_in, n
    assert "rates_stale" in jobs["PRICE"]["escalate"]
    assert "_dq.json" in " ".join(jobs["FULL_A"]["inputs"])
    fb_in = " ".join(jobs["FULL_B"]["inputs"])
    for n in ("holdings_g8.json", "gate3_answers.json", "_dq.json", "previous/"):
        assert n in fb_in, n
    rt_in = " ".join(jobs["RED_TEAM"]["inputs"])
    for n in ("phaseA.json", "cap_result.json", "_price.json"):
        assert n in rt_in, n
    hr_in = " ".join(jobs["HOLD_REVIEW"]["inputs"])
    for n in ("red_team.json", "holdings_g8.json", "last_full_signal_card.json", "_dq.json"):
        assert n in hr_in, n
    readme = (REPO / "routing" / "README.md").read_text(encoding="utf-8")
    assert "rates_stale" in readme and "上次訊號卡等級與登錄表不同" in readme
    hr = (PROMPTS / "HOLD_REVIEW.md").read_text(encoding="utf-8").splitlines()[4]
    assert "論點檢查判證偽但框架核對未過" in hr


# ---------------------------------------------------------------- A3-M1B-05、A3-M1C-10（main 已修，鎖住）

def test_screen_docs_references_fixed():
    head = (SCREEN / "scripts" / "screen.py").read_text(encoding="utf-8").splitlines()[13]
    assert "evidence_statistical.md" in head and "evidence_structural.md" in head
    assert sorted(p.name for p in (SCREEN / "references").glob("*.md")) == [
        "data_sources.md", "evidence_statistical.md", "evidence_structural.md"]
    ds = (SCREEN / "references" / "data_sources.md").read_text(encoding="utf-8")
    assert "InterestExpenseOperating" in ds and "InterestExpenseLongTermDebt" in ds and "刻意不收" in ds
    assert "procedure/08_data_requirements.md" in ds
    for p in SCREEN.rglob("*.md"):
        t = p.read_text(encoding="utf-8")
        assert "01_rules_and_sources" not in t and "berkshire-deep-analysis" not in t and "evidence.md" not in t, p
    es = (SCREEN / "references" / "evidence_statistical.md").read_text(encoding="utf-8")
    assert "研究原型" in es.split("## 五、")[1][:200] and "研究原型" in es.split("## 七、")[1][:200]
    assert "REIT" in es and "ROA < 3%" in es


def test_skill_special_watch_documented():
    t = (SCREEN / "SKILL.md").read_text(encoding="utf-8")
    sec5 = t.split("## 5.")[1].split("## 6.")[0]
    assert "special_watch" in sec5 and "只供人工觀察" in sec5 and "不得交給深度分析前半" in sec5
