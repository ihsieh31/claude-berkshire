"""持股完整複查的 H2 賣出決策（HOLD_REVIEW）。

觸發：持股（帳本有股數或登錄表 HOLDING）的完整分析結束且未得出 BUY——FULL_A 停止、FULL_B 非 BUY、RED_TEAM 降為 WATCH。
買進閘門不通過不是賣出理由（rulebook H0）；本工作依 rulebook §9（Q0–Q6）決定 HOLD／TRIM／SELL_ALL 與是否停止加碼。

框架覆核（模型說賣不等於賣）：
- 業務類不得 TRIM；業務類全清須具名被否定的假設與事實（Q2、Q5）。
- 估值類須有完整分析的 IV_base，且價格 ≥ IV_base（V 的條件之一）；TRIM 只限估值類，比例 0–1。
- 制度、事件、換股、資金需求類 → 不由模型下單：改 HOLD＋停止加碼，記異常待人工。
覆核不過 → 退回一次；仍不過 → HOLD＋停止加碼，記異常待人工。輸出不合格同樣處理。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .. import db
from ..ledger import book
from .deep import _skill_inputs, _td, _vals, _file_sections, _read_json, stage_g8_view
from .handlers import Handler, next_job
from .review_jobs import _stage_thesis
from .sandbox import create_sandbox, stage_inputs

SELL_ACTIONS = ("TRIM", "SELL_ALL")
HUMAN_CATEGORIES = ("制度", "事件", "SWITCH", "FUNDING")
VALUATION_CATEGORIES = ("估值", "估值＋業務")
TWO_YEARS_DAYS = 730
INPUT_FILES = ("phaseA.json", "report_A.md", "report.md", "signal_card.json", "cap_result.json")


def is_holding(conn, ticker: str) -> bool:
    """帳本有股數，或登錄表為 HOLDING。"""
    if (book.positions(conn).get(ticker) or {}).get("qty", 0) > 0:
        return True
    row = conn.execute("SELECT status FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    return row is not None and row["status"] == "HOLDING"


def sell_tree_text(skill_deep) -> str:
    """rulebook §9 賣出決策樹原文（到下一個 `---` 為止）。找不到照實標缺。"""
    f = Path(skill_deep) / "references" / "rulebook.md"
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return "（缺：找不到 references/rulebook.md）"
    start = text.find("## 9. 賣出決策樹")
    end = text.find("\n---", start + 1)
    if start < 0 or end < 0:
        return "（缺：rulebook.md 內找不到第 9 節）"
    return text[start:end].strip()


def valuation_numbers(td: Path, params: dict | None = None) -> tuple[float | None, float | None]:
    """(IV_base, price)：IV_base 只取 cap_calc 結果（完整分析走到估值、或 PRICE 以 cap_inputs 重算才有）；
    價格取價格檔，沒有則取工作參數（PRICE 觸發時帶入）。"""
    iv = price = None
    cap = td / "cap_result.json"
    if cap.is_file():
        v = _read_json(cap).get("IV_base")
        iv = float(v) if isinstance(v, (int, float)) else None
    for p in sorted((td / "dossier").glob("*_price.json")) if (td / "dossier").is_dir() else []:
        v = _read_json(p).get("price")
        price = float(v) if isinstance(v, (int, float)) else None
    if price is None and isinstance((params or {}).get("price"), (int, float)):
        price = float(params["price"])
    return iv, price


def check(result: dict, *, iv_base: float | None, price: float | None, held_days: int | None) -> tuple[list[str], bool]:
    """回傳 (錯誤清單, 是否需轉人工)。錯誤 → 退回；需轉人工 → HOLD＋停止加碼＋異常。"""
    act, cat = result.get("action"), result.get("sell_category")
    if act not in SELL_ACTIONS:
        return [], False
    errs: list[str] = []
    named = str(result.get("falsified_assumption") or "").strip()
    if not cat:
        return ["賣出（TRIM／SELL_ALL）必須填 sell_category"], False
    if cat in HUMAN_CATEGORIES:
        return [], True
    if act == "TRIM":
        if cat != "估值":
            errs.append(f"TRIM 只用於估值類（Q3c／荒謬級）；{cat} 類不得只賣一部分")
        f = result.get("trim_fraction")
        if not isinstance(f, (int, float)) or isinstance(f, bool) or not 0 < f < 1:
            errs.append(f"TRIM 的 trim_fraction 須介於 0 與 1：{f!r}")
    if cat in ("業務", "估值＋業務") and not named:
        errs.append("業務類賣出必須具名：哪條假設或失效條件被哪個事實否定（falsified_assumption）")
    if cat in VALUATION_CATEGORIES:
        if iv_base is None:
            errs.append("估值類賣出需要完整分析的 IV_base；本次完整分析未走到估值")
        elif price is None or price < iv_base:
            errs.append(f"估值類賣出需價格 ≥ IV_base（V 的條件之一）：價格 {price}，IV_base {iv_base}")
    if held_days is not None and held_days < TWO_YEARS_DAYS and not named:
        errs.append("建倉未滿 2 年（Q5）：須具名被否定的假設，寫不出 → 持有")
    return errs, False


def _position_text(conn, ticker: str, asof: str) -> tuple[str, int | None]:
    pos = book.positions(conn).get(ticker)
    if not pos:
        return "帳本無股數（登錄表為 HOLDING）", None
    days = None
    if pos.get("first_buy_date"):
        days = (date.fromisoformat(asof) - date.fromisoformat(pos["first_buy_date"])).days
    return (f"{pos['qty']} 股，每股成本 {pos['avg_cost']:.4f}，首次建倉 {pos.get('first_buy_date') or '缺'}"
            f"（持有 {days if days is not None else '缺'} 日）"), days


class HoldReviewHandler(Handler):
    job_type = "HOLD_REVIEW"

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        T = job.ticker
        items = _skill_inputs(ctx)
        items += [(td / n, f"{T}/{n}") for n in INPUT_FILES if (td / n).is_file()]
        items.append((stage_g8_view(ctx, job, td), "portfolio/holdings_g8.json"))   # Q3「更好的用途」
        names = stage_inputs(sb, items, job.job_type)
        names += _stage_thesis(sb, job, ctx.paths)
        return _file_sections(sb, [n for n in names if not n.startswith("pkg/")])

    def prompt_values(self, job, ctx, backend):
        td = _td(job, ctx)
        iv, price = valuation_numbers(td, job.params)
        pos, _ = _position_text(ctx.conn, job.ticker, job.asof)
        stage = job.params.get("full_stage")
        full = {"A": "FULL_A 停在 {g}（{c}），未走到估值；無 IV_base".format(
                    g=job.params.get("stopped_at_gate") or "?", c=job.params.get("reason_code") or "?"),
                "B": f"走完 FULL_B，最終訊號 {job.params.get('final_signal') or '?'}；IV_base {iv}",
                "PRICE": f"價格複查（PRICE）觸發賣出檢視：現價 {price} 對 IV_base {iv}（以最近完整分析的 cap_inputs 重算）；"
                         "依 SKILL §6 第 2 條判斷是否有更好用途（減碼或換股）"}.get(stage, "缺")
        tr = job.params.get("thesis_result")
        T = job.ticker
        files = [f"{T}/{n}" for n in INPUT_FILES if (td / n).is_file()]
        return _vals(job, backend, {
            "position": pos,
            "price": f"{price}" if price is not None else "缺",
            "full_status": full,
            "thesis_result": "```json\n" + json.dumps(tr, ensure_ascii=False, indent=1) + "\n```" if tr
                             else "（無：本次不是由論點檢查觸發）",
            "files": "、".join(files + ["portfolio/holdings_g8.json（G8 比較檔）", "thesis/（論點檔與增補）"]),
            "sell_tree": sell_tree_text(ctx.paths.skill_deep),
        })

    def prompt_suffix(self, job):
        fb = job.params.get("feedback")
        if not fb:
            return ""
        return "\n\n## 框架退回（本次為修正版）\n" + "\n".join(f"- {x}" for x in fb)

    def post(self, job, result, ctx):
        from ..analysis.writeback import write_hold_review_result
        td = _td(job, ctx)
        iv, price = valuation_numbers(td, job.params)
        _, days = _position_text(ctx.conn, job.ticker, job.asof)
        errs, human = check(result, iv_base=iv, price=price, held_days=days)
        notes: list[str] = []
        if errs and not job.params.get("review_retry"):
            p = dict(job.params)
            p.pop("created_ts", None)
            p.update({"review_retry": 1, "feedback": ["覆核未通過：" + "；".join(errs)]})
            return [next_job(job, "HOLD_REVIEW", ctx, p)]
        if errs or human:
            why = ("覆核兩次未通過：" + "；".join(errs)) if errs else \
                f"{result.get('sell_category')} 類賣出由人工處理（框架不依模型下單）"
            notes.append(why + f"；模型原動作 {result.get('action')}，改為 HOLD＋停止加碼")
            result = {**result, "action": "HOLD", "stop_adding": True}
            _anomaly(ctx.conn, job.ticker, {"run_id": job.run_id, "why": why, "result": result})
        _write_json(td / "hold_review.json", result)
        write_hold_review_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result,
                                 job.params, notes, price=price, iv_base=iv)
        return []

    def on_invalid(self, job, errors, ctx):
        from ..analysis.writeback import write_hold_review_result
        why = "HOLD_REVIEW 輸出不合格：" + "；".join(errors)
        result = {"ticker": job.ticker, "decision_date": job.asof, "path": [], "action": "HOLD",
                  "sell_category": None, "trim_fraction": None, "stop_adding": True, "observe_until": None,
                  "falsified_assumption": None, "reason": why}
        _anomaly(ctx.conn, job.ticker, {"run_id": job.run_id, "why": why})
        write_hold_review_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result,
                                 job.params, [why + "；保守 HOLD＋停止加碼"])
        return []


def _anomaly(conn, ticker: str, detail: dict[str, Any]) -> None:
    db.insert(conn, "anomalies", {"ticker": ticker, "kind": "hold_review_needs_human", "detail": detail,
                                  "status": "open", "pause_trading": 0, "created_at": db.now_iso()})


def _write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
