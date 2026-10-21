"""深度分析工作：DOSSIER → FULL_A →（gate3_check）→ FULL_B →（cap_calc 覆核）→ RED_TEAM?

沙箱佈局（{sb}＝工作沙箱；Claude Code 模式下模型只能讀寫沙箱內）：
  {sb}/pkg/berkshire-deep-merged/{SKILL.md, references/, assets/, scripts/cap_calc.py, scripts/gate3_check.py}
  {sb}/pkg/routing/output_schemas.json
  {sb}/dossier/{T}.md                 資料包（不含價格）
  {sb}/dossier/{T}_price.json         只有 FULL_B（gate3 答覆存檔之後才開）
  {sb}/sec/*.txt                      只有 DOSSIER（決策日前的 SEC 原文，已刪價格句）
  {sb}/{T}/...                        前一步的輸出（phaseA.json、report_A.md…）與本步輸出

正式輸出區 ticker_dir（state/work/{run}/{T}）：
  dossier/{T}.md、dossier/{T}_price.json、dossier/{T}_dq.json、sec/*.txt、
  phaseA.json、report_A.md、gate3_answers.json、gate3_result.json、price_file_opened_at.txt（FULL_B 開價格檔時間）、
  cap_input.json、cap_result.json、report.md、signal_card.json、registry_entry.json、decision_log.json、thesis.md、
  red_team.json、decision.json（回寫結果）
終點（FULL_A 停止、FULL_B 非 BUY、RED_TEAM 結束、無法完成）由框架回寫 DB（analysis.writeback）。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..models import JobSpec
from .context import Section
from . import validate
from .handlers import Handler, HandlerCtx, next_job
from .sandbox import collect_outputs, create_sandbox, open_price_file, scrub_prices, stage_inputs, ticker_dir

SKILL_FILES = ["SKILL.md", "references", "assets", "scripts/cap_calc.py", "scripts/gate3_check.py"]
TD_OUT_A = ["report_A.md", "gate3_answers.json"]
FULL_A_PRIOR = ["phaseA.json", *TD_OUT_A]   # FULL_A 框架退回附的上一版輸出（A3-M5A-02）
TD_OUT_B = ["cap_input.json", "report.md", "signal_card.json", "registry_entry.json", "decision_log.json", "thesis.md"]


# ---------------------------------------------------------------- 共用

# 沿著 DOSSIER → FULL_A → FULL_B → RED_TEAM →（持股）HOLD_REVIEW 傳遞的參數
# news：觸發窗口新聞（D75：完整分析的觸發說明附標題，否則新聞觸發後沒有模型看過標題）
CARRY = ("screen_context", "portfolio_state", "largecap_pe", "holding", "thesis_result", "thesis_file", "reasons", "news")


def _carry(job: JobSpec, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    p = {k: job.params.get(k) for k in CARRY if job.params.get(k) is not None}
    p.update(extra or {})
    return p


def _td(job: JobSpec, ctx: HandlerCtx) -> Path:
    return ticker_dir(ctx.work_dir, job)


def _shelve(job: JobSpec, td: Path, names: list[str]) -> None:
    """收檔前把 td 內上一版同名輸出移到 td/previous_{本工作序號}/（D45、M5-D1-03）：退回重寫漏寫的檔
    不得由上一版或同 run PRICE 前置寫的 cap_input／cap_result 補上。退回重寫要附的上一版已在 prepare 複製進沙箱。"""
    dest = td / f"previous_{job.job_id.rsplit(':', 1)[-1]}"
    for n in names:
        if (td / n).is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(td / n), str(dest / n))


def _dq_item(td: Path, ticker: str) -> list[tuple[Path, str]]:
    """DOSSIER 的資料包核對結果（dq_checks／missing；M5-D5-01）；有才 stage。隔離工作經 stage_inputs 刪價格鍵。"""
    f = td / "dossier" / f"{ticker}_dq.json"
    return [(f, f"dossier/{ticker}_dq.json")] if f.is_file() else []


def holding_stop(pa: dict | None) -> dict | None:
    """設計決策 D30：持股版 FULL_A 的未過閘門（phaseA.holding_stop＝{gate, reason_code, reason}）；沒有 → None。"""
    hs = (pa or {}).get("holding_stop") if isinstance(pa, dict) else None
    return hs if isinstance(hs, dict) and hs.get("gate") else None


# 觸發理由遮數值時保留的數字（RM2-3／M2_D4_04）：日期、表單名、規則與題號（P-50、SEL-020、G7、Q2、D01）、期間（12 個月）
_KEEP_OR_NUM = re.compile(r"(\d{4}-\d{2}-\d{2}|\b\d+-[KQ](?:/A)?\b|(?<![A-Za-z0-9])[A-Za-z][A-Za-z_]*-?\d+(?:[_-][A-Za-z]*\d+)*"
                          r"|\d+\s*(?:個月|年|季|週|天))|[-+−]?[$＄]?\d+(?:[.,]\d+)*(?:\s*(?:%|％|pp|倍))?")
_PRICE_MOVE = re.compile(r"[±→]")   # review_due 的價格變動理由（單季自高點跌、單日 ±4%、較成本 −12%、殖利率 a → b）


def _mask_nums(txt: str) -> str:
    return _KEEP_OR_NUM.sub(lambda mm: mm.group(1) or "〔數值略〕", txt)


# 價格盲（FULL_A）新聞標題的保守濾網 _blind_drop 移到 analysis.dossier_prep（A3-M5B-04：消除 dossier_prep 反向局部匯入 jobs 層）
from ..analysis.dossier_prep import _blind_drop  # noqa: E402,F401  (test_fix_RM5_5 等仍自本模組匯入)


# review_due 的未檢查註記以「（…）」整句附在 reasons（框架內部狀態，非觸發原因；A3-M5D-01）。價格盲工作一律略過，
# 只留與價格無關、影響證據完整度的三種（新聞未提供、觸發檢查失敗、申報抓取失敗），且不得含價格字眼
_NOTE_KEEP = re.compile(r"未提供新聞|觸發檢查失敗|申報抓取失敗")
_NOTE_PRICE = re.compile(r"價|上限|荒謬|收盤|殖利率|長債|減碼|加碼|漲|跌|高點|成本|線下|線上|PRICE|IV|cap", re.IGNORECASE)


def _unchecked_note(raw: str) -> bool:
    """價格盲 trigger_text 應略過的未檢查註記：以「（」或「(」開頭，且不是 _NOTE_KEEP 中不含價格字眼的那幾種。"""
    s = raw.lstrip()
    if not s.startswith(("（", "(")):
        return False
    return not (_NOTE_KEEP.search(s) and not _NOTE_PRICE.search(s))


def trigger_text(params: dict, price_blind: bool = True, news: bool = False) -> str:
    """M5-D5-07：本次觸發原因（review_due 的 reasons 逐句刪價格字句，其餘數值遮成〔數值略〕；規則編號、日期、
    表單名、期間保留，RM2-3）與論點檢查結論。價格變動類理由（含 ± 或 →）只留類別。
    price_blind：價格盲工作不列 [PRICE] 類觸發。
    news：附觸發窗口新聞標題（D75；完整分析 FULL_A／FULL_B 用。THESIS 提示另有 {news}，不重複）：
    review_jobs._news_text 已濾價格標題，這裡再刪價格字句、遮數值（同 reasons）；price_blind 另以 _blind_drop 整則丟
    評等、估值、市值、漲跌類標題（X53 返工）。"""
    from ..analysis.dossier_prep import scrub_price_sentences
    lines = []
    for r in params.get("reasons") or []:
        raw = str(r)
        m = re.match(r"\s*(\[[A-Z]+\])", raw)
        cat = m.group(1) if m else "[觸發]"
        if price_blind and cat == "[PRICE]":
            continue
        if price_blind and _unchecked_note(raw):   # A3-M5D-01：review_due 未檢查註記（荒謬級線、等級 C…）洩漏價格方向
            continue
        if price_blind and "P-43" in raw:   # X55：權重超限理由含「因上漲超限不賣」，洩漏價格方向
            lines.append(f"- {cat}（制度性完整複查，細節略）")
            continue
        txt = scrub_price_sentences(raw)[0].strip()
        if not txt or _PRICE_MOVE.search(txt):
            txt = cat + "（細節含數字，略）"
        else:
            txt = _mask_nums(txt)
        lines.append(f"- {txt}")
    tr = params.get("thesis_result")
    if isinstance(tr, dict) and tr.get("verdict"):
        why = scrub_price_sentences(str(tr.get("reason") or ""))[0].strip()[:300]
        if price_blind and why:   # A3-M3D-01：價格盲工作的論點檢查理由同新聞標題濾網：評等／估值／行情字句整段略，其餘遮數值
            why = "理由含評等、估值或行情字句，略" if _blind_drop(why) else _mask_nums(why)
        lines.append(f"- 論點檢查結論：{tr['verdict']}" + (f"（{why}）" if why else ""))
    items = [n for n in params.get("news") or [] if not (price_blind and isinstance(n, dict) and _blind_drop(n.get("headline")))]
    if news and items:
        from .review_jobs import _news_text   # 局部匯入：review_jobs 匯入本模組
        heads = [_mask_nums(h) for h in (scrub_price_sentences(x)[0].strip() for x in _news_text({"news": items}).splitlines())
                 if h and h != "（無）"]
        if heads:
            lines.append("- 觸發窗口內新聞標題（價格標題已濾、數值已遮；事實以申報為準）：\n"
                         + "\n".join("  " + h for h in heads))
    if not lines:
        return "（無：本次不是觸發複查）"
    return "本次為觸發複查，請特別檢查下列觸發點是否改變結論：\n" + "\n".join(lines)


def prior_red_light_text(conn, ticker: str, price_blind: bool) -> str:
    """D92（A3-M2B-03）：登錄表 last_reason_code＝RED_LIGHT（上次完整分析為紅燈）→ 本次重跑附上次的致命路徑／紅燈理由，
    要模型以反方角色逐條檢查（D24）；不是紅燈 → ""。價格盲（FULL_A）：逐條刪價格字句，含評等／估值／行情字句者整條略，其餘遮數值。"""
    from ..analysis.dossier_prep import scrub_price_sentences
    from ..analysis.writeback import _registry_get
    d = ((_registry_get(conn, ticker) or {}).get("data") or {})
    if d.get("last_reason_code") != "RED_LIGHT":
        return ""
    rc = d.get("red_light_context") if isinstance(d.get("red_light_context"), dict) else {}
    items = [str(p.get("path") or "").strip() + (f"（{str(p.get('probability_note')).strip()}）"
                                                 if str(p.get("probability_note") or "").strip() else "")
             for p in rc.get("fatal_paths") or [] if isinstance(p, dict)]
    items += [str(x) for x in rc.get("reasons") or [] if str(x).strip()]
    out = []
    for s in items:
        s = s.strip()[:300]
        if price_blind:
            s = scrub_price_sentences(s)[0].strip()
            s = "（一條含價格、評等或估值字句，略）" if not s or _blind_drop(s) else _mask_nums(s)
        if s:
            out.append("  - " + s)
    return (f"- 上次完整分析（{rc.get('date') or d.get('last_full_review_date') or '日期缺'}）為紅燈 RED_LIGHT（P-64／D24）："
            "本次為紅燈後重跑，請以反方角色逐條檢查下列致命路徑／紅燈理由是否已排除，未排除者照實寫入 G9（D92）"
            + ("：\n" + "\n".join(out) if out else "（上次紅燈理由未存檔）"))


def _with_red_light(trigger: str, red: str) -> str:
    if not red:
        return trigger
    return ("本次為紅燈後重跑：\n" + red) if trigger.startswith("（無") else trigger + "\n" + red


def screen_context_for_prompt(sc: Any) -> dict:
    """A3-M1D-01：交 FULL_A（價格盲）的 screen_context 去掉 delete_codes 的 X5_ 碼（近期已淘汰：只擋初選再選入，
    交給模型會錨定前次結論）；decisions.json 原檔不動（P39R 追溯）。再刪價格鍵。"""
    from ..pipelines.screen import X5_CODE
    sc = dict(sc) if isinstance(sc, dict) else {}
    dc = sc.get("delete_codes")
    if isinstance(dc, str):
        sc["delete_codes"] = ";".join(c for c in dc.split(";") if c.strip() and not c.strip().startswith(X5_CODE))
    elif isinstance(dc, list):
        sc["delete_codes"] = [c for c in dc if not str(c).strip().startswith(X5_CODE)]
    return scrub_prices(sc)


def _skill_inputs(ctx: HandlerCtx) -> list[tuple[Path, str]]:
    """skill 副本（模型讀規則用）。"""
    root = ctx.paths.skill_deep
    out: list[tuple[Path, str]] = []
    for rel in SKILL_FILES:
        src = root / rel
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    out.append((f, f"pkg/berkshire-deep-merged/{f.relative_to(root).as_posix()}"))
        elif src.is_file():
            out.append((src, f"pkg/berkshire-deep-merged/{rel}"))
    out.append((ctx.paths.schemas_json, "pkg/routing/output_schemas.json"))
    return out


def _skill_sections(ctx: HandlerCtx, job_type: str) -> list[Section]:
    """API 模式的規則段落：SKILL.md 不可刪；procedure／rulebook 次之；knowledge 最先刪。"""
    root = ctx.paths.skill_deep
    secs = [Section("berkshire-deep-merged/SKILL.md", (root / "SKILL.md").read_text(encoding="utf-8"))]
    if job_type in ("FULL_A", "FULL_B", "RED_TEAM"):
        for f in sorted((root / "references" / "procedure").glob("*.md")):
            secs.append(Section(f"references/procedure/{f.name}", f.read_text(encoding="utf-8"), droppable=1))
        for f in sorted((root / "references" / "knowledge").glob("*.md")):
            secs.append(Section(f"references/knowledge/{f.name}", f.read_text(encoding="utf-8"), droppable=2))
    if job_type == "FULL_A":
        q = root / "assets" / "gate3_questionnaire.json"
        secs.append(Section("assets/gate3_questionnaire.json", q.read_text(encoding="utf-8")))
    if job_type == "FULL_B":
        for n in ("signal_card_schema.json", "registry_schema.json", "decision_log_schema.json"):
            secs.append(Section(f"assets/{n}", (root / "assets" / n).read_text(encoding="utf-8")))
        doc = (root / "scripts" / "cap_calc.py").read_text(encoding="utf-8").split('"""')[1]
        secs.append(Section("scripts/cap_calc.py 輸入格式說明", doc))
    return secs


def _file_sections(sandbox: Path, names: list[str], droppable: int = 0) -> list[Section]:
    out = []
    for n in names:
        p = sandbox / n
        try:
            out.append(Section(f"輸入檔：{n}", p.read_text(encoding="utf-8"), droppable))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _sec_sections(sandbox: Path, names: list[str]) -> list[Section]:
    """DOSSIER 的 SEC 原文：10-K 依 Item 切段；業務、風險、MD&A、財報附註優先，其餘先刪。"""
    from ..analysis.dossier_prep import split_10k_items
    keep = {"1", "1A", "3", "5", "7", "7A", "8", "9A"}
    out = []
    for n in names:
        text = (sandbox / n).read_text(encoding="utf-8")
        if Path(n).name.startswith("10-K-HIST"):   # 多年業別的歷年 10-K：整份最先刪（檔案模式仍完整可讀）
            for item, body in split_10k_items(text):
                out.append(Section(f"{n} Item {item}", body, 2))
        elif Path(n).name.startswith("10-K"):
            for item, body in split_10k_items(text):
                out.append(Section(f"{n} Item {item}", body, 1 if item in keep or item == "ALL" else 2))
        else:
            out.append(Section(n, text, 1))
    return out


def _vals(job: JobSpec, backend: str, extra: dict[str, str]) -> dict[str, str]:
    """Claude Code：{pkg}／{work} 為沙箱內絕對路徑（模型只能讀沙箱）；API：相對路徑（內容已打包）。"""
    if backend == "claude_code" and job.sandbox is not None:
        v = {"pkg": str(Path(job.sandbox) / "pkg"), "work": str(job.sandbox)}
    else:
        v = {"pkg": "pkg", "work": "."}
    v.update(extra)
    return v


def _read_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _record(td: Path, decision_id: str, **kw) -> None:
    _write_json(td / "decision.json", {"decision_id": decision_id, **kw})


def _stop_log_fields(job: JobSpec, ctx: HandlerCtx) -> dict[str, Any]:
    """停止結果日誌由框架補的欄（M8-D2-04）：初篩判定與碼（screen_context）、時區（schedule.timezone）。"""
    sc = job.params.get("screen_context") or {}
    sched = getattr(getattr(ctx, "config", None), "schedule", None) or {}
    return {"screen_decision": sc.get("screen_decision"), "flag_codes": _codes(sc.get("flag_codes")),
            "distress_codes": _codes(sc.get("distress_codes")),
            "time_zone": sched.get("timezone") if isinstance(sched, dict) else None}


def _stop(job: JobSpec, ctx: HandlerCtx, phase_a: dict, notes: list[str], key: str | None = None,
          hold_unresolved: bool = False, non_analytic: bool | None = None) -> str:
    """key：post 路徑傳 job.job_id 當回寫冪等鍵（M6-D9-03）；on_fail／on_invalid 不傳（jobs retry 後要能寫新結論）。
    hold_unresolved：持股 FULL_A 退回後仍停止（A3-M3B-01）→ 不推進 last_full_review_date、next_full_review_due＝決策日＋91 天。
    non_analytic：見 writeback.write_stop_result（D80、A3-M5A-01）。"""
    from ..analysis.writeback import write_stop_result
    did = write_stop_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, phase_a, notes, _td(job, ctx),
                            log_fields=_stop_log_fields(job, ctx), job_id=key, hold_no_advance=hold_unresolved,
                            non_analytic=non_analytic)
    _record(_td(job, ctx), did, signal="NO_ACTION", reason_code=phase_a.get("reason_code") or "MISSING_DATA",
            notes=notes)
    return did


