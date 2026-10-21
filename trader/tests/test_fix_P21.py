"""P21（M2-B4）：閘門 3 時點與降級＋設計決策 D18、D24。

M2_D6_05／M2-D8-02：證據日期嚴格 YYYY-MM-DD、決策日以框架 asof 為準。
M2_D9_09＋D18：宿主模式（小資金）M6＝良好不降級；降級依 driver_id 去重，validate 擋重複。
M2-D10-04：開價格檔時間留存在分析目錄，路徑寫進決策日誌。
D24：問卷不過 → 最多 WATCH（GATE3_FAIL）；停止級答案 → REJECT／TOO_HARD、不給 auto_buy_cap；「不知道」→ TOO_HARD；
     PRICE 遇 GATE3_FAIL／RED_LIGHT 不得升成買進。附帶：FULL_B stopped_at_gate 可填 4A（D71）。
"""
import copy
import json

from trader.analysis import writeback as W
from trader.analysis.checks import _skill_module, run_gate3
from trader.jobs import validate as V
from trader.paths import get_paths

import test_engine as TE
from test_checks import _passing_answers
from test_fix_F05 import ASOF, GATES_OK, _card, _log, _reg, _td, env  # noqa: F401  (env 為 fixture)
from test_jobs_io import GOOD

G3 = _skill_module("gate3_check")
SCHEMAS = V.load_schemas(get_paths().schemas_json)


def _ans(**answers):
    a = _passing_answers()
    a["decision_date"] = ASOF
    for q, v in answers.items():
        a["answers"][q]["answer"] = v
    return a


# ---------------------------------------------------------------- M2_D6_05／M2-D8-02

def test_gate3_strict_evidence_dates():
    assert G3.evaluate(_ans(), asof=ASOF)["gate3_pass"] is True
    for bad in ("10/20/2026", "2026", " 2027-01-01", "20260930", "2026-02-30"):
        a = _ans()
        a["answers"]["C2"]["evidence"][0]["date"] = bad
        r = G3.evaluate(a, asof=ASOF)
        assert r["gate3_pass"] is False, bad
        assert any(x.startswith("C2 證據日期") for x in r["failures"]), (bad, r["failures"])
    a = _ans()
    a["answers"]["C2"]["evidence"][0]["date"] = " 2026-09-30 "   # 空白去掉後合法且早於決策日
    assert G3.evaluate(a, asof=ASOF)["gate3_pass"] is True


def test_gate3_decision_date_must_equal_asof(tmp_path):
    a = _ans()
    a["decision_date"] = "2027-12-31"   # 作答檔自填較晚的決策日：不得放行前視證據
    r = G3.evaluate(a, asof=ASOF)
    assert r["gate3_pass"] is False and any("框架決策日" in x for x in r["failures"])
    a["decision_date"] = " " + ASOF
    assert G3.evaluate(a, asof=ASOF)["gate3_pass"] is True
    p = tmp_path / "a.json"
    a["decision_date"] = "2026-10-20"
    p.write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")
    assert run_gate3(p)["gate3_pass"] is True             # 無 asof：沿用作答檔（相容）
    assert run_gate3(p, asof=ASOF)["gate3_pass"] is False   # 框架給 asof：以它為準


def test_full_a_post_passes_job_asof_to_gate3(tmp_path):
    a = TE.full_a()
    ans = json.loads(a["files"]["KO/gate3_answers.json"])
    ans["decision_date"] = "2027-01-01"
    a["files"] = dict(a["files"])
    a["files"]["KO/gate3_answers.json"] = json.dumps(ans, ensure_ascii=False)
    env = TE.make_env(tmp_path, {"DOSSIER": TE.dossier_ok(), "FULL_A": a, "FULL_B": TE.full_b("BUY"),
                                 "RED_TEAM": TE.red_team(False)})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(TE.RUN)
    g3 = json.loads((env.paths.work / TE.RUN / "KO" / "gate3_result.json").read_text(encoding="utf-8"))
    assert g3["gate3_pass"] is False and any("框架決策日" in x for x in g3["failures"])
    dec = env.conn.execute("SELECT signal, reason_code FROM decisions").fetchall()
    assert [(r["signal"], r["reason_code"]) for r in dec] == [("WATCH", "GATE3_FAIL")]


