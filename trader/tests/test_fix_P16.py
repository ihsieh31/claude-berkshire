"""P16：每日收盤正確性（M8-D7-01、M7-D7-05、M7-D2-01、M7-D1-09、M7-D5-03、M7-D6-02、M7-D9-06、
M7-D5-06、M7-D5-01、M7-D1-10）。沿用 test_daily_pipeline 的合成情境。"""
import json
from datetime import datetime, timezone

import pytest

import test_daily_pipeline as TD
from test_daily_pipeline import CARD, D1, D2, D3, SAT, FakeAlpaca, _bars, _dec, _reg, _run, world  # noqa: F401
from trader import cli, db
from trader.ledger import book
from trader.pipelines import daily as DP
from trader.portfolio import sizing as Z
from trader.scheduler import Scheduler

D4 = "2026-10-20"


def _bars4():
    b = _bars()
    b[D4] = dict(b[D3])
    return b


def _runr(cfg, paths, conn, d, alp):
    """跑觸發檢查（run_review 由各測試 monkeypatch）。"""
    return DP.run_daily(cfg, paths, conn, d, clients=TD._clients(alp), skip_review=False,
                        router=TD._router(cfg, paths), now=TD.NOW)


def _runs(conn, d):
    r = conn.execute("SELECT kind, status FROM runs WHERE run_id=?", (f"daily-{d}",)).fetchone()
    return tuple(r) if r else None


# ---- M8-D7-01：首次 daily 讀得到 bootstrap 決策
def test_first_daily_reads_bootstrap_decision(world):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)   # bootstrap 決策日 10-15
    alp = FakeAlpaca(_bars4())
    s = _run(cfg, paths, conn, D3, alp, now=datetime(2026, 10, 21, 12, tzinfo=timezone.utc))
    assert "error" not in s, s
    assert [o["decision_id"] for o in s["orders"]] == ["AAA-20261015-1"]
    s2 = _run(cfg, paths, conn, D4, alp, now=datetime(2026, 10, 21, 12, tzinfo=timezone.utc))
    assert "error" not in s2, s2
    assert all(p["decision_id"] != "AAA-20261015-1" for p in s2["buys"])   # 第二次不重讀


# ---- M7-D7-05（＝M7-D6-06、M8-D7-05）：紐約未收盤不跑；--force 略過；當日無日線不標 done
def test_not_closed_in_new_york_raises(world):
    cfg, paths, conn = world
    alp = FakeAlpaca(_bars())
    with pytest.raises(DP.DailyError, match="尚未收盤"):   # 紐約 D1 10:00（台北 22:00）
        _run(cfg, paths, conn, D1, alp, now=datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc))
    assert _runs(conn, D1) is None
    with pytest.raises(DP.DailyError, match="尚未收盤"):   # 未來日期
        _run(cfg, paths, conn, D2, alp, now=datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc))
    s = _run(cfg, paths, conn, D1, alp, now=datetime(2026, 10, 15, 21, 31, tzinfo=timezone.utc))   # 17:31 EDT
    assert "error" not in s and _runs(conn, D1) == ("daily_close", "done")


def test_force_skips_close_check(world):
    cfg, paths, conn = world
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()), now=datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc),
             force=True)
    assert "error" not in s, s


def test_cli_daily_has_force_flag(monkeypatch, state_dir, tmp_path):
    seen = {}

    def fake(cfg, paths, conn, date, **kw):
        seen.update(kw)
        return {"date": date}

    monkeypatch.setattr(DP, "run_daily", fake)
    cfgp = TD.TE._write_config(tmp_path)
    rc = cli.main(["daily", "--date", D1, "--force", "--config", str(cfgp), "--state-dir", str(state_dir)])
    assert rc == 0 and seen.get("force") is True


def test_no_bars_today_fails_not_done(world):
    cfg, paths, conn = world
    b = _bars()
    del b[D2]
    s = _run(cfg, paths, conn, D2, FakeAlpaca(b))
    assert "沒有任何當日日線" in s["error"]
    assert _runs(conn, D2) == ("daily_close", "failed")