def _codes(v: Any) -> list[str]:
    """初篩的 flag_codes／distress_codes（handoff 為逗號字串）→ 清單。"""
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [x for x in str(v or "").replace(";", ",").replace(" ", ",").split(",") if x]


def _framework_gates(job: JobSpec, ctx: HandlerCtx, td: Path) -> dict[str, Any]:
    """SKILL §0A 三閘門的框架判定（與 FullBHandler.post 的 cap_signal_by_gates 同一輸入：gate3_result.json、
    cap_result.json、screen_context.screen_decision），連同問卷版本、作答檔、初篩判定、價格日、時區，
    交 write_full_result 覆寫模型日誌（M6_D7_02、M6-D5-02）。"""
    from ..analysis.checks import _is_delete, gate2_failures
    from ..pipelines.screen import UNSCREENED
    sc = job.params.get("screen_context") or {}
    g3 = _read_json(td / "gate3_result.json") if (td / "gate3_result.json").is_file() else None
    cap = _read_json(td / "cap_result.json") if (td / "cap_result.json").is_file() else None
    g2 = gate2_failures(cap) if cap is not None else ["無 cap_calc 結果"]
    hs = holding_stop(_read_json(td / "phaseA.json") if (td / "phaseA.json").is_file() else None)
    hs_fail = [f"持股版前半在 {hs['gate']} 未過（{hs.get('reason_code') or '?'}；D30）：停止加碼"] if hs else []
    ans = td / "gate3_answers.json"
    try:
        ans_rel = ans.resolve().relative_to(ctx.paths.state.resolve()).as_posix() if ans.is_file() else None
    except ValueError:
        ans_rel = str(ans)
    pf = td / "dossier" / f"{job.ticker}_price.json"
    price_date = _read_json(pf).get("price_date") if pf.is_file() else None
    sched = getattr(ctx.config, "schedule", None) or {}
    sd = sc.get("screen_decision")
    # D17：觸發升級等沒有 12 個月內初篩判定（screen_context_for 回「未篩」或缺）→ 閘門 1 通過但記警示，寫入訊號卡
    g1_warn = (f"12 個月內無初篩判定（{sd or '缺'}）：閘門 1 視為通過（D17）"
               if not sd or str(sd).startswith(UNSCREENED) else None)
    return {
        "gate1": not _is_delete(sd), "gate1_warning": g1_warn,
        "gate2": not g2, "gate2_failures": g2,
        "gate3": bool(g3) and g3.get("gate3_pass") is True and not hs,
        "gate3_failures": list((g3 or {}).get("failures") or ([] if g3 else ["缺閘門 3 結果"])) + hs_fail,
        "questionnaire_version": (g3 or {}).get("questionnaire_version"),
        "gate3_answers_file": ans_rel,
        "screen_decision": sc.get("screen_decision"),
        "flag_codes": _codes(sc.get("flag_codes")),
        "distress_codes": _codes(sc.get("distress_codes")),
        "price_date": price_date if isinstance(price_date, str) else None,
        "time_zone": sched.get("timezone") if isinstance(sched, dict) else None,
    }


def _finish(job: JobSpec, ctx: HandlerCtx, final_signal: str | None, notes: list[str], key: str | None = None,
            framework_hold: bool = False) -> str:
    """key：post 路徑傳 job.job_id 當回寫冪等鍵（M6-D9-03）；on_fail／on_invalid 不傳。
    framework_hold：FULL_B 判 BUY、框架降成 WATCH（A3-M5B-01，writeback 改 reason_code FRAMEWORK_HOLD）。"""
    from ..analysis.writeback import write_full_result
    td = _td(job, ctx)
    acct = getattr(ctx.config, "account", None)
    did = write_full_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, td, final_signal, notes,
                            gates=_framework_gates(job, ctx, td), job_id=key,
                            user_hard_cap=getattr(acct, "user_hard_cap", None),   # 登錄表 T 與 plan_buys 同口徑（M6-D10-01）
                            framework_hold=framework_hold)
    # M5_D10-02：td 的訊號卡改成實際回寫的版本（閘門／紅隊降級、對帳後），之後的 HOLD_REVIEW 才不會讀到模型原卡
    row = ctx.conn.execute("SELECT data FROM signals WHERE decision_id = ?", (did,)).fetchone()
    card = json.loads(row["data"]) if row and row["data"] else _read_json(td / "signal_card.json")
    _write_json(td / "signal_card.json", card)
    _record(td, did, signal=final_signal or card.get("signal"), notes=notes)
    return did


def _holding(job: JobSpec, ctx: HandlerCtx) -> bool:
    from .hold_jobs import is_holding
    return bool(job.params.get("holding")) or is_holding(ctx.conn, job.ticker)


def _hold_review_after(job: JobSpec, ctx: HandlerCtx, final_signal: str | None) -> list[JobSpec]:
    """持股的完整分析結束且非 BUY → HOLD_REVIEW（H2）。BUY（加碼）不需要。"""
    if final_signal == "BUY" or not _holding(job, ctx):
        return []
    return [next_job(job, "HOLD_REVIEW", ctx, _carry(job, {"full_stage": "B", "final_signal": final_signal}))]


def _missing(job: JobSpec, ctx: HandlerCtx, why: str, gate: str = "STEP0", key: str | None = None,
             non_analytic: bool | None = None) -> list[JobSpec]:
    _stop(job, ctx, {"ticker": job.ticker, "decision_date": job.asof, "stopped": True, "stopped_at_gate": gate,
                     "reason_code": "MISSING_DATA"}, [why], key, non_analytic=non_analytic)
    return []


def _hold_after_fail(job: JobSpec, ctx: HandlerCtx, why: str, gate: str) -> list[JobSpec]:
    """深度鏈無法完成且為持股 → HOLD_REVIEW（買進側無法完成不是賣出理由，比照 FULL_A 停止走 H2；M5-D1-06、M5_D4-06）。
    full_stage＝FAIL：沒有完整分析結論（hold_jobs 不取 IV_base）。同 run 該檔已有未失敗的 HOLD_REVIEW 時不再建。
    on_fail（傳輸、準備例外）只走這裡：不寫 MISSING_DATA（不動登錄表、不推進複查日、不撤買單，下次觸發照常重排）；
    非持股回傳空清單，由 engine.finalize_run 寫 WATCH（D46）。"""
    if not _holding(job, ctx) or ctx.conn.execute(
            "SELECT 1 FROM jobs WHERE run_id=? AND ticker=? AND job_type='HOLD_REVIEW' "
            "AND status NOT IN ('failed','skipped')", (job.run_id, job.ticker)).fetchone():
        return []
    return [next_job(job, "HOLD_REVIEW", ctx, _carry(job, {
        "full_stage": "FAIL", "fail_reason": why, "stopped_at_gate": gate, "reason_code": "MISSING_DATA"}))]


def _missing_hold(job: JobSpec, ctx: HandlerCtx, why: str, gate: str = "STEP0", key: str | None = None,
                  non_analytic: bool = True) -> list[JobSpec]:
    """模型輸出不完整（on_invalid、缺輸出檔；D43）：記 NO_ACTION/MISSING_DATA，持股另交 HOLD_REVIEW。key 見 _stop。
    non_analytic：預設 True＝模型輸出失敗不是對公司的判斷（D80；G6、G7 也一樣，不靠 gate 字串，A3-M5A-01）；
    資料檢查不過（D20 金融前置檢查）傳 False，維持分析性停止（失敗關閉）。"""
    _missing(job, ctx, why, gate, key, non_analytic=non_analytic)
    return _hold_after_fail(job, ctx, why, gate)


