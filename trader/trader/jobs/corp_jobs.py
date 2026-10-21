"""公司行動工作處理器（CORP_ACTION）：提示值取自帳本，覆核與入帳／拒收由 trader.ledger.corp_actions 負責。

價格隔離不適用於本工作（CONTRACTS_M5 §B）。
覆核失敗或 LLM 輸出重試後仍不合格：不入帳、寫異常並暫停該檔（見 ledger.corp_actions.reject／on_invalid）。
"""
from __future__ import annotations

import json
from typing import Any

from ..analysis.dossier_prep import exhibit_any as earnings_exhibit_any
from ..ledger import corp_actions as ca
from ..models import JobSpec
from .handlers import Handler, HandlerCtx
from .sandbox import ticker_dir

CORP_8K_CHARS = 20_000


def _fmt(x: Any) -> str:
    return "缺" if x is None else str(x)


class CorpActionHandler(Handler):
    job_type = "CORP_ACTION"

    @staticmethod
    def _event(job: JobSpec) -> dict | None:
        ev = job.params.get("event")
        return dict(ev) if isinstance(ev, dict) and ev.get("ticker") else None

    def pre_script(self, job: JobSpec, ctx: HandlerCtx) -> None:
        """8-K 事件只有項目碼：抓申報全文與 EX-99 附件附到事件原文，模型才有東西可讀。抓不到照實標示。"""
        ev = self._event(job)
        if not ev or ev.get("source") != "8k" or not ev.get("url") or ev.get("filing_text_added"):
            return
        if ctx.sec is None:
            ev["text"] = ev.get("text", "") + "\n（8-K 原文：缺，未設定 SEC 客戶端）"
        else:
            try:
                body = ctx.sec.filing_text(ev["url"])
                body += earnings_exhibit_any(ctx.sec, ev["url"])
                cut = "" if len(body) <= CORP_8K_CHARS else f"\n（原文已截斷，原長 {len(body)} 字，只附前 {CORP_8K_CHARS} 字）"
                ev["text"] = ev.get("text", "") + "\n\n8-K 原文：\n" + body[:CORP_8K_CHARS] + cut   # M5-D5-06
            except Exception as e:  # noqa: BLE001
                ev["text"] = ev.get("text", "") + f"\n（8-K 原文：缺，抓取失敗 {type(e).__name__}）"
        ev["filing_text_added"] = True
        job.params["event"] = ev

    def prompt_values(self, job: JobSpec, ctx: HandlerCtx, backend: str) -> dict[str, str]:
        ev = self._event(job) or {}
        T = ca._nt(ev.get("ticker") or job.ticker)
        pos = ca.position_of(ctx.conn, T)
        orders = ca.open_orders_of(ctx.conn, T)
        return {
            # 本工作不經 _vals、沙箱也沒有 pkg/：不給規則檔路徑，避免 {pkg} 落成真實專案根（M3-D10-04）
            "pkg": "（本工作不提供規則檔）",
            "event_text": ev.get("text") or "（無事件原文）",
            "shares": str(pos["qty"]) if pos else "0",
            "cost": f"{pos['avg_cost']:.4f}" if pos else "無持股",
            "open_orders": "；".join(
                f"{o['side']} {o['qty']} 股 限價 {_fmt(o['limit_price'])}" for o in orders) or "無",
            "price_before": _fmt(ev.get("price_before")),
            "date_before": _fmt(ev.get("date_before")),
            "price_after": _fmt(ev.get("price_after")),
            "date_after": _fmt(ev.get("date_after")),
            "registered": _registered(ctx.conn, T, ev.get("date") or job.asof),
        }

    def post(self, job: JobSpec, result: dict, ctx: HandlerCtx) -> list[JobSpec]:
        ev = self._event(job)
        if ev is None:
            errs = ["缺事件資料，無法覆核"]
            ev = {"ticker": job.ticker, "date": job.asof}
        else:
            errs = ca.verify(result, ev, ca.position_of(ctx.conn, ca._nt(ev["ticker"])))

        td = ticker_dir(ctx.work_dir, job)
        td.mkdir(parents=True, exist_ok=True)
        (td / "corp_action.json").write_text(
            json.dumps({"result": result, "errors": errs}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

        if errs:
            ca.reject(ctx.conn, result, ev, errs)
            status = "rejected"
        else:
            out = ca.apply(ctx.conn, ctx.paths, result, ev, job.asof)
            status = out["status"]
            if status == "conflict":
                ca.reject(ctx.conn, result, ev, out["errors"])
        # 事件已有結果（含無公司行動、重複、轉人工）：先前工作失敗留下的異常結案，否則永久暫停、每日重派（M7-D1-01）
        ca.resolve_job_failed(ctx.conn, ev.get("event_id"), status)
        return []

    def on_invalid(self, job: JobSpec, errors: list[str], ctx: HandlerCtx) -> list[JobSpec]:
        ev = self._event(job)
        ca.on_invalid(ctx.conn, job.job_id, job.ticker, job.asof, ev, list(errors))
        ca.resolve_job_failed(ctx.conn, (ev or {}).get("event_id"), "輸出不合格，已轉人工")
        return []


def _registered(conn, ticker: str, date: str) -> str:
    """近 120 日已登錄的公司行動（供模型辨認「8-K 事後公告已入帳的行動」）。"""
    rows = conn.execute(
        "SELECT action_type, effective_date, ratio_new_per_old, cash_per_share, new_ticker, status, source "
        "FROM corporate_actions WHERE ticker = ? AND effective_date >= date(?, '-120 days') "
        "AND effective_date <= date(?, '+60 days') ORDER BY effective_date", (ticker, date, date)).fetchall()
    return "；".join(
        f"{r['effective_date']} {r['action_type']}（比例 {_fmt(r['ratio_new_per_old'])}、每股現金 "
        f"{_fmt(r['cash_per_share'])}、新代號 {_fmt(r['new_ticker'])}；狀態 {r['status']}；來源 {r['source']}）"
        for r in rows) or "無"
