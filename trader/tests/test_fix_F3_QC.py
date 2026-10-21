"""F3-QC：機械版業主盈餘比對（D26）的假差異與閘門 2 不變式。
A3-M2A-09 capex 正負號、A3-M2B-04 拆股未追溯、A3-M2C-06 4A 困境三問全過、A3-M2B-05（D93）g_adjustment_reason、
A3-M3C-06 財年 'FY2020'、A3-M2B-09 荒謬級線 ≤ 自動買進上限／IV_base。提示契約的 A3-M3A-01／A3-M2F-06 在 test_prompt_contract。"""
import json
from pathlib import Path

import pytest

from test_fix_P19 import _table_md
from trader.analysis import checks as C
from trader.analysis.checks import _skill_module, cap_signal_by_gates, gate2_failures
from trader.jobs.validate import check, load_schemas

DQ = _skill_module("deep_quant")
CAP = _skill_module("cap_calc")
N = 10
REPO = Path(__file__).resolve().parents[2]
SCHEMAS = load_schemas(REPO / "routing" / "output_schemas.json")


def _m(md, *a, **kw):
    return DQ.mechanical_oe(DQ.dossier_table(md), *a, **kw)


# ---------------------------------------------------------------- A3-M2A-09

def test_mechanical_oe_negative_capex_same_k():
    pos = _table_md([100.0] * N, [50.0] * N, [100.0] * N, [1000.0] * N, [10.0] * N)
    neg = _table_md([100.0] * N, [50.0] * N, [-100.0] * N, [1000.0] * N, [10.0] * N)
    assert DQ.dossier_table(neg)["capex"][2025] == -100.0
    a, b = _m(pos), _m(neg)
    assert a["K"] == pytest.approx(1.6) and b["K"] == pytest.approx(a["K"])   # 修前負數 → K 恆 1.0
    assert b["E"] == pytest.approx(a["E"]) and b["g10"] == a["g10"]
    md_neg = neg
    assert C.oe_check(md_neg, [], {"oe_per_share": a["E"], "K": 1.6, "g10": 0.0, "g5": 0.0})["diffs"] == []


# ---------------------------------------------------------------- A3-M2B-04

def test_mechanical_oe_split_not_restated_no_false_diff():
    # 每年 OE 總額不變；FY2021 起股數 50→100（拆股基準未追溯）
    md = _table_md([100.0] * N, [50.0] * N, [40.0] * N, [10000.0] * N, [50.0] * 5 + [100.0] * 5)
    m = _m(md)
    assert m["g10"] is None and m["g5"] == pytest.approx(0.0)    # g5 窗口 2021–2025 不跨跳動年
    assert "g10" in m["basis"] and "2021" in m["basis"] and "拆股" in m["basis"]
    assert m["E"] == pytest.approx(1.0)                           # E 以最新年股數換算，不受影響
    chk = C.oe_check(md, [], {"oe_per_share": 1.0, "g10": 0.0, "g5": 0.0, "excluded_shock_years": []})
    assert chk["diffs"] == []                                     # 修前：g10 模型 0、機械版 -0.0741
    # 拆股在 FY2023 → g10、g5 都不算
    m2 = _m(_table_md([100.0] * N, [50.0] * N, [40.0] * N, [10000.0] * N, [50.0] * 7 + [100.0] * 3))
    assert m2["g10"] is None and m2["g5"] is None and "g10／g5" in m2["basis"]
    # 正常範圍內的股數變化（×1.5）照算
    m3 = _m(_table_md([100.0] * N, [50.0] * N, [40.0] * N, [10000.0] * N, [10.0] * 5 + [15.0] * 5))
    assert m3["g10"] == pytest.approx((1 / 1.5) ** (1 / 9) - 1) and "跳動" not in m3["basis"]


def test_deep_quant_shares_jump_local_and_synced():
    """A3-M2B-04 返工：deep_quant 用本地 SHARES_JUMP，不匯入 build_dossier（單獨複製 deep skill 時 fetch_edgar 不在 →
    ModuleNotFoundError 被 deep._oe_check 吞掉＝失敗放行）；值須與 build_dossier 同步。"""
    BD = _skill_module("build_dossier")
    assert DQ.SHARES_JUMP == BD.SHARES_JUMP
    src = Path(DQ.__file__).read_text(encoding="utf-8")
    assert "import build_dossier" not in src and "from build_dossier" not in src


