"""組合層部位規模（sizing.py）：純函式與讀帳本的計畫函式。不連網、不下單。"""
import json

import pytest

from trader import db
from trader.ledger import book
from trader.portfolio import sizing as S

ASOF = "2026-10-16"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}


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


def _pos(conn, t, qty, avg, first):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": first,
                                  "updated_at": db.now_iso()})


def _reg(conn, t, status, data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": data, "updated_at": db.now_iso()},
              key=["ticker"])


def _dec(conn, did, t, date, log, card=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "signal": None,
                                  "data": {"ticker": t, "decision_date": date, **log}, "created_at": db.now_iso()})
    if card is not None:
        db.insert(conn, "signals", {"decision_id": did, "ticker": t, "signal": "BUY", "data": card,
                                    "created_at": db.now_iso()})


def _item(did, t, action="BUY", log=None, card=None, review_type=None):
    return {"decision_id": did, "ticker": t, "action": action, "review_type": review_type,
            "log": {**GATES, "action": action, "review_type": review_type, **(log or {})}, "card": card or _card()}


# ---------------------------------------------------------------- 純函式

def test_target_weight_takes_min_of_caps():
    t, notes = S.target_weight(_card(target_weight_pct=20, certainty_grade="B"), None)
    assert t == pytest.approx(0.10) and notes == []          # 等級 B 上限 10%
    t, _ = S.target_weight(_card(target_weight_pct=12, certainty_grade="A", max_weight_pct={"tail": 8}), None)
    assert t == pytest.approx(0.08)                          # 尾部上限最嚴
    t, _ = S.target_weight(_card(target_weight_pct=12), 0.05)
    assert t == pytest.approx(0.05)                          # 使用者上限


def test_target_weight_grade_c_or_missing_is_zero():
    t, notes = S.target_weight(_card(certainty_grade="C"), None)
    assert t == 0.0 and any("不開倉" in n for n in notes)
    t, notes = S.target_weight({"certainty_grade": "A"}, None)
    assert t == 0.0 and "缺目標權重" in notes[0]


def test_target_weight_below_three_percent_is_zero():
    t, notes = S.target_weight(_card(target_weight_pct=2), None)
    assert t == 0.0 and any("< 3%" in n for n in notes)


def test_target_weight_falls_back_to_position_plan():
    card = {"certainty_grade": "A", "position_plan": {"target_weight_pct": 6}}
    assert S.target_weight(card, None)[0] == pytest.approx(0.06)


def test_buy_limit_strictly_below_cap():
    lim = S.buy_limit(50.0, 60.0, 10)
    assert lim == pytest.approx(50.05) and lim < 60.0
    # 報價＋滑價已到上限附近：限價仍嚴格低於上限（不得四捨五入到上限）
    lim = S.buy_limit(59.99, 60.0, 0)
    assert lim is not None and lim < 60.0
    assert S.buy_limit(60.0, 60.0, 0) is None or S.buy_limit(60.0, 60.0, 0) < 60.0


def test_buy_limit_rejects_bad_inputs():
    assert S.buy_limit(None, 60.0, 10) is None
    assert S.buy_limit(50.0, None, 10) is None
    assert S.buy_limit(0, 60.0, 10) is None


def test_gate_failures_uses_log_then_card_fallback():
    assert S.gate_failures(GATES, None) == []
    assert len(S.gate_failures({"gate1_not_deleted": True, "gate2_price_below_cap": True,
                                "gate3_pass": False}, None)) == 1
    # 日誌缺欄位 → 退回訊號卡 auto_buy_gates
    card = {"auto_buy_gates": {"gate1_not_deleted": True, "gate2_price_below_cap": True,
                               "gate3_questionnaire_pass": True}}
    assert S.gate_failures({}, card) == []
    # placeholder 字串不算真
    assert S.gate_failures({"gate1_not_deleted": "true | false", "gate2_price_below_cap": True,
                            "gate3_pass": True}, None)


