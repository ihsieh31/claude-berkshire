"""opus 同意閘門（M6a）：需要同意的等級（預設 strong：FULL_A、RED_TEAM）先放進 approvals 表，使用者同意後才呼叫模型。

狀態：pending（待同意）→ approved（已同意，等 run-approved 執行）→ done（該檔本 run 已無未完成工作）；
     deferred（延後：留在佇列，不計入一鍵全同意）；rejected（拒絕：該檔記 NO_ACTION/MISSING_DATA，下次觸發再排）；
     superseded（同一檔有新的觸發，舊的作廢）。
同意單位是一檔：同一 run 同一檔只有一列，同意後之後才產生的 strong 工作（RED_TEAM）也直接放行。
等待期間（pending／deferred／approved）該檔暫停加碼（sizing.add_blocked），賣出照常。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from .. import db
from ..models import JobSpec

OPEN = ("pending", "deferred", "approved")
UNDECIDED = ("pending", "deferred")
PASS = ("approved", "done")
NONTERMINAL_JOB = ("pending", "running", "awaiting_approval")

# 用量估算預設值（沒有實際紀錄時）：FULL_A 實測 OTIS／LOW 約 0.65–1.2M 輸入、5～6 分鐘
DEFAULT_EST = {"FULL_A": {"input_tokens": 900_000, "output_tokens": 30_000, "latency_s": 330.0}}
EST_JOB = "FULL_A"   # 每檔估算只算 FULL_A；RED_TEAM 只在 BUY 時才跑，不計入


class ApprovalError(Exception):
    """同意操作不合法（編號不存在、狀態不允許）。"""


def required(config: Any, tier: str) -> bool:
    tiers = getattr(getattr(config, "approval", None), "require_for_tiers", None)
    return tier in (tiers if tiers is not None else ["strong"])


def _reason(spec: JobSpec) -> str:
    p = spec.params or {}
    parts = []
    if p.get("holding"):
        parts.append("持股複查")
    for r in p.get("reasons") or []:
        parts.append(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False))
    tr = p.get("thesis_result") or {}
    if isinstance(tr, dict) and tr.get("verdict"):
        parts.append(f"論點檢查：{tr['verdict']}")
    if not parts:
        sc = p.get("screen_context") or {}
        parts.append(sc.get("note") or "初選短名單")
    return "；".join(parts)[:500]


def _row(conn, run_id: str, ticker: str):
    return conn.execute("SELECT * FROM approvals WHERE run_id=? AND ticker=?", (run_id, ticker)).fetchone()


def gate(conn, spec: JobSpec, tier: str) -> bool:
    """True＝可執行（本 run 該檔已同意）。False＝須等待：建立 pending 列（已有列則沿用），
    並把同一檔其他 run 尚未決定的列作廢（新觸發取代舊的；舊的等待工作改 skipped）。"""
    row = _row(conn, spec.run_id, spec.ticker)
    if row is not None:
        return row["status"] in PASS
    for old in conn.execute(
            "SELECT approval_id, run_id FROM approvals WHERE ticker=? AND status IN ('pending','deferred')",
            (spec.ticker,)).fetchall():
        _close(conn, old["approval_id"], "superseded", via="system", note=f"被 {spec.run_id} 的新觸發取代")
        _skip_waiting(conn, old["run_id"], spec.ticker, f"同意已作廢：被 {spec.run_id} 取代")
    db.insert(conn, "approvals", {
        "ticker": spec.ticker, "run_id": spec.run_id, "asof": spec.asof, "job_id": spec.job_id,
        "job_type": spec.job_type, "tier": tier, "holding": 1 if (spec.params or {}).get("holding") else 0,
        "reason": _reason(spec), "status": "pending", "created_at": db.now_iso()})
    return False


def _skip_waiting(conn, run_id: str, ticker: str, why: str) -> None:
    conn.execute("UPDATE jobs SET status='skipped', error=?, finished_at=? "
                 "WHERE run_id=? AND ticker=? AND status='awaiting_approval'", (why, db.now_iso(), run_id, ticker))
    conn.commit()


def _close(conn, approval_id: int, status: str, via: str, note: str | None = None) -> None:
    conn.execute("UPDATE approvals SET status=?, decided_at=?, decided_via=?, note=COALESCE(?, note) "
                 "WHERE approval_id=?", (status, db.now_iso(), via, note, approval_id))
    conn.commit()


# ---------------------------------------------------------------- 查詢

def list_items(conn, statuses: Iterable[str] = OPEN) -> list[dict]:
    st = tuple(statuses)
    rows = conn.execute(f"SELECT * FROM approvals WHERE status IN ({','.join('?' * len(st))}) "
                        "ORDER BY created_at, approval_id", st).fetchall()
    return [dict(r) for r in rows]


def estimate_per_ticker(conn) -> dict[str, Any]:
    """每檔預估用量：FULL_A 過去成功工作的平均（每個工作加總所有嘗試）；沒有紀錄用預設值。"""
    r = conn.execute(
        "SELECT COUNT(*) AS n, AVG(input_tokens) AS i, AVG(output_tokens) AS o, AVG(latency_s) AS s FROM jobs "
        "WHERE job_type=? AND status='done' AND tier IN ('strong') AND input_tokens > 0", (EST_JOB,)).fetchone()
    if r["n"]:
        return {"input_tokens": int(r["i"]), "output_tokens": int(r["o"]), "latency_s": float(r["s"] or 0.0),
                "samples": int(r["n"]), "basis": "實際平均"}
    return {**DEFAULT_EST[EST_JOB], "samples": 0, "basis": "預設值"}


def summary(conn) -> dict[str, Any]:
    """待同意摘要（儀表板按鈕與 CLI 共用）：pending 檔數與總估算；deferred 另計、不計入一鍵全同意。"""
    est = estimate_per_ticker(conn)
    pend = list_items(conn, ("pending",))
    n = len(pend)
    return {"n_pending": n, "n_deferred": len(list_items(conn, ("deferred",))),
            "n_approved_waiting": len(list_items(conn, ("approved",))),
            "tickers": [x["ticker"] for x in pend],
            "est_input_tokens": est["input_tokens"] * n, "est_output_tokens": est["output_tokens"] * n,
            "est_minutes": round(est["latency_s"] * n / 60.0, 1), "per_ticker": est,
            "note": "只估 FULL_A；擬 BUY 時另有 RED_TEAM。程式讀不到訂閱額度，同意前請自行確認額度"}


def label(s: dict) -> str:
    return f"{s['n_pending']} 檔 · 約 {s['est_input_tokens'] / 1e6:.1f}M token · 約 {s['est_minutes']:.0f} 分鐘"


# ---------------------------------------------------------------- 操作

def _get(conn, approval_id: int) -> dict:
    r = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if r is None:
        raise ApprovalError(f"沒有同意編號 {approval_id}")
    return dict(r)


def approve(conn, ids: Iterable[int] | None = None, via: str = "cli") -> list[int]:
    """同意指定編號（pending 或 deferred）；ids 為 None ＝ 全部 pending（不含 deferred）。回傳實際同意的編號。"""
    if ids is None:
        targets = [x["approval_id"] for x in list_items(conn, ("pending",))]
    else:
        targets = []
        for i in ids:
            a = _get(conn, i)
            if a["status"] not in UNDECIDED:
                raise ApprovalError(f"編號 {i}（{a['ticker']}）狀態為 {a['status']}，不能同意")
            targets.append(i)
    for i in targets:
        _close(conn, i, "approved", via)
    return targets


def defer(conn, ids: Iterable[int], via: str = "cli") -> list[int]:
    out = []
    for i in ids:
        a = _get(conn, i)
        if a["status"] != "pending":
            raise ApprovalError(f"編號 {i}（{a['ticker']}）狀態為 {a['status']}，只能延後 pending")
        _close(conn, i, "deferred", via)
        out.append(i)
    return out


def reject(conn, paths, ids: Iterable[int], via: str = "cli", note: str | None = None) -> list[str]:
    """拒絕：等待中的工作改 skipped，該檔記 NO_ACTION/MISSING_DATA（登錄表非 HOLDING 時改 MISSING_DATA，
    下次觸發會重排；持股不清空登錄表，見 writeback.write_stop_result）。回傳決策編號。"""
    from ..analysis.writeback import write_stop_result
    dids = []
    for i in ids:
        a = _get(conn, i)
        if a["status"] not in UNDECIDED:
            raise ApprovalError(f"編號 {i}（{a['ticker']}）狀態為 {a['status']}，不能拒絕")
        why = "使用者拒絕以強模型分析" + (f"：{note}" if note else "")
        _close(conn, i, "rejected", via, note)
        _skip_waiting(conn, a["run_id"], a["ticker"], why)
        dids.append(write_stop_result(conn, paths, a["ticker"], a["asof"], a["run_id"], {
            "ticker": a["ticker"], "decision_date": a["asof"], "stopped": True, "stopped_at_gate": "STEP0",
            "reason_code": "MISSING_DATA"}, [why]))
    return dids


def release(conn, run_id: str | None = None) -> list[str]:
    """已同意的列：把該檔等待中的工作改回 pending。回傳需要執行的 run_id（依建立先後）。"""
    sql = "SELECT run_id, ticker FROM approvals WHERE status='approved'"
    args: tuple = ()
    if run_id:
        sql += " AND run_id=?"
        args = (run_id,)
    runs: list[str] = []
    for r in conn.execute(sql + " ORDER BY approval_id", args).fetchall():
        conn.execute("UPDATE jobs SET status='pending' WHERE run_id=? AND ticker=? AND status='awaiting_approval'",
                     (r["run_id"], r["ticker"]))
        if r["run_id"] not in runs:
            runs.append(r["run_id"])
    conn.commit()
    return runs


def close_finished(conn, run_id: str) -> None:
    """run 結束時：已同意且該檔本 run 已無未完成工作 → done。"""
    for r in conn.execute("SELECT approval_id, ticker FROM approvals WHERE run_id=? AND status='approved'",
                          (run_id,)).fetchall():
        left = conn.execute(
            f"SELECT 1 FROM jobs WHERE run_id=? AND ticker=? AND status IN ({','.join('?' * len(NONTERMINAL_JOB))})",
            (run_id, r["ticker"], *NONTERMINAL_JOB)).fetchone()
        if left is None:
            conn.execute("UPDATE approvals SET status='done' WHERE approval_id=?", (r["approval_id"],))
    conn.commit()


def add_block_reason(conn, ticker: str) -> str | None:
    """該檔有未結的同意列（待同意、延後、已同意未執行完）→ 暫停加碼的理由。"""
    r = conn.execute("SELECT status FROM approvals WHERE ticker=? AND status IN ('pending','deferred','approved') "
                     "ORDER BY approval_id DESC LIMIT 1", (ticker,)).fetchone()
    if r is None:
        return None
    return {"pending": "等待同意強模型完整分析", "deferred": "強模型完整分析已延後",
            "approved": "強模型完整分析已同意、尚未完成"}[r["status"]]
