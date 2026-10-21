"""X38（checks/P25.json D01 建議＋test_correlation 既有失敗）。合成情境、不連網、不下單。

1. user_hard_cap（制度類硬上限，D01）也限制新建倉 T、加碼門檻與建倉批次目標：不再先買到等級上限、隔天 INSTITUTIONAL 減回；
   daily 把 cfg.account.user_hard_cap 傳給 plan_buys／plan_tranches；None 時行為不變。
2. record_weight_over_cap：當日有賣出決策、缺收盤、制度類減碼中的持股本日沒有檢查等級超限，未結異常維持原狀
   （不誤結案、隔天不以新 first_date 重開）；確認不再超限或已無持股才結案。
3. 相關群（P-31／P-32）的當日已排／未成交買進以收盤（算股數的參考價）估權重，不以限價（D04 後＝上限×0.9999）估。
"""
import dataclasses
import json

import pytest

from test_daily_pipeline import D1, FakeAlpaca, _bars, _run, world  # noqa: F401（world 為 fixture）
from trader import db
from trader.ledger import book
from trader.portfolio import correlation as C
from trader.portfolio import sizing as S

ASOF = "2026-10-16"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    yield c
    c.close()


def _reg(conn, t, status, **data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"ticker": t, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _pos(conn, t, qty, avg, first="2026-10-10"):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": first,
                                  "updated_at": db.now_iso()})


def _card(**kw):
    c = {"target_weight_pct": 10, "certainty_grade": "B", "auto_buy_cap": 60.0, "price_to_conservative": 0.8,
         "value_per_share": {"conservative": 60.0}, "max_weight_pct": {"host": 15}, "external_drivers": []}
    c.update(kw)
    return c


def _item(t, action="BUY", card=None):
    return {"decision_id": f"{t}-20261016-1", "ticker": t, "action": action, "review_type": None,
            "log": {**GATES, "action": action}, "card": card or _card()}


# ---------------------------------------------------------------- 1. user_hard_cap 套到買進目標

def test_hard_cap_limits_new_position_target(conn):
    book.ensure_initial_deposit(conn, 100000.0, "2026-10-01")
    _reg(conn, "NEW", "WATCH", grade="B", asset_class="非金融", external_drivers=[])
    q, adv = {"NEW": 50.0}, {"NEW": 1e9}
    base = S.plan_buys(conn, ASOF, [_item("NEW")], q, adv, 0)
    none = S.plan_buys(conn, ASOF, [_item("NEW")], q, adv, 0, user_hard_cap=None)
    assert base[0]["target_weight"] == none[0]["target_weight"] == pytest.approx(0.10)   # null：行為不變
    assert base[0]["qty"] == none[0]["qty"] == 100
    p = S.plan_buys(conn, ASOF, [_item("NEW")], q, adv, 0, user_hard_cap=0.05)[0]
    assert p["target_weight"] == pytest.approx(0.05) and p["qty"] == 50                  # 首批 50% × 5%
    p = S.plan_buys(conn, ASOF, [_item("NEW")], q, adv, 0, user_hard_cap=0.02)[0]
    assert p["qty"] == 0 and "3%" in p["reason"]                                           # 硬上限 < 3%：不建倉（P-33）


def _building(conn, qty):
    """持有 BLD qty 股 × 50，B 級，建倉計畫 T 10%、剩 2 批；權益 10 萬。"""
    book.ensure_initial_deposit(conn, 100000.0 - qty * 50.0, "2026-10-01")
    _pos(conn, "BLD", qty, 50.0)
    _reg(conn, "BLD", "HOLDING", grade="B", asset_class="非金融", external_drivers=[], auto_buy_cap=60.0,
         build_plan={"target_weight": 0.10, "tranches_left": 2, "days_above_cap": 0, "started": "2026-10-10"})
    db.insert(conn, "decisions", {"decision_id": "BLD-20261010-1", "ticker": "BLD", "decision_date": "2026-10-10",
                                  "signal": "BUY", "data": {**GATES, "action": "BUY"}, "created_at": db.now_iso()})


def test_hard_cap_limits_tranche_target(conn):
    _building(conn, 60)                                                   # 權重 3%
    q, adv = {"BLD": 50.0}, {"BLD": 1e9}
    plans, _ = S.plan_tranches(conn, ASOF, q, adv, 0)
    assert plans[0]["qty"] == 70                                          # (10% × 10 萬 − 3000) ÷ 2 ÷ 50
    plans, _ = S.plan_tranches(conn, ASOF, q, adv, 0, user_hard_cap=None)
    assert plans[0]["qty"] == 70
    plans, _ = S.plan_tranches(conn, ASOF, q, adv, 0, user_hard_cap=0.05)
    assert plans[0]["qty"] == 20                                          # (5% × 10 萬 − 3000) ÷ 2 ÷ 50


def test_hard_cap_blocks_tranche_and_add_above_it(conn):
    _building(conn, 120)                                                  # 權重 6% > 硬上限 5%（< 等級上限 10%）
    q, adv = {"BLD": 50.0}, {"BLD": 1e9}
    plans, _ = S.plan_tranches(conn, ASOF, q, adv, 0, user_hard_cap=0.05)
    assert plans[0]["qty"] == 0 and "權重超過 5%" in plans[0]["reason"]
    add = S.plan_buys(conn, ASOF, [_item("BLD", "ADD")], q, adv, 0, user_hard_cap=0.05)
    assert add[0]["qty"] == 0 and "超過 5%" in add[0]["reason"]
    add = S.plan_buys(conn, ASOF, [_item("BLD", "ADD")], q, adv, 0)        # null：照舊加碼
    assert add[0]["qty"] == 40                                            # (10% × 10 萬 − 6000) ÷ 2 ÷ 50