def test_deep_quant_standalone_copy_split_no_import_error(tmp_path):
    """只複製 deep_quant.py（無 build_dossier／fetch_edgar）時，股數跳動路徑照樣跑得出結果。"""
    import shutil, subprocess, sys
    shutil.copy(DQ.__file__, tmp_path / "deep_quant.py")
    ni = [100.0] * N
    sh = [50.0] * 5 + [100.0] * 5
    md = _table_md(ni, [50.0] * N, [50.0] * N, [1000.0] * N, sh)
    (tmp_path / "t.md").write_text(md, encoding="utf-8")
    code = ("import deep_quant as d,json;"
            "r=d.mechanical_oe(d.dossier_table(open('t.md',encoding='utf-8').read()));"
            "print(json.dumps({'g10':r['g10'],'basis':r['basis']},ensure_ascii=False))")
    out = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["g10"] is None and "股數跳動年" in r["basis"]


# ---------------------------------------------------------------- A3-M2C-06

def test_mechanical_oe_distress_pass_window_median():
    ni = [100.0] * 5 + [100.0, 10.0, 20.0, 200.0, 100.0]   # 2021–2025；三個衝擊年 → 非衝擊年只剩 2 年
    md = _table_md(ni, [50.0] * N, [40.0] * N, [10000.0] * N, [10.0] * N)
    shocks = [2022, 2023, 2024]
    base, dp = _m(md, shocks), _m(md, shocks, distress_pass=True)
    assert base["E"] == pytest.approx(8.6) and "平均" in base["basis"]
    assert dp["E"] == pytest.approx(10.0) and "4A 三問全過" in dp["basis"] and dp["years_used"] == list(range(2021, 2026))
    # 非衝擊年 ≥3 → 與預設相同
    assert _m(md, [2023], distress_pass=True) == _m(md, [2023])
    model = {"oe_per_share": 10.0, "excluded_shock_years": shocks}
    assert any(d.startswith("E：") for d in C.oe_check(md, shocks, model)["diffs"])          # 預設不變
    assert C.oe_check(md, shocks, model, distress_pass=True)["diffs"] == []


# ---------------------------------------------------------------- A3-M2B-05（D93）

def test_oe_check_g_adjustment_reason_notes_only():
    md = _table_md([100.0] * N, [50.0] * N, [40.0] * N, [10000.0] * N, [10.0] * N)   # E=10、K=1、g=0
    model = {"oe_per_share": 10.0, "K": 1.0, "g10": 0.07, "g5": 0.05, "excluded_shock_years": []}
    plain = C.oe_check(md, [], model)
    assert {d.split("：")[0] for d in plain["diffs"]} == {"g10", "g5"} and "g_notes" not in plain
    why = "VAL-013：基期近兩平，改用對數回歸"
    adj = C.oe_check(md, [], {**model, "g_adjustment_reason": why})
    assert adj["diffs"] == [] and len(adj["g_notes"]) == 2 and why in adj["g_notes"][0]
    assert adj["model"]["g_adjustment_reason"] == why
    # E、K 照核
    bad = C.oe_check(md, [], {**model, "oe_per_share": 13.0, "K": 1.5, "g_adjustment_reason": why})
    assert {d.split("：")[0] for d in bad["diffs"]} == {"E", "K"}
    # 空白理由＝沒填
    assert C.oe_check(md, [], {**model, "g_adjustment_reason": "  "})["diffs"] == plain["diffs"]


def test_schema_g_adjustment_reason_is_optional_comment_key():
    ni = SCHEMAS["FULL_A"]["normalized_inputs"]
    assert "g_adjustment_reason" not in ni and "g_adjustment_reason" in ni["_g_adjustment_reason"]
    assert "D93" in ni["_g_adjustment_reason"]
    full_a = (REPO / "routing" / "prompts" / "FULL_A.md").read_text(encoding="utf-8")
    assert "g_adjustment_reason" in full_a and "D93" in full_a


def test_schema_distress_pass_is_optional_comment_key():
    """A3-M2C-06 返工：distress_pass 是選填說明鍵（validate 跳過 _ 開頭），提示第 3 點同句。"""
    ni = SCHEMAS["FULL_A"]["normalized_inputs"]
    assert "distress_pass" not in ni and "distress_pass" in ni["_distress_pass"]
    assert "4A" in ni["_distress_pass"] and "三問全過" in ni["_distress_pass"] and "true" in ni["_distress_pass"]
    full_a = (REPO / "routing" / "prompts" / "FULL_A.md").read_text(encoding="utf-8")
    item3 = next(ln for ln in full_a.splitlines() if ln.startswith("3. "))
    assert "distress_pass" in item3 and "4A" in item3 and "三問全過" in item3


