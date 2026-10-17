"""深度分析 pipeline：初選結果 → 工作參數 → 引擎（FakeRunner），價格只進價格檔、終點回寫 DB。"""
import json

import pandas as pd

from trader import db
from trader.config import load_config
from trader.llm.fake import FakeRunner
from trader.llm.router import Router
from trader.paths import get_paths
from trader.pipelines.analyze import run_analyze

import test_engine as TE

ASOF = TE.ASOF


def test_run_analyze_from_screen(tmp_path, monkeypatch):
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    sd = paths.state / "screens" / ASOF
    (sd / "inputs").mkdir(parents=True)
    (sd / "summary.json").write_text(json.dumps({"to_analyze": [{"ticker": "KO"}]}), encoding="utf-8")
    (sd / "handoff_noprice.json").write_text(json.dumps(
        {"core_shortlist": [{"ticker": "KO", "screen_decision": "PASS"}], "special_handoff": []}), encoding="utf-8")
    pd.DataFrame([{"ticker": "KO", "price": 60.5, "price_date": "2026-10-15"}]).to_csv(sd / "inputs" / "prices.csv",
                                                                                       index=False)
    (sd / "inputs" / "rates.json").write_text(json.dumps({"t30": 0.05, "t30_date": "2026-10-14",
                                                         "t30_20y_median": 0.034}), encoding="utf-8")
    run = f"analyze-{ASOF}"
    # 模擬 DOSSIER 前置已完成（真的前置需要 SEC 連網，另有 dossier_prep 測試）
    td = paths.work / run / "KO"
    (td / "dossier").mkdir(parents=True)
    (td / "sec").mkdir()
    (td / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    (td / "dossier" / "KO_price.json").write_text(json.dumps({"price": 60.5}), encoding="utf-8")

    fake = FakeRunner(responses=TE.full_responses())
    router = Router(cfg, paths.routing_json, dry_run=True, fake=fake)
    out = run_analyze(cfg, paths, conn, ASOF, clients={"sec": object()}, router=router)

    assert out["tickers"] == ["KO"] and out["engine"]["failed"] == 0
    assert out["decisions"]["KO"]["decision_id"] == "KO-20261016-1"
    dossier_job = conn.execute("SELECT params FROM jobs WHERE job_type='DOSSIER'").fetchone()
    p = json.loads(dossier_job["params"])
    assert p["price"] == 60.5 and p["rates"]["t30"] == 0.05
    assert p["portfolio_state"]["holdings"] == []
    from trader.portfolio.holdings_view import view_path
    assert json.loads(view_path(paths, run).read_text(encoding="utf-8"))["holdings"] == []
    assert "holdings_g8" not in json.dumps(p)          # 比較檔不進工作參數（FULL_A 會沿用參數）
    # FULL_A 的提示看不到價格
    fa = [c for c in fake.calls if c.job_type == "FULL_A"][0]
    assert "60.5" not in fa.prompt_text


def test_portfolio_state_with_holdings_has_cash_committed_and_no_market_price(tmp_path):
    """M1：有持股時要有現金、已承諾買單金額、持股檔數與成本權重；不得出現市價欄位（價格隔離）。"""
    from trader.ledger import book, broker
    from trader.pipelines.analyze import _portfolio_state
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    conn = db.connect(get_paths(state_dir=tmp_path / "state").ensure().db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100_000, "2026-10-01")
    db.insert(conn, "positions", {"ticker": "KO", "qty": 100, "avg_cost": 60.0, "first_buy_date": "2026-10-02",
                                  "updated_at": "2026-10-02"})
    broker.place_order(conn, "PEP", "BUY", 10, 150.0, None, "2026-10-14")
    ps = _portfolio_state(cfg, conn)
    assert ps["cash"] == 100_000 and ps["n_holdings"] == 1
    assert ps["committed"] == 1500.0
    h = ps["holdings"][0]
    assert (h["ticker"], h["qty"], h["avg_cost"], h["cost_value"]) == ("KO", 100, 60.0, 6000.0)
    assert h["cost_weight"] == round(6000 / 106_000, 4)
    assert not {"price", "close", "market_value", "weight"} & set(h)


def test_review_held_closes_fetches_missing_holdings(tmp_path):
    """scope 不含持股時，G8 比較檔另抓持股的決策日收盤（不晚於決策日）。"""
    from trader.pipelines.review import _held_closes
    conn = db.connect(get_paths(state_dir=tmp_path / "state").ensure().db)
    db.migrate(conn)
    db.insert(conn, "positions", {"ticker": "KO", "qty": 1, "avg_cost": 60.0, "first_buy_date": "2026-10-02",
                                  "updated_at": "2026-10-02"})

    class Alp:
        def bars(self, tickers, start, end, adjustment):
            assert tickers == ["KO"] and end == ASOF
            return pd.DataFrame([{"ticker": "KO", "date": "2026-10-15", "close": 61.0},
                                 {"ticker": "KO", "date": "2026-10-17", "close": 99.0}])
    out = _held_closes(conn, Alp(), ASOF, {"PEP": {"price": 150.0, "price_date": ASOF}})
    assert out["KO"] == {"price": 61.0, "price_date": "2026-10-15"} and "PEP" in out
    assert _held_closes(conn, None, ASOF, {}) == {}
