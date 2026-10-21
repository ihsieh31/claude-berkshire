"""P52（DOC-2）：procedure／inferred_thresholds／assets／腳本說明與 FULL_B 提示的文件契約鎖定。

不改判定；鎖住：P-21 只由 cap_calc 套用（D22）、降級 driver_id（D18）、P-29 口徑群損失（D11）、典型價成交（D07）、
review_due 觸發判準表與腳本常數一致（M2-D5-03）、P-08 計分清單（M2-D5-13）、價格盲分階段（M2_D6_03）、
訊號卡欄位定義（M2-D5-11）、困境 E 口徑（M2_D2_07）、引用路徑（M2_D1_10）、P-30 列與登錄表欄位說明。"""
import importlib.util
import json
import re
from pathlib import Path

from trader.jobs.validate import check, load_schemas

ROOT = Path(__file__).resolve().parents[2]
DEEP = ROOT / "berkshire-deep-merged"
PROC = DEEP / "references" / "procedure"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _mod(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, DEEP / "scripts" / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------- M2_D2_03／D22：P-21 只降一次

def test_p21_owned_by_cap_calc_in_docs_and_prompt():
    p06 = _read(PROC / "06_certainty_position.md")
    row = next(ln for ln in p06.splitlines() if ln.startswith("| P-21"))
    assert "cap_calc" in row and "非金融" in row and "不得先含" in row and "成長離散度" in row
    fb = _read(ROOT / "routing" / "prompts" / "FULL_B.md")
    assert re.search(r"不得先含 P-21", fb) and "D22" in fb
    assert "不得先含 P-21" in _read(DEEP / "scripts" / "cap_calc.py")


def test_cap_calc_p21_downgrades_input_grade_once():
    cc = _mod("cap_calc_p52", "cap_calc.py")
    base = {"oe": 4.0, "g10": 0.12, "g5": -0.08, "grade": "B", "long_rate": 0.045, "long_rate_20y_med": 0.035,
            "price": 60.0, "largecap_pe_median": 20.0}
    out = cc.calc(dict(base))
    w = out["width_opt_over_cons"]
    assert w is not None and 3 < w <= 4
    assert out["grade_used"] == "C"   # B 只降一級
    assert cc.calc(dict(base, grade="C"))["grade_used"] == "D"   # 模型若先降成 C 再輸入就會變 D——文件禁止


# ---------------------------------------------------------------- D18：driver_id；06:14 只適用非小資金

def test_procedure06_driver_column_and_small_capital_scope():
    p06 = _read(PROC / "06_certainty_position.md")
    assert "driver_id（建議）" in p06 and "D18" in p06
    good = next(ln for ln in p06.splitlines() if ln.startswith("| 良好型"))
    assert "只適用非小資金" in good
    # 建議 id 不重複（同一 id 只能出現一次，validate 擋重複）
    ids = []
    for ln in p06.split("**逐項降一級**")[1].split("A 級須稀少")[0].splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if ln.startswith("|") and len(cells) == 3 and cells[2] not in ("—", "driver_id（建議）", "---"):
            ids += [x for x in cells[2].split("／")]
    assert len(ids) == len(set(ids)) and len(ids) >= 18


# ---------------------------------------------------------------- D11：群最壞相對損失 P-29 口徑

def test_procedure06_group_worst_loss_p29_basis():
    p06 = _read(PROC / "06_certainty_position.md")
    ln = next(x for x in p06.splitlines() if x.startswith("- 群上限"))
    assert "P-29" in ln and "下限 25%" in ln and "缺 tail" in ln and "binding" in ln and "D11" in ln


# ---------------------------------------------------------------- D07：典型價

def test_procedure13_typical_price_fill():
    p13 = _read(PROC / "13_forward_validation.md")
    assert "開盤價" not in p13
    assert "典型價" in p13 and "紙上交易近似" in p13 and "D07" in p13


# ---------------------------------------------------------------- M2-D5-03：觸發判準表 ↔ review_due 常數

def test_trigger_table_matches_review_due_constants():
    rd = _mod("review_due_p52", "review_due.py")
    p07 = _read(PROC / "07_conclusion_execution.md")
    sec = p07.split("## 7. 兩週檢查的觸發判準")[1]
    for code in list(rd.MATERIAL_8K) + list(rd.IGNORE_8K):
        assert code in sec, code
    src = _read(DEEP / "scripts" / "review_due.py")
    assert "timedelta(days=92)" in src and "92 個曆日" in sec
    assert "[-20:]" in src and "20 筆" in sec
    assert '"10-Q", "10-Q/A", "10-K/A"' in src and "10-K/A" in sec
    assert '"10-K", "20-F", "40-F"' in src and "40-F" in sec
    assert "procedure/07_conclusion_execution.md §7" in src            # docstring 引用
    inf = _read(DEEP / "references" / "inferred_thresholds.md")
    for k in ("quarter_drawdown_window", "material_8k_items", "amendment_level", "triage_cap"):
        assert f"| {k} |" in inf


# ---------------------------------------------------------------- M2-D5-13：P-08 清單

def test_procedure01_lists_p08_items():
    p01 = _read(PROC / "01_competence_moat.md")
    for i in range(1, 14):
        assert f"| B{i} |" in p01
    assert "D61" in p01 and "財報" in p01
    assert "| p08_denominator |" in _read(DEEP / "references" / "inferred_thresholds.md")


# ---------------------------------------------------------------- M2_D6_03（含 M2_D4_08）：價格盲分階段

def test_price_blind_stage_split_and_full_b_field():
    p01 = _read(PROC / "01_competence_moat.md")
    assert "SEL-034 待 G7 判例外" in p01
    p02 = _read(PROC / "02_management.md")
    assert "P-26a 分兩階段" in p02 and "capital_allocation_net_after_price" in p02
    fb = _read(ROOT / "routing" / "prompts" / "FULL_B.md")
    assert "SEL-034 待 G7 判例外" in fb and "capital_allocation_net_after_price" in fb
    schemas = load_schemas(ROOT / "routing" / "output_schemas.json")
    assert "capital_allocation_net_after_price" in schemas["FULL_B"]["_說明"]
    assert "capital_allocation_net_after_price" not in {k for k in schemas["FULL_B"] if not k.startswith("_")}  # 選填
    p08 = _read(PROC / "08_data_requirements.md")
    assert re.search(r"月價格（價格類", p08) and re.search(r"分析師預估（價格類", p08)


def test_validate_accepts_capital_allocation_net_after_price():
    schemas = load_schemas(ROOT / "routing" / "output_schemas.json")
    o = {"ticker": "KO", "decision_id": None, "signal": "WATCH", "reason_code": "PASS_ALL", "stopped_at_gate": "G7",
         "certainty_grade": "B", "auto_buy_cap": 50.0, "target_weight": None, "first_tranche_weight": None,
         "intended_weight": {"level": None, "reason": None}, "external_drivers": [],
         "external_drivers_change_reason": None,
         "g8": {"self_adjusted": None, "best_holding": None, "best_holding_adjusted": None,
                "weakest_holding_adjusted": None, "best_watch": None, "best_watch_adjusted": None,
                "treasury": None, "market": None, "winner": "未比較"},
         "files": ["KO/report.md"]}
    _, errs = check("FULL_B", dict(o), schemas)
    assert errs == []
    _, errs = check("FULL_B", dict(o, capital_allocation_net_after_price=-0.5), schemas)
    assert errs == []


# ---------------------------------------------------------------- M2-D5-11、M2_D2_07：訊號卡

def test_signal_card_field_definitions():
    sc = json.loads(_read(DEEP / "assets" / "signal_card_schema.json"))
    assert "x98_clauses_used" not in sc
    note = sc["_說明"]
    for k in ("capital_scale", "thesis_id", "delay_one_cycle", "ledger", "confidence", "OMISSION_CANDIDATE",
              "single_event", "框架會補列"):
        assert k in note, k
    assert "50×正常化 EPS" in note and "50×E（" not in note
    assert "binding" in sc["correlated_group"]["_說明"]
    assert sc["distress_check"]["earnings_normalization"].startswith("ex_shock_median")


def test_procedure11_e_follows_skill():
    p11 = _read(PROC / "11_distress_check.md")
    assert "排除單一谷底年" not in p11
    assert p11.count("E 照 SKILL §3") == 2


# ---------------------------------------------------------------- 登錄表、P-30 列、SKILL §10

def test_registry_peak_qty_entry_norm_pe_documented():
    entry = json.loads(_read(DEEP / "assets" / "registry_schema.json"))["entries"][0]
    assert "峰值" in entry["peak_qty"] and "norm_eps" in entry["entry_norm_pe"]


def test_procedure08_p30_row():
    row = next(ln for ln in _read(PROC / "08_data_requirements.md").splitlines() if ln.startswith("| P-30"))
    for k in ("2.02", "申報日", "次一交易日", "較差", "近 8 次", "null", "P-30 未檢查", "single_event"):
        assert k in row, k


def test_skill_section10_framework_fills_inferred_keys():
    s = _read(DEEP / "SKILL.md")
    sec10 = s.split("## 10.")[1].split("## 11.")[0]
    assert "框架會依實際分支補列" in sec10
    assert "price_context" not in s   # FULL_A 整份貼入（test_engine 價格隔離）


# ---------------------------------------------------------------- M2_D1_10：引用路徑；P-23／≤ 口徑

def test_no_dangling_reference_paths():
    files = [DEEP / "scripts" / "cap_calc.py", DEEP / "scripts" / "deep_quant.py",
             DEEP / "assets" / "gate3_questionnaire.json", DEEP / "assets" / "decision_log_schema.json",
             DEEP / "references" / "rulebook.md", DEEP / "references" / "rule_index" / "07_process_psychology.md",
             PROC / "11_distress_check.md"]
    bad = re.compile(r"assets/templates\.md|references/(0[1-9]|1[0-3])_|references/05 §|\.\./\.\./EVIDENCE_MANIFEST")
    for f in files:
        assert not bad.search(_read(f)), f


def test_rulebook_p23_and_rule_index07_strict_lt():
    rb = _read(DEEP / "references" / "rulebook.md")
    assert "距自動買進上限 <5%" in next(ln for ln in rb.splitlines() if ln.startswith("| P-23"))
    ri = _read(DEEP / "references" / "rule_index" / "07_process_psychology.md")
    assert not re.search(r"≤\s*(買價)?上限", ri.split("\n", 1)[1])   # 檔頭覆寫清單本身寫「≤ → <」
    assert "k×IV_base" in ri.splitlines()[0]