def test_build_amount_formula():
    # (T×權益 − 市值) ÷ 剩餘批數
    assert S.build_amount(0.10, 100000, 5000, 3) == pytest.approx(5000 / 3)
    assert S.build_amount(0.10, 100000, 20000, 3) == 0.0
    assert S.build_amount(0.10, 100000, 0, 0) == 0.0


def test_rank_key_prefers_g8_then_price_to_conservative():
    assert S._rank_key({"g8_adjusted_expected_return": 0.12}, 50) < S._rank_key({}, 50)
    cheap = S._rank_key({"value_per_share": {"conservative": 100}}, 50)
    dear = S._rank_key({"value_per_share": {"conservative": 100}}, 90)
    assert cheap < dear


# ---------------------------------------------------------------- plan_buys

def test_plan_buys_new_position_sizes_first_tranche(conn):
    items = [_item("AAA-20261016-1", "AAA")]
    plans = S.plan_buys(conn, ASOF, items, {"AAA": 50.0}, {"AAA": 1e9}, 10)
    p = plans[0]
    # 首批 50% × 10% × 100000 = 5000；限價 50.05 → 99 股
    assert p["qty"] == 99 and p["limit_price"] == pytest.approx(50.05)
    assert p["side"] == "BUY" and p["decision_id"] == "AAA-20261016-1"
    assert any("推論：缺 G8" in n for n in p["notes"])