# ---------------------------------------------------------------- A3-M3C-06

def test_fy_accepts_int_and_fy_string():
    for v in (2020, 2020.0, "FY2020", "fy 2020", "2020", " FY2020 "):
        assert C._fy(v) == 2020, v
    for v in (None, True, 2020.5, "FY20", "abc", float("nan"), [2020]):
        assert C._fy(v) is None, v


def test_oe_check_fy_string_shock_years_and_adjustments():
    md = _table_md([100.0] * 9 + [60.0], [50.0] * N, [40.0] * N, [10000.0] * N, [10.0] * N)
    base = {"oe_per_share": 10.0, "g10": None, "g5": None}
    assert C.oe_check(md, [2020], {**base, "excluded_shock_years": ["FY2020"]})["diffs"] == []
    assert C.oe_check(md, [2020], {**base, "excluded_shock_years": [2020]})["diffs"] == []
    a_int = C.oe_check(md, [], {**base, "one_time_adjustments": [{"fy": 2025, "amount": 40.0, "source": "10-K"}]})
    a_str = C.oe_check(md, [], {**base, "one_time_adjustments": [{"fy": "FY2025", "amount": 40.0, "source": "10-K"}]})
    assert a_int["adjustments_by_fy"] == a_str["adjustments_by_fy"] == {2025: 40.0}
    assert a_str["mechanical_adjusted"] == a_int["mechanical_adjusted"]


def test_schema_shock_years_example_is_integer():
    ni = SCHEMAS["FULL_A"]["normalized_inputs"]
    assert ni["excluded_shock_years"] == [2020] and "整數" in ni["_excluded_shock_years"]
    assert "整數財年" in ni["_one_time_adjustments"]
    _, errors = check("FULL_A", {"normalized_inputs": {"excluded_shock_years": [2020]}}, SCHEMAS)
    assert not any("excluded_shock_years" in e for e in errors)
    _, errors = check("FULL_A", {"normalized_inputs": {"excluded_shock_years": []}}, SCHEMAS)
    assert not any("excluded_shock_years" in e for e in errors)


# ---------------------------------------------------------------- A3-M2B-09

BASE = {"long_rate": 0.047, "long_rate_20y_med": 0.035, "price": 1.0, "grade": "B", "oe": 1.0, "g10": 0.05, "g5": 0.05}


def test_gate2_absurd_line_not_above_auto_buy_cap_fails():
    r = CAP.calc({**BASE, "g10": 0.07, "g5": 0.07, "norm_eps": 0.3})   # e_lows1.py 原例
    line = 50 * r["price"] / r["norm_pe"]
    assert line == pytest.approx(15.0) and r["auto_buy_cap_raw"] > line      # 原報：15.22 > 15（D90 後 auto_buy_cap＝0.95×15.22）
    keys = set(r)
    g2 = gate2_failures(r)
    assert any("荒謬級線" in x and "IV_base" in x for x in g2)
    assert set(r) == keys                                                     # 不改 cap_calc 輸出
    sig, why = cap_signal_by_gates("BUY", {"gate3_pass": True}, "通過", r)
    assert sig == "WATCH" and any("荒謬級線" in w for w in why)
    ok = CAP.calc(dict(BASE))                                                 # norm_eps 缺 → E＝oe，線 50 高於上限
    assert not any("荒謬級線" in x for x in gate2_failures(ok))


def test_gate2_absurd_line_vs_iv_base_and_missing_values():
    cap = {"auto_buy_cap": 10.0, "price": 5.0, "grade_used": "B", "conclusion_test": None,
           "norm_pe": 20.0, "IV_base": 12.5}                                  # 線＝12.5 ≤ IV_base
    assert any("荒謬級線" in x and "IV_base" in x for x in gate2_failures(cap))
    assert not any("荒謬級線" in x for x in gate2_failures({**cap, "IV_base": 12.0}))
    for k in ("norm_pe", "price"):
        assert not any("荒謬級線" in x for x in gate2_failures({**cap, k: None}))
