"""工作 I/O 層測試：validate、sandbox、context。全部離線執行。"""
from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timezone

import pytest

from trader.jobs import context, sandbox, validate
from trader.jobs.context import ContextOverflowError, Section, build_prompt, estimate_tokens, render_template
from trader.jobs.sandbox import (
    PriceGateError,
    PriceLeakError,
    assert_price_free,
    collect_outputs,
    create_sandbox,
    open_price_file,
    stage_inputs,
    ticker_dir,
)
from trader.models import JobSpec
from trader.paths import PKG_ROOT

SCHEMAS = validate.load_schemas(PKG_ROOT / "routing" / "output_schemas.json")

# ---------------------------------------------------------------- 合格範例（每個工作一份）

GOOD = {
    "DOSSIER": {
        "ticker": "AAPL",
        "decision_date": "2026-01-02",
        "sections_appended": ["業務", "風險因素"],
        "dq_checks": [{"warning": "營收與季報不符", "result": "已核對：原因在於匯率換算"}],
        "missing": ["債務到期表"],
        "contains_price": False,
    },
    "TRIAGE": {
        "ticker": "AAPL",
        "items": [{"ref": "8-K 2026-01-05 項目 2.02", "answer": "否", "touches": "", "reason": "與論點無關"}],
        "escalate": False,
    },
    "PRICE": {
        "ticker": "AAPL",
        "price": 180.5,
        "auto_buy_cap": None,
        "sell_review_above": 210.0,
        "absurd_price_line": 400,
        "gates": {"gate1_screen": "通過", "gate2_price_lt_cap": "無上限", "gate3_on_file": "已過期"},
        "proposed_action": "HOLD",
        "needs_full": False,
        "reason": "價格低於賣出檢視線。",
    },
    "THESIS": {
        "ticker": "AAPL",
        "verdict": "完好",
        "kill_criteria_status": [
            {"criterion": "毛利率連續兩季低於 30%", "status": "未觸發", "evidence": "10-Q 2026-01-30 第 2 節"}
        ],
        "r_list_status": [{"item": "R1", "status": "無變化", "evidence": "測試"}],
        "new_facts": ["新增 8-K（2026-01-05）"],
        "reason": "論點仍成立。",
    },
    "FULL_A": {
        "ticker": "AAPL",
        "decision_date": "2026-01-02",
        "stopped": False,
        "stopped_at_gate": None,
        "reason_code": None,
        "asset_class": "非金融",
        "starting_grade": "A",
        "downgrades": [{"step": "G3", "rule": "G3-2", "reason": "應收帳款周轉變慢"}],
        "moat_tests": {"pricing_power": "強", "attack_copy": "中", "price_cut": "強"},
        "moat_evidence": {"pricing_power": "足夠", "attack_copy": "缺資料", "price_cut": "足夠"},
        "red_flags": {"exclusion": 0, "warning": 1},
        "capital_allocation_net": 0,
        "tail_loss_after_tax_pct": 35.5,
        "normalized_inputs": {
            "oe_per_share": 5.2,
            "g10": 0.08,
            "g5": None,
            "excluded_shock_years": [],
            "roe_std_ex_shock_pp": None,
        },
        "thesis_draft": {
            "one_liner": "品牌定價權支撐長期獲利",
            "key_assumptions": ["定價權維持", "通路不被取代", "資本需求低"],
            "kill_criteria": [{"text": "毛利率連續兩季低於 30%", "due": "2027-01-01"}],
        },
        "missing": [],
        "inferred_thresholds_used": [],
        "files": ["report_A.md", "gate3_answers.json"],
    },
    "FULL_B": {
        "ticker": "AAPL",
        "decision_id": "AAPL-20260102-1",
        "signal": "WATCH",
        "reason_code": "TOO_HARD",
        "stopped_at_gate": "G8",
        "certainty_grade": "B",
        "auto_buy_cap": None,
        "target_weight": 0,
        "first_tranche_weight": 0,
        "files": ["report.md", "signal_card.json"],
    },
    "RED_TEAM": {
        "ticker": "AAPL",
        "fatal": False,
        "fatal_paths": [
            {"path": "品牌受損導致定價權流失", "probability_note": "依 10-K 風險因素", "covered_by_kill_criteria": True}
        ],
        "number_errors": [],
        "rule_violations": [],
        "verdict": "維持",
    },
    "HOLD_REVIEW": {
        "ticker": "AAPL",
        "decision_date": "2026-10-16",
        "path": [{"q": "Q0", "answer": "否", "evidence": "理由不是價格"},
                 {"q": "Q2", "answer": "是", "evidence": "只是疑似：失效條件接近未觸發"}],
        "action": "HOLD",
        "sell_category": None,
        "trim_fraction": None,
        "stop_adding": True,
        "observe_until": "2027-04-16",
        "falsified_assumption": None,
        "reason": "疑似，觀察並停止加碼",
    },
    "CORP_ACTION": {
        "ticker": "AAPL",
        "action_type": "拆股",
        "effective_date": "2026-02-01",
        "pay_date": None,
        "ratio_new_per_old": 4,
        "cash_per_share": None,
        "fractional_handling": "現金了結",
        "new_ticker": None,
        "needs_human": False,
        "evidence": "8-K 2026-01-20 Item 8.01：四拆一",
        "reason": "四拆一，生效日 2026-02-01。",
    },
}


