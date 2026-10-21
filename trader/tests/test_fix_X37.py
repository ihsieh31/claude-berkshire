"""X37（P25 的 needs_other_files）：daily 接上 D01 的權重規則。

- plan_sells 帶入 cfg.account.user_hard_cap：權重超過硬上限 → INSTITUTIONAL 減碼（只減超出部分），即使建倉未滿一年。
- 寫入模式呼叫 record_weight_over_cap：因上漲超過等級上限 → 不賣，寫不暫停交易的 weight_over_cap 異常；dry_run 不寫。
- hold_jobs：THESIS 降級直接交的複查（full_stage＝THESIS）不取舊 cap_result 的 IV_base，提示寫明沒有完整分析。
合成情境、不連網（沿用 test_daily_pipeline 的假 Alpaca／SEC）。
"""
import dataclasses
import json

from test_daily_pipeline import D1, FakeAlpaca, _bars, _reg, _run, world  # noqa: F401（world 為 fixture）
from trader import db
from trader.jobs import hold_jobs as H
from trader.portfolio import sizing as Z


def _hold_aaa(conn, qty=400, first="2026-06-01"):
    """AAA 持股 400 股 × 50＝2 萬；權益＝初始現金＋2 萬 → 權重 > B 級上限 10%（建倉未滿一年）。"""
    db.insert(conn, "positions", {"ticker": "AAA", "qty": qty, "avg_cost": 50.0, "first_buy_date": first,
                                  "updated_at": db.now_iso()})
    _reg(conn, "AAA", "HOLDING", {"cik": "0000000001", "grade": "B", "first_buy_date": first})


def _open_anoms(conn):
    return [dict(r) for r in conn.execute(
        "SELECT ticker, detail, pause_trading FROM anomalies WHERE kind=? AND status='open'", (Z.WEIGHT_OVER_CAP,))]


def _sell_orders(conn):
    return [dict(r) for r in conn.execute("SELECT ticker, qty, limit_price FROM orders WHERE side='SELL'")]


def test_daily_records_weight_over_cap_anomaly_without_selling(world):
    cfg, paths, conn = world
    assert cfg.account.user_hard_cap is None
    _hold_aaa(conn)
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    a = _open_anoms(conn)
    assert len(a) == 1 and a[0]["ticker"] == "AAA" and a[0]["pause_trading"] == 0
    d = json.loads(a[0]["detail"])
    assert d["cap"] == 0.10 and d["weight"] > 0.10 and d["first_date"] == D1
    assert _sell_orders(conn) == []
    assert [p["qty"] for p in s["sells"] if p["ticker"] == "AAA"] == [0]
    c2 = db.connect(paths.db)   # 已提交（另一連線看得到）
    try:
        assert len(_open_anoms(c2)) == 1
    finally:
        c2.close()
    _run(cfg, paths, conn, "2026-10-16", FakeAlpaca(_bars()))   # 次日仍超限：更新同一筆，不重複
    a2 = _open_anoms(conn)
    assert len(a2) == 1 and json.loads(a2[0]["detail"])["first_date"] == D1


def test_daily_dry_run_does_not_write_anomaly(world):
    cfg, paths, conn = world
    _hold_aaa(conn)
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()), dry_run=True)
    assert any(p.get("anomaly", {}).get("kind") == Z.WEIGHT_OVER_CAP for p in s["sells"])
    assert _open_anoms(conn) == []


def test_daily_user_hard_cap_places_institutional_trim(world):
    cfg, paths, conn = world
    cfg = dataclasses.replace(cfg, account=dataclasses.replace(cfg.account, user_hard_cap=0.10))
    _hold_aaa(conn)
    eq_cash = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM cash_ledger").fetchone()[0]
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    equity = eq_cash + 400 * 50.0
    want = 400 - int(0.10 * equity // 50.0)
    orders = [o for o in s["orders"] if o["ticker"] == "AAA"]
    assert len(orders) == 1 and orders[0]["side"] == "SELL" and orders[0]["reason_code"] == "INSTITUTIONAL"
    assert orders[0]["qty"] == want > 0
    assert _sell_orders(conn) == [{"ticker": "AAA", "qty": want, "limit_price": None}]
    assert _open_anoms(conn) == []          # 制度類減碼取代「只記錄」，不另記等級超限異常


def test_thesis_stage_hold_review_has_no_iv(tmp_path):
    (tmp_path / "cap_result.json").write_text(json.dumps({"IV_base": 99.0}), encoding="utf-8")
    assert "THESIS" in H.NO_IV_STAGES
    assert H.valuation_numbers(tmp_path, {"full_stage": "THESIS", "price": 50.0}) == (None, 50.0)
    assert "cap_result.json" not in H._input_files(tmp_path, {"full_stage": "THESIS"})