# ---------------------------------------------------------------- DOSSIER

PREP_DONE = ".prep_done"  # DOSSIER 前置全部完成的標記
META = "_meta.json"       # DOSSIER 前置記下的 SIC 與多年補充業別（D20：金融前置檢查、REIT 歸非金融）

REIT_SIC = "6798"         # 與初篩 screen.py、data/pe_median.py 同一判定（D20）


def dossier_base(td: Path, ticker: str) -> Path:
    """DOSSIER 前置組好的資料包底稿 dossier/{T}.base.md（A3-M5E-01）：post 會以模型追加版覆寫 {T}.md，
    重跑／傳輸重試須從底稿開始，否則模型再追加一次。舊資料夾沒有底稿 → 退回 {T}.md。"""
    base = td / "dossier" / f"{ticker}.base.md"
    return base if base.is_file() else td / "dossier" / f"{ticker}.md"


def _meta(td: Path) -> dict:
    f = td / "dossier" / META
    m = _read_json(f) if f.is_file() else None
    return m if isinstance(m, dict) else {}


def reit_relabel(td: Path, pa: Any) -> bool:
    """D20（M1-RS3-01）：SIC 6798 REIT 歸非金融。讀前半結果時統一改標：phaseA.asset_class 屬金融類 → 改「非金融」，
    模型原標籤留在 asset_class_model，回傳 True（呼叫端寫回 phaseA.json）。cap_input、訊號卡、登錄表、相關群都讀這一個值。"""
    from ..portfolio.correlation import is_financial
    if not isinstance(pa, dict) or str(_meta(td).get("sic") or "").strip() != REIT_SIC or not is_financial(pa.get("asset_class")):
        return False
    pa["asset_class_model"] = pa["asset_class"]
    pa["asset_class"] = "非金融"
    return True


def _reit_note(pa: Any) -> list[str]:
    m = (pa or {}).get("asset_class_model") if isinstance(pa, dict) else None
    return [f"SIC 6798 REIT 歸非金融（D20，與初篩、金融 P/E 中位一致）：前半 asset_class＝{m} 改標「非金融」"] if m else []


def financial_precheck(td: Path, dq: dict) -> str | None:
    """D20：保險／銀行（SIC 判定的多年補充業別）DOSSIER 後先查生存類必要輸入，缺 → 回傳理由（停在 STEP0，不跑 FULL_A）。
    保險：P-58 浮存金成本需 10 年窗、至少 5 個會計年度——找不到任何更早的 10-K（最新一份只涵蓋 3 年），或 DOSSIER
    missing 有「P-58」開頭項 → 缺。銀行：missing 有「P-60」開頭項 → 缺。P-59 綜合率只是加分項，不在此查。
    舊目錄沒有 _meta.json → 不查（照舊由 FULL_A STEP0 依 procedure/08 判定）。"""
    ind = _meta(td).get("industry")
    tag = {"保險": "P-58", "銀行": "P-60"}.get(ind)
    if not tag:
        return None
    hits = [str(m).strip() for m in (dq or {}).get("missing") or [] if str(m).strip().startswith(tag)]
    if ind == "保險":
        idx = td / "sec" / "_index.json"
        texts = _read_json(idx) if idx.is_file() else []
        if not any(isinstance(t, dict) and t.get("history") for t in texts or []):
            hits.insert(0, "P-58：決策日前找不到更早的 10-K，浮存金成本湊不滿 5 個會計年度")
    if not hits:
        return None
    return f"{ind}業生存類必要輸入缺（D20，不跑 FULL_A）：" + "；".join(hits)


_TRIAGE_8K = re.compile(r"8-K_\d{4}-\d{2}-\d{2}(?:_\d{1,2})?\.txt")


def _own_sec_files(td: Path) -> list[Path]:
    """DOSSIER 自己抓的原文：sec/_index.json 列出的檔＋同業節錄（_peers.json 的 peer_files）。
    同 run 其他工作寫進 td 的申報不得混入（M5_D10-01）；舊目錄沒有 _index.json 才退回整個 sec/*.txt
    （排除舊版 TRIAGE 寫在這裡的 8-K_{日期}[_n].txt；DOSSIER 自己的 8-K 檔名帶 6 碼 accession 尾碼）。"""
    sec = td / "sec"
    idx = sec / "_index.json"
    if not idx.is_file():
        return sorted(f for f in sec.glob("*.txt") if not _TRIAGE_8K.fullmatch(f.name)) if sec.is_dir() else []
    names = [t.get("file") for t in _read_json(idx) or [] if isinstance(t, dict)]
    peers = td / "dossier" / "_peers.json"
    if peers.is_file():
        names += [Path(str(f)).name for f in _read_json(peers).get("peer_files") or []]
    return sorted({sec / n for n in names if n and (sec / n).is_file()})


class DossierHandler(Handler):
    """前置：build_dossier（財報表，不含價格）＋ SEC 原文（刪價格句）。模型追加年報節錄並核對警示。"""
    job_type = "DOSSIER"

    def pre_script(self, job, ctx):
        td = _td(job, ctx)
        if (td / "dossier" / f"{job.ticker}.md").is_file() and (
                (td / "dossier" / PREP_DONE).is_file()
                or ((td / "sec" / "_index.json").is_file() and (td / "dossier" / "_sources.json").is_file()
                    and (td / "dossier" / META).is_file())):
            # 續跑：已完整準備過（只有 sec/ 目錄不算：抓取中途被中斷會留下不完整的原文集）。_sources.json 由
            # assemble 通過價格保險絲後才寫；_insiders.json 在組裝前就寫，不能當完成依據（P40R）。
            # 沒有 PREP_DONE 的舊資料夾另須 _meta.json（在 _sources.json 之後才寫；缺則 FULL_B 金融前置讀不到，A3-M4E-03）
            return
        sec = ctx.sec
        if sec is None:
            raise RuntimeError("DOSSIER 需要 SEC 客戶端（未設定 SEC_USER_AGENT？）")
        # D36：前置期間 SEC 快取新鮮度以本工作的決策日判斷（抓取日 ≥ asof 才算完整），結束還原
        prev_asof = getattr(sec, "asof", None)
        sec.asof = job.asof
        try:
            self._prep(job, ctx, sec, td)
        finally:
            sec.asof = prev_asof

    def _prep(self, job, ctx, sec, td):
        from ..analysis.dossier_prep import (DossierError, assemble, build_dossier_base, fetch_sec_texts,
                                             history_section, sec_status)
        p = job.params
        n0, w0 = len(getattr(sec, "stale", None) or []), len(getattr(sec, "warnings", None) or [])
        try:
            # D37：骨架的補充段先不寫入，與原文清單、同業、內部人段一起由 assemble 一次插入並跑價格保險絲
            base = build_dossier_base(sec, job.ticker, job.asof, td / "dossier", p.get("price"),
                                      p.get("price_date"), p.get("rates"), price_meta=p.get("price_meta"),
                                      defer_assemble=True, splits=p.get("splits"))
            base = base if isinstance(base, dict) else {}
        except DossierError as e:
            self._missing_once(job, ctx, f"資料包無法建立：{e}")
            raise
        cik = sec.cik_for(job.ticker)
        status = list(base.get("status") or [])   # 資料狀態（框架）：前身、舊快取、各段「缺：原因」（M4-D9-03）
        texts = fetch_sec_texts(sec, cik, job.asof, td / "sec", sic=base.get("sic"), notes=status)
        _write_json(td / "sec" / "_index.json", texts)
        sections = list(base.get("sections") or [])
        hist = history_section(texts, base.get("industry"))
        if hist:
            sections.append(hist)
        peer_md, universe_source = self._peers(job, ctx, sec, cik, td, texts, status)
        sections += [peer_md, self._insiders(job, sec, cik, td, status)]
        lines, stale, warns = sec_status(sec, n0, w0)
        sources = {**(base.get("sources") or {}), "universe_source": universe_source,
                   "sec_stale": stale, "sec_warnings": warns, "stale_fallback": bool(stale)}
        try:
            assemble(td / "dossier" / f"{job.ticker}.md", sections, status + lines, sources)
        except DossierError as e:
            self._missing_once(job, ctx, f"資料包無法建立：{e}")
            raise
        _write_json(td / "dossier" / META, {"sic": base.get("sic"), "industry": base.get("industry")})   # D20
        md = td / "dossier" / f"{job.ticker}.md"   # 底稿：重跑從這裡開始（A3-M5E-01）
        shutil.copyfile(md, md.with_name(f"{job.ticker}.base.md"))
        (td / "dossier" / PREP_DONE).write_text("ok\n", encoding="utf-8")

    def _insiders(self, job, sec, cik, td, status):
        """內部人交易段（Form 4 股數彙整，不含價格）：只回傳段落（D37）；失敗照實標「缺」，不擋分析。"""
        from ..analysis.insiders import build_insider_section
        try:
            # 拆股字串（'YYYY-MM-DD:比例;…'，與 prices.csv splits 同格式）：股數換到決策日基準；None＝框架未取得（M4-D4-02）
            res = build_insider_section(sec, cik, job.asof, splits=job.params.get("splits"))
            _write_json(td / "dossier" / "_insiders.json", {"stats": res["stats"], "summary": res["summary"]})
            failed = (res.get("stats") or {}).get("failed")
            if isinstance(failed, int) and failed > 0:   # 單份 Form 4 解析失敗照實標「缺」（F3-DP）
                status.append(f"缺：內部人申報 {failed} 份解析失敗")
            return res["markdown"]
        except Exception as e:  # noqa: BLE001
            status.append(f"缺：內部人交易彙整失敗（{type(e).__name__}）")
            return f"\n## 內部人交易\n\n缺：框架彙整失敗（{type(e).__name__}: {str(e)[:200]}）\n"

    def _peers(self, job, ctx, sec, cik, td, texts, status):
        """同業段（衝擊年、主要競爭者、同口徑指標）：只回傳 (段落, 股票池來源)（D37）；失敗照實標「缺」，不擋分析。"""
        from ..analysis.peers import build_peer_section, load_gics
        from ..data.universe import load_universe
        src = None
        try:
            universe, src = load_universe(ctx.paths, ctx.conn, asof=job.asof)   # 決策日當時的成分股版本（D39／M4-D10-06）
            # 一律取最新 10-K；10-K/A（常是 Part III，字數不足以判斷是否重述）只在沒有 10-K 時退用（P40R2／M4-D5-04）
            cur = [t for t in texts if not t.get("history")]
            tenk = [t for t in cur if t["form"] == "10-K"]
            if not tenk:
                tenk = [t for t in cur if t["form"] == "10-K/A"]
                if tenk:
                    status.append(f"注意：無 10-K，同業點名改讀最新 10-K/A（{tenk[-1]['file']}）")
            tenk_text = (td / "sec" / tenk[-1]["file"]).read_text(encoding="utf-8") if tenk else None
            gics = load_gics(ctx.paths.state / "cache" / "universe" / "sp500.csv")
            res = build_peer_section(sec, job.ticker, cik, job.asof, universe, tenk_text, td / "sec", gics,
                                     universe_source=src)
            _write_json(td / "dossier" / "_peers.json", {k: v for k, v in res.items() if k != "markdown"})
            status += res.get("missing") or []
            return res["markdown"], src
        except Exception as e:  # noqa: BLE001
            status.append(f"缺：同業資料補算失敗（{type(e).__name__}）")
            return f"\n## 同業資料\n\n缺：框架補算失敗（{type(e).__name__}: {str(e)[:200]}）\n", src

    def _missing_once(self, job, ctx, why):
        if not (_td(job, ctx) / "decision.json").exists():
            _missing(job, ctx, why, non_analytic=False)   # 資料包建立失敗（DossierError）：資料檢查不過，維持分析性（A3-M5A-01）

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        sec_files = _own_sec_files(td)
        items = _skill_inputs(ctx) + [(dossier_base(td, job.ticker), f"dossier/{job.ticker}.md")]   # A3-M5E-01
        items += [(f, f"sec/{f.name}") for f in sec_files]
        stage_inputs(sb, items, job.job_type)
        return lambda: (_skill_sections(ctx, job.job_type)   # D48：段落只在 API 後端才組
                        + _file_sections(sb, [f"dossier/{job.ticker}.md"])
                        + _sec_sections(sb, [f"sec/{f.name}" for f in sec_files]))

    def prompt_values(self, job, ctx, backend):
        v = _vals(job, backend, {})
        v["inputs_dir"] = f"{v['work']}/sec"
        return v

    def post(self, job, result, ctx):
        from ..analysis.dossier_prep import sanitize_text
        td = _td(job, ctx)
        md = Path(job.sandbox) / "dossier" / f"{job.ticker}.md"
        # RM2-10／M2-RS6-01：與 SEC 原文同一條清洗鏈（先遮回購股數 → 遮每股金額 → 逐句刪價格句）；procedure/08 §1
        text, st = sanitize_text(md.read_text(encoding="utf-8"))
        (td / "dossier" / f"{job.ticker}.md").write_text(text, encoding="utf-8")
        why = financial_precheck(td, result)   # 先看原文：「P-58／P-60」開頭的缺項不因清洗而漏判（D20）
        dq, n_dq = _sanitize_dq(result)   # M3-RS6-03：_dq.json 的模型文字（價格盲 FULL_A 會讀）走同一條清洗鏈
        for r in (result, dq):
            r["framework_price_lines_removed"] = st["removed_lines"] + n_dq   # 刪除句數（資料包＋_dq.json）
            r["framework_buyback_shares_masked"] = st["buyback_masked"]
            r["framework_per_share_masked"] = st["per_share_masked"]
        _write_json(td / "dossier" / f"{job.ticker}_dq.json", dq)
        if why:
            return _missing_hold(job, ctx, why, "STEP0", job.job_id, non_analytic=False)   # D20：資料檢查不過屬分析性
        return [next_job(job, "FULL_A", ctx, _carry(job))]

    def on_invalid(self, job, errors, ctx):
        return _missing_hold(job, ctx, "DOSSIER 輸出不合格：" + "；".join(errors))

    def on_fail(self, job, error, ctx):
        return _hold_after_fail(job, ctx, f"DOSSIER 無法完成：{error}", "STEP0")


