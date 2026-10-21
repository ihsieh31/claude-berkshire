"""輸出驗證：DOSSIER 節名為自由文字、列舉不收整串選項、多包一層工作名稱可拆開。"""
from __future__ import annotations

from trader.jobs.validate import check, load_schemas
from trader.paths import get_paths

SCHEMAS = load_schemas(get_paths().schemas_json)
SECTIONS = ["業務", "分部", "風險因素", "管理層討論", "債務到期表", "利息費用", "或有負債與訴訟",
            "會計師關鍵查核事項", "委託書薪酬", "重大 8-K", "最近一季 10-Q", "附註摘要", "競爭與產業關鍵指標"]


def dossier(**kw):
    d = {"ticker": "KO", "decision_date": "2026-10-15", "sections_appended": SECTIONS,
         "dq_checks": [], "missing": []}
    d.update(kw)
    return d


def test_dossier_accepts_all_sections_named_in_prompt():
    _, errors = check("DOSSIER", dossier(), SCHEMAS)
    assert errors == []


def test_dossier_sections_appended_is_free_text():
    """M3_D1-05：節名為自由文字（產業補充序列、模型自取的節名都不得讓 DOSSIER 不合格）；框架不讀此欄。"""
    _, errors = check("DOSSIER", dossier(sections_appended=["產業補充序列", "會計師報告"]), SCHEMAS)
    assert errors == []


def test_enum_array_rejects_joined_option_string():
    """列舉欄位仍不得把整串選項當一個值（原 DOSSIER 節名測試的保護，改用仍是列舉的 HOLD_REVIEW.path[].q）。"""
    r = {"ticker": "KO", "decision_date": "2026-10-15",
         "path": [{"q": "Q0 | Q1 | Q2", "answer": "是", "evidence": "SELL-030"}],
         "action": "HOLD", "sell_category": None, "trim_fraction": None, "stop_adding": False,
         "observe_until": None, "falsified_assumption": None, "external_drivers": [],
         "external_drivers_change_reason": None, "reason": "測試"}
    _, errors = check("HOLD_REVIEW", r, SCHEMAS)
    assert any(e.startswith("HOLD_REVIEW.path[0].q") and "不在允許選項內" in e for e in errors), errors


def test_wrapper_with_job_type_is_unwrapped():
    out, errors = check("DOSSIER", {"DOSSIER": dossier()}, SCHEMAS)
    assert errors == [] and out["ticker"] == "KO"


def test_corp_action_none_type_allows_null_effective_date():
    """提示要求「無公司行動」時數字與日期填 null：格式驗證不得因此退件（否則重試失敗會暫停該檔交易）。"""
    r = {"ticker": "CTVA", "action_type": "無公司行動", "effective_date": None, "pay_date": None,
         "ratio_new_per_old": None, "cash_per_share": None, "fractional_handling": "不適用", "new_ticker": None,
         "needs_human": False, "evidence": "8-K Item 5.02 董事異動", "reason": "非公司行動"}
    _, errors = check("CORP_ACTION", r, SCHEMAS)
    assert errors == []


def test_hold_review_path_accepts_q3_subnodes():
    """提示要求 TRIM 只用於 Q3c：路徑列舉必須允許 Q3a–Q3d。"""
    r = {"ticker": "KO", "decision_date": "2026-10-15",
         "path": [{"q": "Q3c", "answer": "是", "evidence": "SELL-030"}],
         "action": "TRIM", "sell_category": "估值", "trim_fraction": 0.25, "stop_adding": True,
         "observe_until": None, "falsified_assumption": None, "external_drivers": [],
         "external_drivers_change_reason": None, "reason": "頂級複利型分批減碼"}
    _, errors = check("HOLD_REVIEW", r, SCHEMAS)
    assert errors == []
