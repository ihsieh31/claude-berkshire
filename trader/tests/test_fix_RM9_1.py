"""RM9-1：M2-RS2-02（問卷停止級答案補齊）、M2-RS2-03（GATE3_FAIL 觀察檔不排 PRICE、重跑完整分析一次）。"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from trader.analysis import writeback as W

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
from review_due import check  # noqa: E402

PASS = {"C1": "10年寫得出", "C2": "是", "C3": "是", "C4": "是", "C5": "不屬於", "M1": "強", "M2": "強", "M3": "弱",
        "M4": "是", "M5": "持平", "M6": "偉大"}


def _verdict(tmp_path, **chg):
    a = {**PASS, **chg}
    (tmp_path / "gate3_result.json").write_text(json.dumps({"gate3_pass": False}), encoding="utf-8")
    (tmp_path / "gate3_answers.json").write_text(
        json.dumps({"answers": {q: {"answer": v} for q, v in a.items()}}, ensure_ascii=False), encoding="utf-8")
    return W.gate3_verdict(tmp_path)


@pytest.mark.parametrize("chg,code", [({"C2": "否"}, "TOO_HARD"), ({"C3": "否"}, "TOO_HARD"), ({"C4": "否"}, "TOO_HARD"),
                                      ({"C5": "屬於且加嚴條件未全部成立"}, "TOO_HARD"), ({"M4": "否"}, "REJECT"),
                                      ({"M2": "弱"}, "REJECT"), ({"C1": "寫不到5年"}, "TOO_HARD")])
def test_stop_answers(tmp_path, chg, code):
    v = _verdict(tmp_path, **chg)
    assert v[0] == "NO_ACTION" and v[1] == code


def test_format_only_failure_stays_gate3_fail(tmp_path):
    assert _verdict(tmp_path)[1] == "GATE3_FAIL"


def _px(vals, end="2026-10-15"):
    d0 = date.fromisoformat(end)
    return pd.DataFrame({"date": [d0 - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))], "close": vals})


def test_review_due_skips_price_for_gate3_fail_watch():
    base = dict(ticker="X", status="WATCH", grade="B", last_review_date="2026-10-01", last_full_review_date="2026-06-01",
                review_price=100, t30_at_valuation=0.047, auto_buy_cap=80, sell_review_above=130, absurd_price_line=300)
    low = _px([70.0] * 120)
    assert check(base, [], low, "2026-10-15", 0.047)[0] == "PRICE"
    assert check({**base, "last_reason_code": "GATE3_FAIL"}, [], low, "2026-10-15", 0.047)[0] == "NONE"


def test_oe_check_one_time_adjustments_close_gap():
    """D73b：模型逐項列出的一次性調整加回機械版後再比 E。"""
    sys.path.insert(0, str(ROOT / "trader" / "tests"))
    from test_fix_P19 import _dossier_with_table
    from trader.analysis.checks import oe_check
    md = _dossier_with_table()["files"]["dossier/KO.md"]
    model = {"oe_per_share": 5.0, "g10": 0.0, "g5": 0.0, "excluded_shock_years": []}
    assert any(d.startswith("E：") for d in oe_check(md, [], model)["diffs"])
    adj = [{"fy": y, "item": "減損", "amount": 100.0, "source": "10-K"} for y in range(2021, 2026)]
    chk = oe_check(md, [], {**model, "one_time_adjustments": adj})
    assert not any(d.startswith("E：") for d in chk["diffs"]) and chk["mechanical_adjusted"]["E"] == pytest.approx(5.0)
    no_src = [{**a, "source": ""} for a in adj]   # 無出處的調整不採計
    assert any(d.startswith("E：") for d in oe_check(md, [], {**model, "one_time_adjustments": no_src})["diffs"])