def _sanitize_dq(result: dict) -> tuple[dict, int]:
    """M3-RS6-03：DOSSIER 摘要寫成 _dq.json 前，dq_checks[*].warning／result、missing[*]、sections_appended[*] 的字串
    套 sanitize_text（與資料包 .md 同一條清洗鏈）；整句被刪者留佔位。回傳 (清洗後副本, 刪除句數)。"""
    from ..analysis.dossier_prep import sanitize_text
    n = 0

    def clean(v: Any) -> Any:
        nonlocal n
        if not isinstance(v, str) or not v.strip():   # 空字串照原樣，不換成佔位（RM3-4 小瑕疵）
            return v
        t, st = sanitize_text(v)
        n += st["removed_lines"]
        return t.strip() or "〔含價格字句，框架已刪〕"

    out = dict(result)
    if isinstance(out.get("dq_checks"), list):
        out["dq_checks"] = [{**c, **{k: clean(c[k]) for k in ("warning", "result") if k in c}}
                            if isinstance(c, dict) else clean(c) for c in out["dq_checks"]]
    for key in ("missing", "sections_appended"):
        if isinstance(out.get(key), list):
            out[key] = [clean(x) for x in out[key]]
    return out, n


# ---------------------------------------------------------------- FULL_A

class FullAHandler(Handler):
    """看不到價格：G0–G6、4A、產業檢查、閘門 3 問卷。後置 gate3_check。"""
    job_type = "FULL_A"

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        md = td / "dossier" / f"{job.ticker}.md"
        if not md.is_file():
            raise FileNotFoundError(f"缺資料包 {md}")
        from ..analysis.dossier_prep import buyback_leaks, mask_buyback_shares
        if buyback_leaks(md.read_text(encoding="utf-8")):   # 舊資料包（2026-10-18 前）：G1–G6 前再遮蔽一次回購股數
            md.write_text(mask_buyback_shares(md.read_text(encoding="utf-8"))[0], encoding="utf-8")
        prior = [(td / n, f"{job.ticker}/previous/{n}") for n in job.params.get("prior_files") or []
                 if (td / n).is_file()]   # 框架退回：附上一版輸出（比照 FULL_B，A3-M5A-02）
        names = stage_inputs(sb, _skill_inputs(ctx) + [(md, f"dossier/{job.ticker}.md")] + _dq_item(td, job.ticker)
                             + prior, job.job_type)
        (sb / job.ticker).mkdir(exist_ok=True)
        return lambda: _skill_sections(ctx, job.job_type) + _file_sections(
            sb, [n for n in names if not n.startswith("pkg/")])

    def prompt_values(self, job, ctx, backend):
        sc = screen_context_for_prompt(job.params.get("screen_context") or {})   # A3-M1D-01：去 X5、刪價格鍵
        return _vals(job, backend, {"screen_context": "```json\n" + json.dumps(sc, ensure_ascii=False, indent=1)
                                    + "\n```",
                                    "is_holding": "是" if _holding(job, ctx) else "否",   # D30 持股分支
                                    "trigger": _with_red_light(trigger_text(job.params, news=True),
                                                               prior_red_light_text(ctx.conn, job.ticker, True))})

    def post(self, job, result, ctx):
        from ..analysis.checks import run_gate3
        td = _td(job, ctx)
        _shelve(job, td, TD_OUT_A)
        collect_outputs(Path(job.sandbox) / job.ticker, td, TD_OUT_A)
        reit_relabel(td, result)   # D20：收檔即改標，之後各處讀同一份
        d30 = []
        hs = holding_stop(result)
        if hs and result.get("stopped") not in (True, "true") and not _holding(job, ctx):
            # M3-RS3-02／M3-RS4-02：非持股不適用 D30 持股分支 → 依「非通過即停」改寫成停止結果
            result = {**result, "stopped": True, "stopped_at_gate": hs["gate"],
                      "reason_code": hs.get("reason_code") or "MISSING_DATA", "holding_stop": None,
                      "holding_stop_model": hs}
            d30 = [f"非持股不適用持股分支（D30）：模型填 holding_stop＝{hs.get('gate')}／{hs.get('reason_code')} "
                   "而 stopped=false，框架改為停止結果"]
        _write_json(td / "phaseA.json", result)
        hold_bad = (result.get("stopped") in (True, "true") and str(result.get("stopped_at_gate") or "") != "STEP0"
                    and _holding(job, ctx))   # A3-M3B-01：持股版 G0–G6 不通過不得停止（FULL_A.md 持股分支、D30）
        keep = {k: 1 for k in ("hold_stop_retry", "rule_ids_retry", "oe_retry") if job.params.get(k)}
        if hold_bad and not job.params.get("hold_stop_retry"):
            return [next_job(job, "FULL_A", ctx, _carry(job, {**keep, "hold_stop_retry": 1, "prior_files": FULL_A_PRIOR,
                                                              "feedback": [
                f"is_holding＝是（持股年度完整複查）：你填 stopped=true、stopped_at_gate={result.get('stopped_at_gate')!r}"
                f"、reason_code={result.get('reason_code')!r}。持股版 G0–G6、4A、產業檢查不通過不停止（D30）：請改填 stopped=false、"
                "stopped_at_gate 與 reason_code 填 null，第一個不通過處記在 holding_stop＝{gate, reason_code, reason}，"
                "其餘步驟（問卷、normalized_inputs、論點稿、report_A.md）照常做完；只有 STEP0 資料缺到無法分析時才停止"]}))]
        if result.get("stopped") in (True, "true"):
            notes = [f"FULL_A 停在 {result.get('stopped_at_gate')}"] + d30 + _reit_note(result)
            if hold_bad:
                notes.append("警示：持股版 FULL_A 退回後仍停止（應改填 holding_stop；A3-M3B-01）：本次沒有估值，"
                             "不算完成年度完整複查（last_full_review_date 不推進），next_full_review_due 設為決策日＋91 天")
            rids = validate.stop_rule_ids(result)
            if rids is None and not job.params.get("rule_ids_retry"):   # RM2-9：漏填規則編號 → 退回 FULL_A 補一次
                extra = {"rule_ids_retry": 1, "prior_files": FULL_A_PRIOR, "feedback": [
                    "stopped=true 但 rule_ids 漏填或格式不對：請列出停止所依據的規則編號（字串陣列，照 rulebook 編號，"
                    "例如 [\"SEL-061\"]；SKILL §4 NO_ACTION 必填）。停止點、停止碼與其他判斷不要改"]
                    + ([d30[0] + "（is_holding＝否：閘門不過即停，stopped=true 並填停止點與停止碼，holding_stop 填 null）"]
                       if d30 else [])}
                if job.params.get("oe_retry"):
                    extra["oe_retry"] = job.params["oe_retry"]
                if job.params.get("hold_stop_retry"):
                    extra["hold_stop_retry"] = 1
                return [next_job(job, "FULL_A", ctx, _carry(job, extra))]
            if rids is None:   # 退回後仍缺：保留模型的停止結論（不落成 MISSING_DATA），rule_ids 記 []、記警示
                notes.append(f"警示：rule_ids 缺（退回後模型仍未填停止規則編號；原值 {result.get('rule_ids')!r}）")
            result = {**result, "rule_ids": rids or []}
            _write_json(td / "phaseA.json", result)
            _stop(job, ctx, result, notes, job.job_id, hold_unresolved=hold_bad)
            if _holding(job, ctx):   # 持股：買進閘門停止不是賣出理由，改走 H2
                return [next_job(job, "HOLD_REVIEW", ctx, _carry(job, {
                    "full_stage": "A", "stopped_at_gate": result.get("stopped_at_gate"),
                    "reason_code": result.get("reason_code"),
                    **({"hold_stop_unresolved": True} if hold_bad else {})}))]
            return []
        lost = [n for n in TD_OUT_A if not (Path(job.sandbox) / job.ticker / n).is_file()]   # 只看本次沙箱（M5-D1-03）
        if lost:   # 缺 gate3_answers.json 時 FULL_B 也無法準備（M5-D1-06）
            return _missing_hold(job, ctx, f"FULL_A 未寫 {'、'.join(lost)}", "G6", job.job_id)
        g3 = run_gate3(td / "gate3_answers.json", asof=job.asof)   # 決策日以工作為準（M2_D6_05）
        _write_json(td / "gate3_result.json", g3)
        chk = _oe_check(job, td, result)
        if chk and chk["diffs"] and not job.params.get("oe_retry"):   # D26：與機械版不同 → 退回 FULL_A 一次
            return [next_job(job, "FULL_A", ctx, _carry(job, {"oe_retry": 1, "prior_files": FULL_A_PRIOR, **({"rule_ids_retry": 1} if job.params.get("rule_ids_retry") else {}), **({"hold_stop_retry": 1} if job.params.get("hold_stop_retry") else {}), "feedback": [
                "normalized_inputs 與框架機械版業主盈餘（SKILL §3 公式；只加減你在 normalized_inputs.one_time_adjustments 列出的一次性項目）"
                "差距 >10% 或剔除年與同業共同衝擊年不同：" + "；".join(chk["diffs"])
                + "。請依 SKILL §3 重新核算；差距來自一次性項目（減損、訴訟、處分、重組、證券評價損益）時，在 "
                "normalized_inputs.one_time_adjustments 逐項列出 {fy, item, amount（稅後、加到淨利的金額、單位同資料包財報表）, source}，"
                "框架會以機械版加減後重比（D73b）"]}))]
        return [next_job(job, "FULL_B", ctx, _carry(job))]

    def prompt_suffix(self, job):
        fb = job.params.get("feedback")
        if not fb:
            return ""
        prev = f"\n上一版輸出在 `{job.ticker}/previous/`。" if job.params.get("prior_files") else "\n"
        return "\n\n## 框架退回（本次為修正版）\n" + "\n".join(f"- {x}" for x in fb) + prev + "請修正後重寫全部輸出檔。"

    def on_invalid(self, job, errors, ctx):
        return _missing_hold(job, ctx, "FULL_A 輸出不合格：" + "；".join(errors), "STEP0")

    def on_fail(self, job, error, ctx):
        return _hold_after_fail(job, ctx, f"FULL_A 無法完成：{error}", "STEP0")


def _oe_check(job: JobSpec, td: Path, phase_a: dict) -> dict | None:
    """D26：資料包 10 年財報表算機械版 E、K、g10／g5，與 phaseA.normalized_inputs、同業衝擊年比對，寫 oe_check.json
    （FULL_B 讀它：仍不同 → 記註並最多 WATCH）。資料包缺表或資料不足 → 不比對（刪舊檔）、回傳 None。"""
    from ..analysis.checks import oe_check
    out = td / "oe_check.json"
    md = td / "dossier" / f"{job.ticker}.md"
    peers = td / "dossier" / "_peers.json"
    try:
        shocks = _read_json(peers).get("shock_years") if peers.is_file() else None
        ni = (phase_a or {}).get("normalized_inputs")
        chk = oe_check(md.read_text(encoding="utf-8") if md.is_file() else "", shocks, ni,
                       distress_pass=(ni if isinstance(ni, dict) else {}).get("distress_pass") is True)   # A3-M2C-06
    except Exception as e:  # noqa: BLE001  比對失敗不擋分析（照實記錄）
        chk = {"error": f"{type(e).__name__}: {str(e)[:200]}", "diffs": []}
    if chk is None:
        out.unlink(missing_ok=True)
        return None
    chk["retried"] = bool(job.params.get("oe_retry"))
    _write_json(out, chk)
    return chk


