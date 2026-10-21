"""X51（設計決策 ASK-3＝B）：P-08 品質計分門檻按「可計項」比例換算的文件契約。

計分由模型依文件執行（Python 不算 P-08），這裡鎖住各文件的同一套門檻：
可計 7 項（前半 B3、B4、B8、B12、B13；FULL_B 看價後補評 B7、B11）；需母體 6 項記缺不計分母；
≥60% 優／40–60% 可／<40% 降一級不變；有資料 <4 項 → 最高可；前半有資料 <3 項（因財報缺）→ MISSING_DATA；
前半暫定、FULL_B 最終只能更嚴；HOLD_REVIEW 頂級複利型的品質優只認完整分析的「P-08 最終」。"""
from pathlib import Path

from trader.jobs.hold_jobs import sell_tree_text

ROOT = Path(__file__).resolve().parents[2]
DEEP = ROOT / "berkshire-deep-merged"
REF = DEEP / "references"
PROMPTS = ROOT / "routing" / "prompts"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _line(text: str, key: str) -> str:
    hits = [ln for ln in text.splitlines() if key in ln]
    assert len(hits) == 1, (key, hits)
    return hits[0]


def test_old_fixed_counts_gone_from_host_rules():
    """舊制「<8 項最高可、<5 項 MISSING_DATA」與「B7／B11 不回補」不再是宿主門檻（只留在 rule_index/01 原文並附換算註）。"""
    row = _line(_read(REF / "rulebook.md"), "| P-08 |")
    assert "<8 項" not in row and "<5 項" not in row
    p01 = _read(REF / "procedure" / "01_competence_moat.md")
    assert "不回補" not in p01 and "<8 項" not in p01
    assert "不回補" not in _line(_read(REF / "inferred_thresholds.md"), "| p08_denominator |")
    sel = _line(_read(REF / "rule_index" / "01_selection.md"), "**SEL-078 通過標準**")
    assert "宿主模式" in sel and "<4 項" in sel and "<3 項" in sel and "ASK-3" in sel


def test_thresholds_consistent_across_docs():
    row = _line(_read(REF / "rulebook.md"), "| P-08 |")
    p01 = _read(REF / "procedure" / "01_competence_moat.md")
    den = _line(_read(REF / "inferred_thresholds.md"), "| p08_denominator |")
    bonus = _line(_read(REF / "inferred_thresholds.md"), "| bonus_rate |")
    for doc in (row, p01, den):
        assert "7 項" in doc and "<4 項" in doc and "<3 項" in doc and "ASK-3" in doc
        for b in ("B3", "B4", "B8", "B12", "B13", "B7", "B11"):
            assert b in doc
        assert "財報" in doc and "MISSING_DATA" in doc
    assert "≥60%" in row and "40–60%" in row and "<40%" in row       # 通過率門檻不變
    assert "≥4 項" in bonus and "ASK-3" in bonus
    # B7、B11 改由 FULL_B 看價後補評
    assert "| B7 |" in p01 and "FULL_B 看價後補評" in _line(p01, "| B7 |")
    assert "FULL_B 看價後補評" in _line(p01, "| B11 |")
    assert "暫定" in p01 and "P-08 最終" in p01 and "只能更嚴" in p01


def test_full_a_scores_five_items_provisionally():
    fa = _read(PROMPTS / "FULL_A.md")
    ln = _line(fa, "P-08 暫定")
    for b in ("B3", "B4", "B8", "B12", "B13"):
        assert b in ln
    assert "<3 項" in ln and "財報缺" in ln and "MISSING_DATA" in ln
    assert "<40%" in ln and "品質加分" in ln and "前半不判品質優" in ln


def test_full_b_final_score_only_stricter():
    fb = _read(PROMPTS / "FULL_B.md")
    assert "SEL-034 例外、P-26a、P-08 最終、步 4A" in fb                 # 降級先套用到 cap_input grade
    ln = _line(fb, "P-08 最終（procedure/01 §6")
    assert "B7" in ln and "B11" in ln and "IV_cons" in ln and "7 項" in ln
    assert "≥60% 且有資料 ≥4 項" in ln and "品質加分" in ln and "不重複降" in ln
    assert "report.md" in ln                                             # HOLD_REVIEW 讀 report.md
    assert "不因 P-08 停 MISSING_DATA" in ln


def test_hold_review_quality_requires_final_p08():
    hr = _read(PROMPTS / "HOLD_REVIEW.md")
    ln = _line(hr, "頂級複利型（3a、3c）的「品質優」")
    assert "P-08 最終" in ln and "不算品質優" in ln and "PRICE" in ln
    # 賣出決策樹原文（HOLD_REVIEW 的 {sell_tree}）與 rule_index/04 同樣說明
    tree = sell_tree_text(DEEP)
    assert "3c 只有 V，頂級複利型" in tree and "最終 P-08" in tree and "無此分數不算優" in tree
    ri4 = _line(_read(REF / "rule_index" / "04_hold_sell.md"), "3c 只有 V；頂級複利型")
    assert "最終 P-08" in ri4


def test_prompts_have_no_new_format_braces():
    """新增句不得帶 {}（提示以 format 代入）。"""
    for name in ("FULL_A.md", "FULL_B.md", "HOLD_REVIEW.md"):
        for ln in _read(PROMPTS / name).splitlines():
            if "ASK-3" in ln:
                assert "{" not in ln and "}" not in ln, (name, ln)