def _job(job_type: str = "DOSSIER", ticker: str = "AAPL", job_id: str | None = None) -> JobSpec:
    return JobSpec(
        job_id=job_id or f"r1:{ticker}:{job_type}:1",
        run_id="r1",
        job_type=job_type,
        ticker=ticker,
        asof="2026-01-02",
    )


def _good_copy(job_type: str) -> dict:
    return copy.deepcopy(GOOD[job_type])


# ---------------------------------------------------------------- validate

def test_schemas_cover_all_job_types():
    from trader.models import JOB_TYPES

    assert set(JOB_TYPES) <= set(SCHEMAS)
    assert set(GOOD) == set(JOB_TYPES)


@pytest.mark.parametrize("job_type", sorted(GOOD))
def test_good_example_passes(job_type):
    out, errors = validate.check(job_type, _good_copy(job_type), SCHEMAS)
    assert errors == []
    assert out["ticker"] == "AAPL"


def test_strict_enum_typo_reports_json_path():
    obj = _good_copy("THESIS")
    obj["verdict"] = "完善"  # 打錯一字（正確是 完好）
    _, errors = validate.check("THESIS", obj, SCHEMAS)
    assert errors
    assert any(e.startswith("THESIS.verdict") for e in errors)


def test_strict_enum_typo_in_nested_list():
    obj = _good_copy("THESIS")
    obj["kill_criteria_status"][0]["status"] = "未觸"
    _, errors = validate.check("THESIS", obj, SCHEMAS)
    assert any(e.startswith("THESIS.kill_criteria_status[0].status") for e in errors)


def test_string_true_is_normalized_to_bool():
    obj = _good_copy("TRIAGE")
    obj["escalate"] = "true"
    out, errors = validate.check("TRIAGE", obj, SCHEMAS)
    assert errors == []
    assert out["escalate"] is True
    assert obj["escalate"] == "true"  # 不修改傳入物件


def test_string_false_normalized_and_bad_bool_rejected():
    obj = _good_copy("RED_TEAM")
    obj["fatal"] = "False"
    out, errors = validate.check("RED_TEAM", obj, SCHEMAS)
    assert errors == [] and out["fatal"] is False

    obj["fatal"] = "yes"
    _, errors = validate.check("RED_TEAM", obj, SCHEMAS)
    assert any(e.startswith("RED_TEAM.fatal") for e in errors)


def test_number_or_null_accepts_none_and_rejects_string():
    obj = _good_copy("PRICE")
    obj["auto_buy_cap"] = None
    _, errors = validate.check("PRICE", obj, SCHEMAS)
    assert errors == []

    obj["auto_buy_cap"] = 123.4
    _, errors = validate.check("PRICE", obj, SCHEMAS)
    assert errors == []

    obj["auto_buy_cap"] = "100"
    _, errors = validate.check("PRICE", obj, SCHEMAS)
    assert any(e.startswith("PRICE.auto_buy_cap") for e in errors)