# ---------------------------------------------------------------- FULL_B

def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf"))


def _pe_for_cap(lp: Any) -> tuple[float | None, float | None, dict]:
    """D60：P4′ 的大型股 P/E 中位取凍結檔「最近年報稀釋 EPS」口徑（pe_ttm_median；rulebook 母體以最近年報稀釋 EPS 計），
    公司端用正常化 E（cap_calc 的 norm_eps）。金融中位同口徑取 fin_pe_ttm_median（M1-D10-01）；舊凍結檔沒有此鍵 → 退回
    fin_pe_norm_median，pe_field.fin_pe_fallback 標明。凍結檔缺 → 兩欄 null（P4′ 不適用）。"""
    if isinstance(lp, dict):
        field = {"company": "norm_eps", "largecap_pe_median": "pe_ttm_median"}
        if "fin_pe_ttm_median" in lp:
            fin, field["fin_pe_median"] = lp.get("fin_pe_ttm_median"), "fin_pe_ttm_median"
        else:
            fin, field["fin_pe_median"] = lp.get("fin_pe_norm_median"), "fin_pe_norm_median"
            field["fin_pe_fallback"] = "舊凍結檔沒有 fin_pe_ttm_median，金融中位退回正常化口徑（重凍後改用最近年報口徑）"
        lc = lp.get("pe_ttm_median")
    elif _finite(lp):
        lc, fin, field = float(lp), None, {"company": "norm_eps", "largecap_pe_median": "單一數字（params.largecap_pe）"}
    else:
        lc, fin, field = None, None, {"company": "norm_eps", "largecap_pe_median": "缺（P4′ 不適用）"}
    return (float(lc) if _finite(lc) else None), (float(fin) if _finite(fin) else None), field


_PHASE_A_KEYS = (("oe", "oe_per_share"), ("g10", "g10"), ("g5", "g5"))


def _phase_a_diffs(x: dict, pa: dict | None) -> list[str]:
    """M3_D6-01：cap_input 的 oe／g10／g5 與 phaseA.normalized_inputs（價格盲）相差 >1%（phaseA 為 null 的欄不檢查）。"""
    ni = (pa or {}).get("normalized_inputs") or {}
    out = []
    for ck, ak in _PHASE_A_KEYS:
        a, c = ni.get(ak), x.get(ck)
        if not _finite(a):
            continue
        if not _finite(c) or abs(c - a) > max(0.01 * max(abs(a), abs(c)), 1e-4):
            out.append(f"cap_input.{ck}={c!r}，phaseA.normalized_inputs.{ak}={a!r}")
    return out


def _phase_a_blind_gaps(pa: dict | None, td: Path, result: dict, card: dict) -> list[str]:
    """M3-RS6-01：前半未停止（且非 D30 持股停止）時，phaseA.normalized_inputs 的 oe_per_share／g10／g5 為 null 而
    cap_input 有對應數字，或 starting_grade 為 null 而後半給了 A–D 等級 → 這些值只經看過價格的 FULL_B 決定
    （_phase_a_diffs 與 phase_a_grade 都略過 null）。回傳差異說明（空＝無）。"""
    if not isinstance(pa, dict) or pa.get("stopped") in (True, "true") or holding_stop(pa):
        return []
    try:
        x = _read_json(td / "cap_input.json")
    except (OSError, ValueError):
        x = {}
    ni = pa.get("normalized_inputs") if isinstance(pa.get("normalized_inputs"), dict) else {}
    out = [f"phaseA.normalized_inputs.{ak}=null，cap_input.{ck}={x.get(ck)!r}"
           for ck, ak in _PHASE_A_KEYS if not _finite(ni.get(ak)) and isinstance(x, dict) and _finite(x.get(ck))]
    gb = [g for g in ((result or {}).get("certainty_grade"), card.get("certainty_grade")) if g in ("A", "B", "C", "D")]
    if pa.get("starting_grade") not in ("A", "B", "C", "D") and gb:
        out.append(f"phaseA.starting_grade={pa.get('starting_grade')!r}，FULL_B 等級 {gb[0]}（後半不得自定等級起點，D29）")
    return out


def _p29_writeback(card: dict, result: dict, pa: dict | None, notes: list[str]) -> None:
    """P-29 尾部上限（M3-RS3-03、D31）：以已驗證摘要 tail_worst_loss_pct（最壞損失％＝(1 − 悲觀價值 ÷ 現價)×100）
    算 max_weight_pct.tail＝10pp ÷ max(25%, 最壞損失)，覆寫模型卡值。摘要缺、不是數字或 0<v≤1（疑似小數，A3-M5C-01）→ 記警示，取模型卡值與
    P-32 減損（金融 50%／非金融 30%；缺 asset_class 視為金融）口徑兩者較嚴者（不再靜默略過、也不比 P-32 寬）。"""
    from ..portfolio import correlation as corr
    mw = card.get("max_weight_pct") if isinstance(card.get("max_weight_pct"), dict) else {}
    model = mw.get("tail")
    v = (result or {}).get("tail_worst_loss_pct")
    decimal = _finite(v) and 0 < float(v) <= 1   # A3-M5C-01：0.6 疑似把 60% 填成小數 → 不採，走保守分支
    if _finite(v) and not (0 < float(v) <= 1):
        tail = round(corr.GROUP_LOSS_BUDGET / max(corr.TAIL_FLOOR, float(v) / 100.0) * 100.0, 4)
        if model is not None and model != tail:
            notes.append(f"P-29 尾部上限以摘要 tail_worst_loss_pct={v} 計算 {tail}%，訊號卡模型值 {model!r} 覆寫")
    else:
        imp = corr.impairment(corr.is_financial((pa or {}).get("asset_class")))
        tail = round(corr.GROUP_LOSS_BUDGET / max(corr.TAIL_FLOOR, imp) * 100.0, 4)
        if _finite(model) and 0 < model < tail:
            tail = float(model)
        what = (f"={v} 疑似把小數填成百分比（0<v≤1，A3-M5C-01）" if decimal else f"缺或不是數字（{v!r}）")
        notes.append(f"警示：P-29 摘要 tail_worst_loss_pct {what}，尾部上限取訊號卡模型值 {model!r} "
                     f"與 P-32 減損 {imp:.0%} 口徑較嚴者：{tail}%")
    card["max_weight_pct"] = {**mw, "tail": tail}


def _framework_cap_input(job: JobSpec, ctx: HandlerCtx, td: Path, pa: dict | None, use_phase_a: bool,
                         notes: list[str]) -> bool:
    """cap_calc 前以框架值覆寫 cap_input.json（模型不得自選），回傳是否改到任何值；有改時模型原檔另存
    cap_input_model.json（post 據以判斷 cap_calc 與訊號卡的差異是否框架造成）：
      price、long_rate、long_rate_20y_med ← 價格檔（M3_D6-05、M6-D8-05）；
      largecap_pe_median、fin_pe_median、pe_field ← params.largecap_pe 凍結檔（D60）；
      financial ← phaseA.asset_class（M2-D8-01／M6-D8-01；非「非金融」→ true；SIC 6798 REIT 讀檔時已改標非金融，D20）；
      norm_eps、roe、eps_cagr5、incr_roic_gt_rd、insurance ← phaseA.normalized_inputs 同名鍵（M2_D4_03、M3_D2-06；
        incr_roic_gt_rd 三值 true／false／null 照傳，null＝資料不足、cap_calc 不改上限，D97；
        價格盲的前半是唯一來源；舊 phaseA 沒有該鍵則不動；insurance 只限 asset_class＝保險且三欄齊全）；
      moat_widening ← 問卷 M1–M3 全「強」且 M5＝「變寬」（M2_D4_03；作答檔讀不到則不動）；
      cap_discount_k ← 設定 valuation.cap_discount_k（ASK-1；寫回後 state/cap_inputs 與 PRICE、holdings_view 一致）；
      base_g0、opt_g0、iv_cons、iv_base、iv_opt ← 一律 null（M2_D4_03、M2-RS3-02、M2-RS7-03：看過價格的 FULL_B 不得自填；
        base／opt 走 cap_calc 依已證明成長的預設口徑；金融 IV_base 依 D20 未定義 → auto_buy_cap null，保險 IV_cons 用保險式）；
      w_rev ← true，當資料包營收 3 年或 5 年年複合 ≤0（M2-R4-01／M2-RS7-02；非金融；模型只能從嚴打開，不能關）；
      use_phase_a：oe／g10／g5 ← phaseA.normalized_inputs（M3_D6-01 退回後仍不同；phaseA 為 null 的欄不動）。"""
    from ..portfolio.correlation import is_financial
    path = td / "cap_input.json"
    try:
        x = _read_json(path)
    except (OSError, ValueError):
        return False   # 交 run_cap_calc 報錯（退回重寫）
    if not isinstance(x, dict):
        return False
    fw: dict[str, Any] = {}
    pf = td / "dossier" / f"{job.ticker}_price.json"
    pj = _read_json(pf) if pf.is_file() else {}
    for key, src in (("price", "price"), ("long_rate", "t30"), ("long_rate_20y_med", "t30_20y_median")):
        if _finite(pj.get(src)):
            fw[key] = float(pj[src])
    fw["largecap_pe_median"], fw["fin_pe_median"], fw["pe_field"] = _pe_for_cap(job.params.get("largecap_pe"))
    fin = is_financial((pa or {}).get("asset_class"))   # REIT 已由 reit_relabel 改標「非金融」（D20）
    if str(_meta(td).get("sic") or "").strip() == REIT_SIC:   # 防呆：phaseA 讀不到時 REIT 仍不算金融
        fin = False
    if fin is not None:
        fw["financial"] = fin
    fw.update(_phase_a_cap_fields(pa, notes))
    try:
        ans = (_read_json(td / "gate3_answers.json") or {}).get("answers") or {}
        a = {q: str((ans.get(q) or {}).get("answer") or "").strip() for q in ("M1", "M2", "M3", "M5")}
        fw["moat_widening"] = all(a[q] == "強" for q in ("M1", "M2", "M3")) and a["M5"] == "變寬"
    except (OSError, ValueError, AttributeError):
        pass
    kd = getattr(getattr(ctx.config, "valuation", None), "cap_discount_k", None)
    if kd:
        fw["cap_discount_k"] = dict(kd)
    fw.update(dict.fromkeys(("base_g0", "opt_g0", "iv_cons", "iv_base", "iv_opt")))
    if not fw.get("financial", x.get("financial")) and _revenue_w_rev(td / "dossier" / f"{job.ticker}.md", notes):
        fw["w_rev"] = True
    if use_phase_a:
        ni = (pa or {}).get("normalized_inputs") or {}
        fw.update({ck: float(ni[ak]) for ck, ak in _PHASE_A_KEYS if _finite(ni.get(ak))})
    changed = False
    for k, v in fw.items():
        old = x.get(k)
        # moat_widening：cap_calc 視 null＝false；incr_roic_gt_rd 三值（D97：null＝未判定、false＝確定 ≤ r_d），不得互換
        if old == v or (k == "moat_widening" and old is None and v is False):
            continue
        changed = True
        if k in ("oe", "g10", "g5"):
            notes.append(f"cap_input.{k} 退回後仍與 phaseA 不同，以價格盲的 phaseA 值覆寫：{old!r} → {v!r}")
        elif not (old is None and k in ("pe_field", "cap_discount_k")):
            notes.append(f"框架覆寫 cap_input.{k}：{old!r} → {v!r}")
        x[k] = v
    if changed:
        shutil.copyfile(path, td / "cap_input_model.json")
        _write_json(path, x)
    return changed


