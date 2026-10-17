"""每日收盤流程（daily.py）：合成情境，不連網。模型以 FakeRunner，SEC／Alpaca 以假物件。

日期：D1=2026-10-15（四）、D2=2026-10-16（五）、D3=2026-10-19（一）、週六=2026-10-17。
"""
import json

import pandas as pd
import pytest

import test_engine as TE
from trader import cli, db
from trader.config import load_config
from trader.ledger import book
from trader.llm.fake import FakeRunner
from trader.llm.router import Router
from trader.paths import get_paths
from trader.pipelines import daily as DP

D1, D2, D3 = "2026-10-15", "2026-10-16", "2026-10-19"
SAT = "2026-10-17"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}


class FakeAlpaca:
    """bars：{date: {ticker: (open, high, low, close, volume)}}；calendar 為週一至週五。"""

    def __init__(self, bars, actions=None):
        self.bars_by_date = bars
        self.actions = actions or []

    def calendar(self, start, end):
        return [{"date": d.strftime("%Y-%m-%d"), "open": "09:30", "close": "16:00"}
                for d in pd.bdate_range(start, end)]

    def bars(self, symbols, start, end, adjustment="raw"):
        rows = []
        for date, per in self.bars_by_date.items():
            if not (start <= date <= end):
                continue
            for t, (o, h, lo, c, v) in per.items():
                if t in symbols:
                    rows.append({"ticker": t, "date": date, "open": o, "high": h, "low": lo, "close": c,
                                 "volume": v, "vwap": c})
        return pd.DataFrame(rows, columns=["ticker", "date", "open", "high", "low", "close", "volume", "vwap"])

    def corporate_actions(self, symbols, start, end, types=None):
        return [a for a in self.actions if start < a["ex_date"] <= end and a["ticker"] in symbols]

    def news(self, symbols, start, end):
        return []


class FakeSec:
    user_agent = "Test Runner test@example.com"

    def cik_for(self, ticker):
        return None

    def filings(self, cik, asof, since=None, forms=None):
        return []


def _bars(d1_close=50.0, d2_open=50.0, d2_low=49.5):
    base = {
        "AAA": (50.0, 51.0, 49.0, d1_close, 1_000_000),
        "BBB": (50.0, 51.0, 49.0, 50.0, 1_000_000),
        "ZZZ": (40.0, 41.0, 39.0, 40.0, 1_000_000),
        "HDV": (100.0, 101.0, 99.0, 100.0, 1_000_000),
        "SPY": (400.0, 401.0, 399.0, 400.0, 10_000_000),
    }
    d2 = {t: (o, h, lo, c, v) for t, (o, h, lo, c, v) in base.items()}
    d2["AAA"] = (d2_open, 51.0, d2_low, 50.0, 1_000_000)
    d2["ZZZ"] = (50.0, 51.0, 49.0, 50.0, 1_000_000)
    d2["HDV"] = (100.0, 101.0, 99.0, 100.0, 1_000_000)
    d3 = {t: (o, h, lo, c, v) for t, (o, h, lo, c, v) in base.items()}
    return {"2026-10-14": base, D1: base, D2: d2, D3: d3}


@pytest.fixture
def world(tmp_path, state_dir):
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=state_dir).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, cfg.account.initial_cash, "2026-10-01")
    yield cfg, paths, conn
    conn.close()


def _reg(conn, t, status, data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": data, "updated_at": db.now_iso()},
              key=["ticker"])


def _dec(conn, did, t, date, action, review_type=None, card=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "signal": action,
                                  "data": {"ticker": t, "decision_date": date, "action": action,
                                           "review_type": review_type, **GATES},
                                  "created_at": db.now_iso()})
    if card is not None:
        db.insert(conn, "signals", {"decision_id": did, "ticker": t, "signal": "BUY", "data": card,
                                    "created_at": db.now_iso()})


CARD = {"target_weight_pct": 10, "certainty_grade": "B", "auto_buy_cap": 60.0,
        "value_per_share": {"conservative": 60.0}, "max_weight_pct": {"host": 15},
        "position_plan": {"first_tranche_pct_of_target": 50}}


def _clients(alpaca):
    return {"alpaca": alpaca, "sec": FakeSec(), "fred": None}


def _router(cfg, paths, responses=None):
    return Router(cfg, paths.routing_json, dry_run=True, fake=FakeRunner(responses=responses or {}))