def test_bad_date_rejected_including_feb_30():
    obj = _good_copy("DOSSIER")
    obj["decision_date"] = "2026-02-30"
    _, errors = validate.check("DOSSIER", obj, SCHEMAS)
    assert any(e.startswith("DOSSIER.decision_date") for e in errors)

    obj["decision_date"] = "2026/01/02"
    _, errors = validate.check("DOSSIER", obj, SCHEMAS)
    assert any(e.startswith("DOSSIER.decision_date") for e in errors)

    obj["decision_date"] = "2028-02-29"  # 閏年，合法
    _, errors = validate.check("DOSSIER", obj, SCHEMAS)
    assert errors == []


def test_date_or_null_accepts_none():
    obj = _good_copy("CORP_ACTION")
    obj["pay_date"] = None
    _, errors = validate.check("CORP_ACTION", obj, SCHEMAS)
    assert errors == []
    obj["pay_date"] = "2026-13-01"
    _, errors = validate.check("CORP_ACTION", obj, SCHEMAS)
    assert any(e.startswith("CORP_ACTION.pay_date") for e in errors)


def test_decision_id_format():
    obj = _good_copy("FULL_B")
    for good in ("AAPL-20260102-1", "BRK.B-20260102-12"):
        obj["decision_id"] = good
        _, errors = validate.check("FULL_B", obj, SCHEMAS)
        assert errors == [], good

    for bad in ("aapl-20260102-1", "AAPL-2026-01-02-1", "AAPL-20260230-1", "AAPL-20260102", "AAPL20260102-1"):
        obj["decision_id"] = bad
        _, errors = validate.check("FULL_B", obj, SCHEMAS)
        assert any(e.startswith("FULL_B.decision_id") for e in errors), bad


def test_missing_required_key_reported():
    obj = _good_copy("TRIAGE")
    del obj["escalate"]
    _, errors = validate.check("TRIAGE", obj, SCHEMAS)
    assert any(e.startswith("TRIAGE.escalate") and "缺少" in e for e in errors)


def test_nested_list_of_dict_missing_key():
    obj = _good_copy("THESIS")
    del obj["kill_criteria_status"][0]["evidence"]
    _, errors = validate.check("THESIS", obj, SCHEMAS)
    assert any(e.startswith("THESIS.kill_criteria_status[0].evidence") for e in errors)


def test_free_text_option_with_ellipsis_accepts_any_string():
    obj = _good_copy("DOSSIER")
    obj["dq_checks"][0]["result"] = "數字有誤：正確值 100（來源 10-K 第 7 頁）"
    _, errors = validate.check("DOSSIER", obj, SCHEMAS)
    assert errors == []

    obj["dq_checks"][0]["result"] = "無法核對"
    _, errors = validate.check("DOSSIER", obj, SCHEMAS)
    assert errors == []

    obj["dq_checks"][0]["result"] = 42  # 自由字串仍必須是字串
    _, errors = validate.check("DOSSIER", obj, SCHEMAS)
    assert any(e.startswith("DOSSIER.dq_checks[0].result") for e in errors)


def test_stopped_at_gate_accepts_none():
    obj = _good_copy("FULL_A")
    obj["stopped_at_gate"] = None
    _, errors = validate.check("FULL_A", obj, SCHEMAS)
    assert errors == []
    obj["stopped_at_gate"] = "G2"
    _, errors = validate.check("FULL_A", obj, SCHEMAS)
    assert errors == []


def test_stopped_at_gate_range_expanded_in_full_b():
    obj = _good_copy("FULL_B")
    for ok in (None, "G7", "G9", "G11"):
        obj["stopped_at_gate"] = ok
        assert validate.check("FULL_B", obj, SCHEMAS)[1] == []
    for bad in ("G6", "G12", "G7..G11", "g8"):
        obj["stopped_at_gate"] = bad
        assert validate.check("FULL_B", obj, SCHEMAS)[1], bad


def test_error_feedback_is_nonempty_string():
    msg = validate.error_feedback(["THESIS.verdict：值不在允許選項內"])
    assert isinstance(msg, str) and msg.strip()
    assert "THESIS.verdict" in msg
    assert validate.error_feedback([]).strip()


