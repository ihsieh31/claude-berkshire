"""觸發檢查的工作處理器：TRIAGE（初判）、PRICE（機械快速通道）、THESIS（論點檢查）。

價格隔離：TRIAGE、THESIS 的沙箱與提示都不含價格（PRICE_ISOLATED_JOBS）；PRICE 可讀價格與 cap_calc 結果。
規則來源：routing/prompts/{TRIAGE,PRICE,THESIS}.md、routing/model_routing.json、SKILL.md §0A／§6。
升級：一律帶原工作 params 的完整副本；PRICE、THESIS 以外的工作拿掉 created_ts（由 next_job 重設）。
"""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from .. import db
from ..analysis import writeback
from ..analysis.checks import ABSURD_PE, _is_delete, absurd_line, run_cap_calc
from ..analysis.dossier_prep import drop_price_sections, earnings_exhibit, scrub_price_sentences, scrub_price_text, split_10k_items
from ..models import JobSpec
from ..paths import Paths
from .context import Section
from .deep import _file_sections, _read_json, _skill_inputs, _td, _vals, _write_json
from .handlers import Handler, HandlerCtx, next_job
from .sandbox import create_sandbox, stage_inputs

TRIAGE_8K_CHARS = 30_000
THESIS_FILE_CHARS = 40_000
THESIS_TOTAL_CHARS = 150_000
THESIS_FORMS = ("10-Q", "10-Q/A", "8-K")
GATE3_MAX_DAYS = 365    # 閘門 3 有效期（PRICE.md：≤ 12 個月）
ONE_YEAR_DAYS = 365     # 建倉未滿一年不因價格因素賣出（SKILL §6）


# ---------------------------------------------------------------- 共用

def thesis_files(paths: Paths, ticker: str, thesis_file: str | None) -> list[Path]:
    """論點檔清單：[主檔, *addendum 依序]；沒有論點檔回傳空清單。"""
    main = writeback.main_thesis_path(paths, ticker, thesis_file)
    if main is None:
        return []
    return [main, *writeback.addendum_paths(main)]


def _cap_input_path(paths: Paths, ticker: str) -> Path:
    return paths.state / "cap_inputs" / f"{ticker}.json"


def _upgrade(job: JobSpec, job_type: str, ctx: HandlerCtx, extra: dict[str, Any] | None = None) -> JobSpec:
    """升級建立新工作：帶原 params 的完整副本；PRICE、THESIS 以外拿掉 created_ts。"""
    params = {**job.params, **(extra or {})}
    if job_type not in ("PRICE", "THESIS"):
        params.pop("created_ts", None)
    return next_job(job, job_type, ctx, params)


def _rel_or_abs(job: JobSpec, backend: str, rel: str) -> str:
    """Claude Code：沙箱內絕對路徑；API：相對路徑（內容已打包進提示）。"""
    if backend == "claude_code" and job.sandbox is not None:
        return str(Path(job.sandbox) / rel)
    return rel


def _write_scrubbed(sb: Path, rel: str, text: str) -> str:
    dest = sb / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return rel


def _stage_thesis(sb: Path, job: JobSpec, paths: Paths) -> list[str]:
    """論點檔寫進沙箱 thesis/（逐句刪價格字句）；回傳沙箱內相對檔名。"""
    names = []
    for p in thesis_files(paths, job.ticker, job.params.get("thesis_file")):
        text, _ = scrub_price_sentences(p.read_text(encoding="utf-8"))
        names.append(_write_scrubbed(sb, f"thesis/{p.name}", text))
    return names


def _kill_criteria(params: dict) -> list[str]:
    kc = params.get("kill_criteria")
    if not kc:
        kc = [x.get("text", "") for x in params.get("kill_criteria_due") or [] if isinstance(x, dict)]
    return [str(x) for x in kc if x]


def _thesis_value(job: JobSpec, backend: str, paths: Paths) -> str:
    rels = [f"thesis/{p.name}" for p in thesis_files(paths, job.ticker, job.params.get("thesis_file"))]
    return "、".join(_rel_or_abs(job, backend, r) for r in rels) or "（無論點檔）"


