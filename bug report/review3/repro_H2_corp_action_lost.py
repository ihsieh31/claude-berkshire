import sys
sys.path.insert(0, "/Users/zongen/Downloads/claude code/berkshire/trader")
sys.path.insert(0, "/Users/zongen/Downloads/claude code/berkshire/trader/tests")
import pytest
import test_daily_pipeline as T
from trader import db
from trader.config import load_config
from trader.llm.base import TransportError
from trader.paths import get_paths
from trader.ledger import book
import test_engine as TE

def test_failed_corp_action_job_silently_loses_split(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_STATE_DIR", str(tmp_path / "state"))
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    book.ensure_initial_deposit(conn, cfg.account.initial_cash, "2026-10-01")
    T._reg(conn, "AAA", "HOLDING", {"cik": "0000000001", "grade": "B", "auto_buy_cap": 60.0})
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 100, "avg_cost": 50.0, "first_buy_date": "2025-01-01",
                                  "updated_at": db.now_iso()})
    split = {"ticker": "AAA", "type": "forward_split", "ex_date": T.D2, "process_date": None, "record_date": None,
             "payable_date": None, "ratio_new_per_old": 2.0, "cash_per_share": None, "new_ticker": None, "raw": {"id": "x1"}}
    bars = T._bars()
    bars[T.D2]["AAA"] = (25.0, 26.0, 24.0, 25.0, 1_000_000)   # 拆股後價格減半
    bars[T.D3]["AAA"] = (25.0, 26.0, 24.0, 25.0, 1_000_000)
    alp = T.FakeAlpaca(bars, actions=[split])
    router = T._router(cfg, paths, {"CORP_ACTION": TransportError("claude 結束碼 1：usage limit reached")})
    s2 = T._run(cfg, paths, conn, T.D2, alp, router=router)
    print("D2 error:", s2.get("error"), "| ca_engine:", s2.get("ca_engine"))
    st = conn.execute("select status from runs where run_id='daily-2026-10-16'").fetchone()[0]
    print("daily run status:", st)
    print("anomalies:", conn.execute("select count(*) from anomalies").fetchone()[0])
    print("corporate_actions rows:", conn.execute("select count(*) from corporate_actions").fetchone()[0])
    print("job statuses:", [tuple(r) for r in conn.execute("select job_id,status from jobs")])
    s3 = T._run(cfg, paths, conn, T.D3, alp, router=T._router(cfg, paths, {"CORP_ACTION": {}}))
    print("D3 corp_events:", s3["corp_events"])
    print("position after D3:", book.positions(conn)["AAA"])