def _revenue_w_rev(md: Path, notes: list[str]) -> bool:
    """P-05 W-REV（M2-R4-01／M2-RS7-02）：資料包「10 年財報」營收，最近年度對 3 年前、5 年前的年複合任一 ≤0 → True
    （並記註）；表或年度缺 → False（不判定，沿用模型值）。"""
    from ..analysis.checks import _skill_module
    try:
        rev = _skill_module("deep_quant").dossier_table(md.read_text(encoding="utf-8")).get("revenue") or {}
    except (OSError, ValueError, ImportError):
        return False
    if not rev:
        return False
    last = max(rev)   # 起點 >0 時「年複合 ≤0」⇔ 終值 ≤ 起值
    bad = {n: (rev[last - n], rev[last]) for n in (3, 5) if rev.get(last - n, 0) > 0 and rev[last] <= rev[last - n]}
    if bad:
        notes.append("W-REV：資料包營收 " + "、".join(f"{n} 年（FY{last - n} {a:g} → FY{last} {b:g}）"
                                                for n, (a, b) in sorted(bad.items()))
                     + " 年複合 ≤0 → cap_input.w_rev 由框架設 true（上限只用 IV0×等級係數）")
    return bool(bad)


_PA_CAP_NUMS = ("norm_eps", "roe", "eps_cagr5")
_INS_KEYS = ("tbv_ps", "float_ps", "float_cost")


def _phase_a_cap_fields(pa: dict | None, notes: list[str]) -> dict[str, Any]:
    """phaseA.normalized_inputs 中與 cap_calc 同名的估值欄（FULL_A 契約，M2_D4_03）→ cap_input 覆寫值；沒有該鍵不回傳。"""
    ni = (pa or {}).get("normalized_inputs") or {}
    out: dict[str, Any] = {k: (float(ni[k]) if _finite(ni[k]) else None) for k in _PA_CAP_NUMS if k in ni}
    if "incr_roic_gt_rd" in ni:
        v = ni["incr_roic_gt_rd"]   # D97（A3-M2A-02）三值照傳：null／缺／無法辨識＝未判定 → null（不轉 false）
        s = str(v).strip().lower() if isinstance(v, str) else None
        out["incr_roic_gt_rd"] = True if (v is True or s == "true") else False if (v is False or s == "false") else None
    if "insurance" in ni:
        ins = ni["insurance"] if isinstance(ni["insurance"], dict) else {}
        vals = {k: (float(ins[k]) if _finite(ins.get(k)) else None) for k in _INS_KEYS}
        ok = (pa or {}).get("asset_class") == "保險" and all(v is not None for v in vals.values())
        if (pa or {}).get("asset_class") == "保險" and not ok:
            notes.append("phaseA.normalized_inputs.insurance 三欄不全，cap_input.insurance 不採用（IV_cons 不用保險式）")
        out["insurance"] = vals if ok else None
    return out


_CARD_NUM_FIELDS = (("target_weight_pct",), ("first_tranche_pct_of_target",), ("build_window_days",))
_CARD_NUM_DICTS = ("position_plan", "max_weight_pct")


def _norm_card_numbers(card: dict, give_up: bool, notes: list[str]) -> list[str]:
    """M3_D7-02：訊號卡下單用數值欄正規化（數字字串去 %、逗號轉 float）。無法轉者回傳欄名；
    give_up（已退回過一次）時改設 None 並記註。原地修改 card。"""
    bad = []
    spots = [(card, k[0], k[0]) for k in _CARD_NUM_FIELDS]
    for d in _CARD_NUM_DICTS:
        sub = card.get(d)
        if isinstance(sub, dict):
            spots += [(sub, k, f"{d}.{k}") for k, v in sub.items() if not isinstance(v, (list, dict))]
    for parent, key, label in spots:
        v = parent.get(key)
        if v is None or _finite(v):
            continue
        num = None
        if isinstance(v, str):
            try:
                num = float(v.strip().rstrip("%").replace(",", "").strip())
            except ValueError:
                num = None
        if num is not None and _finite(num):
            parent[key] = num
            notes.append(f"訊號卡 {label}={v!r} 轉為數字 {num}")
        elif give_up:
            parent[key] = None
            notes.append(f"訊號卡 {label}={v!r} 不是數字，退回後仍不合格：設為 null")
        else:
            bad.append(f"{label}={v!r}")
    return bad


def prior_drivers_text(conn, ticker: str) -> str:
    """登錄表既有的 P-31 外部變數判定（提示用）。"""
    row = conn.execute("SELECT data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    d = json.loads(row["data"]) if row and row["data"] else {}
    if d.get("external_drivers") is None:
        return "無（本檔尚未判定；依本次失效條件標註）"
    return (f"{json.dumps(d['external_drivers'], ensure_ascii=False)}（{d.get('external_drivers_determined_on') or '日期缺'} 判定；"
            f"依據 {json.dumps(d.get('external_drivers_basis') or [], ensure_ascii=False)}）。"
            "P-31：之後只能因新事實改判——不同時須在 external_drivers_change_reason 寫新事實、來源、日期，否則框架維持既有判定")


def stage_g8_view(ctx, job, td: Path) -> Path:
    """G8 比較檔（排除本標的自己）寫到 td/holdings_g8.json，回傳路徑（FULL_B、HOLD_REVIEW 共用）。
    run 沒寫比較檔（舊 run）→ 以帳本重建：收盤價與 FRED 缺，逐列標註並附 warning。"""
    from ..portfolio.holdings_view import for_ticker, holdings_g8, view_path
    src = view_path(ctx.paths, job.run_id)
    view = (_read_json(src) if src.is_file()
            else {**holdings_g8(ctx.conn, ctx.paths, job.asof, {}, None),
                  "warning": "本次 run 沒有 G8 比較檔：收盤價與國庫券、大盤缺，無法計算各列預期報酬"})
    out = td / "holdings_g8.json"
    _write_json(out, for_ticker(view, job.ticker))
    return out


class FullBHandler(Handler):
    """價格此時才給。後置：cap_calc 覆核（不一致退回一次，再不一致以腳本為準）、閘門降級、BUY → RED_TEAM。"""
    job_type = "FULL_B"

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        T = job.ticker
        items = _skill_inputs(ctx) + [(td / "dossier" / f"{T}.md", f"dossier/{T}.md")]
        for n in ("phaseA.json", "report_A.md", "gate3_result.json", "gate3_answers.json"):
            if not (td / n).is_file():
                raise FileNotFoundError(f"缺前置輸出 {n}")
            items.append((td / n, f"{T}/{n}"))
        for n in job.params.get("prior_files") or []:   # 退回重寫：附上一版輸出
            if (td / n).is_file():
                items.append((td / n, f"{T}/previous/{n}"))
        items.append((stage_g8_view(ctx, job, td), "portfolio/holdings_g8.json"))
        names = stage_inputs(sb, items + _dq_item(td, T), job.job_type)
        (sb / "dossier").mkdir(exist_ok=True)
        # 只有 gate3 答覆檔存檔早於本工作建立時間才開價格檔
        opened = open_price_file(sb / "dossier", td / "dossier" / f"{T}_price.json", td / "gate3_answers.json",
                                 now=job.params.get("created_ts"))
        (td / "price_file_opened_at.txt").write_text(opened, encoding="utf-8")   # 留存開檔時間，路徑記入決策日誌（M2-D10-04）
        price_rel = [f"dossier/{T}_price.json"]
        inputs = [n for n in names if not n.startswith("pkg/")] + price_rel
        return lambda: _skill_sections(ctx, job.job_type) + _file_sections(sb, inputs)   # D48

    def prompt_values(self, job, ctx, backend):
        pe = job.params.get("largecap_pe")
        pe_txt = json.dumps(pe, ensure_ascii=False) if pe else "缺（P4′ 不適用）"
        ps = job.params.get("portfolio_state") or {}
        return _vals(job, backend, {"portfolio_state": "```json\n" + json.dumps(ps, ensure_ascii=False, indent=1)
                                    + "\n```", "largecap_pe_median": pe_txt,
                                    "prior_external_drivers": prior_drivers_text(ctx.conn, job.ticker),
                                    "trigger": _with_red_light(trigger_text(job.params, price_blind=False, news=True),
                                                               prior_red_light_text(ctx.conn, job.ticker, False))})

    def prompt_suffix(self, job):
        fb = job.params.get("feedback")
        if not fb:
            return ""
        return ("\n\n## 框架退回（本次為修正版）\n" + "\n".join(f"- {x}" for x in fb)
                + f"\n上一版輸出在 `{job.ticker}/previous/`。請修正後重寫全部輸出檔。")

    def post(self, job, result, ctx):
        from ..analysis.checks import (apply_cap_override, cap_signal_by_gates, compare_signal_with_cap,
                                       run_cap_calc, CheckError)
        td = _td(job, ctx)
        T = job.ticker
        _shelve(job, td, TD_OUT_B + ["cap_result.json", "signal_card.model.json"])
        collect_outputs(Path(job.sandbox) / T, td, TD_OUT_B)
        _write_json(td / "phaseB.json", result)   # D31：驗證過的摘要落檔，writeback 以此為準
        missing = [n for n in TD_OUT_B if not (Path(job.sandbox) / T / n).is_file()]   # 只看本次沙箱（M5-D1-03）
        if missing:
            return self._retry_or_stop(job, ctx, [f"缺輸出檔 {missing}"])
        shutil.copyfile(td / "signal_card.json", td / "signal_card.model.json")   # 模型原卡留存（M5_D10-02）
        try:
            card = _read_json(td / "signal_card.json")
        except ValueError as e:   # JSON 壞：退回重寫一次（M5_D7_06）
            return self._retry_or_stop(job, ctx, [f"signal_card.json 不是合法 JSON：{e}"])
        if not isinstance(card, dict):
            return self._retry_or_stop(job, ctx, ["signal_card.json 不是 JSON 物件"])
        notes: list[str] = []
        g3 = _read_json(td / "gate3_result.json") if (td / "gate3_result.json").is_file() else None
        grade_capped = _cap_grade_by_c1(td, g3, card, notes)
        pa = _read_json(td / "phaseA.json") if (td / "phaseA.json").is_file() else None
        if reit_relabel(td, pa):   # 修正前收的 phaseA.json：改標後寫回（登錄表 writeback 讀檔）
            _write_json(td / "phaseA.json", pa)
        notes += _reit_note(pa)
        grade_capped = _cap_grade(td, validate.phase_a_grade(pa), "FULL_A 結束等級（後半不得升級）", card, notes) \
            or grade_capped
        # 輸入覆核（M3_D7-02 訊號卡數值欄、M3_D6-01 cap_input 與 phaseA）：不合格 → 退回一次；仍不合格 → 框架處理並記註
        retried = bool(job.params.get("input_retry")) or self._last(job, ctx)
        bad_nums = _norm_card_numbers(card, retried, notes)
        pa_diffs = _phase_a_diffs(_read_json(td / "cap_input.json"), pa) if (td / "cap_input.json").is_file() else []
        if (bad_nums or pa_diffs) and not retried:
            fb = (["訊號卡數值欄不是數字（百分比請填數字，例如 10）：" + "；".join(bad_nums)] if bad_nums else []) \
                + (["cap_input 必須沿用價格盲的 phaseA.normalized_inputs（不得看價格後改）：" + "；".join(pa_diffs)]
                   if pa_diffs else [])
            return [self._redo(job, ctx, fb, {"input_retry": 1})]
        _write_json(td / "signal_card.json", card)
        fw_changed = _framework_cap_input(job, ctx, td, pa, bool(pa_diffs), notes)
        grade_capped = _cap_grade_after_price(td, result, card, notes) or grade_capped
        # A3-M2C-01：看價後必降（資本配置淨分、P-08 最終品質；validate.check 看不到訊號卡，這裡帶卡再算一次）
        cap_ap, why_ap = validate.after_price_grade_cap(pa, result, card)
        if cap_ap:
            grade_capped = _cap_grade(td, cap_ap, "看價後必降（" + "；".join(why_ap[1:] or why_ap) + "）", card, notes) \
                or grade_capped
        if isinstance(result.get("grade_notes"), list):
            notes += [n for n in result["grade_notes"] if isinstance(n, str) and n not in notes]
        try:
            cap = run_cap_calc(td / "cap_input.json")
        except CheckError as e:
            return self._retry_or_stop(job, ctx, [f"cap_input.json 無法計算：{e}"])
        _write_json(td / "cap_result.json", cap)
        diffs = compare_signal_with_cap(card, cap)
        fw_caused = False   # 訊號卡與模型自己的輸入一致、只因框架覆寫才不同 → 直接以腳本覆寫，不退回
        if diffs and fw_changed:
            try:
                fw_caused = not compare_signal_with_cap(card, run_cap_calc(td / "cap_input_model.json"))
            except CheckError:
                fw_caused = False
        g9_missing = bool(cap.get("g9_reason_required")) and not str(card.get("g9_growth_reason") or "").strip()
        fb = (["訊號卡數字與 cap_calc 不一致（以腳本為準）：" + "；".join(diffs)]
              if diffs and not (grade_capped or fw_caused) else [])
        if g9_missing:
            fb.append("cap_calc g9_reason_required：10 年與 5 年成長差 >5pp，已證明成長取較高者，"
                      "訊號卡 g9_growth_reason 必填理由（同 G9 反方所寫）")
        if fb and not job.params.get("cap_retry") and not self._last(job, ctx):
            return [self._redo(job, ctx, fb, {"cap_retry": 1})]
        if diffs:
            notes.append("cap_calc 覆核" + ("兩次不一致" if job.params.get("cap_retry") else "（框架改了輸入或等級）"
                                            if grade_capped or fw_caused else "（已達 D47 上限，不再退回）")
                         + "，訊號卡上限與 IV 以腳本覆寫：" + "；".join(diffs))
        card = apply_cap_override(card, cap)   # M2_D9_06：一致時其餘腳本欄也一律以 cap_calc 為準
        _p30_writeback(ctx, job, card, notes)   # P-30 單事件上限由框架算（M2_D4_01）
        _p29_writeback(card, result, pa, notes)   # P-29 尾部上限取已驗證摘要（M3-RS3-03、D31）
        _write_json(td / "signal_card.json", card)
        g8_lost, g8_why = _g8_writeback(td, card, cap, result, notes)
        corr_block = _corr_writeback(ctx, T, td, card, result, notes)
        signal = result.get("signal") or card.get("signal")   # D31：以已驗證摘要為準，訊號卡只當報告
        if card.get("signal") and card.get("signal") != signal:
            notes.append(f"訊號卡訊號 {card.get('signal')} 與已驗證摘要 {signal} 不同，以摘要為準")
        gate = result.get("stopped_at_gate") or card.get("stopped_at_gate")
        if gate != card.get("stopped_at_gate"):
            card["stopped_at_gate"] = gate
            _write_json(td / "signal_card.json", card)
        sd = (job.params.get("screen_context") or {}).get("screen_decision")
        signal, why = cap_signal_by_gates(signal, g3, sd, cap)
        notes += why
        # 以下框架「BUY → WATCH」降級點：記 fw_hold，回寫時 reason_code PASS_ALL 改 FRAMEWORK_HOLD
        # （PRICE 不得再升成 ADD／BUY，須重跑完整分析；A3-M5B-01）。閘門值不改
        pre_signal = signal
        if signal == "BUY" and g8_lost:   # SKILL G8：輸了 → WATCH；差 <1pp 交模型依品質
            signal = "WATCH"
            card["stopped_at_gate"] = "G8"
            _write_json(td / "signal_card.json", card)
            notes.append("G8 機會成本未勝出，BUY → WATCH：" + "；".join(g8_why))
        if signal == "BUY" and corr_block:   # P-32：該群共同情境損失 >5% → 停止新買
            signal = "WATCH"
            card["stopped_at_gate"] = "G11"
            _write_json(td / "signal_card.json", card)
            notes.append("P-31／P-32 相關群停止新買，BUY → WATCH：" + corr_block)
        if signal == "BUY" and g9_missing:   # ASK-1：成長取高未寫理由，退回後仍缺 → G9 答不出，停在觀察
            signal = "WATCH"
            card["stopped_at_gate"] = "G9"
            _write_json(td / "signal_card.json", card)
            notes.append("10 年與 5 年成長差 >5pp 而 g9_growth_reason 退回後仍缺，BUY → WATCH")
        oe_chk = _read_json(td / "oe_check.json") if (td / "oe_check.json").is_file() else None
        if oe_chk:   # D26：機械版比對寫入日誌；退回 FULL_A 後仍不同 → 最多 WATCH
            notes.append("D26 機械版業主盈餘對照：" + json.dumps({k: oe_chk.get(k) for k in ("mechanical", "model", "g_notes", "error")
                                                             if oe_chk.get(k) is not None}, ensure_ascii=False))
            if oe_chk.get("diffs"):
                notes.append("D26 與機械版不同（已退回 FULL_A 一次）：" + "；".join(oe_chk["diffs"]))
                if signal == "BUY":
                    signal = "WATCH"
                    card["stopped_at_gate"] = "G7"
                    _write_json(td / "signal_card.json", card)
                    notes.append("D26 業主盈餘與機械版差距未解，BUY → WATCH")
        blind = _phase_a_blind_gaps(pa, td, result, card)
        if blind:   # M3-RS6-01：前半（價格盲）留 null、後半（看過價格）填了數字 → 價格盲覆核落空，最多 WATCH
            notes.append("前半價格盲欄位為 null、後半填了數字（未經價格盲覆核）：" + "；".join(blind))
            if signal == "BUY":
                signal = "WATCH"
                card["stopped_at_gate"] = "G7"
                _write_json(td / "signal_card.json", card)
                notes.append("前半價格盲輸入缺，BUY → WATCH")
        hs = holding_stop(pa)
        if hs:   # D30：持股版前半有閘門未過 → 估值照做（H2 用），買進側不加碼
            notes.append(f"持股版前半在 {hs['gate']} 未過（{hs.get('reason_code') or '?'}）：買進側不加碼（D30）")
            if signal == "BUY":
                signal = "WATCH"
                card["stopped_at_gate"] = hs["gate"]
                _write_json(td / "signal_card.json", card)
                notes.append("持股版前半未過，BUY → WATCH")
        p64 = _p64_red_light(card) if signal == "BUY" else None
        if p64:   # A3-M2C-02：訊號卡自填紅燈／延後／偏誤疊加 ≥3 → 不得照 BUY 回寫，改 NO_ACTION／RED_LIGHT（D24、D92）
            notes.append(p64)
            _write_json(td / "signal_card.json", card)
            _write_json(td / "red_light.json", {"source": "P-64", "reason": p64,
                                                "bias_count": card.get("bias_count"), "red_light": card.get("red_light"),
                                                "delay_one_cycle": card.get("delay_one_cycle")})
            _red_light(td)
            _finish(job, ctx, "NO_ACTION", notes, job.job_id)
            return _hold_review_after(job, ctx, "NO_ACTION")
        if signal == "BUY":
            return [next_job(job, "RED_TEAM", ctx, _carry(job, {
                "redteam_round": int(job.params.get("redteam_round", 0)), "notes": notes}))]
        _finish(job, ctx, signal, notes, job.job_id, framework_hold=pre_signal == "BUY" and signal == "WATCH")
        return _hold_review_after(job, ctx, signal)

    def _redo(self, job, ctx, feedback, extra):
        p = _carry(job, {k: job.params[k] for k in ("redteam_round", "cap_retry", "input_retry", "output_retry")
                         if job.params.get(k) is not None})
        p.update(extra)
        p["feedback"] = feedback
        p["prior_files"] = TD_OUT_B + ["cap_result.json", "red_team.json"]
        return next_job(job, "FULL_B", ctx, p)

    @staticmethod
    def _last(job, ctx) -> bool:
        """本 run 本檔已執行的 FULL_B（含本工作）達 D47 上限：不再退回重寫（第 4 個會被引擎擋成 failed），
        改走各自「已退回過」的保守收尾（M5-D3-05）。"""
        from .engine import MAX_FULL_B_PER_TICKER   # 局部匯入：engine 匯入 handlers→本模組
        return ctx.conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND ticker=? AND job_type='FULL_B' AND attempts>0",
                                (job.run_id, job.ticker)).fetchone()[0] >= MAX_FULL_B_PER_TICKER

    def _retry_or_stop(self, job, ctx, why):
        if not job.params.get("output_retry") and not self._last(job, ctx):
            return [self._redo(job, ctx, why, {"output_retry": 1})]
        return _missing_hold(job, ctx, "FULL_B 無法產出完整輸出：" + "；".join(why), "G7", job.job_id)

    def on_invalid(self, job, errors, ctx):
        return _missing_hold(job, ctx, "FULL_B 輸出不合格：" + "；".join(errors), "G7")

    def on_fail(self, job, error, ctx):
        return _hold_after_fail(job, ctx, f"FULL_B 無法完成：{error}", "G7")


