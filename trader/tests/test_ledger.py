"""紙上帳本測試：撮合、現金流、持股成本、淨值、人工調整。全部離線。"""
import pytest

from trader import db
from trader.ledger import book, broker
from trader.ledger.book import LedgerError
from trader.ledger.broker import BrokerError
from trader.paths import get_paths

D0 = "2026-10-01"
D1 = "2026-10-14"  # 下單日
D2 = "2026-10-15"  # 撮合日


@pytest.fixture
def conn(state_dir):
    paths = get_paths().ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    yield c
    c.close()


def _bar(o, h, l, c=None):
    return {"open": o, "high": h, "low": l, "close": c if c is not None else o}


def _registry(conn, ticker, status="WATCH", data=None):
    db.upsert(conn, "registry", {
        "ticker": ticker, "status": status, "first_buy_date": None,
        "data": data if data is not None else {"cost": None, "grade": "B"}, "updated_at": db.now_iso(),
    }, ["ticker"])


def _pause(conn, ticker):
    db.insert(conn, "anomalies", {
        "ticker": ticker, "kind": "test", "detail": {}, "status": "open", "pause_trading": 1,
        "created_at": db.now_iso(),
    })


def _buy_fill(conn, ticker, qty, limit, open_px, date=D2, created=D1, bps=0.0, comm=0.0):
    """下一日開盤以 open_px 成交的買單，回傳成交列表。"""
    broker.place_order(conn, ticker, "BUY", qty, limit, None, created)
    return broker.match_orders(conn, date, {ticker: _bar(open_px, open_px + 1, open_px - 1)}, bps, comm)


def test_initial_deposit_written_once(conn):
    book.ensure_initial_deposit(conn, 100_000, D0)
    book.ensure_initial_deposit(conn, 100_000, D0)
    rows = conn.execute("SELECT * FROM cash_ledger WHERE kind = 'deposit'").fetchall()
    assert len(rows) == 1
    assert book.cash(conn) == 100_000


def test_initial_deposit_rejects_non_positive(conn):
    with pytest.raises(LedgerError):
        book.ensure_initial_deposit(conn, 0, D0)


