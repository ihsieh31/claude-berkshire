"""RM2-2：M2 複審的 writeback 修復。
- M2-RS3-01／M2_D7_05：估值類 SELL_ALL（rulebook 3b 全清）本系統不採 → HOLD＋停止加碼（D01(3)、X15）；
  價格 ≥ 荒謬級線時依荒謬級 TRIM 下限 1/2（D01(2)、D03：只減一次、記 last_trim）。
- M2-RS7-01／M2-RS8-01：交下游（THESIS also_price／base_action=PRICE → PRICE；PRICE → HOLD_REVIEW）時先不寫
  review_price，下游成功才寫；下游失敗或不合格時邊緣觸發下期照常重排（D46）。
- M2_D9_06：完整分析決策日誌的 g_impl、r_d、buy_price_cap、price_to_cap 取 cap_result（與 PRICE 日誌、訊號卡同口徑）。
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import pandas as pd
import pytest

import test_fix_P24 as P24
import test_fix_P45 as P45
from test_engine import RUN, full_a, make_env
from test_hold_review import _holding, hold
from trader import db
from trader.analysis import writeback as W
from trader.paths import get_paths

env = P24.env   # (paths, conn)
ASOF = P24.ASOF
Q3B = [{"q": "Q3b", "answer": "是", "evidence": "價格≥IV_base，普通企業"}]


def _sell_all(path=Q3B):
    return {**P24._hr(None, path), "action": "SELL_ALL", "trim_fraction": None}


def _reg(conn, t="KO"):
    return json.loads(conn.execute("SELECT data FROM registry WHERE ticker=?", (t,)).fetchone()["data"])


def _set_reg(conn, t="KO", **kv):
    d = _reg(conn, t)
    d.update(kv)
    conn.execute("UPDATE registry SET data=? WHERE ticker=?", (json.dumps(d), t))
    conn.commit()


# ---------------------------------------------------------------- M2-RS3-01：估值類 SELL_ALL 不全清

def test_valuation_sell_all_after_two_years_is_hold(env):
    """滿兩年、價格 ≥ IV_base、未達荒謬級：估值 SELL_ALL → HOLD＋停止加碼，不記 last_trim。"""
    paths, conn = env
    P24._seed(conn, qty=100, absurd=260.0)            # 首次建倉 2024-01-02（> 2 年）
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _sell_all(), price=120.0, iv_base=100.0)
    log = P24._log(conn, did)
    assert log["action"] == "HOLD" and log["stop_adding"] is True and log["trim_fraction"] is None
    assert any("3b" in n and "D01" in n for n in log["framework_notes"])
    assert "last_trim" not in _reg(conn)
    assert conn.execute("SELECT signal FROM decisions WHERE decision_id=?", (did,)).fetchone()["signal"] == "HOLD"


def test_valuation_sell_all_at_absurd_is_absurd_trim_once(env):
    """價格 ≥ 荒謬級線：估值 SELL_ALL 當荒謬級 TRIM 下限（1/2；sizing 只認 0<比例<1），記 last_trim；回到線下前第二次 → HOLD。"""
    paths, conn = env
    P24._seed(conn, qty=100, absurd=260.0)
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _sell_all(), price=300.0, iv_base=100.0)
    log = P24._log(conn, did)
    assert log["action"] == "TRIM" and log["trim_fraction"] == 0.5
    assert _reg(conn)["last_trim"]["shares"] == 50
    assert _reg(conn)["last_trim"]["reason_code"] == "ABSURD"
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r2", _sell_all(), price=300.0, iv_base=100.0)
    assert P24._log(conn, did)["action"] == "HOLD"


def test_business_sell_all_unaffected(env):
    paths, conn = env
    P24._seed(conn, qty=100)
    for cat in ("估值＋業務", "業務"):
        r = {**_sell_all(), "sell_category": cat, "falsified_assumption": "假設 A 被 10-Q 否定"}
        did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r" + cat, r, price=120.0, iv_base=100.0)
        assert P24._log(conn, did)["action"] == "SELL_ALL"


# ---------------------------------------------------------------- M2-RS7-01：THESIS 續接 PRICE 不寫 review_price

@pytest.mark.parametrize("params", [{"also_price": True}, {"base_action": "PRICE"}])
def test_thesis_with_chained_price_keeps_review_price(env, params):
    paths, conn = env
    P24._seed(conn)
    _set_reg(conn, review_price=100.0, last_review_date="2026-09-01")
    W.write_thesis_result(conn, paths, "KO", ASOF, "r", {"verdict": "完好", "reason": "x"},
                          {**params, "price": 106.0, "holding": True})
    d = _reg(conn)
    assert d["review_price"] == 100.0 and d["last_review_date"] == ASOF


def test_thesis_without_chained_price_still_writes_review_price(env):
    paths, conn = env
    P24._seed(conn)
    _set_reg(conn, review_price=100.0, last_review_date="2026-09-01")
    W.write_thesis_result(conn, paths, "KO", ASOF, "r", {"verdict": "完好", "reason": "x"},
                          {"price": 106.0, "holding": True})
    assert _reg(conn)["review_price"] == 106.0


# ---------------------------------------------------------------- M2-RS8-01：PRICE 交 HOLD_REVIEW；HOLD_REVIEW 成功才寫

def _price(conn, paths, defer):
    return W.write_price_result(conn, paths, "KO", ASOF, "rp", action="HOLD", gate1=True, gate2="不通過",
                                gate3="通過", cap=P45.CAP, price=128.0, price_date=ASOF, t30=0.046, reason="x",
                                notes=[], defer_review_price=defer)


def test_price_handing_to_hold_review_defers_review_price(env):
    paths, conn = env
    P24._seed(conn)
    _set_reg(conn, review_price=100.0, last_review_date="2026-09-01")
    _price(conn, paths, defer=True)
    d = _reg(conn)
    assert d["review_price"] == 100.0 and d["last_review_date"] == ASOF and d["sell_review_above"] == 80.0
    _price(conn, paths, defer=False)                  # 預設行為不變
    assert _reg(conn)["review_price"] == 128.0


def test_hold_review_record_price_only_on_success(env):
    paths, conn = env
    P24._seed(conn)
    _set_reg(conn, review_price=100.0)
    W.write_hold_review_result(conn, paths, "KO", ASOF, "r", hold(), price=128.0, iv_base=80.0)   # 保守結論
    assert _reg(conn)["review_price"] == 100.0
    W.write_hold_review_result(conn, paths, "KO", ASOF, "r2", hold(), price=128.0, iv_base=80.0, record_price=True)
    assert _reg(conn)["review_price"] == 128.0


def _engine_reg(tmp_path, hr):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": hr})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    before = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])
    env.engine.run(RUN)
    after = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])
    return before, after, env


def test_hold_review_handler_post_records_price(tmp_path):
    _, ok, _ = _engine_reg(tmp_path, hold())
    assert isinstance(ok.get("review_price"), (int, float))          # post 成功 → 寫


def test_hold_review_handler_on_invalid_keeps_review_price(tmp_path):
    before, bad, env = _engine_reg(tmp_path, {"action": "不合法"})
    assert "HOLD_REVIEW" in [r["job_type"] for r in env.rows()]
    assert _last(env)["review_type"] == "HOLD_REVIEW"                 # 確實走到 on_invalid 的保守 HOLD
    assert bad.get("review_price") == before.get("review_price")     # 不寫：邊緣觸發下期照常重排


def _last(env):
    return json.loads(env.conn.execute("SELECT data FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()["data"])


def test_review_due_retriggers_after_downstream_failure():
    """review_due 端：review_price 保留舊（線下）值 → 下期邊緣觸發仍排（M2-RS8-01 重現序列）。"""
    sys.path.insert(0, str(get_paths().skill_deep / "scripts"))
    from review_due import check

    def bdays(start, n):
        d, out = date.fromisoformat(start), []
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out
    days = bdays("2026-06-01", 120)
    closes = [100.0] * 90 + [100 * 1.025 ** i for i in range(1, 11)] + [128.0] * 20
    px = pd.DataFrame({"date": days, "close": closes})
    D1 = days[100].isoformat()
    base = {"ticker": "AAA", "status": "HOLDING", "cost": 80, "sell_review_above": 120, "absurd_price_line": 1000,
            "auto_buy_cap": 60, "weight": 0.05, "target_weight": 0.05, "t30_at_valuation": 0.047,
            "last_full_review_date": "2026-06-01", "first_buy_date": "2025-01-02", "last_review_date": D1}
    a = days[105].isoformat()
    sub = px[px.date <= days[105]]
    assert check(dict(base, review_price=128.0), [], sub, a, 0.047)[0] == "NONE"     # 舊行為：觸發被吃掉
    assert check(dict(base, review_price=100.0), [], sub, a, 0.047)[0] != "NONE"     # 延後寫：照常重排


# ---------------------------------------------------------------- M2_D9_06：完整分析日誌估值欄取 cap_result

def test_full_result_log_valuation_fields_from_cap_result(env, tmp_path):
    paths, conn = env
    td = P45._tdir(tmp_path, "a", "KO", "WATCH", cap_input=True)
    (td / "decision_log.json").write_text(json.dumps({"action": "WATCH", "g_impl": 0.99, "r_d": 0.99,
                                                      "buy_price_cap": 999.0, "price_to_cap": 9.9}), encoding="utf-8")
    cr = {"IV_base": 160.0, "auto_buy_cap": 150.0, "cap": 140.0, "g_impl": 0.03, "r_d": 0.09,
          "price_to_auto_buy_cap": 0.6667, "grade_used": "B"}
    (td / "cap_result.json").write_text(json.dumps(cr), encoding="utf-8")
    did = W.write_full_result(conn, paths, "KO", ASOF, "r", td)
    log = P24._log(conn, did)
    assert (log["g_impl"], log["r_d"], log["buy_price_cap"], log["price_to_cap"]) == (0.03, 0.09, 140.0, 0.6667)
    assert log["auto_buy_cap"] == 150.0 and log["iv_base"] == 160.0
