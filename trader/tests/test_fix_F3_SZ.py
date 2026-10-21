"""F3-SZ 回歸測試：V3-3 G-trim-unfilled（A3-M7A-02 review_due 端、A3-M7A-03、A3-M7B-07）與
V3-2 G13-sizing-misc（A3-M6B-08、A3-M6C-05、A3-M6C-06、A3-M6E-03）。全部離線、不下單。"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from trader import db
from trader.ledger import book, broker
from trader.pipelines import daily as DP
from trader.portfolio import correlation as C
from trader.portfolio import sizing as S

ROOT = Path(__file__).resolve().parents[2]
ASOF = "2026-10-16"
D1 = "2026-10-14"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    book.ensure_initial_deposit(c, 90000.0, "2026-10-01")
    yield c
    c.close()


def _pos(conn, t, qty, avg, first="2026-10-10"):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": first,
                                  "updated_at": db.now_iso()})


def _reg(conn, t, status, **data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"ticker": t, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _regdata(conn, t):
    return json.loads(conn.execute("SELECT data FROM registry WHERE ticker = ?", (t,)).fetchone()[0])


def _card(**kw):
    c = {"target_weight_pct": 10, "certainty_grade": "B", "auto_buy_cap": 60.0, "price_to_conservative": 0.8,
         "value_per_share": {"conservative": 60.0}, "max_weight_pct": {"host": 15},
         "position_plan": {"first_tranche_pct_of_target": 50}, "external_drivers": None}
    c.update(kw)
    return c


# ================================================================ G-trim-unfilled

def _review_due():
    sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
    try:
        import review_due
    finally:
        sys.path.pop(0)
    return review_due


def _px(vals, end="2026-10-15"):
    d0 = date.fromisoformat(end)
    return pd.DataFrame({"date": [d0 - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))], "close": vals})


@pytest.mark.parametrize("mark", [{"unfilled": "2026-10-02 下單被拒：暫停交易", "shares": 0}, {"shares": 0}])
def test_m7a02_review_due_unfilled_absurd_trim_does_not_block(mark):
    """A3-M7A-02（review_due 端）：荒謬級減碼沒賣成（unfilled／shares＝0）不算減過，價格仍在線上 → 照邊緣觸發 PRICE。"""
    rd = _review_due()
    ha = dict(ticker="X", status="HOLDING", cost=100, last_review_date="2026-10-01", last_full_review_date="2026-06-01",
              review_price=119, t30_at_valuation=0.047, auto_buy_cap=80, sell_review_above=None, absurd_price_line=120)
    lt = {"date": "2026-10-01", "reason_code": "ABSURD", "line": 120, "shares": 50}
    dd = {}
    assert rd.check(dict(ha, last_trim=lt), [], _px([121] * 120), "2026-10-15", 0.047, detail=dd)[0] == "NONE"
    assert any("只減一次" in u for u in dd["unchecked"])                          # 真的減過：仍擋（D03）
    dd = {}
    a = rd.check(dict(ha, last_trim=dict(lt, **mark)), [], _px([121] * 120), "2026-10-15", 0.047, detail=dd)
    assert a[0] == "PRICE" and not any("只減一次" in u for u in dd["unchecked"]), (a, dd)


def _trim_setup(conn):
    _pos(conn, "KO", 100, 50.0, first="2026-01-02")
    _reg(conn, "KO", "HOLDING", last_trim={"date": D1, "reason_code": "ABSURD", "line": 80.0, "fraction": 0.5,
                                            "shares": 50, "peak_shares": 100})
    db.insert(conn, "decisions", {"decision_id": "KO-H-1", "ticker": "KO", "decision_date": D1, "signal": "TRIM",
                                  "data": {"action": "TRIM", "review_type": "HOLD_REVIEW", "sell_category": "估值"},
                                  "created_at": db.now_iso()})
    return {"ticker": "KO", "action": "TRIM", "decision_id": "KO-H-1", "qty": 0, "reason": "此決策已下過單"}


def test_m7a03_rerun_does_not_mark_placed_trim_unfilled(conn):
    """A3-M7A-03：同日重跑 plan_sells 回 qty 0「此決策已下過單」：委託仍 open → last_trim 不得被標 unfilled／shares 0。"""
    plan = _trim_setup(conn)
    broker.place_order(conn, "KO", "SELL", 50, 80.0, "KO-H-1", D1)
    DP._trim_not_placed(conn, plan, f"{D1} 計畫股數 0：此決策已下過單")
    lt = _regdata(conn, "KO")["last_trim"]
    assert lt["shares"] == 50 and "unfilled" not in lt


def test_m7a03_filled_order_also_not_marked(conn):
    plan = _trim_setup(conn)
    oid = broker.place_order(conn, "KO", "SELL", 50, 80.0, "KO-H-1", D1)
    conn.execute("UPDATE orders SET status = 'filled' WHERE order_id = ?", (oid,))
    conn.commit()
    DP._trim_not_placed(conn, plan, "x")
    assert "unfilled" not in _regdata(conn, "KO")["last_trim"]


def test_m7a03_no_order_still_marked_unfilled(conn):
    """沒有委託（真的沒下成）照舊標 unfilled（M7-RS3-01 行為不變）。"""
    plan = _trim_setup(conn)
    DP._trim_not_placed(conn, plan, f"{D1} 下單被拒：暫停")
    lt = _regdata(conn, "KO")["last_trim"]
    assert lt["shares"] == 0 and "下單被拒" in lt["unfilled"]


def test_m7b07_contract_documents_d82_and_unfilled():
    txt = (ROOT / "trader" / "CONTRACTS_M7.md").read_text(encoding="utf-8")
    assert "D82" in txt and "unfilled" in txt
    assert "D82" in (broker.recheck_pending.__doc__ or "") and "unfilled" in broker.recheck_pending.__doc__


# ================================================================ G13-sizing-misc

def test_m6b08_add_outside_build_uses_min_T(conn):
    """A3-M6B-08：建倉期外加碼 T＝min(訊號卡 T, 登錄表 target_weight, 等級上限)。登錄表 6% < 卡 10% → 依 6% 補缺口。"""
    _pos(conn, "BBB", 100, 50.0)                     # 權益 9 萬＋5 千＝9.5 萬
    _reg(conn, "BBB", "HOLDING", grade="B", auto_buy_cap=60.0, target_weight=0.06)
    item = {"decision_id": "BBB-20261016-1", "ticker": "BBB", "action": "ADD", "review_type": "PRICE",
            "log": {**GATES, "action": "ADD"}, "card": _card()}
    p = S.plan_buys(conn, ASOF, [item], {"BBB": 50.0}, {"BBB": 1e9}, 0)[0]
    assert p["qty"] == 14, p                          # (0.06×95000 − 5000) ÷ 50；修前依卡 10% → 90 股
    assert any("登錄表 target_weight" in n for n in p["notes"])


def test_m6c05_cash_bound_qty_uses_limit_new_buy(conn, monkeypatch):
    """A3-M6C-05：現金綁定時以限價算股數，qty×限價（預扣口徑）不超過可用現金。"""
    monkeypatch.setattr(S, "_avail_cash", lambda c, eq: 1000.0)
    _reg(conn, "AAA", "WATCH", grade="B")
    item = {"decision_id": "AAA-20261016-1", "ticker": "AAA", "action": "BUY", "review_type": None,
            "log": {**GATES, "action": "BUY"}, "card": _card()}
    p = S.plan_buys(conn, ASOF, [item], {"AAA": 50.0}, {"AAA": 1e9}, 0)[0]
    assert p["limit_price"] == 59.99 and p["qty"] == 16, p    # floor(1000/59.99)；修前 floor(1000/50)=20 → 承諾 1199.8
    assert p["qty"] * p["limit_price"] <= 1000.0


def test_m6c05_cash_bound_qty_uses_limit_tranche(conn, monkeypatch):
    monkeypatch.setattr(S, "_avail_cash", lambda c, eq: 1000.0)
    _pos(conn, "BBB", 10, 50.0)
    _reg(conn, "BBB", "HOLDING", grade="B", auto_buy_cap=60.0,
         build_plan={"target_weight": 0.10, "tranches_left": 1, "days_above_cap": 0, "started": "2026-10-10"})
    db.insert(conn, "decisions", {"decision_id": "BBB-20261010-1", "ticker": "BBB", "decision_date": "2026-10-10",
                                  "signal": "BUY", "data": {**GATES, "action": "BUY"}, "created_at": db.now_iso()})
    plans, cancels = S.plan_tranches(conn, ASOF, {"BBB": 50.0}, {"BBB": 1e9}, 0)
    assert not cancels and plans[0]["qty"] == 16, plans
    assert plans[0]["qty"] * plans[0]["limit_price"] <= 1000.0


def test_m6c06_buy_room_includes_candidate_tail(conn, monkeypatch):
    """A3-M6C-06：P-31 額度要把候選自身尾部算進買後的群：Σw·l＋x·l_c ≤ 10pp。
    持有 AAA 10%（最壞 25%），候選 BBB 最壞 60%、A 級（2×15%＝30%）：修前 room＝min(30%, 10/0.25)−10%＝20%，
    買後 0.025＋0.2×0.6＝14.5pp 越界；修後 x ≤ (0.10−0.025)/0.6＝12.5%。"""
    infos = {"AAA": {"ticker": "AAA", "variables": ["rates"], "financial": False, "impairment": 0.1,
                     "worst_loss": 0.25, "grade": "A", "g8": None},
             "BBB": {"ticker": "BBB", "variables": ["rates"], "financial": False, "impairment": 0.1,
                     "worst_loss": 0.6, "grade": "A", "g8": None}}
    monkeypatch.setattr(C, "member_info", lambda c, t, v: infos[t])
    r = C.buy_room(conn, "BBB", {}, {}, info=infos["BBB"], weights={"AAA": 0.10})
    assert r["status"] == "ok" and r["room"] == pytest.approx(0.125), r
    x = r["room"]
    assert 0.10 * 0.25 + x * 0.6 <= C.GROUP_LOSS_BUDGET + 1e-9
    # 候選尾部與群相同時與舊口徑一致：cap − weight
    infos["BBB"]["worst_loss"] = 0.25
    assert C.buy_room(conn, "BBB", {}, {}, info=infos["BBB"], weights={"AAA": 0.10})["room"] == pytest.approx(0.20)


def test_m6e03_record_weight_over_cap_commits(conn):
    """A3-M6E-03：只 UPDATE（結案）也要提交，不留隱式交易給後續 book.transaction 併入。"""
    db.insert(conn, "anomalies", {"ticker": "AAA", "kind": S.WEIGHT_OVER_CAP, "detail": json.dumps({"first_date": D1}),
                                  "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
    S.record_weight_over_cap(conn, [], ASOF)
    assert not conn.in_transaction
    assert conn.execute("SELECT status FROM anomalies").fetchone()[0] == "resolved"


def test_m6e03_record_trim_anomalies_commits(conn):
    db.insert(conn, "anomalies", {"ticker": "AAA", "kind": "corr_group_trim", "detail": json.dumps({"date": D1}),
                                  "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
    C.record_trim_anomalies(conn, {"trim_recommendations": []}, ASOF)
    assert not conn.in_transaction
    assert conn.execute("SELECT status FROM anomalies").fetchone()[0] == "resolved"


def test_m6e03_record_anomaly_functions_atomic_on_error(conn, monkeypatch):
    """寫到一半出錯整筆回滾（不提交半套）。"""
    db.insert(conn, "anomalies", {"ticker": "AAA", "kind": S.WEIGHT_OVER_CAP, "detail": json.dumps({"first_date": D1}),
                                  "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
    plans = [{"ticker": "ZZZ", "anomaly": {"kind": S.WEIGHT_OVER_CAP, "weight": 0.2}}]

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(book, "write_row", boom)
    monkeypatch.setattr(db, "insert", boom)
    with pytest.raises(RuntimeError):
        S.record_weight_over_cap(conn, plans, ASOF)
    assert not conn.in_transaction
    assert conn.execute("SELECT status FROM anomalies").fetchone()[0] == "open"   # 結案的 UPDATE 一起回滾