def test_extra_keys_and_underscore_keys_are_fine():
    obj = _good_copy("TRIAGE")
    obj["extra_note"] = "允許額外鍵"
    _, errors = validate.check("TRIAGE", obj, SCHEMAS)
    assert errors == []


def test_unknown_job_type_raises():
    with pytest.raises(ValueError):
        validate.check("NOPE", {}, SCHEMAS)


# ---------------------------------------------------------------- sandbox

def test_isolated_job_rejects_price_file(tmp_path):
    src = tmp_path / "AAPL_price.json"
    src.write_text("{}", encoding="utf-8")
    sb = tmp_path / "sb"
    sb.mkdir()
    with pytest.raises(PriceLeakError):
        stage_inputs(sb, [src], "DOSSIER")
    assert not (sb / "AAPL_price.json").exists()


def test_isolated_job_rejects_price_file_given_as_tuple_name(tmp_path):
    src = tmp_path / "whatever.json"
    src.write_text("{}", encoding="utf-8")
    sb = tmp_path / "sb"
    sb.mkdir()
    with pytest.raises(PriceLeakError):
        stage_inputs(sb, [(src, "X_price.json")], "TRIAGE")


def test_isolated_job_scrubs_price_keys(tmp_path):
    src = tmp_path / "facts.json"
    src.write_text(json.dumps({
        "ticker": "AAPL",
        "price_context": {"last": 1},
        "market_cap": 999,
        "earnings": {"price": 180.0, "eps": 5.2},
        "items": [{"pe_ratio": 20, "note": "keep"}],
        "text": "保留",
    }, ensure_ascii=False), encoding="utf-8")
    sb = tmp_path / "sb"
    sb.mkdir()
    names = stage_inputs(sb, [src], "THESIS")
    assert names == ["facts.json"]
    data = json.loads((sb / "facts.json").read_text(encoding="utf-8"))
    assert "price_context" not in data
    assert "market_cap" not in data
    assert data["earnings"] == {"eps": 5.2}
    assert data["items"] == [{"note": "keep"}]
    assert data["text"] == "保留"
    assert_price_free(sb)  # 不應拋錯


def test_isolated_job_rejects_unparseable_json(tmp_path):
    src = tmp_path / "bad.json"
    src.write_text("{not json", encoding="utf-8")
    sb = tmp_path / "sb"
    sb.mkdir()
    with pytest.raises(PriceLeakError):
        stage_inputs(sb, [src], "FULL_A")