# ---------------------------------------------------------------- RED_TEAM

def _g8_writeback(td: Path, card: dict, cap: dict, result: dict, notes: list[str]) -> tuple[bool, list[str]]:
    """訊號卡 g8_adjusted_expected_return 以腳本重算（與比較檔同一算法）；模型比較表的落差、缺列記入 notes。
    回傳 (G8 是否明顯輸, 理由)，由呼叫端決定是否降級。"""
    from ..portfolio.holdings_view import g8_notes, g8_verdict, missing_rows, self_adjusted
    adj = self_adjusted(_read_json(td / "cap_input.json"), cap)
    card["g8_adjusted_expected_return"] = adj
    _write_json(td / "signal_card.json", card)
    staged = _read_json(td / "holdings_g8.json") if (td / "holdings_g8.json").is_file() else {}
    notes += g8_notes((result or {}).get("g8"), adj, staged) + missing_rows(staged)
    return g8_verdict(adj, staged)


def _corr_writeback(ctx, ticker: str, td: Path, card: dict, result: dict, notes: list[str]) -> str | None:
    """P-31／P-32：模型標的外部變數正規化後寫進訊號卡（改判規則見 correlation.decide_drivers），
    以決策日持股權重（比較檔 market_weight）算本標的的相關群剩餘額度，只寫進 card["correlated_group"]（報告用）。
    不寫進 max_weight_pct：剩餘額度是增量、不是總權重上限，下單時由 sizing 以 buy_room 即時限制（M6-D2-02）。
    回傳停止新買的理由（P-32 >5%）或 None。"""
    from ..analysis.writeback import _registry_get
    from ..portfolio import correlation as corr
    vocab = corr.load_vocab(ctx.paths.skill_deep)
    ids, basis, warns = corr.normalize_drivers((result or {}).get("external_drivers", card.get("external_drivers")),
                                               vocab)
    notes += warns
    reg = _registry_get(ctx.conn, ticker)
    prior = (reg or {}).get("data", {}).get("external_drivers") if reg else None
    final, note = corr.decide_drivers(prior, ids, (result or {}).get("external_drivers_change_reason"))
    if note:
        notes.append(note)
    if ids is not None and final is not None and set(final) != set(ids):   # 改判被駁回：依據也用登錄表既有的（M6-D10-04）
        basis = (reg or {}).get("data", {}).get("external_drivers_basis")
    pa = _read_json(td / "phaseA.json") if (td / "phaseA.json").is_file() else {}
    card["external_drivers"] = final
    card["external_drivers_basis"] = basis
    card["external_drivers_change_reason"] = (result or {}).get("external_drivers_change_reason")
    card["asset_class"] = pa.get("asset_class") or card.get("asset_class")
    staged = _read_json(td / "holdings_g8.json") if (td / "holdings_g8.json").is_file() else {}
    weights = {r["ticker"]: float(r.get("market_weight") or 0) for r in staged.get("holdings") or []
               if r.get("ticker")}
    me = staged.get("self") or {}
    if me.get("market_weight") is not None:
        weights[ticker] = float(me["market_weight"])
    fin = corr.is_financial(card["asset_class"])
    info = {"ticker": ticker, "drivers": final, "variables": sorted(corr.variables(final, vocab)) if final is not None
            else None, "financial": fin, "impairment": corr.impairment(fin),
            "grade": card.get("certainty_grade"), "g8": card.get("g8_adjusted_expected_return"),
            "worst_loss": corr.tail_worst_loss(card)}   # P-29：本標的自己也用訊號卡 tail 反推（P44 交接）
    room = corr.buy_room(ctx.conn, ticker, {}, vocab, info=info, weights=weights)
    card["correlated_group"] = {k: room.get(k) for k in ("status", "group", "variables", "room", "reason", "warning")
                                if room.get(k) is not None}
    blocked = None
    if room["status"] == "unknown":
        notes.append(room["warning"])
    elif room["status"] == "blocked":
        blocked = room["reason"]
    _write_json(td / "signal_card.json", card)
    return blocked


P30_LOOKBACK_DAYS = 3 * 365   # 近 8 次財報約 2 年；多抓 1 年容錯（推論）