# ---------------------------------------------------------------- M2_D9_09＋D18

def test_m6_good_does_not_lower_grade_in_host_mode():
    r = G3.evaluate(_ans(M6="良好"), asof=ASOF)
    assert r["gate3_pass"] is True and r["max_grade_from_C1"] == "A"
    q = json.loads((get_paths().skill_deep / "assets" / "gate3_questionnaire.json").read_text(encoding="utf-8"))
    assert "只適用非小資金模式" in q["grade_link"] and "D18" in q["grade_link"]


def _pa(downgrades):
    a = copy.deepcopy(GOOD["FULL_A"])
    a["downgrades"] = downgrades
    return a


def test_downgrades_same_driver_rejected_and_counted_once():
    row = {"step": "G3", "rule": "P-02", "driver_id": "循環性", "reason": "景氣谷底虧損"}
    ok, errs = V.check("FULL_A", _pa([row]), SCHEMAS)
    assert errs == []
    _, errs = V.check("FULL_A", _pa([row, {**row, "rule": "P-06"}]), SCHEMAS)
    assert any("driver_id" in e and "循環性" in e for e in errs)
    _, errs = V.check("FULL_A", _pa([{"step": "G3", "rule": "P-02", "reason": "x"}]), SCHEMAS)
    assert any("driver_id：缺少必填鍵" in e for e in errs)
    # 等級計算：同 driver 只降一次；沒有 driver_id 的舊列各算一次
    pa = {"starting_grade": "A", "downgrades": [row, {**row, "rule": "P-06"}, {**row, "driver_id": "金融業別"}]}
    assert V.phase_a_grade(pa) == "C"
    assert V.phase_a_grade({"starting_grade": "B", "downgrades": [1, 2]}) == "D"


# ---------------------------------------------------------------- D71 附帶：FULL_B 可停在 4A

def test_full_b_stopped_at_4a_allowed():
    fb = copy.deepcopy(GOOD["FULL_B"])
    fb.update({"signal": "NO_ACTION", "reason_code": "REJECT", "stopped_at_gate": "4A"})
    assert not [e for e in V.check("FULL_B", fb, SCHEMAS)[1] if "stopped_at_gate" in e]
    fb["stopped_at_gate"] = "G6"
    assert [e for e in V.check("FULL_B", fb, SCHEMAS)[1] if "stopped_at_gate" in e]
    t = (get_paths().schemas_json.parent / "prompts" / "FULL_B.md").read_text(encoding="utf-8")
    assert "stopped_at_gate 填 4A" in t and "stopped_at_gate 填 G7" not in t


# ---------------------------------------------------------------- D24：回寫對帳

def _g3_td(tmp_path, answers, passed, name="td"):
    d = _td(tmp_path, name=name, cap={"auto_buy_cap": 55.0, "IV_base": 70.0, "grade_used": "B"},
            cin={"norm_eps": 5.0})
    (d / "gate3_answers.json").write_text(json.dumps({"answers": {q: {"answer": v} for q, v in answers.items()}},
                                                     ensure_ascii=False), encoding="utf-8")
    (d / "gate3_result.json").write_text(json.dumps({"gate3_pass": passed}), encoding="utf-8")
    return d


G3_FAIL = {**GATES_OK, "gate3": False, "gate3_failures": ["C2 缺證據"]}


def test_gate3_fail_is_watch_with_gate3_fail_code(env, tmp_path):
    conn, paths = env
    td = _g3_td(tmp_path, {"C1": "10年寫得出", "M5": "持平", "M6": "偉大"}, False)
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [], gates=G3_FAIL)
    card = _card(conn, did)
    assert (card["signal"], card["reason_code"]) == ("WATCH", "GATE3_FAIL")
    assert conn.execute("SELECT reason_code FROM decisions WHERE decision_id=?", (did,)).fetchone()[0] == "GATE3_FAIL"
    assert _reg(conn, "AAA")[0] == "WATCH"