def test_buy_fills_only_on_next_trading_day(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    broker.place_order(conn, "AAPL", "BUY", 10, 100.0, "AAPL-1", D1)
    # 同日不撮合，即使 bar 低點已低於限價
    assert broker.match_orders(conn, D1, {"AAPL": _bar(99, 101, 98)}, 0, 0) == []
    fills = broker.match_orders(conn, D2, {"AAPL": _bar(99, 101, 98)}, 0, 0)
    assert len(fills) == 1
    assert fills[0]["price"] == pytest.approx(99.3333)  # min(典型價 (101+98+99)/3, 限價)（D07）
    assert fills[0]["qty"] == 10
    assert book.cash(conn) == pytest.approx(10_000 - 993.33)
    assert book.positions(conn)["AAPL"]["qty"] == 10


def test_buy_not_filled_when_low_above_limit(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    broker.place_order(conn, "AAPL", "BUY", 10, 100.0, None, D1)
    assert broker.match_orders(conn, D2, {"AAPL": _bar(105, 110, 101)}, 0, 0) == []
    assert book.open_orders(conn)[0]["status"] == "open"


def test_buy_slippage_and_commission_cash_flows(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    fills = _buy_fill(conn, "AAPL", 10, 101.0, 100.0, bps=10, comm=1.0)
    assert fills[0]["price"] == pytest.approx(100.1)
    assert book.cash(conn) == pytest.approx(10_000 - 1001.0 - 1.0)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM cash_ledger ORDER BY entry_id")]
    assert kinds == ["deposit", "buy", "commission"]


def test_buy_reduced_when_cash_insufficient(conn):
    book.ensure_initial_deposit(conn, 1_000, D0)
    broker.place_order(conn, "AAPL", "BUY", 20, 100.0, None, D1)
    fills = broker.match_orders(conn, D2, {"AAPL": _bar(100, 101, 99)}, 0, 1.0)
    # 可用 999 元 → floor(999/100) = 9 股
    assert fills[0]["qty"] == 9
    assert book.positions(conn)["AAPL"]["qty"] == 9
    assert book.cash(conn) == pytest.approx(1_000 - 900 - 1)
    order = conn.execute("SELECT status, cancel_reason FROM orders").fetchone()
    assert order["status"] == "filled"
    assert "部分成交 9/20" in order["cancel_reason"]


def test_buy_stays_open_when_no_cash_for_one_share(conn):
    book.ensure_initial_deposit(conn, 50, D0)
    broker.place_order(conn, "AAPL", "BUY", 1, 100.0, None, D1)
    assert broker.match_orders(conn, D2, {"AAPL": _bar(100, 101, 99)}, 0, 0) == []
    assert book.open_orders(conn)[0]["status"] == "open"
    assert book.positions(conn) == {}


def test_avg_cost_and_first_buy_date(conn):
    book.ensure_initial_deposit(conn, 100_000, D0)
    _buy_fill(conn, "AAPL", 10, 100.0, 100.0, date="2026-10-15", created="2026-10-14")
    _buy_fill(conn, "AAPL", 10, 120.0, 120.0, date="2026-10-20", created="2026-10-19")
    pos = book.positions(conn)["AAPL"]
    assert pos["qty"] == 20
    assert pos["avg_cost"] == pytest.approx(110.0)
    assert pos["first_buy_date"] == "2026-10-15"


def test_sell_limit_none_fills_at_typical_with_slippage(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "AAPL", 10, 100.0, 100.0)
    broker.place_order(conn, "AAPL", "SELL", 10, None, None, "2026-10-16")
    fills = broker.match_orders(conn, "2026-10-19", {"AAPL": _bar(110, 111, 109)}, 10, 0)
    assert fills[0]["price"] == pytest.approx(109.89)
    assert book.cash(conn) == pytest.approx(10_000 - 1000 + 1098.90)


def test_sell_limit_requires_high_reaching_limit(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "AAPL", 10, 100.0, 100.0)
    oid = broker.place_order(conn, "AAPL", "SELL", 10, 120.0, None, "2026-10-16")
    assert broker.match_orders(conn, "2026-10-19", {"AAPL": _bar(118, 119, 117)}, 0, 0) == []
    # 限價賣單（估值 TRIM）當日有效：未觸及即作廢，不留到隔日（D03）
    r = conn.execute("SELECT status, cancel_reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert r["status"] == "expired" and "當日有效" in r["cancel_reason"]
    broker.place_order(conn, "AAPL", "SELL", 10, 120.0, None, "2026-10-19")
    fills = broker.match_orders(conn, "2026-10-20", {"AAPL": _bar(118, 121, 117)}, 0, 0)
    assert fills[0]["price"] == 120.0  # max(典型價 118.67, 限價)


def test_sell_all_deletes_position_and_sets_registry_watch(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _registry(conn, "AAPL", data={"cost": None, "grade": "B"})
    _buy_fill(conn, "AAPL", 10, 100.0, 100.0)
    assert conn.execute("SELECT status FROM registry").fetchone()["status"] == "HOLDING"
    broker.place_order(conn, "AAPL", "SELL", 10, None, None, "2026-10-16")
    broker.match_orders(conn, "2026-10-19", {"AAPL": _bar(100, 101, 99)}, 0, 0)
    assert book.positions(conn) == {}
    reg = conn.execute("SELECT status, data FROM registry").fetchone()
    assert reg["status"] == "WATCH"


def test_partial_sell_keeps_avg_cost(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "AAPL", 10, 100.0, 100.0)
    broker.place_order(conn, "AAPL", "SELL", 4, None, None, "2026-10-16")
    broker.match_orders(conn, "2026-10-19", {"AAPL": _bar(100, 101, 99)}, 0, 0)
    pos = book.positions(conn)["AAPL"]
    assert pos["qty"] == 6
    assert pos["avg_cost"] == pytest.approx(100.0)


def test_sell_capped_at_holdings_and_cancelled_without_position(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "AAPL", 5, 100.0, 100.0)
    broker.place_order(conn, "AAPL", "SELL", 8, None, None, "2026-10-16")
    fills = broker.match_orders(conn, "2026-10-19", {"AAPL": _bar(100, 101, 99)}, 0, 0)
    assert fills[0]["qty"] == 5
    broker.place_order(conn, "AAPL", "SELL", 1, None, None, "2026-10-19")
    assert broker.match_orders(conn, "2026-10-20", {"AAPL": _bar(100, 101, 99)}, 0, 0) == []
    assert book.open_orders(conn) == []  # 無持股的賣單已取消


def test_no_bar_means_no_match(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    broker.place_order(conn, "AAPL", "BUY", 1, 100.0, None, D1)
    assert broker.match_orders(conn, D2, {"MSFT": _bar(100, 101, 99)}, 0, 0) == []


def test_paused_ticker_not_matched_and_rejected(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    broker.place_order(conn, "AAPL", "BUY", 1, 100.0, None, D1)
    _pause(conn, "AAPL")
    assert broker.match_orders(conn, D2, {"AAPL": _bar(100, 101, 99)}, 0, 0) == []
    with pytest.raises(BrokerError):
        broker.place_order(conn, "AAPL", "BUY", 1, 100.0, None, D2)


def test_place_order_validation(conn):
    with pytest.raises(BrokerError):
        broker.place_order(conn, "AAPL", "BUY", 1.5, 100.0, None, D1)
    with pytest.raises(BrokerError):
        broker.place_order(conn, "AAPL", "BUY", 0, 100.0, None, D1)
    with pytest.raises(BrokerError):
        broker.place_order(conn, "AAPL", "BUY", 1, None, None, D1)
    with pytest.raises(BrokerError):
        broker.place_order(conn, "AAPL", "BUY", 1, 0, None, D1)
    with pytest.raises(BrokerError):
        broker.place_order(conn, "AAPL", "HOLD", 1, 100.0, None, D1)


def test_order_id_format_and_normalization(conn):
    oid1 = broker.place_order(conn, "brk-b", "BUY", 1, 100.0, None, "2026-10-14")
    oid2 = broker.place_order(conn, "BRK.B", "BUY", 1, 100.0, None, "2026-10-14")
    assert oid1 == "BRK.B-BUY-20261014-1"
    assert oid2 == "BRK.B-BUY-20261014-2"


def test_cancel_order(conn):
    oid = broker.place_order(conn, "AAPL", "BUY", 1, 100.0, None, D1)
    broker.cancel_order(conn, oid, "人工取消")
    row = conn.execute("SELECT status, cancel_reason FROM orders WHERE order_id = ?", (oid,)).fetchone()
    assert row["status"] == "cancelled"
    assert row["cancel_reason"] == "人工取消"
    with pytest.raises(BrokerError):
        broker.cancel_order(conn, oid, "再取消")


def test_dividend_receivable_then_paid(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "KO", 10, 50.0, 50.0)
    # 除息日：應收 +5
    db.insert(conn, "cash_ledger", {"date": "2026-10-20", "amount": 5.0, "kind": "dividend_receivable",
                                    "ref": "CA:KO:cash_dividend:2026-10-20", "note": "除息"})
    assert book.receivables(conn) == 5.0
    assert book.cash(conn) == pytest.approx(10_000 - 500)
    # 發放日：收現金、沖銷應收
    db.insert(conn, "cash_ledger", {"date": "2026-11-01", "amount": 5.0, "kind": "dividend",
                                    "ref": "CA:KO:cash_dividend:2026-10-20", "note": "發放"})
    db.insert(conn, "cash_ledger", {"date": "2026-11-01", "amount": -5.0, "kind": "dividend_receivable",
                                    "ref": "CA:KO:cash_dividend:2026-10-20", "note": "沖銷"})
    assert book.receivables(conn) == 0
    assert book.cash(conn) == pytest.approx(10_000 - 500 + 5)


def test_equity_and_record_nav(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "AAPL", 10, 100.0, 100.0)
    _buy_fill(conn, "MSFT", 5, 200.0, 200.0, date="2026-10-16", created="2026-10-15")
    closes = {"AAPL": 110.0}  # MSFT 缺收盤價，以成本 200 估值
    assert book.equity(conn, closes) == pytest.approx(10_000 - 1000 - 1000 + 1100 + 1000)
    row = book.record_nav(conn, "2026-10-16", closes, 4500.0)
    assert row["nav"] == pytest.approx(10_100)
    assert row["n_positions"] == 2
    assert row["data"]["missing_close"] == ["MSFT"]
    assert row["data"]["weights"]["AAPL"] == pytest.approx(0.1089, abs=1e-4)
    # 同日重算為 upsert，不新增列
    book.record_nav(conn, "2026-10-16", {"AAPL": 120.0, "MSFT": 210.0}, 4510.0)
    rows = conn.execute("SELECT nav, benchmark_close, data FROM nav_daily").fetchall()
    assert len(rows) == 1
    assert rows[0]["nav"] == pytest.approx(10_000 - 2000 + 1200 + 1050)
    assert rows[0]["benchmark_close"] == 4510.0


def test_books_balance_after_round_trip(conn):
    """買進後全數賣出（價格不變、無滑價）：現金只剩手續費損失。"""
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "AAPL", 7, 100.0, 100.0, comm=2.0)
    broker.place_order(conn, "AAPL", "SELL", 7, None, None, "2026-10-16")
    broker.match_orders(conn, "2026-10-19", {"AAPL": _bar(100, 101, 99)}, 0, 2.0)
    assert book.positions(conn) == {}
    assert book.cash(conn) == pytest.approx(10_000 - 4.0)


def test_show_reports_weights_and_open_items(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "AAPL", 10, 100.0, 100.0)
    broker.place_order(conn, "MSFT", "BUY", 1, 50.0, None, D2)
    s = book.show(conn, {"AAPL": 100.0})
    assert s["equity"] == pytest.approx(10_000)
    assert s["positions"][0]["weight"] == pytest.approx(0.1)
    assert len(s["open_orders"]) == 1
    assert s["open_anomalies"] == []


def test_committed_buy_value(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    broker.place_order(conn, "AAPL", "BUY", 10, 100.0, None, D1)
    broker.place_order(conn, "MSFT", "BUY", 2, 50.0, None, D1)
    assert book.committed_buy_value(conn) == pytest.approx(1100.0)


def test_match_is_atomic(conn, monkeypatch):
    """第二筆成交失敗時，第一筆成交也必須回滾。"""
    book.ensure_initial_deposit(conn, 10_000, D0)
    broker.place_order(conn, "AAPL", "BUY", 10, 100.0, None, D1)
    broker.place_order(conn, "MSFT", "BUY", 10, 100.0, None, D1)
    real_fill = broker._fill
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模擬寫入失敗")
        return real_fill(*args, **kwargs)

    monkeypatch.setattr(broker, "_fill", flaky)
    with pytest.raises(RuntimeError):
        broker.match_orders(conn, D2, {"AAPL": _bar(100, 101, 99), "MSFT": _bar(100, 101, 99)}, 0, 0)
    assert conn.in_transaction is False
    assert book.cash(conn) == 10_000
    assert book.positions(conn) == {}
    assert conn.execute("SELECT COUNT(*) AS c FROM fills").fetchone()["c"] == 0
    assert all(o["status"] == "open" for o in book.open_orders(conn))


def test_adjust_cash_delta_writes_adjust_row(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    res = book.adjust(conn, date=D2, cash_delta=-25.5, note="手續費補記")
    assert res["cash_delta"] == -25.5
    row = conn.execute("SELECT amount, kind, note FROM cash_ledger WHERE kind = 'adjust'").fetchone()
    assert row["amount"] == -25.5 and row["note"] == "手續費補記"
    assert book.cash(conn) == pytest.approx(10_000 - 25.5)


def test_adjust_requires_note_and_some_action(conn):
    with pytest.raises(LedgerError):
        book.adjust(conn, date=D2, cash_delta=1.0, note="  ")
    with pytest.raises(LedgerError):
        book.adjust(conn, date=D2, note="只有備註")


def test_adjust_set_qty_creates_and_zero_deletes(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    res = book.adjust(conn, date=D2, ticker="aapl", set_qty=5, set_avg_cost=80.0, note="補登持股")
    assert res["position"]["after"]["qty"] == 5
    assert book.positions(conn)["AAPL"]["first_buy_date"] == D2
    audit = conn.execute("SELECT kind, status, detail FROM anomalies WHERE kind = 'manual_adjust'").fetchone()
    assert audit["status"] == "resolved"
    book.adjust(conn, date=D2, ticker="AAPL", set_qty=0, note="清除錯誤持股")
    assert book.positions(conn) == {}


def test_adjust_avg_cost_only_keeps_qty_and_first_date(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _buy_fill(conn, "AAPL", 10, 100.0, 100.0)
    book.adjust(conn, date="2026-10-30", ticker="AAPL", set_avg_cost=95.0, note="修正成本")
    pos = book.positions(conn)["AAPL"]
    assert pos["qty"] == 10 and pos["avg_cost"] == 95.0
    assert pos["first_buy_date"] == D2


def test_adjust_rejects_bad_inputs(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    with pytest.raises(LedgerError):
        book.adjust(conn, date=D2, ticker="AAPL", set_qty=2.5, note="x")  # 非整數股
    with pytest.raises(LedgerError):
        book.adjust(conn, date=D2, ticker="AAPL", set_qty=3, note="x")  # 新建缺成本
    with pytest.raises(LedgerError):
        book.adjust(conn, date=D2, set_qty=3, set_avg_cost=10.0, note="x")  # 缺代號
    with pytest.raises(LedgerError):
        book.adjust(conn, date=D2, cash_delta=0, note="x")


def test_adjust_resolve_anomaly_and_unpause(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _pause(conn, "AAPL")
    _pause(conn, "AAPL")
    aid = conn.execute("SELECT MIN(anomaly_id) AS a FROM anomalies").fetchone()["a"]
    assert book.paused(conn) == {"AAPL"}
    book.adjust(conn, date=D2, resolve_anomaly=aid, note="已人工確認")
    row = conn.execute("SELECT status, resolution FROM anomalies WHERE anomaly_id = ?", (aid,)).fetchone()
    assert row["status"] == "resolved" and row["resolution"] == "已人工確認"
    assert book.paused(conn) == {"AAPL"}  # 另一筆仍未結
    with pytest.raises(LedgerError):
        book.adjust(conn, date=D2, resolve_anomaly=aid, note="重複")
    res = book.adjust(conn, date=D2, unpause="AAPL", note="全部解除")
    assert len(res["resolved"]) == 1
    assert book.paused(conn) == set()


def test_limit_never_worse_after_slippage(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    fills = _buy_fill(conn, "AAPL", 10, 100.0, 100.0, bps=10)       # 開盤＝限價：滑價後仍不得高於限價
    assert fills[0]["price"] == pytest.approx(100.0)
    broker.place_order(conn, "AAPL", "SELL", 10, 120.0, None, "2026-10-20")
    f = broker.match_orders(conn, "2026-10-21", {"AAPL": _bar(120, 121, 119)}, 10, 0)
    assert f[0]["price"] == pytest.approx(120.0)                      # 賣價不低於限價


def test_adjust_first_buy_date_inherits_for_spin_off(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    book.adjust(conn, date=D2, ticker="NEWCO", set_qty=10, set_avg_cost=20.0, first_buy_date=D0,
                note="分拆取得，沿用原持股建倉日")
    assert book.positions(conn)["NEWCO"]["first_buy_date"] == D0
    with pytest.raises(LedgerError):      # 晚於調整日
        book.adjust(conn, date=D0, ticker="NEWCO", set_qty=10, first_buy_date=D2, note="x")
    with pytest.raises(LedgerError):      # 格式錯
        book.adjust(conn, date=D2, ticker="NEWCO", set_qty=10, first_buy_date="2026/01/01", note="x")
    with pytest.raises(LedgerError):      # 未指定代號
        book.adjust(conn, date=D2, first_buy_date=D0, note="x")
    with pytest.raises(LedgerError):      # 無持股又未給股數
        book.adjust(conn, date=D2, ticker="ZZZ", first_buy_date=D0, note="x")
    book.adjust(conn, date=D2, ticker="NEWCO", first_buy_date="2026-01-02", note="只改建倉日")
    assert book.positions(conn)["NEWCO"] == {"qty": 10, "avg_cost": 20.0, "first_buy_date": "2026-01-02"}


def test_adjust_syncs_registry_status(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    db.insert(conn, "registry", {"ticker": "OLD", "status": "HOLDING", "data": "{}", "updated_at": db.now_iso()})
    db.insert(conn, "registry", {"ticker": "NEW", "status": "WATCH", "data": "{}", "updated_at": db.now_iso()})
    book.adjust(conn, date=D2, ticker="OLD", set_qty=5, set_avg_cost=10.0, note="補登")
    book.adjust(conn, date=D2, ticker="OLD", set_qty=0, note="併購消滅")
    book.adjust(conn, date=D2, ticker="NEW", set_qty=8, set_avg_cost=12.0, note="換得新股")
    st = dict(conn.execute("SELECT ticker, status FROM registry").fetchall())
    assert st == {"OLD": "WATCH", "NEW": "HOLDING"}


def test_adjust_set_qty_zero_cancels_open_orders(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    book.adjust(conn, date=D2, ticker="QRVO", set_qty=5, set_avg_cost=80.0, note="補登持股")
    oid = broker.place_order(conn, "QRVO", "BUY", 3, 79.0, None, D2)
    res = book.adjust(conn, date=D2, ticker="QRVO", set_qty=0, note="已下市")
    assert res["cancelled_orders"] == [oid]
    assert conn.execute("SELECT status FROM orders WHERE order_id = ?", (oid,)).fetchone()["status"] == "cancelled"
