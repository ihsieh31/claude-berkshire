"""P51（DOC-1）：cap_calc 缺 norm_eps 的代用註記（M2_D2_08），以及規則文件與現行契約同步的最小鎖定
（D19 五處同文、ASK-1＝B 上限公式、DOSSIER 40k、FULL_A phaseA.json 說明）。"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEEP = ROOT / "berkshire-deep-merged"


def _cap_calc():
    spec = importlib.util.spec_from_file_location("cap_calc_p51", DEEP / "scripts" / "cap_calc.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BASE = {"oe": 4.0, "g10": 0.06, "g5": 0.07, "grade": "B", "long_rate": 0.045, "long_rate_20y_med": 0.035,
        "price": 60.0, "largecap_pe_median": 20.0}

NOTE = "缺 norm_eps：P4′／W-PE 以 oe（業主盈餘 E）代替正常化 EPS"


def test_cap_calc_notes_oe_substitute_when_norm_eps_missing():
    out = _cap_calc().calc(dict(BASE))
    assert NOTE in out["notes"]
    assert out["components"]["P4p"] == 20.0 * 4.0          # 判定不變：P4′ 照舊用 oe


def test_cap_calc_no_substitute_note_when_norm_eps_given():
    out = _cap_calc().calc(dict(BASE, norm_eps=5.0))
    assert NOTE not in out["notes"]
    assert out["components"]["P4p"] == 20.0 * 5.0


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


D19_KEYS = ("≥10% 市占", "≥2 年下降 >15%", "商品價格循環", "泛用樣板句不算")
D19_FILES = ("berkshire-deep-merged/references/rulebook.md",
             "berkshire-deep-merged/references/procedure/01_competence_moat.md",
             "berkshire-deep-merged/references/rule_index/01_selection.md",
             "routing/prompts/FULL_A.md",
             "berkshire-deep-merged/references/inferred_thresholds.md")


def test_d19_fast_change_definition_same_in_all_places():
    c5 = next(q for q in json.loads(_read("berkshire-deep-merged/assets/gate3_questionnaire.json"))["questions"]
              if q["id"] == "C5")["text"]
    texts = [_read(f) for f in D19_FILES] + [c5]
    for t in texts:
        for k in D19_KEYS[:2]:
            assert k in t, k
        assert "定價或單位量年變動 >15%" not in t and "把技術替代列為主要風險" not in t   # 舊口徑已刪
    for t in texts[:4] + [c5]:
        for k in D19_KEYS[2:]:
            assert k in t, k


def test_ask1_b_cap_formula_synced():
    for f in ("berkshire-deep-merged/SKILL.md", "berkshire-deep-merged/references/rulebook.md",
              "berkshire-deep-merged/references/procedure/12_auto_buy_gates.md"):
        t = _read(f)
        assert "k × `IV_base`" in t or "k × IV_base" in t, f
        assert "min(P2′, `IV_base`)" not in t and "min(P2′, IV_base)" not in t, f


def test_dossier_limit_and_full_a_phase_a_note():
    assert "≤35k" not in _read("berkshire-deep-merged/SKILL.md")
    a = _read("routing/prompts/FULL_A.md")
    out = next(ln for ln in a.splitlines() if ln.startswith("輸出："))
    assert "phaseA.json" in out and "框架" in out and "不要自己寫" in out
