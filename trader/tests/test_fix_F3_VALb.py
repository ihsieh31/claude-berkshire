"""F3-VALb：A3-M2C-01 P-08 最終降級在 FullBHandler.post 生效、06 文件 max_grade。"""
from pathlib import Path
from trader.jobs import validate
ROOT = Path(__file__).resolve().parents[2]

def test_after_price_cap_p08_card():
    pa = {"starting_grade": "A", "downgrades": []}
    cap, why = validate.after_price_grade_cap(pa, {}, {"p08_final": {"quality": "降級"}})
    assert cap == "B" and any("P-08" in w for w in why)

def test_fullb_post_calls_after_price_cap_with_card():
    s = (ROOT / "trader/trader/jobs/deep.py").read_text(encoding="utf-8")
    assert "validate.after_price_grade_cap(pa, result, card)" in s

def test_doc06_max_grade():
    s = (ROOT / "berkshire-deep-merged/references/procedure/06_certainty_position.md").read_text(encoding="utf-8")
    assert "max_grade" in s
