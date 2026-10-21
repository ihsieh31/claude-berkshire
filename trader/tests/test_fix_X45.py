"""X45：build_dossier 權益 ≤0 年度的 ROE／有息負債÷權益印「不適用」；框架把 cap_calc 套用的推論門檻鍵補進
訊號卡 inferred_thresholds_used（M2-D5-06 程式部分、M2-D5-04 未盡項）。"""
import importlib
import re
import sys

import pytest

from trader.analysis.checks import _skill_module, add_cap_inferred_keys, apply_cap_override, cap_inferred_keys
from trader.paths import get_paths

ASOF = "2026-10-15"
PATHS = get_paths()


@pytest.fixture(scope="module")
def B():
    for sub in ("berkshire-screen-merged", "berkshire-deep-merged"):
        d = str(PATHS.root / sub / "scripts")
        if d not in sys.path:
            sys.path.insert(0, d)
    return importlib.import_module("build_dossier")


def _facts(eq_m):
    def cal(y, v):
        return {"form": "10-K", "fp": "FY", "start": f"{y}-01-01", "end": f"{y}-12-31", "filed": f"{y + 1}-02-20", "val": v}

    def calp(y, v):
        return {"form": "10-K", "fp": "FY", "end": f"{y}-12-31", "filed": f"{y + 1}-02-20", "val": v}
    us = {"NetIncomeLoss": [cal(y, 1000e6) for y in eq_m], "Revenues": [cal(y, 1e10) for y in eq_m],
          "StockholdersEquity": [calp(y, v * 1e6) for y, v in eq_m.items()],
          "LongTermDebtNoncurrent": [calp(y, 1e9) for y in eq_m], "InterestExpense": [cal(y, 5e7) for y in eq_m]}
    return {"cik": 1, "entityName": "T", "facts": {"us-gaap": {k: {"units": {"USD": v}} for k, v in us.items()}}}


def _row(rtbl, head):
    ln = next(x for x in rtbl.splitlines() if x.startswith("| " + head))
    return [c.strip() for c in ln.strip().strip("|").split("|")][1:]


# 權益：FY2022 期末 −100（期初 500，平均 +200 仍為正）、FY2023 −800、FY2024 +100（期初 −800）
EQ = {2016: 500, 2017: 500, 2018: 500, 2019: 500, 2020: 500, 2021: 500, 2022: -100, 2023: -800, 2024: 100, 2025: 900}


def test_roe_not_applicable_when_period_end_equity_le_0_even_if_average_positive(B):
    """修前：FY2022 平均權益 +200 → ROE 印 5.000（像正常數字）；修後印「不適用」。"""
    T, (R, filed, last, warn, bands) = B.build(_facts(EQ), "T", ASOF)
    _, rtbl = B.render(T, R, bands, filed, {})
    roe = dict(zip(T.index, _row(rtbl, "ROE")))
    assert roe[2022] == B.NA_EQ and roe[2023] == B.NA_EQ
    assert roe[2024] == B.NA_EQ          # 期初權益 ≤0（修前印 nan，和缺值分不出來）
    assert roe[2025] == "2.000"          # 兩端皆正：照算
    assert roe[2016] == "nan"            # 期初缺：仍是缺值 nan，不是「不適用」
    de = dict(zip(T.index, _row(rtbl, "有息負債÷權益")))
    assert de[2022] == de[2023] == B.NA_EQ and de[2024] == "10.000"
    assert any("股東權益 ≤0" in w and "FY2022、FY2023" in w and "次一年" in w for w in warn)


def test_dossier_ratio_strings_do_not_break_table_parser(B):
    """下游唯一的資料包表格讀取端 deep_quant.dossier_table 只讀「## 10 年財報」、非數字格略過。"""
    T, (R, filed, last, warn, bands) = B.build(_facts(EQ), "T", ASOF)
    tbl, rtbl = B.render(T, R, bands, filed, {})
    md = "## 10 年財報\n\n" + tbl + "\n\n## 衍生比率\n\n" + rtbl + "\n"
    dq = _skill_module("deep_quant")
    t = dq.dossier_table(md)
    assert t["net_income"][2022] == 1000


# ---------------------------------------------------------------- inferred_thresholds_used

def _md_keys():
    text = (PATHS.skill_deep / "references" / "inferred_thresholds.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\|\s*([A-Za-z0-9_]+)\s*\|", text, re.M))


BASE_IN = {"oe": 5.0, "g10": 0.08, "g5": 0.07, "grade": "B", "long_rate": 0.045, "long_rate_20y_med": 0.035,
           "price": 60.0, "norm_eps": 5.0, "largecap_pe_median": 20.0, "financial": False}


