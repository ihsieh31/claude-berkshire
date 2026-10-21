"""F3-GLUE：先前修復群留下的跨檔收尾（設計決策 D90、D91、D97；A3-M2D-01、A3-M2E-02、A3-M3C-03、A3-M3D-01 (2)(3)）。
每個測試對應一項；修前失敗、修後通過（純文件項以文字檢查）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import test_fix_P20 as P20
import test_review_jobs as TR
from test_engine import CAP_INPUT, RUN, dossier_ok, red_team
from trader.analysis import checks as CK
from trader.analysis import writeback as W
from trader.jobs import deep as D
from trader.jobs import review_jobs as RJ
from trader.jobs.review_jobs import ThesisHandler, TriageHandler
from trader.pipelines import review as RP
from trader.report import metrics as MT

ROOT = Path(__file__).resolve().parents[2]
ROUTING = ROOT / "routing"
REFS = ROOT / "berkshire-deep-merged" / "references"
env = TR.env   # (paths, conn)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _schemas() -> dict:
    return json.loads(_read(ROUTING / "output_schemas.json"))


# ---------------------------------------------------------------- 1. D97：incr_roic_gt_rd 三值照傳

@pytest.mark.parametrize("v,want", [(None, None), ("null", None), ("不知道", None), (True, True), ("true", True),
                                    (False, False), ("False", False)])
def test_D97_phase_a_incr_roic_three_valued(v, want):
    out = D._phase_a_cap_fields({"normalized_inputs": {"incr_roic_gt_rd": v}}, [])
    assert out["incr_roic_gt_rd"] is want      # 修前 null／未知 → False（cap_calc 視為「確定 ≤ r_d」壓上限）


def test_D97_null_reaches_cap_calc_not_false(tmp_path):
    """FULL_A 填 null、FULL_B cap_input 填 false：框架以價格盲的 null 覆寫，cap_calc 收到 null（不改上限，D97）。"""
    a = P20._fa(incr_roic_gt_rd=None)
    env_ = P20._env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": a,
                               "FULL_B": P20._fb({**CAP_INPUT, "incr_roic_gt_rd": False}), "RED_TEAM": red_team(False)})
    env_.engine.run(RUN)
    td = P20._td(env_)
    assert P20._j(td / "cap_input.json")["incr_roic_gt_rd"] is None
    assert P20._j(td / "cap_result.json")["incr_roic_gt_rd"] is None


def test_D97_schema_and_full_a_prompt_say_null():
    ni = _schemas()["FULL_A"]["normalized_inputs"]
    assert "null" in ni["incr_roic_gt_rd"] and "資料不足填 false" not in ni["incr_roic_gt_rd"]
    assert "incr_roic_gt_rd 填 null" in ni["_cap_input"] and "incr_roic_gt_rd 填 false" not in ni["_cap_input"]
    fa = _read(ROUTING / "prompts" / "FULL_A.md")
    assert "資料不足填 false" not in fa and "未判定填 null" in fa


def test_A3_M2D_01_full_a_evidence_date_is_filing_date():
    fa = _read(ROUTING / "prompts" / "FULL_A.md")
    line = next(l for l in fa.splitlines() if "gate3_questionnaire.json" in l)
    assert "filingDate" in line and "資料包已收錄" in line and "期末日" in line


# ---------------------------------------------------------------- 2. D90：conclusion_test 死碼

def test_D90_price_prompt_and_routing_drop_conclusion_test():
    assert "conclusion_test" not in _read(ROUTING / "prompts" / "PRICE.md")
    assert "conclusion_test" not in _read(ROUTING / "model_routing.json")
    src = _read(Path(RJ.__file__))
    assert "flip_full" not in src and '.get("conclusion_test")' not in src
    assert '.get("conclusion_test")' not in _read(Path(CK.__file__))


def test_D90_gate2_and_card_ignore_conclusion_test():
    cap = {"auto_buy_cap": 50.0, "price": 40.0, "grade_used": "A", "conclusion_test": {"flips": True}}
    assert CK.gate2_failures(cap) == []                                       # 修前「結論測試翻轉」
    assert CK.apply_cap_override({}, cap)["conclusion_test_flips"] is False


def test_D90_inferred_keys_follow_actual_cap_calc_output():
    cap = CK._skill_module("cap_calc").calc(dict(CAP_INPUT))
    assert cap["auto_buy_cap"] is not None and "conclusion_test" not in cap
    keys = CK.cap_inferred_keys(cap)
    assert "near_cap" in keys and "stress_sens" in keys                       # 修前依 conclusion_test，恆不列


def test_D90_price_flips_key_ignored_gate2_passes(env, monkeypatch):
    """即使 cap_result 帶舊鍵 conclusion_test.flips（cap_calc 已不輸出），PRICE 也不再據以擋閘門 2 或另走完整分析。"""
    paths, conn = env
    TR._write_cap_input(paths)
    TR._seed_registry(conn, "KO", "WATCH", {"grade": "B"})
    TR._gate3_row(conn)
    real = RJ.run_cap_calc
    monkeypatch.setattr(RJ, "run_cap_calc", lambda p: {**real(p), "conclusion_test": {"flips": True}})
    result = TR._price_result(needs_full=False)
    TR._run_price(paths, conn, TR._price_params(), result)
    assert result["framework_gates"]["gate2"] == "通過" and "framework_mechanical_full" not in result


# ---------------------------------------------------------------- 3. A3-M2E-02 宿主端：check_error → 異常

def _cerr_rows(conn):
    return conn.execute("SELECT ticker, detail, pause_trading FROM anomalies WHERE kind='review_check_error' "
                        "AND status='open' ORDER BY anomaly_id").fetchall()


def test_A3_M2E_02_check_error_opens_anomaly_once_per_asof(env):
    _, conn = env
    rows = [{"ticker": "KO", "action": "NONE", "check_error": "check_error: KeyError: 'x'"},
            {"ticker": "PEP", "action": "NONE", "check_error": None}]
    got = RP.record_check_errors(conn, rows, "2026-10-15", "/w/due.json")
    assert got == [{"ticker": "KO", "check_error": "check_error: KeyError: 'x'"}]
    RP.record_check_errors(conn, rows, "2026-10-15")                 # 同 asof 重跑：不重複
    a = _cerr_rows(conn)
    assert len(a) == 1 and a[0]["ticker"] == "KO" and a[0]["pause_trading"] == 0
    d = json.loads(a[0]["detail"])
    assert d["date"] == "2026-10-15" and "KeyError" in d["check_error"]
    RP.record_check_errors(conn, rows, "2026-10-29")                 # 新決策日仍出錯：另開一筆
    assert len(_cerr_rows(conn)) == 2


def test_A3_M2E_02_run_review_wires_check_errors():
    src = _read(Path(RP.__file__))
    i = src.index("due = json.loads(due_path.read_text")
    assert "record_check_errors(conn, rows, asof" in src[i:]


# ---------------------------------------------------------------- 4. 文件同步 D90／D91

def test_D90_D91_valuation_docs_match_cap_calc():
    p05 = _read(REFS / "procedure" / "05_valuation_caps.md")
    assert "+3pp +3pp" not in p05 and "30 年期 +6pp" in p05 and "D91" in p05
    assert "0.95 × min(P2′, k×`IV_base`)" in p05
    concl = next(l for l in p05.splitlines() if l.startswith("- **結論測試**"))
    assert "D90" in concl and "不再另做翻轉測試" in concl
    rb = _read(REFS / "rulebook.md")
    p18 = next(l for l in rb.splitlines() if l.startswith("| P-18 "))
    assert "長期利率＋6pp" in p18 and "＋**3pp**" not in p18 and "會翻轉結論 → 本週期 WATCH" not in p18
    assert "0.95 × min(P2′, k × `IV_base`)" in rb
    concl = next(l for l in rb.splitlines() if l.startswith("- **結論測試**"))
    assert "D90" in concl and "不再另做翻轉測試" in concl


def _it_row(key):
    return next(l for l in _read(REFS / "inferred_thresholds.md").splitlines() if l.startswith(f"| {key} |"))


def test_D90_D91_inferred_thresholds_rows():
    assert "0.95" in _it_row("near_cap") and "D90" in _it_row("near_cap")
    assert "+7pp" in _it_row("stress_sens") and "只作報告" in _it_row("stress_sens")
    assert "+6pp" in _it_row("stress_add") and "D91" in _it_row("stress_add")
    assert "0.95 × min(P2′, k × IV_base)" in _it_row("cap_discount_k")


def test_hold_stop_retry_days_documented():
    row = _it_row("HOLD_STOP_RETRY_DAYS")
    assert f"{W.HOLD_STOP_RETRY_DAYS} 個曆日" in row and "推論" in row and W.HOLD_STOP_RETRY_DAYS == 91


# ---------------------------------------------------------------- 5. A3-M3C-03 文件半

def test_A3_M3C_03_schema_says_optional_key_grade_after():
    assert _schemas()["HOLD_REVIEW"]["_grade_after"].startswith("選填鍵 grade_after")


# ---------------------------------------------------------------- 6. A3-M3D-01 (2)(3)

TR_RESULT = {"ticker": "KO", "escalate": True, "items": [
    {"ref": "2026-10-10 Goldman upgrades KO to Buy", "answer": "不確定", "touches": "",
     "reason": "KO trades at 25x earnings, analyst sees 30% upside", "url": "https://news.example/ko-upgrade"},
    {"ref": "8-K 2026-10-01 Item 5.02", "answer": "是", "touches": "管理層", "reason": "CFO resigned."}]}


def test_A3_M3D_01_blind_triage_result_unit():
    out = RJ.blind_triage_result(TR_RESULT)
    a, b = out["items"]
    assert a["ref"] == RJ.BLIND_OMITTED and a["reason"] == RJ.BLIND_OMITTED and a["answer"] == "不確定"
    assert "url" not in a and b["ref"] == "8-K 2026-10-01 Item 5.02" and b["reason"] == "CFO resigned."
    assert TR_RESULT["items"][0]["ref"].startswith("2026-10-10 Goldman")     # 不改傳入物件


def test_A3_M3D_01_thesis_sandbox_triage_result_is_price_blind(env):
    paths, conn = env
    job = TR._job("THESIS", TR._price_params(last_review_date="2026-01-01", kill_criteria=["毛利率連兩季下滑"],
                                             triage_result=TR_RESULT))
    ctx = TR._ctx(paths, conn)
    h = ThesisHandler()
    h.pre_script(job, ctx)
    h.prepare(job, ctx)
    txt = (job.sandbox / "triage_result.json").read_text(encoding="utf-8")
    for bad in ("Goldman", "25x", "upside", "https://"):
        assert bad not in txt, bad
    assert "CFO resigned." in txt


def test_A3_M3D_01_triage_prompt_has_no_url(env):
    paths, conn = env
    url = "https://news.example/2026/10/ko-shares-slide-price-target"
    job = TR._job("TRIAGE", TR._price_params(triage=[{"type": "news", "date": "2026-10-10",
                                                      "headline": "Coca-Cola signs bottler deal",
                                                      "source": "Reuters", "url": url}],
                                             kill_criteria=["毛利率連兩季下滑"], base_action="NONE",
                                             thesis_file=None))
    ctx = TR._ctx(paths, conn)
    h = TriageHandler()
    h.pre_script(job, ctx)
    h.prepare(job, ctx)
    items = h.prompt_values(job, ctx, "claude_code")["triage_items"]
    assert "Coca-Cola signs bottler deal" in items and url not in items and '"url"' not in items
    assert job.params["triage"][0]["url"] == url                              # 只從提示去掉，參數保留


# ---------------------------------------------------------------- 7. ANOMALY_KINDS 中文名

def test_anomaly_kind_names():
    assert MT.ANOMALY_KINDS["screen_review"][0] == "初篩待複核"
    assert MT.ANOMALY_KINDS[RP.CHECK_ERROR_KIND][0] == "觸發檢查出錯"


def test_anomalies_view_shows_check_error_name(env):
    _, conn = env
    RP.record_check_errors(conn, [{"ticker": "KO", "check_error": "check_error: ValueError: bad"}], "2026-10-15")
    row = next(r for r in MT.anomalies(conn)["open"] if r["kind"] == RP.CHECK_ERROR_KIND)
    assert row["kind_name"] == "觸發檢查出錯" and row["meaning"]


def test_D97_validator_accepts_null_incr_roic_in_full_a():
    """schema 三值 "true | false | null"：FULL_A 填 null 合格（修前驗證只收 true／false → null 被退回、保守處理）。"""
    from trader.jobs import validate as V
    a = P20.full_a()
    a.pop("files", None)
    for v, ok in ((None, True), ("null", True), (False, True), ("maybe", False)):
        a["normalized_inputs"]["incr_roic_gt_rd"] = v
        out, errs = V.check("FULL_A", a, P20.SCHEMAS)
        assert (not any("incr_roic_gt_rd" in e for e in errs)) is ok, (v, errs)
        if ok and v in (None, "null"):
            assert out["normalized_inputs"]["incr_roic_gt_rd"] is None