def test_assert_price_free_catches_leaked_files(tmp_path):
    sb = tmp_path / "sb"
    (sb / "nested").mkdir(parents=True)
    (sb / "nested" / "leak.json").write_text(json.dumps({"items": [{"price": 3}]}), encoding="utf-8")
    with pytest.raises(PriceLeakError):
        assert_price_free(sb)

    (sb / "nested" / "leak.json").unlink()
    (sb / "AAPL_price.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PriceLeakError):
        assert_price_free(sb)

    (sb / "AAPL_price.json").unlink()
    (sb / "notes.md").write_text("price 字樣在 markdown 中不算", encoding="utf-8")
    assert_price_free(sb)


def test_non_isolated_job_can_stage_price_file(tmp_path):
    src = tmp_path / "AAPL_price.json"
    src.write_text(json.dumps({"price": 180.5}), encoding="utf-8")
    sb = tmp_path / "sb"
    sb.mkdir()
    names = stage_inputs(sb, [src], "FULL_B")
    assert names == ["AAPL_price.json"]
    assert json.loads((sb / "AAPL_price.json").read_text(encoding="utf-8")) == {"price": 180.5}


def test_open_price_file_requires_gate3(tmp_path):
    sb = tmp_path / "sb"
    sb.mkdir()
    price = tmp_path / "AAPL_price.json"
    price.write_text("{}", encoding="utf-8")
    with pytest.raises(PriceGateError):
        open_price_file(sb, price, tmp_path / "gate3_answers.json")
    assert not (sb / "AAPL_price.json").exists()


def test_open_price_file_rejects_gate3_not_earlier_than_now(tmp_path):
    sb = tmp_path / "sb"
    sb.mkdir()
    price = tmp_path / "AAPL_price.json"
    price.write_text("{}", encoding="utf-8")
    gate3 = tmp_path / "gate3_answers.json"
    gate3.write_text("{}", encoding="utf-8")
    mtime = gate3.stat().st_mtime
    with pytest.raises(PriceGateError):
        open_price_file(sb, price, gate3, now=mtime)  # mtime == now
    with pytest.raises(PriceGateError):
        open_price_file(sb, price, gate3, now=mtime - 10)  # mtime > now
    assert not (sb / "AAPL_price.json").exists()


def test_open_price_file_success_copies_and_records_time(tmp_path):
    sb = tmp_path / "sb"
    sb.mkdir()
    price = tmp_path / "AAPL_price.json"
    price.write_text(json.dumps({"price": 180.5}), encoding="utf-8")
    gate3 = tmp_path / "gate3_answers.json"
    gate3.write_text("{}", encoding="utf-8")
    old = time.time() - 1000
    os.utime(gate3, (old, old))
    now = time.time()

    opened = open_price_file(sb, price, gate3, now=now)

    assert (sb / "AAPL_price.json").read_text(encoding="utf-8") == price.read_text(encoding="utf-8")
    recorded = (sb / "price_file_opened_at.txt").read_text(encoding="utf-8")
    assert recorded == opened
    assert opened.endswith("Z")
    parsed = datetime.strptime(opened, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs(parsed.timestamp() - int(now)) <= 1  # 秒精度的 UTC 時間


def test_create_sandbox_layout_and_rebuild_clears(tmp_path):
    job = _job("DOSSIER", "AAPL", job_id="r1:AAPL:DOSSIER:7")
    sb = create_sandbox(tmp_path / "work", job)
    assert job.sandbox == sb
    assert sb == tmp_path / "work" / "r1" / "AAPL" / "_DOSSIER_7"
    assert sb.is_dir()
    (sb / "stale.txt").write_text("舊檔", encoding="utf-8")

    sb2 = create_sandbox(tmp_path / "work", job)
    assert sb2 == sb
    assert sb2.is_dir()
    assert not (sb2 / "stale.txt").exists()


def test_ticker_dir_is_shared_by_jobs_of_same_ticker(tmp_path):
    a = _job("DOSSIER", "AAPL", job_id="r1:AAPL:DOSSIER:1")
    b = _job("FULL_A", "AAPL", job_id="r1:AAPL:FULL_A:2")
    assert ticker_dir(tmp_path, a) == ticker_dir(tmp_path, b) == tmp_path / "r1" / "AAPL"


def test_collect_outputs_copies_existing_only(tmp_path):
    sb = tmp_path / "sb"
    (sb / "sub").mkdir(parents=True)
    (sb / "report_A.md").write_text("# 報告", encoding="utf-8")
    (sb / "sub" / "gate3_answers.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "ticker"

    copied = collect_outputs(sb, dest, ["report_A.md", "missing.md", "sub/gate3_answers.json"])

    assert copied == ["report_A.md", "sub/gate3_answers.json"]
    assert (dest / "report_A.md").read_text(encoding="utf-8") == "# 報告"
    assert (dest / "sub" / "gate3_answers.json").exists()
    assert not (dest / "missing.md").exists()


def test_collect_outputs_rejects_path_escape(tmp_path):
    sb = tmp_path / "sb"
    sb.mkdir()
    with pytest.raises(ValueError):
        collect_outputs(sb, tmp_path / "dest", ["../secret.txt"])


# ---------------------------------------------------------------- context

def test_estimate_tokens_cjk_and_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("中" * 10) == 10          # 中文每字 1
    assert estimate_tokens("a" * 8) == 2             # 其他字元每 4 字 1
    assert estimate_tokens("abcde") == 2             # 向上取整
    assert estimate_tokens("中文ab") == 3            # 2 + ceil(2/4)


def test_render_template_keeps_json_braces():
    tmpl = '範例 {"ok": true, "ticker": "{ticker}", "n": {n}} 與 {asof}'
    out = render_template(tmpl, {"ticker": "AAPL", "asof": "2026-01-02"})
    assert out == '範例 {"ok": true, "ticker": "AAPL", "n": {n}} 與 2026-01-02'
    assert render_template("{}", {}) == "{}"


def test_claude_code_mode_lists_filenames_without_content(tmp_path):
    job = _job("TRIAGE")
    sb = create_sandbox(tmp_path / "work", job)
    (sb / "8k_list.json").write_text("{}", encoding="utf-8")
    prompt = build_prompt(
        job, "claude_code", 100000, PKG_ROOT,
        sections=[Section("8-K", "SECRET_MARKER_XYZ 內容不應被內嵌")],
    )
    assert "8k_list.json" in prompt
    assert "SECRET_MARKER_XYZ" not in prompt
    assert "## 輸入檔" in prompt


def test_openai_mode_embeds_content_and_files_note(tmp_path):
    job = _job("TRIAGE")
    prompt = build_prompt(
        job, "openai_compat", 100000, PKG_ROOT,
        sections=[Section("8-K", "EMBED_MARKER_ABC")],
    )
    assert "EMBED_MARKER_ABC" in prompt
    assert "files：{檔名: 內容}" in prompt


def test_prompt_contains_common_rules_and_own_schema_section():
    job = _job("THESIS")
    prompt = build_prompt(job, "openai_compat", 100000, PKG_ROOT, sections=[])
    assert "共通規則" in prompt
    assert "2026-01-02" in prompt  # {asof} 已代入
    assert "AAPL" in prompt        # {ticker} 已代入
    assert "{asof}" not in prompt
    assert json.dumps(SCHEMAS["THESIS"], ensure_ascii=False, indent=2) in prompt  # 本工作的 schema 節
    assert "完好 | 疑似 | 證偽" in prompt
    assert "FULL_A" not in prompt.split("## 輸出格式")[1].split("```")[0]


def test_overflow_drops_droppable_2_before_1():
    job = _job("TRIAGE")
    base = build_prompt(job, "openai_compat", 10**9, PKG_ROOT, sections=[])
    base_tokens = estimate_tokens(base)
    secs = [
        Section("refs", "R" * 2000, droppable=1),        # 約 500 tokens
        Section("knowledge", "K" * 2000, droppable=2),   # 約 500 tokens
        Section("core", "C" * 40, droppable=0),
    ]
    # 上限容得下一段 500 tokens：先刪 droppable=2 的 knowledge 即可
    prompt = build_prompt(job, "openai_compat", base_tokens + 600, PKG_ROOT, sections=secs)
    assert "RRRR" in prompt
    assert "KKKK" not in prompt
    assert "CCCC" in prompt

    # 上限連一段都容不下：兩段可刪段都要刪，不可刪段保留
    prompt = build_prompt(job, "openai_compat", base_tokens + 100, PKG_ROOT, sections=secs)
    assert "RRRR" not in prompt and "KKKK" not in prompt
    assert "CCCC" in prompt


def test_overflow_still_too_big_raises():
    job = _job("TRIAGE")
    base_tokens = estimate_tokens(build_prompt(job, "openai_compat", 10**9, PKG_ROOT, sections=[]))
    secs = [Section("core", "X" * 100000, droppable=0)]
    with pytest.raises(ContextOverflowError):
        build_prompt(job, "openai_compat", base_tokens + 10, PKG_ROOT, sections=secs)


def test_overflow_in_claude_code_mode_raises_when_tiny_limit(tmp_path):
    job = _job("TRIAGE")
    create_sandbox(tmp_path / "work", job)
    with pytest.raises(ContextOverflowError):
        build_prompt(job, "claude_code", 10, PKG_ROOT)


def test_module_exports_context_api():
    assert hasattr(context, "build_prompt") and hasattr(context, "estimate_tokens")
    assert hasattr(sandbox, "open_price_file") and hasattr(validate, "check")


def test_string_null_in_nullable_enum_is_normalized():
    obj = _good_copy("FULL_A")
    obj["stopped_at_gate"] = "null"
    obj["reason_code"] = "null"
    out, errors = validate.check("FULL_A", obj, SCHEMAS)
    assert errors == []
    assert out["stopped_at_gate"] is None and out["reason_code"] is None
