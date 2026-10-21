"""opus 同意閘門（M6a）：需要同意的等級（預設 strong：FULL_A、RED_TEAM）先放進 approvals 表，使用者同意後才呼叫模型。

狀態：pending（待同意）→ approved（已同意，等 run-approved 執行）→ done（該檔本 run 已無未完成、也無失敗工作；有失敗時留 approved、note 記原因）；
     deferred（延後：留在佇列，不計入一鍵全同意）；rejected（拒絕：該檔記 NO_ACTION/MISSING_DATA，下次觸發再排）；
     superseded（作廢：同一檔有新的觸發，舊 run 未決、已同意未執行或鏈已停（被閘工作 failed／skipped）的列；或逾 EXPIRE_TRADING_DAYS 個交易日未決，
     設計決策 D42）；放棄（trader jobs abandon）記 rejected、decided_via='abandon'。
同意單位是一檔：同一 run 同一檔只有一列，同意後之後才產生的 strong 工作（RED_TEAM）也直接放行。
等待期間（pending／deferred／approved）該檔暫停加碼（sizing.add_blocked），賣出照常。
被拒絕／作廢／放棄的等待工作改 skipped（error 以「使用者拒絕」「同意已作廢」「使用者放棄」開頭），jobs retry 不會重設。
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
CLOSED = ("rejected", "superseded")
EXPIRE_TRADING_DAYS = 5   # 設計決策 D42：未決同意列放滿 5 個交易日自動作廢

# 用量估算預設值（沒有實際紀錄時）：FULL_A 實測 OTIS／LOW 約 0.65–1.2M 輸入、5～6 分鐘
DEFAULT_EST = {"FULL_A": {"input_tokens": 900_000, "output_tokens": 30_000, "latency_s": 330.0}}
MEASURED_BACKENDS = ("claude_code", "openai_compat")   # 回報實際 token 的後端
EST_JOB = "FULL_A"   # 每檔估算只算 FULL_A；RED_TEAM 只在 BUY 時才跑，不計入
MAX_FULL_A_RUNS = 3  # 每檔 FULL_A 最多跑 3 次：首次＋框架退回 rule_ids（RM2-9）＋D26 業主盈餘各一次（A3-M5B-03 選 A）


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
    並把同一檔其他 run 未決或已同意未執行的列作廢（新觸發取代舊的；舊的等待工作改 skipped，D42）。
    本 run 該檔的列已拒絕或作廢 → 本工作改 skipped 並回傳 False（不留 awaiting_approval，M5_D2-02）。"""
    row = _row(conn, spec.run_id, spec.ticker)
    if row is not None:
        if row["status"] in CLOSED:
            why = ("使用者拒絕以強模型分析（本 run 該檔同意列已拒絕）" if row["status"] == "rejected"
                   else "同意已作廢（本 run 該檔同意列已作廢）")
            conn.execute("UPDATE jobs SET status='skipped', error=?, finished_at=? WHERE job_id=?",
                         (why, db.now_iso(), spec.job_id))
            conn.commit()
            return False
        return row["status"] in PASS
    for old in conn.execute(
            "SELECT approval_id, run_id, ticker, job_id, status FROM approvals WHERE ticker=? AND run_id<>? "
            "AND status IN ('pending','deferred','approved')", (spec.ticker, spec.run_id)).fetchall():
        if old["status"] == "approved" and _started(conn, old):
            continue   # 已同意且舊鏈仍在進行：不打斷，讓舊鏈跑完，由 close_finished 結案
        _close(conn, old["approval_id"], "superseded", via="system", note=f"被 {spec.run_id} 的新觸發取代")
        _skip_waiting(conn, old["run_id"], spec.ticker, f"同意已作廢：被 {spec.run_id} 取代",
                      ("awaiting_approval", "pending") if old["status"] == "approved" else ("awaiting_approval",))
    db.insert(conn, "approvals", {
        "ticker": spec.ticker, "run_id": spec.run_id, "asof": spec.asof, "job_id": spec.job_id,
        "job_type": spec.job_type, "tier": tier, "holding": 1 if (spec.params or {}).get("holding") else 0,
        "reason": _reason(spec), "status": "pending", "created_at": db.now_iso()})
    return False