# ---- M7-D2-01（＝M7-D8-05）：CORP_ACTION 引擎不標 daily-D done；中斷寫 failed
def test_corp_action_engine_uses_own_run_and_interrupt_marks_failed(world, monkeypatch):
    cfg, paths, conn = world
    _reg(conn, "AAA", "HOLDING", {"cik": "0000000001", "grade": "B", "auto_buy_cap": 60.0})
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 100, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    split = {"ticker": "AAA", "type": "forward_split", "ex_date": D2, "process_date": None, "record_date": None,
             "payable_date": None, "ratio_new_per_old": 2.0, "cash_per_share": None, "new_ticker": None,
             "raw": {"id": "x1"}}
    result = {"ticker": "AAA", "action_type": "拆股", "effective_date": D2, "pay_date": None, "ratio_new_per_old": 2.0,
              "cash_per_share": None, "fractional_handling": "不適用", "new_ticker": None, "needs_human": False,
              "evidence": "測試原文", "reason": "2 拆 1"}
    b = _bars()
    b[D2]["AAA"] = (25.0, 26.0, 24.0, 25.0, 1_000_000)
    alp = FakeAlpaca(b, actions=[split])

    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(DP.corp_actions, "settle_dividends", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _run(cfg, paths, conn, D2, alp, router=TD._router(cfg, paths, {"CORP_ACTION": result}))
    assert _runs(conn, D2) == ("daily_close", "failed")
    assert conn.execute("SELECT kind, status FROM runs WHERE run_id=?", (f"daily-{D2}:ca",)).fetchone()[0] == "corp_action"
    assert not Scheduler(cfg, paths)._daily_done(conn, D2)


# ---- M7-D1-09：非交易日寫 done，排程視為完成
def test_non_trading_day_marks_done(world):
    cfg, paths, conn = world
    s = _run(cfg, paths, conn, SAT, FakeAlpaca(_bars()))
    assert s["skipped"] == "非交易日"
    assert Scheduler(cfg, paths)._daily_done(conn, SAT)
    # 不影響「上次 daily 完成時間」（late_after）
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind='daily_close'").fetchone()[0] == 0


# ---- M7-D5-03／M7-D5-06：觸發檢查失敗 → 延後買進、批次標未下單；次日重核下單
def test_review_failure_defers_buy_to_next_daily(world, monkeypatch):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    alp = FakeAlpaca(_bars())
    monkeypatch.setattr(DP, "run_review", lambda *a, **k: {"run_id": "r", "error": "模擬失敗", "engine": None})
    s1 = _runr(cfg, paths, conn, D1, alp)
    assert s1["orders"] == []
    assert [x["decision_id"] for x in s1["deferred_buys"]] == ["AAA-20261015-1"]
    a = conn.execute("SELECT detail, pause_trading FROM anomalies WHERE kind='buy_deferred_review_failed'").fetchone()
    assert "AAA-20261015-1" in json.loads(a["detail"])["decision_ids"] and a["pause_trading"] == 0
    monkeypatch.setattr(DP, "run_review", lambda *a, **k: {"run_id": "r", "engine": {"status": "done", "failed": 0}})
    s2 = _runr(cfg, paths, conn, D2, alp)
    assert "error" not in s2, s2
    assert [o["decision_id"] for o in s2["orders"]] == ["AAA-20261015-1"]


def test_deferred_buy_dropped_when_newer_decision(world, monkeypatch):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    alp = FakeAlpaca(_bars())
    monkeypatch.setattr(DP, "run_review", lambda *a, **k: {"run_id": "r", "error": "模擬失敗", "engine": None})
    _runr(cfg, paths, conn, D1, alp)
    _dec(conn, "AAA-20261016-1", "AAA", D2, "NO_ACTION")
    s2 = _run(cfg, paths, conn, D2, alp)
    assert s2["orders"] == []


def test_tranches_marked_unplaced_when_review_fails(world, monkeypatch):
    cfg, paths, conn = world
    monkeypatch.setattr(DP.Z, "plan_tranches", lambda *a, **k: ([Z._plan("AAA", "BUY", 10, 50.0, "d1", "ADD", "批次")], []))
    monkeypatch.setattr(DP, "run_review", lambda *a, **k: {"run_id": "r", "error": "模擬失敗", "engine": None})
    s = _runr(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    assert s["tranches"][0]["placed"] is False and s["tranches"][0]["skip_reason"] == "觸發檢查失敗"
    assert s["orders"] == []


# ---- M7-D5-01：參考價留底
def test_summary_has_reference_quotes(world):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars(d1_close=49.87)))
    assert s["quotes"]["AAA"] == 49.87 and s["price_date"] == D1
    assert s["price_source"].startswith("FakeAlpaca:")


# ---- M7-D6-02：不可回頭重跑
def test_past_date_rerun_after_later_daily_refused(world):
    cfg, paths, conn = world
    alp = FakeAlpaca(_bars())
    _run(cfg, paths, conn, D1, alp)
    _run(cfg, paths, conn, D2, alp)
    nav1 = conn.execute("SELECT nav FROM nav_daily WHERE date=?", (D1,)).fetchone()[0]
    with pytest.raises(DP.DailyError, match="不可回頭重跑"):
        _run(cfg, paths, conn, D1, alp)
    assert conn.execute("SELECT nav FROM nav_daily WHERE date=?", (D1,)).fetchone()[0] == nav1
    s = _run(cfg, paths, conn, D2, alp)   # 同日重跑不受影響
    assert "error" not in s, s


# ---- M7-D9-06：委託與 build_plan 同一交易
def test_place_and_registry_update_atomic(world, monkeypatch):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)
    real = Z.merge_registry

    def boom(c, t, patch, d):
        if "build_plan" in patch:
            raise RuntimeError("模擬中斷")
        return real(c, t, patch, d)

    monkeypatch.setattr(DP.Z, "merge_registry", boom)
    s = _run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    assert "模擬中斷" in s["error"]
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


# ---- M7-D1-10：持股缺當日 bar → 以最近收盤估值，仍列 missing_close
def test_missing_today_bar_uses_last_close(world):
    cfg, paths, conn = world
    _reg(conn, "BBB", "HOLDING", {"cik": "0000000002", "grade": "B"})
    db.insert(conn, "positions", {"ticker": "BBB", "qty": 10, "avg_cost": 30.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    b = _bars()
    del b[D2]["BBB"]   # D1 收盤 50
    s = _run(cfg, paths, conn, D2, FakeAlpaca(b))
    assert "error" not in s, s
    assert s["nav"]["data"]["prices"]["BBB"] == 50.0
    assert "BBB" in s["nav"]["data"]["missing_close"]
    row = json.loads(conn.execute("SELECT data FROM nav_daily WHERE date=?", (D2,)).fetchone()[0])
    assert row["prices"]["BBB"] == 50.0 and "BBB" in row["missing_close"]