def _run(cfg, paths, conn, date, alpaca, **kw):
    return DP.run_daily(cfg, paths, conn, date, clients=_clients(alpaca), skip_review=True,
                        router=kw.pop("router", None) or _router(cfg, paths), **kw)


def test_non_trading_day_is_skipped(world):
    cfg, paths, conn = world
    s = _run(cfg, paths, conn, SAT, FakeAlpaca(_bars()))
    assert s == {"skipped": "非交易日", "date": SAT}
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_buy_next_day_fill_and_nav(world):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    alp = FakeAlpaca(_bars())

    s1 = _run(cfg, paths, conn, D1, alp)
    assert "error" not in s1, s1
    assert len(s1["orders"]) == 1
    o = s1["orders"][0]
    # 首批 50% × 10% × 100000 = 5000；限價 50.05 → 99 股；當日建單、次日撮合
    assert o["qty"] == 99 and o["created_date"] == D1 and o["side"] == "BUY"
    assert s1["fills"] == []

    s2 = _run(cfg, paths, conn, D2, alp)
    assert "error" not in s2, s2
    assert len(s2["fills"]) == 1 and s2["fills"][0]["qty"] == 99
    assert s2["fills"][0]["price"] == pytest.approx(50.05)
    pos = book.positions(conn)["AAA"]
    assert pos["qty"] == 99 and pos["first_buy_date"] == D2
    assert s2["nav"]["n_positions"] == 1
    # 成交後同日 plan_tranches 立即下第一批加碼（consume 一批）；建倉計畫剩 2 批
    adds = [x for x in s2["orders"] if x["action"] == "ADD"]
    assert len(adds) == 1 and adds[0]["qty"] > 0
    reg = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])
    assert reg["build_plan"]["tranches_left"] == 2 and reg["build_plan"]["target_weight"] == pytest.approx(0.10)
    assert reg["weight"] == pytest.approx(s2["nav"]["data"]["weights"]["AAA"]) and reg["weight"] > 0  # 每日更新
    row = conn.execute("SELECT kind, status FROM runs WHERE run_id=?", (f"daily-{D2}",)).fetchone()
    assert (row["kind"], row["status"]) == ("daily_close", "done")


def test_full_portfolio_records_switch_candidate(world):
    """持股已滿 10 檔：新 BUY 不建倉，記一筆不暫停的換股候選異常（同檔只記一次）。"""
    cfg, paths, conn = world
    for i in range(10):
        t = f"H{i:02d}"
        db.insert(conn, "positions", {"ticker": t, "qty": 1, "avg_cost": 10.0, "first_buy_date": "2025-01-01",
                                      "updated_at": db.now_iso()})
        _reg(conn, t, "HOLDING", {"cik": f"00000001{i:02d}", "grade": "B"})
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    s1 = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    assert "error" not in s1, s1
    assert s1["orders"] == []
    rows = conn.execute("SELECT ticker, pause_trading FROM anomalies WHERE kind='switch_candidate'").fetchall()
    assert [tuple(r) for r in rows] == [("AAA", 0)]


def test_dry_run_writes_nothing(world):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("orders", "fills", "cash_ledger", "positions", "nav_daily", "runs", "registry")}
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()), dry_run=True)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after
    assert s["dry_run"] is True and s["orders"] == []
    assert len(s["plans_dry_run"]) == 1 and s["plans_dry_run"][0]["qty"] == 99


