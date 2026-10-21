"""X40：P47 的 needs_other_files（writeback 端）。
(a) 持股完整分析回寫保留 broker 寫的 peak_qty／entry_norm_pe（_HELD_KEYS）；
(b) _vet_valuation_sell 的峰值＝max(現有股數, last_trim.peak_shares, 登錄表 peak_qty)：
    硬上限或人工減碼（不寫 last_trim）之後，3c「累計保留 ≥ 峰值 2/3」不再偏鬆（設計決策 D03、M2_D4_06）。"""
import json

import pytest

import test_fix_P24 as P24
import test_fix_P45 as P45
from trader import db
from trader.analysis import writeback as W
from trader.ledger import book, broker

env = P24.env   # (paths, conn)


def _reg_data(conn, t="KO"):
    return json.loads(conn.execute("SELECT data FROM registry WHERE ticker=?", (t,)).fetchone()["data"])


def _set_reg(conn, t="KO", **kv):
    d = _reg_data(conn, t)
    d.update(kv)
    conn.execute("UPDATE registry SET data=? WHERE ticker=?", (json.dumps(d), t))
    conn.commit()


# ---------------------------------------------------------------- (a) _HELD_KEYS

def test_holding_full_review_keeps_peak_qty_and_entry_norm_pe(env, tmp_path):
    paths, conn = env
    W.write_full_result(conn, paths, "KO", "2026-10-01", "r0", P45._tdir(tmp_path, "a", "KO", "WATCH"))
    conn.execute("UPDATE registry SET status='HOLDING'")
    db.insert(conn, "positions", {"ticker": "KO", "qty": 120, "avg_cost": 50.0, "first_buy_date": "2024-01-02",
                                  "updated_at": db.now_iso()})
    _set_reg(conn, peak_qty=150, entry_norm_pe=10.0)
    W.write_full_result(conn, paths, "KO", P45.ASOF, "r1", P45._tdir(tmp_path, "b", "KO", "WATCH"))
    d = _reg_data(conn)
    assert d["last_full_review_date"] == P45.ASOF            # 確實走了持股完整分析回寫
    assert d["peak_qty"] == 150 and d["entry_norm_pe"] == pytest.approx(10.0)


# ---------------------------------------------------------------- (b) 峰值含登錄表 peak_qty

def test_q3c_peak_uses_registry_peak_qty(env):
    paths, conn = env
    P24._seed(conn, qty=120)            # 無 last_trim（硬上限系統減碼不寫）
    P24.seed_p08(conn)                  # A3-M3D-02
    _set_reg(conn, peak_qty=150)
    did = W.write_hold_review_result(conn, paths, "KO", P24.ASOF, "r", P24._hr(0.25, P24.Q3C), price=80.0, iv_base=60.0)
    log = P24._log(conn, did)
    assert log["trim_fraction"] == pytest.approx(1 - (2 / 3) * 150 / 120)   # 1/6，而非 1/4
    assert P24._reg_data(conn)["last_trim"]["peak_shares"] == 150


def test_q3c_below_two_thirds_of_registry_peak_is_hold(env):
    paths, conn = env
    P24._seed(conn, qty=90)             # 90 < 150 × 2/3
    _set_reg(conn, peak_qty=150)
    did = W.write_hold_review_result(conn, paths, "KO", P24.ASOF, "r", P24._hr(0.25, P24.Q3C), price=80.0, iv_base=60.0)
    assert P24._log(conn, did)["action"] == "HOLD"


def test_peak_is_max_of_qty_last_trim_and_registry(env):
    paths, conn = env
    P24._seed(conn, qty=120, last_trim={"date": "2025-01-02", "reason_code": "VALUATION", "line": 60.0,
                                        "fraction": 0.2, "shares": 30, "peak_shares": 160})
    _set_reg(conn, peak_qty=150)        # last_trim 的 160 較大 → 用 160
    P24.seed_p08(conn)                  # A3-M3D-02
    did = W.write_hold_review_result(conn, paths, "KO", P24.ASOF, "r", P24._hr(0.25, P24.Q3C), price=80.0, iv_base=60.0)
    assert P24._log(conn, did)["trim_fraction"] == pytest.approx(1 - (2 / 3) * 160 / 120)


# ---------------------------------------------------------------- 整條：broker 成交 → 系統減碼 → 持股完整分析 → H2 減碼

def test_broker_peak_survives_full_review_and_bounds_q3c_trim(env, tmp_path):
    paths, conn = env
    book.ensure_initial_deposit(conn, 1_000_000, "2025-01-01")
    W.write_full_result(conn, paths, "KO", "2025-01-02", "r0", P45._tdir(tmp_path, "a", "KO", "WATCH", cap_input=True))
    bar = {"open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0}
    broker.place_order(conn, "KO", "BUY", 150, 51.0, None, "2025-01-02")
    broker.match_orders(conn, "2025-01-03", {"KO": bar}, 0.0, 0.0)
    broker.place_order(conn, "KO", "SELL", 30, None, None, "2025-06-02")   # 硬上限／人工減碼：不寫 last_trim
    broker.match_orders(conn, "2025-06-03", {"KO": {**bar, "open": 70.0, "close": 70.0}}, 0.0, 0.0)
    assert _reg_data(conn)["peak_qty"] == 150
    W.write_full_result(conn, paths, "KO", P45.ASOF, "r1", P45._tdir(tmp_path, "b", "KO", "WATCH", cap_input=True))
    d = _reg_data(conn)
    assert d["peak_qty"] == 150 and d["entry_norm_pe"] == pytest.approx(10.0)   # 50 ÷ oe 5.0
    P24.seed_p08(conn)                  # A3-M3D-02：完整分析卡補 P-08 品質優
    did = W.write_hold_review_result(conn, paths, "KO", P45.ASOF, "r2", P24._hr(0.25, P24.Q3C), price=80.0, iv_base=60.0)
    assert P24._log(conn, did)["trim_fraction"] == pytest.approx(1 - (2 / 3) * 150 / 120)