def test_gate3_stop_answer_overrides_full_a_pass(env, tmp_path):
    conn, paths = env
    td = _g3_td(tmp_path, {"C1": "寫不到5年", "M5": "變窄", "M6": "良好"}, False)
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [], gates=G3_FAIL)
    card = _card(conn, did)
    assert (card["signal"], card["reason_code"], card["stopped_at_gate"]) == ("NO_ACTION", "REJECT", "G3")
    assert card["auto_buy_cap"] is None
    status, data = _reg(conn, "AAA")
    assert status == "REJECT" and data["auto_buy_cap"] is None
    assert any("前後不一致" in n for n in _log(conn, did)["framework_notes"])


def test_gate3_unknown_is_too_hard(env, tmp_path):
    conn, paths = env
    td = _g3_td(tmp_path, {"C1": "10年寫得出", "C3": "不知道", "M5": "持平"}, False)
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [], gates=G3_FAIL)
    card = _card(conn, did)
    assert (card["signal"], card["reason_code"], card["stopped_at_gate"]) == ("NO_ACTION", "TOO_HARD", "G1")
    assert _reg(conn, "AAA")[0] == "TOO_HARD"


def test_gate3_pass_untouched(env, tmp_path):
    conn, paths = env
    td = _g3_td(tmp_path, {"C1": "寫不到5年"}, True)   # 框架判通過時不對帳（gates.gate3 True）
    did = W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "BUY", [], gates=GATES_OK)
    assert (_card(conn, did)["signal"], _card(conn, did)["reason_code"]) == ("BUY", "PASS_ALL")


def _price(conn, paths, action):
    return W.write_price_result(conn, paths, "AAA", "2026-10-20", "r2", action=action, gate1=True, gate2="通過",
                                gate3="通過", cap={"auto_buy_cap": 70.0, "IV_base": 80.0, "grade_used": "B"},
                                price=50.0, price_date="2026-10-20", t30=0.047, reason="", notes=[])


def test_price_cannot_buy_after_gate3_fail_or_red_light(env, tmp_path):
    conn, paths = env
    td = _g3_td(tmp_path, {"C1": "10年寫得出"}, False)
    W.write_full_result(conn, paths, "AAA", ASOF, "r", td, "WATCH", [], gates=G3_FAIL)
    did = _price(conn, paths, "BUY")
    card = _card(conn, did)
    assert card["signal"] == "WATCH" and card["reason_code"] == "GATE3_FAIL"
    assert _log(conn, did)["action"] == "WATCH" and any("D24" in n for n in _log(conn, did)["framework_notes"])
    td2 = _td(tmp_path, name="td2", card={"signal": "WATCH", "reason_code": "RED_LIGHT", "certainty_grade": "B"})
    W.write_full_result(conn, paths, "BBB", ASOF, "r", td2, "WATCH", [], gates=GATES_OK)
    did = W.write_price_result(conn, paths, "BBB", "2026-10-20", "r2", action="ADD", gate1=True, gate2="通過",
                               gate3="通過", cap={"auto_buy_cap": 70.0}, price=50.0, price_date="2026-10-20",
                               t30=0.047, reason="", notes=[])
    assert _log(conn, did)["action"] == "HOLD" and _card(conn, did)["signal"] != "BUY"


# ---------------------------------------------------------------- M2-D10-04

def test_decision_log_has_price_file_opened_at_path(tmp_path):
    env = TE.make_env(tmp_path, TE.full_responses())
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(TE.RUN)
    td = env.paths.work / TE.RUN / "KO"
    assert (td / "price_file_opened_at.txt").read_text(encoding="utf-8").endswith("Z")
    log = json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()[0])
    assert log["price_file_opened_at_file"] == f"work/{TE.RUN}/KO/price_file_opened_at.txt"
