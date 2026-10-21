"""RM3-1（M3-R8-01）：8-K 預告的公司行動被拒收／人工結案後占住去重鍵 (代號, 類型, 生效日)，
生效日 Alpaca 事件不得被當 duplicate 靜默丟棄；但真正入帳過（8-K 自動入帳、人工已調帳）者不重複調整。"""
import json
from pathlib import Path

from trader.ledger import book
from trader.ledger import corp_actions as ca

from test_fix_P28 import env, hold, ev, res, NAME, SPLIT, run_post, open_anoms, ASOF  # noqa: F401

REPO = Path(__file__).resolve().parents[2]


def _pos(conn):
    return [tuple(x) for x in conn.execute("SELECT ticker, qty FROM positions ORDER BY ticker")]


def _row(conn):
    r = conn.execute("SELECT ca_id, status, source, new_ticker, data FROM corporate_actions").fetchall()
    assert len(r) == 1
    return r[0]


def _8k_rename(env):
    r8 = res(action_type="代號變更", cash_per_share=None, new_ticker="KOX", needs_human=True, fractional_handling="不適用")
    run_post(env, r8, ev(source="8k", date="2026-10-14", seq="acc1"))
    row = _row(env.conn)
    assert row["status"] == "pending_human" and row["source"] == "8k"
    return row["ca_id"]


def _alpaca_rename(env):
    r = res(action_type="代號變更", cash_per_share=None, new_ticker="KOX", fractional_handling="不適用")
    run_post(env, r, ev(source="alpaca", alp=NAME))


def test_8k_rename_resolved_then_alpaca_applies(env):
    hold(env.conn, "KO", 10, 50.0)
    ca_id = _8k_rename(env)
    book.adjust(env.conn, date="2026-10-15", unpause="KO", resolve_corp_action=ca_id, note="8-K 預告，等 Alpaca")
    _alpaca_rename(env)
    assert _pos(env.conn) == [("KOX", 10)]
    row = _row(env.conn)
    assert (row["ca_id"], row["status"], row["source"], row["new_ticker"]) == (ca_id, "applied", "alpaca", "KOX")
    sup = json.loads(row["data"])["superseded"]
    assert sup["source"] == "8k" and sup["status"] == "applied" and sup["data"]["manual"]
    assert open_anoms(env.conn) == []


def test_8k_rename_unresolved_then_alpaca_applies_and_closes_8k_anomaly(env):
    hold(env.conn, "KO", 10, 50.0)
    _8k_rename(env)
    assert open_anoms(env.conn)          # 8-K 轉人工的暫停
    _alpaca_rename(env)
    assert _pos(env.conn) == [("KOX", 10)]
    row = _row(env.conn)
    assert row["status"] == "applied" and row["source"] == "alpaca"
    assert json.loads(row["data"])["superseded"]["status"] == "pending_human"
    assert open_anoms(env.conn) == []    # 被取代列自己的異常結案；不留暫停


def test_8k_split_announcement_rejected_then_alpaca_split_applies(env):
    hold(env.conn, "KO", 10, 50.0)
    r8 = res(action_type="拆股", cash_per_share=None, ratio_new_per_old=2.0, fractional_handling="不適用")
    run_post(env, r8, ev(source="8k", date="2026-10-14", seq="acc2", pb=60.0, pa=60.5))
    ca_id = _row(env.conn)["ca_id"]
    assert _row(env.conn)["status"] == "anomaly"
    book.adjust(env.conn, date="2026-10-15", unpause="KO", resolve_corp_action=ca_id, note="預告，等生效")
    r = res(action_type="拆股", cash_per_share=None, ratio_new_per_old=2.0, fractional_handling="不適用")
    run_post(env, r, ev(source="alpaca", alp=SPLIT, pb=60.0, pa=30.0))
    assert _pos(env.conn) == [("KO", 20)]
    assert _row(env.conn)["source"] == "alpaca"


def test_manual_qty_adjust_after_8k_row_goes_conflict_not_double(env):
    """人工已依 8-K 改過股數 → Alpaca 拆股不自動再拆（避免 40 股），也不靜默：新異常轉人工。"""
    hold(env.conn, "KO", 10, 50.0)
    r8 = res(action_type="拆股", cash_per_share=None, ratio_new_per_old=2.0, fractional_handling="不適用")
    run_post(env, r8, ev(source="8k", date="2026-10-14", seq="acc2", pb=60.0, pa=60.5))
    ca_id = _row(env.conn)["ca_id"]
    book.adjust(env.conn, date="2026-10-16", ticker="KO", set_qty=20, set_avg_cost=25.0, unpause="KO",
                resolve_corp_action=ca_id, note="人工拆股")
    r = res(action_type="拆股", cash_per_share=None, ratio_new_per_old=2.0, fractional_handling="不適用")
    run_post(env, r, ev(source="alpaca", alp=SPLIT, pb=60.0, pa=30.0))
    assert _pos(env.conn) == [("KO", 20)]
    assert _row(env.conn)["source"] == "8k"
    assert open_anoms(env.conn)          # conflict → reject → 新異常


def test_8k_auto_applied_same_action_still_duplicate(env):
    """P28R 行為不變：同日 8-K 已自動入帳 → Alpaca 同一行動 duplicate。"""
    hold(env.conn, "KO", 100, 60.0)
    r = res(action_type="拆股", ratio_new_per_old=2.0, cash_per_share=None, pay_date=None)
    assert ca.apply(env.conn, env.paths, r, ev(source="8k", pb=60.0, pa=30.0), ASOF)["status"] == "applied"
    assert ca.apply(env.conn, env.paths, r, ev(alp=SPLIT, pb=60.0, pa=30.0, seq="s1"), ASOF)["status"] == "duplicate"
    assert book.positions(env.conn)["KO"]["qty"] == 200


def test_prompt_8k_future_action_is_no_action():
    p = (REPO / "routing" / "prompts" / "CORP_ACTION.md").read_text(encoding="utf-8")
    assert "生效日晚於事件日（預告" in p and "生效日由 Alpaca 公司行動入帳" in p