def _cap(**kw):
    return _skill_module("cap_calc").calc({**BASE_IN, **kw})


def test_keys_exist_in_inferred_thresholds_md():
    md = _md_keys()
    caps = [_cap(), _cap(grade="A", incr_roic_gt_rd=True), _cap(g10=0.005, g5=0.004), _cap(g10=0.15, g5=0.05),
            _cap(financial=True, iv_cons=60, iv_base=80, iv_opt=100, fin_pe_median=12), _cap(long_rate=0.09),
            _cap(g10=None, g5=None)]
    for c in caps:
        ks = cap_inferred_keys(c)
        assert ks and set(ks) <= md, set(ks) - md
        assert len(ks) == len(set(ks))


def test_keys_follow_cap_calc_branches():
    k = cap_inferred_keys(_cap())
    assert {"rd_floor", "grade_caps", "gimpl_cap", "g_term_impl", "cons_path_decay", "cap_discount_k",
            "abc_consistency", "g0_haircut"} <= set(k)
    assert "fin_hard_cap" not in k and "near_zero_g" not in k and "g9_growth_spread" not in k
    assert "rd_spread" in cap_inferred_keys(_cap(long_rate=0.09)) and "rd_floor" not in cap_inferred_keys(_cap(long_rate=0.09))
    assert "near_zero_g" in cap_inferred_keys(_cap(g10=0.005, g5=0.004))
    assert "g9_growth_spread" in cap_inferred_keys(_cap(g10=0.15, g5=0.05))
    assert "p4_soft_A" in cap_inferred_keys(_cap(grade="A", incr_roic_gt_rd=True))
    fin = cap_inferred_keys(_cap(financial=True, iv_cons=60, iv_base=80, iv_opt=100, fin_pe_median=12))
    assert "fin_hard_cap" in fin and "p4_fin" in fin and "gimpl_cap" not in fin and "cons_path_decay" not in fin
    assert cap_inferred_keys(_cap(g10=None, g5=None)) == ["rd_floor"]   # 無法估值：只算 r_d


def test_apply_cap_override_appends_keys_without_overwriting_model_list():
    """修前：apply_cap_override 不動 inferred_thresholds_used（cap_calc 套用的鍵沒人列）。"""
    cap = _cap()
    card = {"inferred_thresholds_used": ["min_years", "cap_discount_k", "min_years"]}
    out = apply_cap_override(card, cap)
    got = out["inferred_thresholds_used"]
    assert got[:2] == ["min_years", "cap_discount_k"]          # 模型已列者保留在前（重複去掉）
    assert got.count("cap_discount_k") == 1 and "gimpl_cap" in got and "abc_consistency" in got
    assert len(got) == len(set(got))
    assert card["inferred_thresholds_used"] == ["min_years", "cap_discount_k", "min_years"]   # 不改傳入的卡
    assert any("框架補列 inferred_thresholds_used" in n for n in out["framework_notes"])
    again = apply_cap_override(out, cap)                       # 再跑一次不重複、不再加註
    assert again["inferred_thresholds_used"] == got
    assert sum("框架補列" in n for n in again["framework_notes"]) == 1


def test_missing_or_null_list_is_created():
    card = {"inferred_thresholds_used": None}
    added = add_cap_inferred_keys(card, _cap())
    assert card["inferred_thresholds_used"] == added and added


# ---------------------------------------------------------------- X46（check-X45 小修）

def test_x46_non_string_model_items_do_not_raise():
    """修前：模型列含 dict／list 時 dict.fromkeys 拋 TypeError → FULL_B post 失敗。"""
    card = {"inferred_thresholds_used": ["min_years", {"key": "near_cap"}, ["x"], {"note": "y"}, "min_years"]}
    out = apply_cap_override(card, _cap())
    got = out["inferred_thresholds_used"]
    assert got[:2] == ["min_years", "near_cap"]                # 字串保留原序、dict 取 key
    assert all(isinstance(k, str) for k in got) and len(got) == len(set(got))
    assert any("略過非字串項目" in n for n in out["framework_notes"])
    assert card["inferred_thresholds_used"][1] == {"key": "near_cap"}   # 不改傳入的卡


def test_x46_p4_soft_A_only_when_multiplier_applied():
    """修前：A 級但增量 ROIC > r_d 未成立（P4_multiplier=1.0）仍列 p4_soft_A。"""
    c = _cap(grade="A")
    assert c.get("P4_multiplier") == 1.0 and "p4_soft_A" not in cap_inferred_keys(c)
    c = _cap(grade="A", incr_roic_gt_rd=True)
    assert c.get("P4_multiplier") == 1.2 and "p4_soft_A" in cap_inferred_keys(c)
