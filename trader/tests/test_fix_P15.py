"""P15（M6-B2）：部位與下單計畫。M6_D7_03、M6-D1-02、M6_D7_08、M6_D7_07、M6_D7_06（M5_D7_04）、M6-D2-02、
M6-D2-07、M6-D2-04、M6-D2-03、M6-D2-09、M6-D10-03。daily 的 M6_D7_09 與 review skipped 在 test_daily_pipeline.py。"""
import json

import pytest

from trader import db
from trader.analysis import writeback as W
from trader.ledger import book, broker
from trader.portfolio import correlation
from trader.portfolio import sizing as S

ASOF = "2026-10-16"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}
BIG_ADV = 1e9


def _card(**kw):
    c = {"target_weight_pct": 10, "certainty_grade": "B", "auto_buy_cap": 60.0, "price_to_conservative": 0.8,
         "value_per_share": {"conservative": 60.0}, "max_weight_pct": {"host": 15, "tail": None},
         "position_plan": {"first_tranche_pct_of_target": 50}}
    c.update(kw)
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    book.ensure_initial_deposit(c, 100000.0, "2026-10-01")
    yield c
    c.close()


def _pos(conn, t, qty, avg=50.0, first="2025-01-02"):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": first,
                                  "updated_at": db.now_iso()})


def _reg(conn, t, status="HOLDING", **data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"grade": "B", "auto_buy_cap": 60.0, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _dec(conn, did, t, date, log):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "signal": None,
                                  "data": {"ticker": t, "decision_date": date, **log}, "created_at": db.now_iso()})


def _item(did, t, action="BUY", log=None, card=None, review_type=None):
    return {"decision_id": did, "ticker": t, "action": action, "review_type": review_type,
            "log": {**GATES, "action": action, "review_type": review_type, **(log or {})}, "card": card or _card()}


def _room(value, group=("X",)):
    return lambda *a, **k: {"status": "ok", "room": value, "reason": "測試群額度", "group": list(group)}


# ---------------------------------------------------------------- M6_D7_03：賣單不重複

def test_same_day_rerun_does_not_duplicate_trim(conn):
    _pos(conn, "AAA", 100)
    it = _item("AAA-20261016-1", "AAA", action="TRIM", review_type="THESIS")
    p1 = S.plan_sells(conn, ASOF, [it], {"AAA": 50.0})
    assert p1[0]["qty"] == 50
    broker.place_order(conn, "AAA", "SELL", 50, None, it["decision_id"], ASOF)
    p2 = S.plan_sells(conn, ASOF, [it], {"AAA": 50.0})
    assert p2 == [] or (p2[0]["qty"] == 0 and "此決策已下過單" in p2[0]["reason"])
    assert all(p["qty"] == 0 for p in p2)


def test_cooling_off_trim_blocks_next_day_weight_trim(conn):
    """PRICE 冷靜期 TRIM（委託日＝次日，open）：次日權重超限分支看到未成交賣單就不再掛。
    D01 之後因上漲超過等級上限不減碼，仍會減碼的只有制度類硬上限（user_hard_cap），以它驗證。"""
    _pos(conn, "AAA", 400, first="2024-01-02")          # 400 × 50 = 2 萬；權益 12 萬 → 16.7% > 硬上限 10%
    _reg(conn, "AAA")
    plans = S.plan_sells(conn, ASOF, [], {"AAA": 50.0}, user_hard_cap=0.10)
    assert [p["qty"] for p in plans] == [160] and plans[0]["reason_code"] == "INSTITUTIONAL"   # 對照：無未成交賣單時會減
    broker.place_order(conn, "AAA", "SELL", 200, None, "AAA-20261015-1", ASOF)
    plans = S.plan_sells(conn, ASOF, [], {"AAA": 50.0}, user_hard_cap=0.10)
    assert [p["qty"] for p in plans] == [0] and "已有未成交賣單" in plans[0]["reason"]