def _gate3_status(conn, ticker: str, asof: str) -> str:
    """閘門 3 狀態：取該檔最近一次 FULL 決策（排除 PRICE／THESIS 列）的 gate3_pass。
    回傳 通過 | 不通過 | 已過期。"""
    rows = conn.execute(
        "SELECT decision_date, data FROM decisions WHERE ticker = ? AND decision_date <= ? "
        "ORDER BY decision_date DESC, created_at DESC",
        (ticker, asof),  # 時點上界：補跑過去日期時不得用到之後的決策
    ).fetchall()
    for r in rows:
        d = json.loads(r["data"] or "{}")
        if d.get("review_type") or d.get("gate3_pass") is None:
            continue
        if d.get("gate3_pass") not in (True, "true"):
            return "不通過"
        age = (date.fromisoformat(asof) - date.fromisoformat(r["decision_date"])).days
        return "通過" if age <= GATE3_MAX_DAYS else "已過期"
    return "不通過"


RECENT_FULL_DAYS = 90   # 完整分析當時已看到「現價 < 上限」仍未判 BUY：90 天內不因 PRICE 再重跑（推論：約一季）


def _last_full_card(conn, ticker: str, asof: str) -> tuple[str, dict] | None:
    """最近一次完整分析（決策日誌無 review_type）的 (決策日, 訊號卡)。"""
    for r in conn.execute(
            "SELECT d.decision_date, d.data AS log, s.data AS card FROM decisions d JOIN signals s "
            "ON s.decision_id = d.decision_id WHERE d.ticker = ? AND d.decision_date <= ? "
            "ORDER BY d.decision_date DESC, d.created_at DESC",
            (ticker, asof)):
        if not json.loads(r["log"] or "{}").get("review_type"):
            return r["decision_date"], json.loads(r["card"] or "{}")
    return None


def _full_already_saw_buyable(conn, ticker: str, asof: str) -> str | None:
    """近 RECENT_FULL_DAYS 天的完整分析，當時現價已低於自動買進上限卻沒判 BUY → 回傳該日期。"""
    last = _last_full_card(conn, ticker, asof)
    if last is None:
        return None
    d, card = last
    px, cap = card.get("price"), card.get("auto_buy_cap")
    if card.get("signal") == "BUY" or not isinstance(px, (int, float)) or not isinstance(cap, (int, float)):
        return None
    if px < cap and (date.fromisoformat(asof) - date.fromisoformat(d)).days <= RECENT_FULL_DAYS:
        return d
    return None


def _pe_medians(lp: Any) -> tuple[float | None, float | None]:
    """largecap_pe：凍結檔 dict（pe_norm_median、fin_pe_norm_median）或單一數字。"""
    if isinstance(lp, dict):
        return lp.get("pe_norm_median"), lp.get("fin_pe_norm_median")
    if isinstance(lp, (int, float)) and not isinstance(lp, bool):
        return float(lp), None
    return None, None


def _held_under_year(first_buy: str | None, asof: str) -> bool:
    if not first_buy:
        return True  # 不知道建倉日：保守視為未滿一年
    try:
        return (date.fromisoformat(asof) - date.fromisoformat(first_buy)).days < ONE_YEAR_DAYS
    except ValueError:
        return True


def _map_action(proposed: str, holding: bool, passed: bool, *, first_buy: str | None, asof: str,
                weight: float | None, target: float | None) -> tuple[str, list[str]]:
    """proposed_action → 決策動作（PRICE 任務的對應表）。回傳 (action, 理由)。"""
    watch_or_hold = "HOLD" if holding else "WATCH"
    if proposed == "BUY_CANDIDATE":
        if not passed:
            return watch_or_hold, ["三閘門未全過，不買"]
        if not holding:
            return "BUY", []
        if weight is not None and target is not None and weight >= target:
            return "HOLD", ["已達目標權重，不加碼"]
        return "ADD", []
    if proposed == "SELL_HALF":
        if not holding:
            return "WATCH", []
        if _held_under_year(first_buy, asof):
            return "HOLD", ["建倉未滿一年，不因價格因素賣出（SKILL §6）"]
        return "TRIM", []
    if proposed == "SELL_REVIEW":
        if not holding:
            return "WATCH", []
        return "HOLD", ["賣出檢視：本次先持有，交 HOLD_REVIEW 依賣出決策樹判斷（含換股／資金用途）"]
    if proposed == "HOLD":
        return "HOLD", []
    return watch_or_hold, []  # NO_CHANGE 或不認得的值