def test_hard_cap_limits_add_target(conn):
    _building(conn, 60)
    q, adv = {"BLD": 50.0}, {"BLD": 1e9}
    add = S.plan_buys(conn, ASOF, [_item("BLD", "ADD")], q, adv, 0, user_hard_cap=0.05)
    assert add[0]["qty"] == 20 and add[0]["target_weight"] == pytest.approx(0.05)


def test_daily_passes_user_hard_cap_to_buy_planners(world, monkeypatch):
    cfg, paths, conn = world
    cfg = dataclasses.replace(cfg, account=dataclasses.replace(cfg.account, user_hard_cap=0.07))
    seen = {}
    for name in ("plan_buys", "plan_tranches"):
        orig = getattr(S, name)

        def wrap(*a, _orig=orig, _name=name, **kw):
            seen[_name] = kw.get("user_hard_cap")
            return _orig(*a, **kw)
        monkeypatch.setattr(S, name, wrap)
    _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    assert seen == {"plan_buys": 0.07, "plan_tranches": 0.07}


# ---------------------------------------------------------------- 2. weight_over_cap 不誤結案

def _open(conn):
    return [dict(r) for r in conn.execute("SELECT ticker, detail FROM anomalies WHERE kind=? AND status='open'",
                                          (S.WEIGHT_OVER_CAP,))]


def _day(conn, asof, items, quotes, hard=None):
    plans = S.plan_sells(conn, asof, items, quotes, user_hard_cap=hard)
    S.record_weight_over_cap(conn, plans, asof)
    return plans


def test_weight_over_cap_not_resolved_when_unchecked(conn):
    book.ensure_initial_deposit(conn, 80000.0, "2026-10-01")
    _pos(conn, "AAA", 200, 100.0, first="2026-06-01")                    # 2 萬／10 萬＝20% > B 級 10%
    _reg(conn, "AAA", "HOLDING", grade="B")
    q = {"AAA": 100.0}
    _day(conn, "2026-10-14", [], q)
    assert len(_open(conn)) == 1
    trim = {"decision_id": "AAA-20261015-1", "ticker": "AAA", "action": "TRIM", "review_type": "HOLD_REVIEW",
            "log": {"action": "TRIM"}}
    _day(conn, "2026-10-15", [trim], q)                                   # 當日有賣出決策：沒檢查
    _day(conn, "2026-10-16", [], {})                                      # 缺收盤：沒檢查
    plans = _day(conn, "2026-10-19", [], q, hard=0.15)                   # 制度類減碼中：沒檢查
    assert any(p.get("reason_code") == "INSTITUTIONAL" for p in plans)
    a = _open(conn)
    assert len(a) == 1 and json.loads(a[0]["detail"])["first_date"] == "2026-10-14"
    _day(conn, "2026-10-20", [], q)                                       # 仍超限：同一筆、保留 first_date
    a = _open(conn)
    assert len(a) == 1 and json.loads(a[0]["detail"])["first_date"] == "2026-10-14"
    assert conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind=?", (S.WEIGHT_OVER_CAP,)).fetchone()[0] == 1
    _day(conn, "2026-10-21", [], {"AAA": 40.0})                           # 跌回上限以下：確認後結案
    assert _open(conn) == []


def test_weight_over_cap_resolved_when_sold(conn):
    book.ensure_initial_deposit(conn, 80000.0, "2026-10-01")
    _pos(conn, "AAA", 200, 100.0, first="2026-06-01")
    _reg(conn, "AAA", "HOLDING", grade="B")
    _day(conn, "2026-10-14", [], {"AAA": 100.0})
    conn.execute("DELETE FROM positions WHERE ticker='AAA'")
    sell = {"decision_id": "AAA-20261015-1", "ticker": "AAA", "action": "SELL_ALL", "review_type": "THESIS",
            "log": {"action": "SELL_ALL"}}
    _day(conn, "2026-10-15", [sell], {"AAA": 100.0})                     # 已無持股 → 結案
    assert _open(conn) == []


# ---------------------------------------------------------------- 3. 相關群：已排／未成交買進以收盤估權重

def test_corr_pending_and_committed_buys_use_close_not_limit(conn):
    book.ensure_initial_deposit(conn, 90000.0, "2026-10-01")
    _pos(conn, "AAA", 100, 100.0)                                         # 10%，損失 3%
    for t, st in (("AAA", "HOLDING"), ("BBB", "WATCH"), ("CCC", "WATCH")):
        _reg(conn, t, st, grade="B", asset_class="非金融", external_drivers=["interest_rate_up"])
    vocab = C.load_vocab()
    closes = {"AAA": 100.0, "BBB": 50.0, "CCC": 50.0}
    # 未成交買單 BBB 66 股，限價 59.99（上限 60 × 0.9999），收盤 50 → 以 3.3% 計，不以 3.96% 計
    db.insert(conn, "orders", {"order_id": "o1", "ticker": "BBB", "side": "BUY", "qty": 66, "limit_price": 59.99,
                               "status": "open", "decision_id": None, "created_date": "2026-10-15",
                               "cancel_reason": None, "updated_at": db.now_iso()})
    w, _ = C.holdings_weights(conn, closes)
    assert w["BBB"] == pytest.approx(0.033)
    room = C.buy_room(conn, "CCC", closes, vocab)
    assert room["room"] == pytest.approx((0.05 - 0.03 - 0.033 * 0.3) / 0.3)   # ≈ 3.37%
    w, _ = C.holdings_weights(conn, {"AAA": 100.0})                       # 缺收盤：退回限價（從嚴）
    assert w["BBB"] == pytest.approx(66 * 59.99 / 100000)