def test_late_after_reread_of_filled_trim_is_skipped(conn):
    """跨日：失敗 daily 後以 late_after 重讀同一 TRIM；該決策已有成交委託 → 不再下單。"""
    _pos(conn, "AAA", 50)
    it = _item("AAA-20261015-1", "AAA", action="TRIM", review_type="THESIS")
    oid = broker.place_order(conn, "AAA", "SELL", 50, None, it["decision_id"], "2026-10-15")
    conn.execute("UPDATE orders SET status='filled' WHERE order_id=?", (oid,))
    plans = S.plan_sells(conn, ASOF, [it], {"AAA": 50.0})
    assert plans[0]["qty"] == 0 and "此決策已下過單" in plans[0]["reason"]


def test_sell_all_after_open_trim_sells_full_position(conn):
    """SELL_ALL 一律掛全量（P15R）：暫停代號只撮合 SELL_ALL 決策的賣單，扣掉先前 TRIM 會留下部位；
    不會重複賣：broker 以 min(qty, 持股) 成交，持股歸零後其餘賣單以無持股可賣取消。"""
    _pos(conn, "AAA", 100)
    broker.place_order(conn, "AAA", "SELL", 50, None, "AAA-20261015-1", ASOF)
    plans = S.plan_sells(conn, ASOF, [_item("AAA-20261016-1", "AAA", action="SELL_ALL")], {"AAA": 50.0})
    assert plans[0]["qty"] == 100


def test_sell_all_on_paused_ticker_with_open_trim_clears_position(conn):
    """持股 100、未成交 TRIM 50、代號暫停：SELL_ALL 計畫賣 100，兩個撮合日內持股歸零（P15R）。"""
    _pos(conn, "AAA", 100)
    _dec(conn, "AAA-T", "AAA", "2026-10-15", {"action": "TRIM", "review_type": "THESIS"})
    _dec(conn, "AAA-S", "AAA", "2026-10-15", {"action": "SELL_ALL", "review_type": "THESIS"})
    broker.place_order(conn, "AAA", "SELL", 50, None, "AAA-T", "2026-10-15")
    db.insert(conn, "anomalies", {"ticker": "AAA", "kind": "x", "detail": "{}", "status": "open",
                                  "pause_trading": 1, "created_at": db.now_iso()})
    assert "AAA" in book.paused(conn)
    it = {"decision_id": "AAA-S", "ticker": "AAA", "action": "SELL_ALL", "review_type": "THESIS",
          "log": {"action": "SELL_ALL"}, "card": {}}
    p = S.plan_sells(conn, "2026-10-15", [it], {"AAA": 50.0})[0]
    assert p["qty"] == 100
    broker.place_order(conn, "AAA", "SELL", p["qty"], None, "AAA-S", "2026-10-15")
    bar = {"open": 50, "high": 51, "low": 49, "close": 50}
    broker.match_orders(conn, "2026-10-16", {"AAA": bar}, 0, 0)
    broker.match_orders(conn, "2026-10-19", {"AAA": bar}, 0, 0)
    assert book.positions(conn).get("AAA", {}).get("qty", 0) == 0
    # 同一決策重跑仍不重掛
    p2 = S.plan_sells(conn, "2026-10-19", [it], {"AAA": 50.0})
    assert all(x["qty"] == 0 for x in p2)


def test_weight_branch_no_noise_for_position_under_cap_with_open_sell(conn):
    """權重超限段：沒超限的持股即使有未成交賣單也不輸出 qty=0 紀錄（P15R）。"""
    _pos(conn, "AAA", 100, first="2024-01-02")          # 5000／10.5 萬 ≈ 4.8% < B 10%
    _reg(conn, "AAA")
    broker.place_order(conn, "AAA", "SELL", 50, None, "AAA-20261015-1", ASOF)
    assert S.plan_sells(conn, ASOF, [], {"AAA": 50.0}) == []


# ---------------------------------------------------------------- M6-D1-02：壞卡不拖垮

@pytest.mark.parametrize("patch", [{"target_weight_pct": "10%"}, {"max_weight_pct": 10},
                                   {"max_weight_pct": {"single_event": "不適用", "host": 15}},
                                   {"position_plan": {"first_tranche_pct_of_target": "50%"}},
                                   {"g8_adjusted_expected_return": "高"}])