# ---------------------------------------------------------------- TRIAGE

class TriageHandler(Handler):
    """8-K 原文（刪價格）＋ 新聞標題 → 是／否／不確定。escalate → THESIS；否則依 base_action 處理。"""
    job_type = "TRIAGE"

    def pre_script(self, job, ctx):
        td = _td(job, ctx)
        items = [dict(it) for it in job.params.get("triage") or [] if isinstance(it, dict)]
        seen: dict[str, int] = {}
        for it in items:
            if it.get("type") != "8-K" or not it.get("url"):
                continue
            if ctx.sec is None:
                it["text_missing"] = True
                continue
            try:
                raw = ctx.sec.filing_text(it["url"]) + earnings_exhibit(ctx.sec, it["url"], it.get("items"))
            except Exception:  # noqa: BLE001 - 單筆原文抓取失敗只影響該項，TRIAGE 會以「不確定」處理
                it["text_missing"] = True
                continue
            raw, _ = drop_price_sections(raw)
            text, _ = scrub_price_text(raw)
            base = f"8-K_{it.get('date')}"
            seen[base] = seen.get(base, 0) + 1
            name = f"{base}.txt" if seen[base] == 1 else f"{base}_{seen[base]}.txt"
            _write_scrubbed(td, f"sec/{name}", text[:TRIAGE_8K_CHARS])
            it["text_file"] = f"sec/{name}"
        job.params["triage"] = items  # 只在記憶體中標註，提示用

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        names = _stage_thesis(sb, job, ctx.paths)
        sec_names = sorted(p.name for p in (td / "sec").glob("*.txt")) if (td / "sec").is_dir() else []
        stage_inputs(sb, [(td / "sec" / n, f"sec/{n}") for n in sec_names], job.job_type)
        names += [f"sec/{n}" for n in sec_names]
        return _file_sections(sb, names)

    def prompt_values(self, job, ctx, backend):
        items = job.params.get("triage") or []
        kc = _kill_criteria(job.params)
        return _vals(job, backend, {
            "thesis_file": _thesis_value(job, backend, ctx.paths),
            "kill_criteria": "\n".join(f"- {k}" for k in kc) or "（無）",
            "triage_items": "```json\n" + json.dumps(items, ensure_ascii=False, indent=1) + "\n```"
            if items else "（無）",
            "tickers": job.ticker,
        })

    def next_jobs(self, job, result, ctx):
        if result.get("escalate") is True:
            return [_upgrade(job, "THESIS", ctx, {"triage_result": result})]
        base = job.params.get("base_action")
        if base == "PRICE":
            if _cap_input_path(ctx.paths, job.ticker).is_file():
                return [_upgrade(job, "PRICE", ctx)]
            return [_upgrade(job, "DOSSIER", ctx)]  # 沒有 cap_inputs 無法快速重算
        if base == "FULL":
            return [_upgrade(job, "DOSSIER", ctx)]
        if base == "THESIS":
            return [_upgrade(job, "THESIS", ctx)]
        writeback.touch_review(ctx.conn, job.ticker, job.asof)
        return []

    def on_invalid(self, job, errors, ctx):
        # 初判答不出來視同「不確定」→ 升級 THESIS（deps 清空，避免被失敗的 TRIAGE 連帶跳過）
        nj = _upgrade(job, "THESIS", ctx, {"triage_invalid": True})
        nj.deps = []
        return [nj]


# ---------------------------------------------------------------- PRICE