def test_price_triggered_buy_has_cooling_off(world):
    cfg, paths, conn = world
    _reg(conn, "BBB", "WATCH", {"cik": "0000000002", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "BBB-20261015-1", "BBB", D1, "BUY", review_type="PRICE", card=CARD)
    alp = FakeAlpaca(_bars())
    s1 = _run(cfg, paths, conn, D1, alp)
    assert s1["orders"][0]["created_date"] == D2     # 冷靜期：次一交易日才可撮合
    s2 = _run(cfg, paths, conn, D2, alp)
    assert s2["fills"] == []                         # D2 不撮合（created_date 不早於 D2）
    s3 = _run(cfg, paths, conn, D3, alp)
    assert len(s3["fills"]) == 1


def test_thesis_sell_all_fills_next_day_at_open_less_slippage(world):
    cfg, paths, conn = world
    _reg(conn, "ZZZ", "HOLDING", {"cik": "0000000003", "grade": "B", "auto_buy_cap": 60.0})
    db.insert(conn, "positions", {"ticker": "ZZZ", "qty": 100, "avg_cost": 40.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    _dec(conn, "ZZZ-20261015-1", "ZZZ", D1, "SELL_ALL", review_type="THESIS")
    alp = FakeAlpaca(_bars())
    s1 = _run(cfg, paths, conn, D1, alp)
    assert s1["orders"][0]["qty"] == 100 and s1["orders"][0]["created_date"] == D1   # 證偽不受冷靜期限制
    s2 = _run(cfg, paths, conn, D2, alp)
    assert len(s2["fills"]) == 1
    assert s2["fills"][0]["price"] == pytest.approx(50.0 * (1 - 10 / 1e4))           # 開盤價減滑價
    assert "ZZZ" not in book.positions(conn)


def test_paused_ticker_gets_no_order(world):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    db.insert(conn, "anomalies", {"ticker": "AAA", "kind": "corp_action_check_failed", "detail": "{}",
                                  "status": "open", "pause_trading": 1, "created_at": db.now_iso()})
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    assert s["orders"] == []
    assert s["buys"][0]["qty"] == 0 and "暫停" in s["buys"][0]["reason"]


def test_cash_dividend_receivable_then_paid(world):
    cfg, paths, conn = world
    _reg(conn, "HDV", "HOLDING", {"cik": "0000000004", "grade": "B", "auto_buy_cap": 120.0})
    db.insert(conn, "positions", {"ticker": "HDV", "qty": 100, "avg_cost": 100.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    db.insert(conn, "cash_ledger", {"date": "2025-01-01", "amount": -10000.0, "kind": "buy", "ref": "SEED",
                                    "note": "測試建倉現金"})
    action = {"ticker": "HDV", "type": "cash_dividend", "ex_date": D2, "process_date": None, "record_date": None,
              "payable_date": D3, "ratio_new_per_old": None, "cash_per_share": 1.0, "new_ticker": None, "raw": {}}
    result = {"ticker": "HDV", "action_type": "現金股利", "effective_date": D2, "pay_date": D3,
              "ratio_new_per_old": None, "cash_per_share": 1.0, "fractional_handling": "不適用",
              "new_ticker": None, "needs_human": False, "evidence": "測試原文", "reason": "每股 1 元現金股利"}
    alp = FakeAlpaca(_bars(), actions=[action])
    router = _router(cfg, paths, {"CORP_ACTION": result})
    s2 = _run(cfg, paths, conn, D2, alp, router=router)
    assert "error" not in s2, s2
    assert s2["corp_events"] and s2["corp_events"][0]["source"] == "alpaca"
    assert book.receivables(conn) == pytest.approx(100.0)
    s3 = _run(cfg, paths, conn, D3, alp, router=router)
    assert "error" not in s3, s3
    assert book.receivables(conn) == pytest.approx(0.0)
    assert s3["settled"] and s3["settled"][0]["amount"] == pytest.approx(100.0)


def test_failed_corp_action_job_pauses_ticker_and_is_redetected_next_day(world):
    """H2：CORP_ACTION 工作失敗 → 寫 pause_trading 異常、事件不當成已處理；下次 daily 重派，成功後解除暫停。"""
    from trader.llm.base import TransportError
    cfg, paths, conn = world
    _reg(conn, "AAA", "HOLDING", {"cik": "0000000001", "grade": "B", "auto_buy_cap": 60.0})
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 100, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    split = {"ticker": "AAA", "type": "forward_split", "ex_date": D2, "process_date": None, "record_date": None,
             "payable_date": None, "ratio_new_per_old": 2.0, "cash_per_share": None, "new_ticker": None, "raw": {"id": "x1"}}
    bars = _bars()
    bars[D2]["AAA"] = (25.0, 26.0, 24.0, 25.0, 1_000_000)
    bars[D3]["AAA"] = (25.0, 26.0, 24.0, 25.0, 1_000_000)
    alp = FakeAlpaca(bars, actions=[split])
    s2 = _run(cfg, paths, conn, D2, alp,
              router=_router(cfg, paths, {"CORP_ACTION": TransportError("claude 結束碼 1：usage limit reached")}),
              sleep=lambda x: None)
    assert s2["ca_engine"]["failed"] > 0
    rows = conn.execute("SELECT ticker, pause_trading, status FROM anomalies WHERE kind='corp_action_job_failed'").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("AAA", 1, "open")]
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 0
    assert any("未完成" in n for n in s2["notes"])
    # 同日重跑不重複寫異常
    _run(cfg, paths, conn, D2, alp, router=_router(cfg, paths, {"CORP_ACTION": TransportError("usage limit reached")}),
         sleep=lambda x: None)
    assert conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='corp_action_job_failed'").fetchone()[0] == 1
    # 隔天：事件已在窗口外，仍須重派；成功後入帳並解除暫停
    result = {"ticker": "AAA", "action_type": "拆股", "effective_date": D2, "pay_date": None, "ratio_new_per_old": 2.0,
              "cash_per_share": None, "fractional_handling": "不適用", "new_ticker": None, "needs_human": False,
              "evidence": "測試原文", "reason": "2 拆 1"}
    s3 = _run(cfg, paths, conn, D3, alp, router=_router(cfg, paths, {"CORP_ACTION": result}))
    assert "error" not in s3, s3
    assert [e["ticker"] for e in s3["corp_events"]] == ["AAA"]
    assert book.positions(conn)["AAA"]["qty"] == 200
    s4 = _run(cfg, paths, conn, D3, alp, router=_router(cfg, paths, {"CORP_ACTION": result}))
    assert s4["corp_events"] == []
    assert conn.execute("SELECT status FROM anomalies WHERE kind='corp_action_job_failed'").fetchone()[0] == "resolved"


def test_benchmark_dividend_not_detected(world):
    """基準 SPY 未持有、不在觀察名單：股利不產生 CORP_ACTION 工作。"""
    cfg, paths, conn = world
    action = {"ticker": "SPY", "type": "cash_dividend", "ex_date": D2, "process_date": None, "record_date": None,
              "payable_date": D3, "ratio_new_per_old": None, "cash_per_share": 1.9, "new_ticker": None, "raw": {}}
    s2 = _run(cfg, paths, conn, D2, FakeAlpaca(_bars(), actions=[action]))
    assert "error" not in s2, s2
    assert s2["corp_events"] == []
    # 正規化後的鍵是 cash_per_share（不是 cash）：基準含股利報酬要記到這筆股利
    row = conn.execute("SELECT data FROM nav_daily WHERE date = ?", (D2,)).fetchone()
    assert json.loads(row["data"])["benchmark_dividend"] == pytest.approx(1.9)


def test_unregistered_holding_flagged_once(world):
    cfg, paths, conn = world
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 10, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    s1 = _run(cfg, paths, conn, D2, FakeAlpaca(_bars()))
    s2 = _run(cfg, paths, conn, D3, FakeAlpaca(_bars()))
    assert any("AAA 持股不在登錄表" in n for n in s1["notes"] + s2["notes"])
    rows = conn.execute("SELECT pause_trading FROM anomalies WHERE kind='unregistered_holding'").fetchall()
    assert [r["pause_trading"] for r in rows] == [0]            # 只記一筆，且不暫停


def test_review_failure_blocks_new_buys_but_not_sells(world, monkeypatch):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)

    def boom(*a, **k):
        return {"run_id": "review-x", "error": "模擬觸發檢查失敗", "engine": None}

    monkeypatch.setattr(DP, "run_review", boom)
    s = DP.run_daily(cfg, paths, conn, D1, clients=_clients(FakeAlpaca(_bars())), skip_review=False,
                     router=_router(cfg, paths))
    assert s["review"]["ok"] is False
    assert s["orders"] == [] and s["buys"] == []
    assert any("今日不下新買單" in n for n in s["notes"])


def test_missing_alpaca_raises_error_summary_not_crash(world):
    cfg, paths, conn = world
    with pytest.raises(DP.DailyError):
        DP.run_daily(cfg, paths, conn, D1, clients={"alpaca": None, "sec": FakeSec()})


def test_cli_daily_and_ledger(state_dir, tmp_path, monkeypatch, capsys):
    cfg_path = TE._write_config(tmp_path)
    monkeypatch.setenv("TRADER_CONFIG", str(cfg_path))
    monkeypatch.setattr(DP, "make_clients", lambda cfg, paths: _clients(FakeAlpaca(_bars())))
    rc = cli.main(["daily", "--date", SAT, "--skip-review", "--state-dir", str(state_dir),
                   "--config", str(cfg_path)])
    assert rc == 0 and '"skipped"' in capsys.readouterr().out
    rc = cli.main(["ledger", "adjust", "--note", "測試入金", "--cash", "250", "--state-dir", str(state_dir)])
    assert rc == 0
    rc = cli.main(["ledger", "show", "--state-dir", str(state_dir)])
    out = capsys.readouterr().out
    assert rc == 0 and '"cash"' in out