def test_bad_card_values_skip_or_plan_without_exception(conn, patch):
    plans = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", card=_card(**patch))], {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)
    assert len(plans) == 1
    if "target_weight_pct" in patch:
        assert plans[0]["qty"] == 0 and "不是數字" in plans[0]["reason"]
    else:
        assert plans[0]["qty"] == 99   # 10% × 50% × 10 萬 ÷ 50.05


def test_one_ticker_exception_does_not_stop_others(conn, monkeypatch):
    real = correlation.buy_room

    def room(conn_, t, *a, **k):
        if t == "BAD":
            raise ValueError("壞資料")
        return real(conn_, t, *a, **k)
    monkeypatch.setattr(correlation, "buy_room", room)
    plans = S.plan_buys(conn, ASOF, [_item("BAD-1", "BAD"), _item("AAA-1", "AAA")],
                        {"BAD": 50.0, "AAA": 50.0}, {"BAD": BIG_ADV, "AAA": BIG_ADV}, 10)
    by = {p["ticker"]: p for p in plans}
    assert by["BAD"]["qty"] == 0 and "計畫計算失敗" in by["BAD"]["reason"]
    assert by["AAA"]["qty"] > 0


def test_tranche_exception_is_per_ticker(conn):
    _pos(conn, "AAA", 10, first="2026-10-10")
    _reg(conn, "AAA", build_plan={"target_weight": 0.1, "tranches_left": "三"})
    plans, cancels = S.plan_tranches(conn, ASOF, {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)
    assert cancels == [] and (plans == [] or plans[0]["qty"] == 0)


def test_writeback_card_numbers_normalized():
    card = {"target_weight_pct": "10%", "max_weight_pct": {"host": 15, "single_event": "不適用"},
            "position_plan": {"first_tranche_pct_of_target": "50%", "target_weight_pct": 8}}
    notes: list[str] = []
    W._card_numbers(card, notes)
    assert card["target_weight_pct"] is None and card["max_weight_pct"] == {"host": 15, "single_event": None}
    assert card["position_plan"] == {"first_tranche_pct_of_target": None, "target_weight_pct": 8}
    assert len(notes) == 3
    card = {"max_weight_pct": 10}
    W._card_numbers(card, notes)
    assert card["max_weight_pct"] == {}


# ---------------------------------------------------------------- M6_D7_08：首批固定 50%、單位、夾限

@pytest.mark.parametrize("pct", [100, 0.5, 150, None])
def test_first_tranche_fixed_fifty_percent(conn, pct):
    card = _card(position_plan={"first_tranche_pct_of_target": pct})
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", card=card)], {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["qty"] == 99 and p["target_weight"] == pytest.approx(0.10)
    assert (pct is None) or any("首批比例固定 50%" in n for n in p["notes"])


def test_decimal_target_weight_is_rejected(conn):
    t, notes = S.target_weight(_card(target_weight_pct=0.1), None)
    assert t == 0 and "疑似小數" in notes[0]
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", card=_card(target_weight_pct=0.1))],
                    {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["qty"] == 0 and "疑似小數" in p["reason"]


def test_build_plan_target_clamped_by_grade_cap(conn):
    _pos(conn, "AAA", 100, first="2026-10-10")          # 5,000；權益 105,000
    _reg(conn, "AAA", build_plan={"target_weight": 0.30, "tranches_left": 1})
    _dec(conn, "AAA-20261010-1", "AAA", "2026-10-10", {**GATES, "action": "BUY"})
    plans, _ = S.plan_tranches(conn, ASOF, {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)
    amt = plans[0]["qty"] * S.ref_price(50.0, 10)       # D04：限價＝上限×0.9999，金額以 收盤×(1+滑價) 計
    assert plans[0]["qty"] > 0 and amt <= 0.10 * 105000 - 5000 + 1   # 不是 30% − 市值


# ---------------------------------------------------------------- M6_D7_07：未成交新部位占名額

def test_open_new_buys_count_toward_max_holdings(conn):
    for i in range(9):
        _pos(conn, f"H{i}", 1, avg=10.0)
    broker.place_order(conn, "P0", "BUY", 1, 10.0, "P0-1", ASOF)
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA")], {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["qty"] == 0 and p["reason"].startswith(f"持股已達 {S.MAX_HOLDINGS} 檔")


# ---------------------------------------------------------------- M6_D7_06／M5_D7_04：完整分析 BUY 解除疑似

def test_full_buy_after_thesis_suspect_unblocks_add(conn):
    _dec(conn, "AAA-20261014-1", "AAA", "2026-10-14", {"review_type": "THESIS", "thesis_check": "疑似"})
    assert S.add_blocked(conn, "AAA", ASOF) == "論點疑似或證偽"
    _dec(conn, "AAA-20261015-1", "AAA", "2026-10-15", {"review_type": None, "action": "WATCH"})
    assert S.add_blocked(conn, "AAA", ASOF) == "論點疑似或證偽"     # 完整分析非 BUY 不解除
    _dec(conn, "AAA-20261016-1", "AAA", ASOF, {"review_type": None, "action": "BUY"})
    assert S.add_blocked(conn, "AAA", ASOF) is None


# ---------------------------------------------------------------- M6-D2-02／M6-D2-07：相關群額度

def test_correlated_group_in_card_is_not_total_cap(conn, monkeypatch):
    """持股 6%、群剩餘 2%：T 不被壓成 2%，金額由 buy_room 縮到 2% 權益。"""
    assert S.target_weight(_card(max_weight_pct={"correlated_group": 2}), None)[0] == pytest.approx(0.10)
    _pos(conn, "AAA", 120, avg=50.0)                     # 6,000；權益 106,000
    _reg(conn, "AAA")
    monkeypatch.setattr(correlation, "buy_room", _room(0.02))
    card = _card(max_weight_pct={"correlated_group": 2})
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", card=card)], {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["action"] == "ADD" and p["target_weight"] == pytest.approx(0.10)
    assert p["qty"] == int(0.02 * 106000 // 50.05)


def test_new_position_target_clamped_by_group_room_before_first_tranche(conn, monkeypatch):
    """群額度 4%、T 8% → 首批 2%、build_plan.target_weight 4%。"""
    monkeypatch.setattr(correlation, "buy_room", _room(0.04))
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", card=_card(target_weight_pct=8))],
                    {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["target_weight"] == pytest.approx(0.04)
    assert p["qty"] == int(0.02 * 100000 // 50.05)


# ---------------------------------------------------------------- M6-D2-04：建倉期外一次補足

def test_add_outside_build_window_fills_whole_gap(conn):
    _pos(conn, "AAA", 100)                                # 5,000；權益 105,000；T 10% → 缺口 5,500
    _reg(conn, "AAA")
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", action="ADD")], {"AAA": 50.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["qty"] == int(5500 // 50.05) and "建倉期外加碼一次補足缺口" in p["notes"]


# ---------------------------------------------------------------- M6-D2-03：建倉剩餘批次預留現金

def test_remaining_tranches_reserve_cash(conn, monkeypatch):
    _pos(conn, "HHH", 100, first="2026-10-10")           # 5,000；權益 105,000
    _reg(conn, "HHH", build_plan={"target_weight": 0.10, "tranches_left": 2})
    assert S._build_reserve(conn, 105000.0, {"HHH": 50.0}) == {"HHH": pytest.approx(5500.0)}
    monkeypatch.setattr(S, "_avail_cash", lambda c, e: 6000.0)
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA")], {"AAA": 50.0, "HHH": 50.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["qty"] == int(500 // p["limit_price"])      # 6,000 − 預留 5,500；現金綁定以限價算股數（A3-M6C-05）
    assert any("建倉預留" in n for n in p["notes"])


# ---------------------------------------------------------------- M6-D2-09：P-38 管整個部位

def test_adv_caps_position_not_each_order(conn):
    _pos(conn, "AAA", 40, first="2026-10-10")            # 2,000
    _reg(conn, "AAA", build_plan={"target_weight": 0.10, "tranches_left": 3})
    _dec(conn, "AAA-20261010-1", "AAA", "2026-10-10", {**GATES, "action": "BUY"})
    adv = 400000.0                                       # 1% = 4,000 → 部位上限 4,000
    plans, _ = S.plan_tranches(conn, ASOF, {"AAA": 50.0}, {"AAA": adv}, 10)
    amt = plans[0]["qty"] * S.ref_price(50.0, 10)       # D04：限價＝上限×0.9999，金額以 收盤×(1+滑價) 計
    assert 0 < amt <= (4000 - 2000) / 3 + 1 and any("P-38" in n for n in plans[0]["notes"])


# ---------------------------------------------------------------- M6-D10-03：合股後以登錄表上限為準

def test_reverse_split_uses_registry_cap(conn):
    _reg(conn, "AAA", status="WATCH", auto_buy_cap=600.0)   # 1:10 合股：登錄表上限 ×10，卡仍 60
    card = _card(auto_buy_cap=60.0, value_per_share={"conservative": 60.0})
    p = S.plan_buys(conn, ASOF, [_item("AAA-1", "AAA", card=card)], {"AAA": 500.0}, {"AAA": BIG_ADV}, 10)[0]
    assert p["qty"] > 0 and "上限 600.0" in p["reason"]
    assert S._rank_key(card, 500.0, None, 10.0) == (1, pytest.approx(500 / 600))


# ---------------------------------------------------------------- P30R2：收尾列解除後視同不存在

def _p30r2_env(tmp_path, prior_log):
    from test_engine import make_env
    from test_hold_review import _holding
    from trader.llm.base import TransportError
    env = make_env(tmp_path, {"TRIAGE": TransportError("斷線")})
    _holding(env)
    db.insert(env.conn, "decisions", {"decision_id": "KO-20260901-1", "ticker": "KO", "decision_date": "2026-09-01",
                                      "run_id": "old", "job_id": "old:KO:X:1", "signal": "HOLD", "reason_code": "X",
                                      "data": json.dumps(prior_log), "created_at": "2026-09-01T00:00:00"})
    env.conn.commit()
    return env


def test_p30r2_x1_prior_stop_adding_survives_retry(tmp_path):
    """X1：持股先有 HOLD_REVIEW 停止加碼；今日 TRIAGE 傳輸失敗 → retry 成功（TRIAGE 判否）後仍擋。"""
    from test_engine import RUN, triage
    from trader.jobs.engine import retry_failed
    env = _p30r2_env(tmp_path, {"review_type": "HOLD_REVIEW", "stop_adding": True, "action": "HOLD"})
    asof = __import__("test_engine").ASOF
    assert S.add_blocked(env.conn, "KO", asof) is not None
    env.add(env.job("KO", "TRIAGE", 1, params={"holding": True}))
    env.engine.run(RUN)
    assert S.add_blocked(env.conn, "KO", asof) is not None
    retry_failed(env.conn, RUN)
    env.fake.responses["TRIAGE"] = triage(False)
    assert env.engine.run(RUN)["status"] == "done"
    assert "停止加碼" in (S.add_blocked(env.conn, "KO", asof) or "")


def test_p30r2_x2_prior_thesis_suspect_survives_resolve(tmp_path):
    """X2：先有 THESIS 疑似，再寫收尾列；異常結案後仍擋（論點疑似）。"""
    from test_engine import ASOF as asof, RUN
    env = _p30r2_env(tmp_path, {"review_type": "THESIS", "thesis_check": "疑似"})
    env.add(env.job("KO", "TRIAGE", 1, params={"holding": True}))
    env.engine.run(RUN)
    env.conn.execute("UPDATE anomalies SET status='resolved' WHERE kind='job_failed'")
    env.conn.commit()
    assert S.add_blocked(env.conn, "KO", asof) == "論點疑似或證偽"