class PriceHandler(Handler):
    """機械快速通道：cap_calc 重算（只更新 price、long_rate、PE 中位）→ 模型整理 → 框架覆核與回寫。"""
    job_type = "PRICE"

    def pre_script(self, job, ctx):
        cap_path = _cap_input_path(ctx.paths, job.ticker)
        if not cap_path.is_file():
            raise FileNotFoundError(f"缺 cap_inputs（應先建 DOSSIER）：{cap_path}")
        p = job.params
        if p.get("price") is None:
            raise ValueError("PRICE 缺現價")
        x = _read_json(cap_path)
        x["price"] = float(p["price"])
        rates = p.get("rates") or {}
        if rates.get("t30") is not None:
            x["long_rate"] = float(rates["t30"])
        if rates.get("t30_20y_median") is not None:
            x["long_rate_20y_med"] = float(rates["t30_20y_median"])
        largecap, fin = _pe_medians(p.get("largecap_pe"))
        if largecap is not None:
            x["largecap_pe_median"] = largecap
        if fin is not None:
            x["fin_pe_median"] = fin
        # 只更新 price、long_rate、PE 中位（routing：PRICE pre_script）；寫在本次工作目錄，state 的原輸入不動
        td = _td(job, ctx)
        _write_json(td / "cap_input.json", x)
        cap = run_cap_calc(td / "cap_input.json")
        cap["absurd_price_line"] = absurd_line(x)
        _write_json(td / "cap_result.json", cap)

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        stage_inputs(sb, [(td / "cap_result.json", "cap_result.json")], job.job_type)
        card = writeback.latest_signal(ctx.conn, job.ticker)
        reg = writeback._registry_get(ctx.conn, job.ticker)
        _write_json(sb / "last_signal_card.json", (card or {}).get("data") or {})
        _write_json(sb / "registry_entry.json", (reg or {}).get("data") or {})
        _write_json(sb / "portfolio_state.json", job.params.get("portfolio_state") or {})
        names = ["cap_result.json", "last_signal_card.json", "registry_entry.json", "portfolio_state.json"]
        thesis = thesis_files(ctx.paths, job.ticker, job.params.get("thesis_file"))
        if thesis:
            stage_inputs(sb, [(p, f"thesis/{p.name}") for p in thesis], job.job_type)
            names += [f"thesis/{p.name}" for p in thesis]
        return _file_sections(sb, names)

    def prompt_values(self, job, ctx, backend):
        return _vals(job, backend, {"tickers": job.ticker})

    def prompt_suffix(self, job):
        return "\n\n本工作只有一檔：輸出單一 JSON 物件（不是陣列）。"

    def post(self, job, result, ctx):
        td = _td(job, ctx)
        T, asof, p = job.ticker, job.asof, job.params
        cap = _read_json(td / "cap_result.json")
        price = float(p["price"])
        reg = writeback._registry_get(ctx.conn, T)
        holding = reg is not None and reg["status"] == "HOLDING"
        rdata = reg["data"] if reg else {}
        card = writeback.latest_signal(ctx.conn, T)
        screen = (p.get("screen_context") or {}).get("screen_decision") or \
            ((card or {}).get("data") or {}).get("screen_decision")

        gate1 = not _is_delete(screen)
        auto = cap.get("auto_buy_cap")
        grade_ok = cap.get("grade_used") in ("A", "B")  # C 級最多觀察（cap_calc conclusion_hint）
        flips = bool((cap.get("conclusion_test") or {}).get("flips"))
        if auto is None:
            gate2 = "無上限"
        else:
            gate2 = "通過" if (price < auto and grade_ok and not flips) else "不通過"
        gate3 = _gate3_status(ctx.conn, T, asof)
        passed = gate1 and gate2 == "通過" and gate3 == "通過"

        action, notes = _map_action(
            str(result.get("proposed_action") or ""), holding, passed,
            first_buy=rdata.get("first_buy_date"), asof=asof,
            weight=rdata.get("weight"), target=rdata.get("target_weight"),
        )
        notes = ["auto_buy_cap、IV_base（sell_review_above）、荒謬級線以 cap_calc 覆寫"] + notes

        # 框架覆寫模型的數字（不信模型的數字）；result 會被存進 jobs.result
        result.update({
            "price": price, "auto_buy_cap": auto, "sell_review_above": cap.get("IV_base"),
            "absurd_price_line": cap.get("absurd_price_line"), "framework_action": action,
            "framework_gates": {"gate1": gate1, "gate2": gate2, "gate3": gate3},
        })

        needs_full = bool(result.get("needs_full")) or gate3 == "已過期"
        if needs_full:
            return [_upgrade(job, "DOSSIER", ctx)]  # 需要判斷的題目：交給完整分析，不在此寫結論
        if action == "BUY":
            # 新部位：觀察卡沒有走到 G8–G11（目標權重、尾部與單事件上限），也沒經過 RED_TEAM（SKILL §4、§5）
            # → 交給完整分析決定；近期完整分析已看過可買價位仍未判 BUY 者，不重跑（避免每兩週燒強模型）
            seen = _full_already_saw_buyable(ctx.conn, T, asof)
            if seen is None:
                result["framework_action"] = "BUY_CANDIDATE → 完整分析"
                return [_upgrade(job, "DOSSIER", ctx)]
            action = "WATCH"
            notes.append(f"{seen} 的完整分析當時現價已低於上限仍未判 BUY：{RECENT_FULL_DAYS} 天內不重跑，維持 WATCH")
            result["framework_action"] = action

        t30 = _read_json(td / "cap_input.json").get("long_rate")
        writeback.write_price_result(
            ctx.conn, ctx.paths, T, asof, job.run_id, action=action, gate1=gate1, gate2=gate2, gate3=gate3,
            cap=cap, price=price, price_date=p.get("price_date"), t30=t30,
            reason=str(result.get("reason") or ""), notes=notes,
        )
        # SKILL §6 第 2 條（價格 ≥ IV_base → 賣出檢視：有更好用途 → 減碼或換股）：持股交 HOLD_REVIEW 走賣出決策樹，
        # 不由 PRICE（弱模型）直接決定。模型沒提但現價已 ≥ IV_base 也一樣送。
        iv = cap.get("IV_base")
        above = isinstance(iv, (int, float)) and price >= iv
        if holding and (str(result.get("proposed_action") or "") == "SELL_REVIEW" or above):
            return [_upgrade(job, "HOLD_REVIEW", ctx, {"full_stage": "PRICE", "price": price})]
        return []

    def next_jobs(self, job, result, ctx):
        return []  # post 內已處理升級

    def on_invalid(self, job, errors, ctx):
        # 不合格的 PRICE 也要留一筆「閘門未過」的 PRICE 紀錄：否則加碼會沿用更早那筆全過的閘門紀錄
        db.insert(ctx.conn, "decisions", {
            "decision_id": job.job_id, "ticker": job.ticker, "decision_date": job.asof, "run_id": job.run_id,
            "job_id": job.job_id, "signal": "NO_ACTION", "reason_code": "MISSING_DATA",
            "data": {"job_type": job.job_type, "errors": list(errors), "review_type": "PRICE",
                     "gate1_not_deleted": None, "gate2_price_below_cap": "不通過", "gate3_pass": False,
                     "framework_notes": ["PRICE 輸出不合格：本次閘門視為未過，暫停加碼至下次複查"]},
            "created_at": db.now_iso(),
        })
        return []


