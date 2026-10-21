"""P30T：HOLD_REVIEW 的 full_stage＝"FAIL"（深度鏈中途失敗，deep._missing_hold）。全部 FakeRunner，不連網。

深度鏈失敗不是「FULL_A 停在閘門」：提示要照實說沒有完整分析結論、無 IV_base（Q3 不適用），
前一輪 FULL_B 留下的 cap_result.json 也不得被當成 IV_base。"A"／"B"／"PRICE" 的文字不變。
"""
from __future__ import annotations

import json

from trader.jobs.hold_jobs import HoldReviewHandler, check, valuation_numbers

from test_engine import make_env
from test_hold_review import _holding, hold


def _full_status(env, params: dict) -> str:
    job = env.job("KO", "HOLD_REVIEW", 1, params=params)
    return HoldReviewHandler().prompt_values(job, env.engine.ctx, "fake")["full_status"]


def test_fail_stage_text_says_no_full_conclusion(tmp_path):
    env = make_env(tmp_path, {})
    _holding(env)
    env.price_file("KO")
    why = "FULL_B 輸出不合格：" + "缺欄位" * 200
    txt = _full_status(env, {"full_stage": "FAIL", "stopped_at_gate": "G7", "reason_code": "MISSING_DATA",
                             "fail_reason": why})
    assert txt.startswith("深度分析在 G7 失敗，未能完成（FULL_B 輸出不合格：")
    assert "MISSING_DATA" not in txt                          # P30R 後傳輸／準備失敗不一定寫 MISSING_DATA，不得聲稱已記
    assert txt.endswith("；本次沒有完整分析結論，無 IV_base")
    assert why[:300] in txt and why[:301] not in txt          # 原因最多 300 字
    assert "FULL_A 停在" not in txt and txt != "缺"
    # 沒帶原因也不崩，照實標缺
    assert "深度分析在 STEP0 失敗，未能完成（原因缺）" in _full_status(env, {"full_stage": "FAIL", "stopped_at_gate": "STEP0"})


def test_other_stage_texts_unchanged(tmp_path):
    env = make_env(tmp_path, {})
    _holding(env)
    env.price_file("KO")
    assert _full_status(env, {"full_stage": "A", "stopped_at_gate": "G3", "reason_code": "FAIL_G3"}) == \
        "FULL_A 停在 G3（FAIL_G3），未走到估值；無 IV_base"
    assert _full_status(env, {"full_stage": "B", "final_signal": "WATCH"}) == "走完 FULL_B，最終訊號 WATCH；IV_base None"
    assert _full_status(env, {"full_stage": "PRICE", "price": 60.5}) == (
        "價格複查（PRICE）觸發賣出檢視：現價 60.5 對 IV_base None（以最近完整分析的 cap_inputs 重算）；"
        "依 SKILL §6 第 2 條判斷是否有更好用途（減碼或換股）")
    assert _full_status(env, {}) == "缺"


def test_fail_stage_ignores_stale_cap_result_so_valuation_sell_rejected(tmp_path):
    env = make_env(tmp_path, {})
    _holding(env)
    env.price_file("KO")                                       # 價格 60.5
    td = env.paths.work / "r1" / "KO"
    (td / "cap_result.json").write_text(json.dumps({"IV_base": 50.0}), encoding="utf-8")  # 前一輪 FULL_B 殘留
    assert valuation_numbers(td, {"full_stage": "B"}) == (50.0, 60.5)
    iv, price = valuation_numbers(td, {"full_stage": "FAIL"})
    assert (iv, price) == (None, 60.5)
    assert "IV_base None" not in _full_status(env, {"full_stage": "FAIL", "stopped_at_gate": "G7"})
    errs, human = check(hold("SELL_ALL", "估值"), iv_base=iv, price=price, held_days=1000)
    assert not human and any("IV_base" in e for e in errs)    # Q3 不適用：估值類賣出被擋
