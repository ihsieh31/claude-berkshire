"""P49：M7-D5-02（檢討報告成交類欄位缺漏率由 fills 推得）與 M7-D2-08（契約同步）的護欄。"""
from __future__ import annotations

import json
from pathlib import Path

from test_approvals import _env
from test_dashboard import _nav

from trader import db
from trader.ledger import broker
from trader.report import validation as V

ROOT = Path(__file__).resolve().parents[1]


def _dec(c, did, action, review_type=None, d="2026-10-15"):
    log = {"action": action, **({"review_type": review_type} if review_type else {})}
    db.insert(c, "decisions", {"decision_id": did, "ticker": did.split("-")[0], "decision_date": d, "run_id": "r",
                               "signal": action, "data": json.dumps(log), "created_at": db.now_iso()})


def _order(c, oid, did, status, side="BUY"):
    db.insert(c, "orders", {"order_id": oid, "ticker": did.split("-")[0], "side": side, "qty": 10, "limit_price": 50.0,
                            "status": status, "decision_id": did, "created_date": "2026-10-15",
                            "updated_at": db.now_iso()})


def _fill(c, oid, t):
    db.insert(c, "fills", {"order_id": oid, "ticker": t, "side": "BUY", "qty": 10, "price": 50.05,
                           "commission": 0.0, "fill_date": "2026-10-16"})


def test_fill_fields_derived_from_fills_not_always_missing(tmp_path):
    """已成交 BUY 的 fill_price 等缺漏率為 0%；未成交不計入分母（修前：fill_* 不列、slippage_assumed 等 100%）。"""
    env = _env(tmp_path)
    c = env.conn
    _dec(c, "KO-1", "BUY")
    _order(c, "o1", "KO-1", "filled")
    _fill(c, "o1", "KO")
    _dec(c, "PEP-1", "BUY")           # 未成交：不計
    _order(c, "o2", "PEP-1", "open")
    _dec(c, "HIG-1", "WATCH")         # 沒有委託：不計
    c.commit()
    comp = V.compliance(c, env.paths, "2026-10-01", "2026-10-31", V._logs(c, "2026-10-01", "2026-10-31"))
    for f in ("slippage_assumed", "tranche_weight", "portfolio_weight_after", "fill_price", "fill_date"):
        assert comp["missing_rate"].get(f) == 0.0, (f, comp["missing_rate"].get(f))
    assert comp["fill_basis"] == {"with_orders": 2, "filled": 1, "no_fill_rows": 0}
    assert comp["missing_rate"].get("time_zone") == 1.0   # 其他欄位照舊計算


def test_filled_order_without_fill_row_counts_missing(tmp_path):
    """委託標 filled 卻沒有 fills 列＝真正沒記錄 → 計為缺漏（分得出『沒成交』與『沒記錄』）。"""
    env = _env(tmp_path)
    c = env.conn
    _dec(c, "KO-1", "BUY")
    _order(c, "o1", "KO-1", "filled")
    _fill(c, "o1", "KO")
    _dec(c, "XOM-1", "TRIM", "HOLD_REVIEW")
    _order(c, "o3", "XOM-1", "filled", side="SELL")   # 無 fills 列
    c.commit()
    comp = V.compliance(c, env.paths, "2026-10-01", "2026-10-31", V._logs(c, "2026-10-01", "2026-10-31"))
    assert comp["missing_rate"]["fill_price"] == 0.5
    assert comp["fill_basis"]["no_fill_rows"] == 1


def test_render_explains_fill_basis_and_slippage(tmp_path):
    env = _env(tmp_path)
    c = env.conn
    _nav(c, "2026-10-15", 100000.0, 100000.0, 500.0)
    _nav(c, "2026-10-31", 100000.0, 100000.0, 500.0)
    _dec(c, "KO-1", "BUY")
    _order(c, "o1", "KO-1", "filled")
    _fill(c, "o1", "KO")
    c.commit()
    md = V.render(V.build(c, env.paths, "2026-10-01", "2026-10-31"))
    assert "由 fills 表推得" in md and "已成交 1 筆" in md and "account.slippage_bps" in md
    assert "fill_price" not in [ln.split("|")[1].strip() for ln in md.splitlines() if ln.startswith("| fill")]


def test_contracts_match_code_rules():
    """M7-D2-08：契約不得再寫已過時的口徑（開盤價撮合、(ticker, effective_date) 去重、只讀當日決策、run/once 嚴格擋）。"""
    m5 = (ROOT / "CONTRACTS_M5.md").read_text(encoding="utf-8")
    m7 = (ROOT / "CONTRACTS_M7.md").read_text(encoding="utf-8")
    assert "min(open, limit)" not in m5 and "max(open, limit)" not in m5 and "open × (1 − bps" not in m5
    assert "min(TP×(1＋滑價), 限價)" in m5 and "max(TP×(1−滑價), 限價)" in m5
    assert "不論 type" not in m5 and "(代號, 類型, 生效日)" in m5 and "event_id" in m5
    assert "decision_date == date" not in m5 and "late_after" in m5
    assert "否則 `install|run|once` 拒絕" not in m7 and "只擋登錄表為空" in m7
    assert "永不放棄" in m7 and "ledger_stalled" in m7 and "scheduler reset" in m7
    # 契約公式與程式一致：典型價夾限價（D07）
    assert broker._typical({"high": 12.0, "low": 9.0, "close": 9.0}) == 10.0