# ---------------------------------------------------------------- THESIS

def _excerpt(sec, f: dict) -> str:
    """單份申報的節錄：刪價格字句；10-Q 只留 MD&A（Item 2）及其後，找不到則取前段；每份至多 40,000 字。"""
    raw = sec.filing_text(f["url"])
    if str(f.get("form", "")).startswith("8-K"):
        raw += earnings_exhibit(sec, f["url"], f.get("items"))
    raw, _ = drop_price_sections(raw)
    text, _ = scrub_price_text(raw)
    if str(f.get("form", "")).startswith("10-Q"):
        items = split_10k_items(text)
        codes = [c for c, _ in items]
        if "2" in codes:
            text = "".join(body for _, body in items[codes.index("2"):])
    return text[:THESIS_FILE_CHARS]


class ThesisHandler(Handler):
    """論點檢查：論點檔＋上次分析後的新申報節錄（10-Q、8-K）＋ TRIAGE 結果 → 完好／疑似／證偽。"""
    job_type = "THESIS"

    def pre_script(self, job, ctx):
        td = _td(job, ctx)
        out = td / "thesis_sec"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        if ctx.sec is None:
            return  # 無 SEC 客戶端：只用論點檔判斷（提示內不另附申報）
        last = job.params.get("last_review_date")
        cik = job.params.get("cik") or ctx.sec.cik_for(job.ticker)
        if not cik:
            return
        found = ctx.sec.filings(cik, job.asof, since=last, forms=list(THESIS_FORMS))
        found = [f for f in found if f.get("url") and f["filingDate"] < job.asof
                 and (not last or f["filingDate"] > last)]
        budget = THESIS_TOTAL_CHARS
        chosen: list[tuple[dict, str]] = []
        for f in sorted(found, key=lambda f: f["filingDate"], reverse=True):  # 由新到舊取，總量有上限
            if budget <= 0:
                break
            text = _excerpt(ctx.sec, f)[:budget]
            budget -= len(text)
            chosen.append((f, text))
        for f, text in sorted(chosen, key=lambda ft: ft[0]["filingDate"]):
            tag = str(f["form"]).replace("/", "").replace(" ", "")
            acc_tail = str(f.get("accessionNumber") or "").replace("-", "")[-6:]
            name = f"{tag}_{f['filingDate']}_{acc_tail}.txt"
            _write_scrubbed(out, name, f"# {f['form']} {f['filingDate']} {f['url']}\n{text}")

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        names = _stage_thesis(sb, job, ctx.paths)
        out = td / "thesis_sec"
        sec_names = sorted(p.name for p in out.glob("*.txt")) if out.is_dir() else []
        if sec_names:
            stage_inputs(sb, [(out / n, f"thesis_sec/{n}") for n in sec_names], job.job_type)
            names += [f"thesis_sec/{n}" for n in sec_names]
        tr = job.params.get("triage_result")
        if tr:
            _write_json(sb / "triage_result.json", tr)
            names.append("triage_result.json")
        # 讀規則用的 skill 副本（提示以 {pkg} 路徑指向 references/procedure）
        stage_inputs(sb, _skill_inputs(ctx), job.job_type)
        sections = _file_sections(sb, names)
        proc = ctx.paths.skill_deep / "references" / "procedure"
        for n in ("07_conclusion_execution.md", "09_postmortem_signals.md"):
            if (proc / n).is_file():
                sections.append(Section(f"references/procedure/{n}", (proc / n).read_text(encoding="utf-8"), 1))
        return sections

    def prompt_values(self, job, ctx, backend):
        kc = _kill_criteria(job.params)
        return _vals(job, backend, {
            "thesis_file": _thesis_value(job, backend, ctx.paths),
            "kill_criteria": "\n".join(f"- {k}" for k in kc) or "（無）",
            "last_review_date": job.params.get("last_review_date") or "（無紀錄）",
            "r_list": r_list_text(ctx.paths.skill_deep),
        })

    def post(self, job, result, ctx):
        writeback.write_thesis_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result, job.params)
        return self.next_jobs(job, result, ctx)

    def on_invalid(self, job, errors, ctx):
        # 答不出來當成「疑似」（停止加碼，並交給完整分析）；不可留下沒有 review_type 的紀錄讓更早的「完好」繼續生效
        result = {"verdict": "疑似", "reason": "THESIS 輸出不合格，視為疑似：" + "；".join(errors)[:500],
                  "kill_criteria_status": [], "r_list_status": [], "new_facts": []}
        writeback.write_thesis_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result, job.params)
        nj = _upgrade(job, "DOSSIER", ctx, {"thesis_result": result})
        nj.deps = []
        return [nj]

    def next_jobs(self, job, result, ctx):
        if result.get("verdict") in ("疑似", "證偽"):
            return [_upgrade(job, "DOSSIER", ctx, {"thesis_result": result})]
        return []


def r_list_text(skill_deep) -> str:
    """SELL-011 完整複查清單 R1–R10 原文（rule_index/04_hold_sell.md，到 SELL-012 為止）。找不到照實標缺。"""
    f = Path(skill_deep) / "references" / "rule_index" / "04_hold_sell.md"
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return "（缺：找不到 rule_index/04_hold_sell.md）"
    start = text.find("**SELL-011")
    end = text.find("**SELL-012", start + 1)
    if start < 0 or end < 0:
        return "（缺：04_hold_sell.md 內找不到 SELL-011 段）"
    return text[start:end].strip()