def test_plan_buys_uses_split_adjusted_registry_cap(conn):
    """拆股後登錄表上限已換算（÷4），訊號卡仍是舊值：取較低者 → 現價 50 不低於 15 → 不買。"""
    _reg(conn, "SPL", "WATCH", {"auto_buy_cap": 15.0})
    plans = S.plan_buys(conn, ASOF, [_item("SPL-20261016-1", "SPL")], {"SPL": 50.0}, {"SPL": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "未低於" in plans[0]["reason"]


def test_plan_buys_gate_failure_and_pause_skip(conn):
    bad = _item("BBB-20261016-1", "BBB", log={"gate3_pass": False})
    plans = S.plan_buys(conn, ASOF, [bad], {"BBB": 50.0}, {"BBB": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "閘門3" in plans[0]["reason"]

    db.insert(conn, "anomalies", {"ticker": "CCC", "kind": "corp_action_check_failed", "detail": "{}",
                                  "status": "open", "pause_trading": 1, "created_at": db.now_iso()})
    plans = S.plan_buys(conn, ASOF, [_item("CCC-20261016-1", "CCC")], {"CCC": 50.0}, {"CCC": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "暫停" in plans[0]["reason"]


def test_plan_buys_requires_quote_below_cap(conn):
    plans = S.plan_buys(conn, ASOF, [_item("DDD-20261016-1", "DDD")], {"DDD": 59.99}, {"DDD": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "未低於" in plans[0]["reason"]   # 59.99 × 1.001 ≥ 60


def test_plan_buys_adv_limits_amount(conn):
    # ADV 1,000,000 美元 × 1% = 10,000 美元 上限；首批 5000 不受限，改把 ADV 壓到 100,000 → 上限 1000
    plans = S.plan_buys(conn, ASOF, [_item("EEE-20261016-1", "EEE")], {"EEE": 50.0}, {"EEE": 100000}, 10)
    assert plans[0]["qty"] == 19   # floor(1000 / 50.05)


def test_plan_buys_daily_new_cap_and_ranking(conn):
    items = []
    for i, t in enumerate(["F1", "F2", "F3", "F4", "F5"]):
        # 保守價值越高（價格相對越便宜）越優先
        items.append(_item(f"{t}-20261016-1", t, card=_card(value_per_share={"conservative": 100 - i})))
    quotes = {t: 50.0 for t in ["F1", "F2", "F3", "F4", "F5"]}
    plans = S.plan_buys(conn, ASOF, items, quotes, {t: 1e9 for t in quotes}, 10)
    placed = [p["ticker"] for p in plans if p["qty"] > 0]
    assert len(placed) == S.MAX_NEW_PER_DAY == 4
    assert placed[0] == "F1"   # 保守價值最高（50/100 最便宜）→ 最優先；推論排序
    skipped = [p for p in plans if p["qty"] == 0]
    assert len(skipped) == 1 and "當日新部位" in skipped[0]["reason"]


def test_plan_buys_max_holdings_blocks_new(conn):
    for i in range(S.MAX_HOLDINGS):
        t = f"H{i}"
        _pos(conn, t, 10, 50.0, "2026-06-01")
    plans = S.plan_buys(conn, ASOF, [_item("NEW-20261016-1", "NEW")], {"NEW": 50.0}, {"NEW": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "需勝最弱持股" in plans[0]["reason"]


def test_plan_buys_cash_floor_limits_amount(conn):
    # 現金 100000，5% 下限 = 5000；若已承諾 95000 的買單，可動用現金 = 0
    db.insert(conn, "orders", {"order_id": "X-BUY-20261015-1", "ticker": "X", "side": "BUY", "qty": 1900,
                               "limit_price": 50.0, "status": "open", "decision_id": None,
                               "created_date": "2026-10-15", "updated_at": db.now_iso()})
    plans = S.plan_buys(conn, ASOF, [_item("GGG-20261016-1", "GGG")], {"GGG": 50.0}, {"GGG": 1e9}, 10)
    assert plans[0]["qty"] == 0


def test_plan_buys_add_requires_build_plan_and_uses_batch_formula(conn):
    _pos(conn, "HLD", 100, 50.0, "2026-10-10")  # 市值 5000（5%），權重 ≤ 15%
    _reg(conn, "HLD", "HOLDING", {"auto_buy_cap": 60.0, "build_plan": {"target_weight": 0.10, "tranches_left": 3}})
    items = [_item("HLD-20261016-1", "HLD", action="ADD", card=_card())]
    plans = S.plan_buys(conn, ASOF, items, {"HLD": 50.0}, {"HLD": 1e9}, 10)
    # 權益 = 現金 100000（測試未扣現金）＋市值 5000 = 105000；(0.10 × 105000 − 5000) ÷ 3 = 1833.33
    # → floor(1833.33 / 50.05) = 36
    assert plans[0]["qty"] == 36 and plans[0]["action"] == "ADD"


def test_plan_buys_add_without_build_plan_uses_card_target(conn):
    _pos(conn, "HLD2", 100, 50.0, "2026-10-10")
    _reg(conn, "HLD2", "HOLDING", {"auto_buy_cap": 60.0})
    plans = S.plan_buys(conn, ASOF, [_item("HLD2-20261016-1", "HLD2", action="ADD")], {"HLD2": 50.0},
                        {"HLD2": 1e9}, 10)
    # 建倉期外的加碼（PRICE 判定權重未達目標）：目標取訊號卡，缺口分 3 批
    assert plans[0]["qty"] > 0 and any("建倉期外" in n for n in plans[0]["notes"])


def test_plan_buys_skips_when_open_buy_exists(conn):
    db.insert(conn, "orders", {"order_id": "OPN-BUY-20261015-1", "ticker": "OPN", "side": "BUY", "qty": 10,
                               "limit_price": 50.0, "status": "open", "decision_id": None,
                               "created_date": "2026-10-15", "updated_at": db.now_iso()})
    plans = S.plan_buys(conn, ASOF, [_item("OPN-20261016-1", "OPN")], {"OPN": 50.0}, {"OPN": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "未成交買單" in plans[0]["reason"]


# ---------------------------------------------------------------- plan_sells

def test_sell_all_and_trim(conn):
    _pos(conn, "SA", 100, 50.0, "2026-01-01")
    _pos(conn, "TR", 101, 50.0, "2026-01-01")
    decs = [_item("SA-20261016-1", "SA", action="SELL_ALL"), _item("TR-20261016-1", "TR", action="TRIM")]
    plans = S.plan_sells(conn, ASOF, decs, {"SA": 50.0, "TR": 50.0})
    by = {p["ticker"]: p for p in plans}
    assert by["SA"]["qty"] == 100 and by["SA"]["limit_price"] is None
    assert by["TR"]["qty"] == 50                          # floor(101/2)


def test_sell_cancels_open_buys_first(conn):
    _pos(conn, "SC", 100, 50.0, "2026-01-01")
    db.insert(conn, "orders", {"order_id": "SC-BUY-20261015-1", "ticker": "SC", "side": "BUY", "qty": 5,
                               "limit_price": 49.0, "status": "open", "decision_id": None,
                               "created_date": "2026-10-15", "updated_at": db.now_iso()})
    plans = S.plan_sells(conn, ASOF, [_item("SC-20261016-1", "SC", action="SELL_ALL")], {"SC": 50.0})
    assert plans[0]["cancel_buys"] == ["SC-BUY-20261015-1"]


def test_weight_overrun_under_one_year_only_noted(conn):
    _pos(conn, "OV", 400, 50.0, "2026-06-01")          # 市值 20000 / 權益 100000 = 20% > 10%（B）
    _reg(conn, "OV", "HOLDING", {"grade": "B"})
    plans = S.plan_sells(conn, ASOF, [], {"OV": 50.0})
    assert plans[0]["qty"] == 0 and "未滿一年" in plans[0]["reason"]


def test_weight_overrun_with_grade_c_is_recorded_not_sold(conn):
    _pos(conn, "OVC", 400, 50.0, "2025-01-01")         # 20% > 最高等級上限
    _reg(conn, "OVC", "HOLDING", {"grade": "C"})
    plans = S.plan_sells(conn, ASOF, [], {"OVC": 50.0})
    assert plans[0]["qty"] == 0 and "只記錄" in plans[0]["reason"]


def test_weight_overrun_after_one_year_trims_to_cap(conn):
    _pos(conn, "OV2", 400, 50.0, "2025-01-01")
    _reg(conn, "OV2", "HOLDING", {"grade": "B"})
    plans = S.plan_sells(conn, ASOF, [], {"OV2": 50.0})
    # 權益 = 100000 ＋ 市值 20000 = 120000；上限 10% → 目標 240 股，賣 400 − 240 = 160
    assert plans[0]["qty"] == 160 and plans[0]["limit_price"] is None


# ---------------------------------------------------------------- plan_tranches / tick

def _build_setup(conn, t="BLD", first="2026-10-10", bp=None, cap=60.0, qty=100):
    _pos(conn, t, qty, 50.0, first)
    _reg(conn, t, "HOLDING", {"auto_buy_cap": cap, "build_plan": bp or {"target_weight": 0.10, "tranches_left": 3}})
    _dec(conn, f"{t}-20261010-1", t, "2026-10-10", {"review_type": None, **GATES})


def test_tranche_planned_when_eligible(conn):
    _build_setup(conn)
    plans, cancels = S.plan_tranches(conn, ASOF, {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert cancels == [] and plans[0]["qty"] == 36 and plans[0]["action"] == "ADD"


def test_tranche_skipped_while_previous_buy_open(conn):
    _build_setup(conn)
    db.insert(conn, "orders", {"order_id": "BLD-BUY-20261015-1", "ticker": "BLD", "side": "BUY", "qty": 36,
                               "limit_price": 50.5, "status": "open", "decision_id": None,
                               "created_date": "2026-10-15", "updated_at": db.now_iso()})
    plans, cancels = S.plan_tranches(conn, ASOF, {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert cancels == [] and plans[0]["qty"] == 0 and "尚未成交" in plans[0]["reason"]


def test_tranche_window_expiry_cancels(conn):
    _build_setup(conn, first="2026-09-01")               # 45 曆日 > 21
    plans, cancels = S.plan_tranches(conn, ASOF, {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert plans == [] and cancels[0]["ticker"] == "BLD" and "窗口到期" in cancels[0]["reason"]


def test_tranche_cancel_after_ten_days_above_cap(conn):
    _build_setup(conn, bp={"target_weight": 0.10, "tranches_left": 3, "days_above_cap": 10})
    plans, cancels = S.plan_tranches(conn, ASOF, {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert plans == [] and "連 10 日" in cancels[0]["reason"]


def _ca(conn, t, typ, eff):
    db.insert(conn, "corporate_actions", {"ticker": t, "action_type": typ, "effective_date": eff,
                                          "status": "pending_human", "source": "alpaca", "data": "{}",
                                          "created_at": db.now_iso()})


def test_tranche_cancelled_after_spin_off(conn):
    _build_setup(conn)
    _ca(conn, "BLD", "分拆", "2026-10-12")
    plans, cancels = S.plan_tranches(conn, ASOF, {"BLD": 10.0}, {"BLD": 1e9}, 10)
    assert plans == [] and "分拆" in cancels[0]["reason"] and "上限失效" in cancels[0]["reason"]


def test_tranche_unaffected_by_dividend_or_old_action(conn):
    _build_setup(conn)
    _ca(conn, "BLD", "現金股利", "2026-10-12")
    _ca(conn, "BLD", "併購換股", "2026-01-05")          # 建倉前的事件不影響
    plans, cancels = S.plan_tranches(conn, ASOF, {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert cancels == [] and plans[0]["qty"] > 0


def test_tranche_blocked_by_thesis_suspect(conn):
    _build_setup(conn)
    _dec(conn, "BLD-20261015-1", "BLD", "2026-10-15", {"review_type": "THESIS", "thesis_check": "疑似"})
    plans, _ = S.plan_tranches(conn, ASOF, {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "疑似" in plans[0]["reason"]


def test_tranche_blocked_when_gate_record_stale(conn):
    _build_setup(conn)
    conn.execute("UPDATE decisions SET decision_date='2025-01-01' WHERE decision_id='BLD-20261010-1'")
    conn.commit()
    plans, _ = S.plan_tranches(conn, ASOF, {"BLD": 50.0}, {"BLD": 1e9}, 10)
    assert plans[0]["qty"] == 0 and "365 天" in plans[0]["reason"]


def test_tick_build_state_counts_once_per_day(conn):
    _build_setup(conn)
    S.tick_build_state(conn, ASOF, {"BLD": 70.0})         # 高於上限 60
    S.tick_build_state(conn, ASOF, {"BLD": 70.0})         # 同一日重跑不重複計
    bp = S._reg_data(conn, "BLD")["build_plan"]
    assert bp["days_above_cap"] == 1 and bp["last_tick"] == ASOF
    S.tick_build_state(conn, "2026-10-17", {"BLD": 50.0})  # 回到上限內 → 歸零
    assert S._reg_data(conn, "BLD")["build_plan"]["days_above_cap"] == 0


def test_load_decisions_attaches_latest_card(conn):
    _dec(conn, "LD-20261016-1", "LD", ASOF, {"action": "BUY", **GATES}, card=_card())
    items = S.load_decisions(conn, ASOF)
    assert items[0]["action"] == "BUY" and items[0]["card"]["auto_buy_cap"] == 60.0
    assert json.dumps(items[0]["log"])  # 可序列化


def test_load_decisions_includes_non_trading_days(conn):
    _dec(conn, "AAA-20261017-1", "AAA", "2026-10-17", {"action": "BUY"})   # 週六做的分析
    _dec(conn, "BBB-20261016-1", "BBB", "2026-10-16", {"action": "BUY"})   # 前一交易日：已處理過
    got = [d["decision_id"] for d in S.load_decisions(conn, "2026-10-19", since="2026-10-16")]
    assert got == ["AAA-20261017-1"]