def _p30_data(ctx, ticker: str, asof: str) -> tuple[list[str], list[tuple[str, float]]]:
    """P-30 輸入（M2_D4_01）：決策日前近 8 次 8-K item 2.02（財報公布）申報日，與其間的拆股調整日收盤（只取決策日之前）。
    SEC 用 ctx.sec，價格用研究用價格客戶端（data.inputs.make_clients 的 prices）；缺客戶端或抓取失敗拋例外。"""
    from datetime import date, timedelta
    from ..data.inputs import make_clients
    from ..portfolio.sizing import P30_EVENTS
    # M4-R3-02：pipeline 注入的研究用價格客戶端（ctx.prices）優先；缺 SEC 或價格客戶端才重建
    sec, prices = ctx.sec, getattr(ctx, "prices", None)
    if sec is None or prices is None:
        cl = make_clients(ctx.config, ctx.paths) if ctx.config is not None else {}
        sec = sec if sec is not None else cl.get("sec")
        prices = prices if prices is not None else cl.get("prices")
    if sec is None or prices is None:
        raise RuntimeError("缺 SEC 或價格客戶端")
    cik = sec.cik_for(ticker)
    if not cik:
        raise RuntimeError("SEC 對照表找不到 CIK")
    a = date.fromisoformat(asof[:10])
    rows = sec.filings(cik, asof[:10], since=(a - timedelta(days=P30_LOOKBACK_DAYS)).isoformat(), forms=["8-K"])
    dates = sorted({r["filingDate"] for r in rows
                    if "2.02" in [c.strip() for c in str(r.get("items") or "").split(",")]})[-P30_EVENTS:]
    if not dates:
        return [], []
    df = prices.bars([ticker], (date.fromisoformat(dates[0]) - timedelta(days=10)).isoformat(), asof[:10],
                     adjustment="split")
    if df is None or not len(df):
        return dates, []
    return dates, [(str(d)[:10], c) for d, c in zip(df["date"], df["close"]) if str(d)[:10] < asof[:10]]


def _p30_writeback(ctx, job: JobSpec, card: dict, notes: list[str]) -> None:
    """P-30 單事件上限以框架值寫進訊號卡 max_weight_pct.single_event（模型值一律覆寫；比照 correlated_group 由框架算）。
    取不到 → null，註記「P-30 未檢查」（sizing 下單時再註一次，不靜默略過）。"""
    from ..portfolio.sizing import single_event_cap
    try:
        dates, closes = _p30_data(ctx, job.ticker, job.asof)
        cap, why = single_event_cap(dates, closes) if dates else \
            (None, "P-30 未檢查：決策日前 3 年沒有 8-K item 2.02（財報公布）申報")
    except Exception as e:   # noqa: BLE001 - 資料取不到只影響這一項上限，照實標缺
        cap, why = None, f"P-30 未檢查：財報日或日線取不到（{type(e).__name__}：{str(e)[:200]}）"
    mw = card.get("max_weight_pct") if isinstance(card.get("max_weight_pct"), dict) else {}
    if mw.get("single_event") is not None and mw.get("single_event") != cap:
        why += f"；訊號卡模型值 {mw.get('single_event')!r} 以框架值覆寫"
    card["max_weight_pct"] = {**mw, "single_event": cap}
    notes.append(why)


def _cap_grade_by_c1(td: Path, g3: dict | None, card: dict, notes: list[str]) -> bool:
    """閘門 3 的 C1（可預見年限）決定等級上限（gate3_check max_grade_from_C1）：cap_input 與訊號卡等級較高時壓低。
    回傳是否有壓低（有則 cap_calc 與訊號卡的差異是框架造成，直接以腳本覆寫、不退回重做）。"""
    return _cap_grade(td, (g3 or {}).get("max_grade_from_C1"), "閘門 3 C1 限等級最高", card, notes)


def _cap_grade(td: Path, mg: str | None, why: str, card: dict, notes: list[str]) -> bool:
    """cap_input 與訊號卡等級高於 mg 時壓低到 mg（在 cap_calc 之前）；回傳是否有壓低。
    mg 來源：閘門 3 C1 上限、FULL_A 結束等級（D29：FULL_B 不得高於前半）。"""
    from ..analysis.checks import worse_grade
    path = td / "cap_input.json"
    if mg is None or not path.is_file():
        return False
    x = _read_json(path)
    capped = False
    if x.get("grade") in ("A", "B", "C", "D") and worse_grade(x["grade"], mg) != x["grade"]:
        notes.append(f"{why} {mg}：cap_input 等級 {x['grade']} → {mg}")
        x["grade"] = mg
        _write_json(path, x)
        capped = True
    if card.get("certainty_grade") in ("A", "B", "C", "D") and worse_grade(card["certainty_grade"], mg) != card["certainty_grade"]:
        card["certainty_grade"] = mg
        _write_json(td / "signal_card.json", card)
        capped = True
    return capped


def _cap_grade_after_price(td: Path, result: dict, card: dict, notes: list[str]) -> bool:
    """M2-RS2-01：步 10 看價後補判的降級（SEL-034 例外、P-26a、4A…）只寫在摘要／訊號卡等級；cap_input.grade 須壓到
    兩者較嚴者，否則 cap_calc（auto_buy_cap 的 k、P4′ 倍數）與 state/cap_inputs（PRICE、閘門 2）用較高等級。
    摘要等級可能已含 cap_calc 自動套用的 P-21 降級：先試算，cap_calc 已降過時目標往回一級，不重複降（D22）。"""
    from ..analysis.checks import CheckError, GRADE_ORDER, run_cap_calc, worse_grade
    s = worse_grade(result.get("certainty_grade"), card.get("certainty_grade"))
    path = td / "cap_input.json"
    try:
        g_in = _read_json(path).get("grade")
        used = run_cap_calc(path).get("grade_used")
    except (OSError, ValueError, AttributeError, CheckError):
        return False   # 交後面的 run_cap_calc 報錯
    if s is None or g_in not in GRADE_ORDER or used not in GRADE_ORDER or worse_grade(s, used) == used:
        return False
    target = GRADE_ORDER[GRADE_ORDER.index(s) - 1] if used != g_in else s
    return _cap_grade(td, target, f"看價後補判等級（摘要／訊號卡 {s}" + ("，P-21 由 cap_calc 另降" if used != g_in else "")
                      + "）", card, notes)


P64_RED = 3    # P-64：同一邊偏誤 3–4 紅燈
P64_NO_GO = 5  # P-64：同一邊 ≥5 不做（暫以紅燈處理，A3-M2C-02）


def _bias_num(v: Any) -> float | None:
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            return None
    return float(v) if _finite(v) else None


def _p64_red_light(card: dict) -> str | None:
    """A3-M2C-02：訊號卡 red_light／delay_one_cycle 為 true，或 bias_count 任一邊 ≥3（P-64）→ 回傳降為 RED_LIGHT 的理由；
    否則 None。P-64 觀察名單快速例外（≤12 個月論點、現價 ≤ 上限×0.8 → 只當期以反方重跑 G9、不延後）未實作，一律紅燈。"""
    why = []
    if card.get("red_light") in (True, "true"):
        why.append("訊號卡 red_light=true")
    if card.get("delay_one_cycle") in (True, "true"):
        why.append("訊號卡 delay_one_cycle=true（P-64／P-65 延後一個決策週期）")
    bc = card.get("bias_count") if isinstance(card.get("bias_count"), dict) else {}
    hi = {k: n for k in ("pro_trade", "pro_sell_or_wait") if (n := _bias_num(bc.get(k))) is not None and n >= P64_RED}
    if hi:
        why.append("bias_count " + "、".join(f"{k}={v:g}" for k, v in hi.items()) + " ≥3（P-64 同向偏誤疊加紅燈"
                   + ("；≥5 依 P-64 為不做，暫以紅燈處理" if max(hi.values()) >= P64_NO_GO else "") + "）")
    if not why:
        return None
    return ("擬 BUY 但" + "、".join(why) + " → NO_ACTION／RED_LIGHT（P-64；A3-M2C-02）：本週期不送單，登錄表留觀察名單；"
            "P-64 觀察名單快速例外（現價 ≤ 上限×0.8 只當期重跑 G9）未實作")


def _red_light(td: Path) -> None:
    """D24：紅隊致命 → 已驗證摘要 phaseB.json 與訊號卡改為 NO_ACTION／RED_LIGHT、停在 G9（write_full_result 以摘要的
    reason_code 為準：登錄表 WATCH、next_full_review_due＝決策日＋14 天、PRICE 不得升買）；模型原摘要另存 phaseB.model.json。"""
    fix = {"signal": "NO_ACTION", "reason_code": "RED_LIGHT", "stopped_at_gate": "G9"}
    pb = td / "phaseB.json"
    if pb.is_file():
        s = _read_json(pb)
        _write_json(td / "phaseB.model.json", s)
        _write_json(pb, {**(s if isinstance(s, dict) else {}), **fix})
    else:
        _write_json(pb, fix)
    card = _read_json(td / "signal_card.json")
    _write_json(td / "signal_card.json", {**card, **fix})


class RedTeamHandler(Handler):
    """擬 BUY 才跑。不過 → 退回 FULL_B 修正一次；仍判致命 → NO_ACTION／RED_LIGHT（D24）；其他仍不過 → WATCH。"""
    job_type = "RED_TEAM"

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        T = job.ticker
        items = _skill_inputs(ctx) + [(td / "dossier" / f"{T}.md", f"dossier/{T}.md"),
                                      (td / "report.md", f"{T}/report.md"),
                                      (td / "signal_card.json", f"{T}/signal_card.json")]
        # 重算上限與正常化盈餘所需的輸入（M5-D5-03）：有才附
        items += [(td / n, f"{T}/{n}") for n in ("phaseA.json", "cap_input.json", "cap_result.json", "gate3_result.json")
                  if (td / n).is_file()]
        pf = td / "dossier" / f"{T}_price.json"
        items += ([(pf, f"dossier/{T}_price.json")] if pf.is_file() else []) + _dq_item(td, T)
        names = stage_inputs(sb, items, job.job_type)
        return lambda: _skill_sections(ctx, job.job_type) + _file_sections(
            sb, [n for n in names if not n.startswith("pkg/")])

    def prompt_values(self, job, ctx, backend):
        return _vals(job, backend, {})

    def prompt_suffix(self, job):
        notes = [str(n) for n in job.params.get("notes") or [] if str(n).strip()]
        if not notes:
            return ""
        return ("\n\n## 框架已調整（FULL_B 之後框架的覆寫與降級；訊號卡與 cap_result 已是調整後的值，"
                "這些差異不是報告的數字錯誤，不要列入 number_errors）\n" + "\n".join(f"- {n}" for n in notes))

    def post(self, job, result, ctx):
        td = _td(job, ctx)
        _write_json(td / "red_team.json", result)
        notes = list(job.params.get("notes") or [])
        bad = result.get("fatal") in (True, "true") or bool(validate.real_items(result.get("number_errors"))) \
            or bool(validate.real_items(result.get("rule_violations"))) or result.get("verdict") == "退回"   # M3_D4-03；佔位「無」不算（M3-RS4-03）
        rnd = int(job.params.get("redteam_round", 0))
        if not bad:
            result["final_signal"] = "BUY"
            _finish(job, ctx, "BUY", notes, job.job_id)
            return []
        if rnd == 0 and not FullBHandler._last(job, ctx):
            fb = ["紅隊退回：" + json.dumps({k: result.get(k) for k in ("fatal_paths", "number_errors",
                                                                       "rule_violations")}, ensure_ascii=False)]
            return [FullBHandler()._redo(job, ctx, fb, {"redteam_round": 1, "cap_retry": None})]
        if rnd == 0:   # M5-D3-05：FULL_B 已達 D47 上限（第 4 個會被引擎擋成 failed）
            notes.append("FULL_B 已達 D47 上限，紅隊退回改按第二輪處理")
        if result.get("fatal") in (True, "true"):   # D24（M3_D2-08）：修正後仍判致命 → RED_LIGHT（G9 停止碼）
            _red_light(td)
            result["final_signal"] = "NO_ACTION"
            _finish(job, ctx, "NO_ACTION", notes + ["紅隊修正後仍判致命 → NO_ACTION／RED_LIGHT（G9；D24）："
                                                     "登錄表留觀察名單，下個決策週期以反方角色重跑，不進 12 個月排除"],
                    job.job_id)
            return _hold_review_after(job, ctx, "NO_ACTION")
        result["final_signal"] = "WATCH"   # 數字錯誤／違規修正後仍不過 → 最多 WATCH
        _finish(job, ctx, "WATCH", notes + ["紅隊修正後仍不過（數字錯誤或違規），維持 WATCH"], job.job_id)
        return _hold_review_after(job, ctx, "WATCH")

    def on_invalid(self, job, errors, ctx):
        # 紅隊答不出來：不得放行 BUY，保守降為 WATCH
        _finish(job, ctx, "WATCH", ["RED_TEAM 輸出不合格，保守降為 WATCH：" + "；".join(errors)])
        return _hold_review_after(job, ctx, "WATCH")

    def on_fail(self, job, error, ctx):
        # 紅隊沒跑完（傳輸、準備失敗）：同樣不得放行 BUY；FULL_B 的結果以 WATCH 回寫
        _finish(job, ctx, "WATCH", [f"RED_TEAM 無法完成，保守降為 WATCH：{error}"])
        return _hold_review_after(job, ctx, "WATCH")