def _started(conn, old) -> bool:
    """D42「已同意但還沒執行」的反面＝舊鏈仍在進行：該 run 該檔有 running 工作，或被閘工作（approvals.job_id）
    已跑過（done／conservative_fallback）且該檔還有未完成工作。被閘工作 failed／skipped、或鏈已無未完成工作
    （只剩失敗）都不算：照常作廢，否則新 run 結案後舊 approved 列會永久擋加碼（M5-RS2-01）。"""
    if conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND ticker=? AND status='running'",
                    (old["run_id"], old["ticker"])).fetchone() is not None:
        return True
    gated = conn.execute("SELECT status FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone()
    if gated is None or gated["status"] not in ("done", "conservative_fallback"):
        return False
    return conn.execute(
        f"SELECT 1 FROM jobs WHERE run_id=? AND ticker=? AND status IN ({','.join('?' * len(NONTERMINAL_JOB))})",
        (old["run_id"], old["ticker"], *NONTERMINAL_JOB)).fetchone() is not None


def _skip_waiting(conn, run_id: str, ticker: str, why: str,
                  statuses: tuple[str, ...] = ("awaiting_approval",)) -> None:
    conn.execute(f"UPDATE jobs SET status='skipped', error=?, finished_at=? WHERE run_id=? AND ticker=? "
                 f"AND status IN ({','.join('?' * len(statuses))})", (why, db.now_iso(), run_id, ticker, *statuses))
    conn.commit()


def expire_stale(conn, asof: str, days: int = EXPIRE_TRADING_DAYS, cal: Any = None) -> list[int]:
    """設計決策 D42：未決（pending／deferred）同意列，資料日之後到 asof（含）已滿 days 個交易日 → superseded，
    等待中的工作改 skipped（下次觸發以新資料日重建）。daily 每天呼叫。回傳作廢的同意編號。"""
    from datetime import date, timedelta
    from ..calendar import TradingCalendar
    cal = cal or TradingCalendar()
    out = []
    for r in conn.execute("SELECT approval_id, run_id, ticker, asof FROM approvals WHERE status IN ('pending','deferred')"
                          " ORDER BY approval_id").fetchall():
        start = (date.fromisoformat(str(r["asof"])[:10]) + timedelta(days=1)).isoformat()
        if start > asof or len(cal.trading_days(start, asof)) < days:
            continue
        _close(conn, r["approval_id"], "superseded", via="system",
               note=f"逾 {days} 個交易日未決（資料日 {r['asof']}），自動作廢")
        _skip_waiting(conn, r["run_id"], r["ticker"], f"同意已作廢：逾 {days} 個交易日未決（資料日 {r['asof']}）")
        out.append(int(r["approval_id"]))
    return out


def abandon(conn, run_id: str, ticker: str | None = None, note: str | None = None) -> list[int]:
    """trader jobs abandon（M5_D4-01）：該 run（或該檔）pending／deferred／approved 的列 → rejected（decided_via='abandon'）。
    不寫決策；等待工作由呼叫端（engine.abandon）處理。回傳編號。"""
    sql, args = "SELECT approval_id FROM approvals WHERE run_id=? AND status IN ('pending','deferred','approved')", [run_id]
    if ticker:
        sql += " AND ticker=?"
        args.append(ticker)
    ids = [int(r[0]) for r in conn.execute(sql, args).fetchall()]
    for i in ids:
        _close(conn, i, "rejected", via="abandon", note=note)
    return ids


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
    """每檔預估用量：FULL_A 過去成功工作的平均（每個工作加總所有嘗試）；沒有紀錄用預設值。
    有任一呼叫來自不回報實際用量的後端（claude_code／openai_compat 以外，例如 hark_queue、fake）的工作不計：那些 token 不是實測（M5-D5-05）。"""
    r = conn.execute(
        "SELECT COUNT(*) AS n, AVG(input_tokens) AS i, AVG(output_tokens) AS o, AVG(latency_s) AS s FROM jobs j "
        "WHERE job_type=? AND status='done' AND tier IN ('strong') AND input_tokens > 0 "
        "AND NOT EXISTS (SELECT 1 FROM llm_calls c WHERE c.job_id=j.job_id "
        "AND COALESCE(c.backend, '') NOT IN (?, ?))", (EST_JOB, *MEASURED_BACKENDS)).fetchone()
    if r["n"]:
        backends = [b[0] for b in conn.execute(   # 樣本來自哪些後端（同意頁顯示依據，M8-D5-07）
            "SELECT DISTINCT c.backend FROM llm_calls c JOIN jobs j ON j.job_id=c.job_id WHERE j.job_type=? "
            "AND j.status='done' AND j.tier='strong' AND j.input_tokens > 0 AND c.backend IS NOT NULL ORDER BY 1",
            (EST_JOB,))]
        return {"input_tokens": int(r["i"]), "output_tokens": int(r["o"]), "latency_s": float(r["s"] or 0.0),
                "samples": int(r["n"]), "basis": "實際平均", "backends": [b for b in backends if b in MEASURED_BACKENDS]}
    return {**DEFAULT_EST[EST_JOB], "samples": 0, "basis": "預設值"}


def summary(conn) -> dict[str, Any]:
    """待同意摘要（儀表板按鈕與 CLI 共用）：pending 檔數與總估算；deferred 另計、不計入一鍵全同意。"""
    est = estimate_per_ticker(conn)
    pend = list_items(conn, ("pending",))
    n = len(pend)
    return {"n_pending": n, "n_deferred": len(list_items(conn, ("deferred",))),
            "n_approved_waiting": len(list_items(conn, ("approved",))),
            "n_runnable": len(runnable(conn)),   # 「執行已同意」按鈕：只剩失敗工作的已同意列不算（M8-D5-04）
            "tickers": [x["ticker"] for x in pend],
            "ids": [x["approval_id"] for x in pend],   # 一鍵同意要送出畫面上這批編號（D54：伺服器不收 all）
            "est_input_tokens": est["input_tokens"] * n, "est_output_tokens": est["output_tokens"] * n,
            "est_minutes": round(est["latency_s"] * n / 60.0, 1), "per_ticker": est,
            # 含可能退回的上限（A3-M5B-03 選 A：不加退回上限，估算註明）
            "est_input_tokens_max": est["input_tokens"] * n * MAX_FULL_A_RUNS,
            "est_output_tokens_max": est["output_tokens"] * n * MAX_FULL_A_RUNS,
            "est_minutes_max": round(est["latency_s"] * n * MAX_FULL_A_RUNS / 60.0, 1),
            "note": f"只估 FULL_A；含可能退回時上限約 {MAX_FULL_A_RUNS} 倍（框架退回 FULL_A 最多 {MAX_FULL_A_RUNS - 1} 次）；"
                    "擬 BUY 時另有 RED_TEAM。程式讀不到訂閱額度，同意前請自行確認額度"}


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


DEFAULT_TZ = "America/New_York"


def config_timezone(config_arg: Any = None) -> str | None:
    """設定檔 schedule.timezone；讀不到回 None（呼叫端退回 DEFAULT_TZ）。"""
    try:
        from ..config import load_config
        sched = getattr(load_config(config_arg), "schedule", None) or {}
        tz = sched.get("timezone") if isinstance(sched, dict) else None
        return str(tz) if tz else None
    except Exception:  # noqa: BLE001 - 時區只是日誌欄位，拿不到用預設
        return None


def _screen_decision(conn, run_id: str, ticker: str) -> str | None:
    """該 run 該檔工作 params.screen_context.screen_decision（第一個有值者）。"""
    for r in conn.execute("SELECT params FROM jobs WHERE run_id=? AND ticker=? ORDER BY rowid", (run_id, ticker)):
        try:
            sc = (json.loads(r[0] or "{}") or {}).get("screen_context") or {}
        except ValueError:
            continue
        if isinstance(sc, dict) and sc.get("screen_decision"):
            return sc["screen_decision"]
    return None


def reject(conn, paths, ids: Iterable[int], via: str = "cli", note: str | None = None,
           time_zone: str | None = None) -> list[str]:
    """拒絕：等待中的工作改 skipped，該檔記 NO_ACTION/MISSING_DATA（登錄表非 HOLDING 時改 MISSING_DATA，
    下次觸發會重排；持股不清空登錄表，見 writeback.write_stop_result）。回傳決策編號。
    停止日誌補 screen_decision（該 run 該檔工作 screen_context）與 time_zone（呼叫端傳 config schedule.timezone，
    缺則 DEFAULT_TZ，M8-D2-04）。"""
    from ..analysis.writeback import write_stop_result
    tz = time_zone or DEFAULT_TZ
    dids = []
    for i in ids:
        a = _get(conn, i)
        if a["status"] not in UNDECIDED:
            raise ApprovalError(f"編號 {i}（{a['ticker']}）狀態為 {a['status']}，不能拒絕")
        why = "使用者拒絕以強模型分析" + (f"：{note}" if note else "")
        # 先寫結論再結案（A3-M5E-05）：寫回失敗時同意列仍未決、可再拒絕；冪等鍵 reject:{編號}，重拒不重寫
        dids.append(write_stop_result(conn, paths, a["ticker"], a["asof"], a["run_id"], {
            "ticker": a["ticker"], "decision_date": a["asof"], "stopped": True, "stopped_at_gate": "STEP0",
            "reason_code": "MISSING_DATA"}, [why],
            log_fields={"screen_decision": _screen_decision(conn, a["run_id"], a["ticker"]), "time_zone": tz},
            job_id=f"reject:{i}"))
        _close(conn, i, "rejected", via, note)
        _skip_waiting(conn, a["run_id"], a["ticker"], why)
    return dids


# 已同意列「還需要 run-approved」：該檔本 run 沒有 failed 工作，或仍有未完成工作（含該 run 沒有任何工作的列）。
# 只剩失敗工作的列（close_finished 留 approved，M8-D5-04）不算：重跑舊 run 只會再記失敗，等使用者 retry／abandon。
# 該 run 預算暫停（runs.status='paused_budget'）也不算：D44 不自動續跑，只由使用者「續跑」／trader jobs resume（X47）。
# release 第一輪、排程的 run-approved、儀表板「執行已同意」按鈕與 /api/approvals/run 共用這個條件。
PAUSED_SQL = "EXISTS (SELECT 1 FROM runs r WHERE r.run_id=a.run_id AND r.status='paused_budget')"
RUNNABLE_SQL = (f"a.status='approved' AND NOT {PAUSED_SQL} AND (NOT EXISTS (SELECT 1 FROM jobs j WHERE j.run_id=a.run_id "
                "AND j.ticker=a.ticker AND j.status='failed') OR EXISTS (SELECT 1 FROM jobs j WHERE j.run_id=a.run_id "
                "AND j.ticker=a.ticker AND j.status IN ('pending','running','awaiting_approval')))")


def runnable(conn) -> list[dict]:
    """需要 run-approved 的已同意列（RUNNABLE_SQL）。"""
    return [dict(r) for r in conn.execute(f"SELECT a.* FROM approvals a WHERE {RUNNABLE_SQL} ORDER BY a.approval_id")]


def release(conn, run_id: str | None = None, only_new: bool = False) -> list[str]:
    """已同意的列：把該檔等待中的工作改回 pending。回傳需要執行的 run_id（依建立先後；只算 RUNNABLE_SQL 的列）。
    only_new：只回傳這次真的有工作從 awaiting_approval 改回 pending 的 run（run-approved 執行中途新同意的，M8-D2-03）。
    預算暫停的 run 一律不回傳（工作照樣改回 pending，等使用者續跑時一起跑，D44／X47）。"""
    sql = f"SELECT a.run_id, a.ticker, ({RUNNABLE_SQL}) AS ok, ({PAUSED_SQL}) AS paused FROM approvals a WHERE a.status='approved'"
    args: tuple = ()
    if run_id:
        sql += " AND a.run_id=?"
        args = (run_id,)
    runs: list[str] = []
    for r in conn.execute(sql + " ORDER BY a.approval_id", args).fetchall():
        n = conn.execute("UPDATE jobs SET status='pending' WHERE run_id=? AND ticker=? AND status='awaiting_approval'",
                         (r["run_id"], r["ticker"])).rowcount
        if not r["paused"] and (n or (r["ok"] and not only_new)) and r["run_id"] not in runs:
            runs.append(r["run_id"])
    conn.commit()
    return runs


FAILED_NOTE = "有失敗工作，見異常頁（trader jobs retry 重跑或 trader jobs abandon 放棄）"


REPLACED_NOTE = "失敗已由替代工作收尾（例如持股的替代 HOLD_REVIEW）"


def replaced_ok(conn, job_id: str, ok: tuple[str, ...] = ("done", "conservative_fallback", "skipped"),
                _seen: frozenset = frozenset()) -> bool:
    """failed 工作已由 on_fail 建的替代工作（result.replaced_by）收尾：每個替代工作都存在且為 ok 狀態，
    或本身 failed 但又被替代收尾（A3-M5B-02）。沒有替代工作 → False。"""
    row = conn.execute("SELECT result FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    try:
        res = json.loads(row["result"]) if row is not None and row["result"] else {}
    except ValueError:
        res = {}
    ids = res.get("replaced_by") if isinstance(res, dict) else None
    if not ids or job_id in _seen:
        return False
    for i in ids:
        st = conn.execute("SELECT status FROM jobs WHERE job_id=?", (i,)).fetchone()
        if st is None:
            return False
        if st["status"] == "failed":
            if not replaced_ok(conn, i, ok, _seen | {job_id}):
                return False
        elif st["status"] not in ok:
            return False
    return True


def close_finished(conn, run_id: str) -> None:
    """run 結束時：已同意且該檔本 run 已無未完成工作 → done。該檔有 failed 工作時不設 done（留在待同意頁、
    note 記原因；重跑完成或放棄後才結案，M8-D5-04）；failed 工作已由替代工作收尾者不算（replaced_ok，A3-M5B-02：
    被替代的工作 retry 不會重設，留 approved 會永久擋加碼）。"""
    for r in conn.execute("SELECT approval_id, ticker FROM approvals WHERE run_id=? AND status='approved'",
                          (run_id,)).fetchall():
        left = conn.execute(
            f"SELECT 1 FROM jobs WHERE run_id=? AND ticker=? AND status IN ({','.join('?' * len(NONTERMINAL_JOB))})",
            (run_id, r["ticker"], *NONTERMINAL_JOB)).fetchone()
        if left is not None:
            continue
        failed = [f[0] for f in conn.execute("SELECT job_id FROM jobs WHERE run_id=? AND ticker=? AND status='failed'",
                                             (run_id, r["ticker"])).fetchall()]
        if any(not replaced_ok(conn, j) for j in failed):
            conn.execute("UPDATE approvals SET note=? WHERE approval_id=?", (FAILED_NOTE, r["approval_id"]))
        else:   # 重跑成功：清舊失敗註記（M8-R5-03）；失敗皆由替代工作收尾：改記 REPLACED_NOTE
            conn.execute("UPDATE approvals SET status='done', note=CASE WHEN note=? OR note IS NULL THEN ? ELSE note END "
                         "WHERE approval_id=?", (FAILED_NOTE, REPLACED_NOTE if failed else None, r["approval_id"]))
    conn.commit()


def add_block_reason(conn, ticker: str) -> str | None:
    """該檔有未結的同意列（待同意、延後、已同意未執行完）→ 暫停加碼的理由。"""
    r = conn.execute("SELECT status FROM approvals WHERE ticker=? AND status IN ('pending','deferred','approved') "
                     "ORDER BY approval_id DESC LIMIT 1", (ticker,)).fetchone()
    if r is None:
        return None
    return {"pending": "等待同意強模型完整分析", "deferred": "強模型完整分析已延後",
            "approved": "強模型完整分析已同意、尚未完成"}[r["status"]]
