"""RM2-3：PRICE 交 HOLD_REVIEW 時延後寫 review_price（RM2-2 needs_other；D46、M2-RS8-01）。
HOLD_REVIEW 失敗（不合格保守結論）後，登錄表 review_price 仍是上次的值 → 下次 review_due 照常排出（邊緣觸發不被吃掉）。
（P-50 結案部分的測試見 test_fix_RM2_3_p50.py，隨 deep.py／hold_jobs.py／writeback.py 的修補一起加入。）"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from trader.analysis.checks import run_cap_calc
from trader.jobs.hold_jobs import HoldReviewHandler

from test_review_jobs import (ASOF, CAP_INPUT, _ctx, _job, _price_params, _price_result, _reg, _run_price,  # noqa: F401
                              _seed_registry, _write_cap_input, env)


def _review_due(paths):
    spec = importlib.util.spec_from_file_location("review_due_rm2_3", paths.skill_deep / "scripts" / "review_due.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_price_to_hold_review_failure_keeps_trigger(env, tmp_path):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "HOLDING", {"first_buy_date": "2020-01-01", "last_review_date": "2026-09-01",
                                           "last_full_review_date": "2026-06-01", "review_price": 50.0})
    f = tmp_path / "c.json"
    f.write_text(json.dumps(CAP_INPUT), encoding="utf-8")
    iv = run_cap_calc(f)["IV_base"]
    px = iv * 1.1
    job, nxt = _run_price(paths, conn, _price_params(price=px), _price_result(proposed_action="HOLD"))
    assert [j.job_type for j in nxt] == ["HOLD_REVIEW"]
    d = _reg(conn)["data"]
    assert d["review_price"] == 50.0 and d["last_review_date"] == ASOF   # 交 HOLD_REVIEW：review_price 不寫
    # HOLD_REVIEW 失敗 → 保守結論（不寫 review_price）
    hr = nxt[0]
    HoldReviewHandler().on_invalid(hr, ["測試：輸出不合格"], _ctx(paths, conn))
    e = {**_reg(conn)["data"], "ticker": "KO", "status": "HOLDING"}
    assert e["review_price"] == 50.0 and float(e["sell_review_above"]) <= px
    # 下一個交易日仍在線上：review_due 照常排出（review_price 在線下 → 上次分析時不算已滿足）
    nxt_day = (date.fromisoformat(ASOF) + timedelta(days=3)).isoformat()
    days = [date.fromisoformat(ASOF) - timedelta(days=i) for i in range(10, -4, -1)]
    closes = pd.DataFrame({"date": days, "close": [px] * len(days)})
    rd = _review_due(paths)
    action, reasons, _ = rd.check(e, [], closes, nxt_day, e.get("t30_at_valuation") or 0.047)
    assert action in ("PRICE", "THESIS", "FULL") and any("PRICE" in str(r) for r in reasons), (action, reasons)
    # 對照：若 review_price 被寫成本次價格（修前行為），同一情境不再觸發 PRICE
    a2, r2, _ = rd.check({**e, "review_price": px}, [], closes, nxt_day, e.get("t30_at_valuation") or 0.047)
    assert not any("PRICE" in str(r) for r in r2)
