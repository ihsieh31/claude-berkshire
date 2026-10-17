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
  phaseA.json、report_A.md、gate3_answers.json、gate3_result.json、
  cap_input.json、cap_result.json、report.md、signal_card.json、registry_entry.json、decision_log.json、thesis.md、
  red_team.json、decision.json（回寫結果）
終點（FULL_A 停止、FULL_B 非 BUY、RED_TEAM 結束、無法完成）由框架回寫 DB（analysis.writeback）。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..models import JobSpec
from .context import Section
from .handlers import Handler, HandlerCtx, next_job
from .sandbox import collect_outputs, create_sandbox, open_price_file, scrub_prices, stage_inputs, ticker_dir

SKILL_FILES = ["SKILL.md", "references", "assets", "scripts/cap_calc.py", "scripts/gate3_check.py"]
TD_OUT_A = ["report_A.md", "gate3_answers.json"]
TD_OUT_B = ["cap_input.json", "report.md", "signal_card.json", "registry_entry.json", "decision_log.json", "thesis.md"]


# ---------------------------------------------------------------- 共用

# 沿著 DOSSIER → FULL_A → FULL_B → RED_TEAM →（持股）HOLD_REVIEW 傳遞的參數
CARRY = ("screen_context", "portfolio_state", "largecap_pe", "holding", "thesis_result", "thesis_file", "reasons")


def _carry(job: JobSpec, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    p = {k: job.params.get(k) for k in CARRY if job.params.get(k) is not None}
    p.update(extra or {})
    return p


def _td(job: JobSpec, ctx: HandlerCtx) -> Path:
    return ticker_dir(ctx.work_dir, job)


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
        if Path(n).name.startswith("10-K"):
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


def _stop(job: JobSpec, ctx: HandlerCtx, phase_a: dict, notes: list[str]) -> str:
    from ..analysis.writeback import write_stop_result
    did = write_stop_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, phase_a, notes, _td(job, ctx))
    _record(_td(job, ctx), did, signal="NO_ACTION", reason_code=phase_a.get("reason_code") or "MISSING_DATA",
            notes=notes)
    return did


def _finish(job: JobSpec, ctx: HandlerCtx, final_signal: str | None, notes: list[str]) -> str:
    from ..analysis.writeback import write_full_result
    td = _td(job, ctx)
    did = write_full_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, td, final_signal, notes)
    card = _read_json(td / "signal_card.json")
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


def _missing(job: JobSpec, ctx: HandlerCtx, why: str, gate: str = "STEP0") -> list[JobSpec]:
    _stop(job, ctx, {"ticker": job.ticker, "decision_date": job.asof, "stopped": True, "stopped_at_gate": gate,
                     "reason_code": "MISSING_DATA"}, [why])
    return []


# ---------------------------------------------------------------- DOSSIER

class DossierHandler(Handler):
    """前置：build_dossier（財報表，不含價格）＋ SEC 原文（刪價格句）。模型追加年報節錄並核對警示。"""
    job_type = "DOSSIER"

    def pre_script(self, job, ctx):
        from ..analysis.dossier_prep import DossierError, build_dossier_base, fetch_sec_texts
        td = _td(job, ctx)
        if (td / "dossier" / f"{job.ticker}.md").is_file() and (td / "sec").is_dir():
            return  # 續跑：已準備過
        sec = ctx.sec
        if sec is None:
            raise RuntimeError("DOSSIER 需要 SEC 客戶端（未設定 SEC_USER_AGENT？）")
        p = job.params
        try:
            build_dossier_base(sec, job.ticker, job.asof, td / "dossier", p.get("price"), p.get("price_date"),
                               p.get("rates"))
        except DossierError as e:
            self._missing_once(job, ctx, f"資料包無法建立：{e}")
            raise
        cik = sec.cik_for(job.ticker)
        texts = fetch_sec_texts(sec, cik, job.asof, td / "sec")
        _write_json(td / "sec" / "_index.json", texts)
        self._add_peers(job, ctx, sec, cik, td, texts)
        self._add_insiders(job, sec, cik, td)

    def _add_insiders(self, job, sec, cik, td):
        """內部人交易段（Form 4 股數彙整，不含價格）附加到資料包；失敗照實標「缺」，不擋分析。"""
        from ..analysis.dossier_prep import _insert_before_text_section, scrub_price_text
        from ..analysis.insiders import build_insider_section
        md = td / "dossier" / f"{job.ticker}.md"
        try:
            res = build_insider_section(sec, cik, job.asof)
            section, _ = scrub_price_text(res["markdown"])
            _write_json(td / "dossier" / "_insiders.json", {"stats": res["stats"], "summary": res["summary"]})
        except Exception as e:  # noqa: BLE001
            section = f"\n## 內部人交易\n\n缺：框架彙整失敗（{type(e).__name__}: {str(e)[:200]}）\n"
        md.write_text(_insert_before_text_section(md.read_text(encoding="utf-8"), section), encoding="utf-8")

    def _add_peers(self, job, ctx, sec, cik, td, texts):
        """同業段（衝擊年、主要競爭者、同口徑指標）附加到資料包；失敗照實標「缺」，不擋分析。"""
        from ..analysis.dossier_prep import _insert_before_text_section
        from ..analysis.peers import build_peer_section, load_gics
        from ..data.universe import load_universe
        md = td / "dossier" / f"{job.ticker}.md"
        try:
            universe, _ = load_universe(ctx.paths, ctx.conn)
            tenk = [t for t in texts if t["form"] in ("10-K", "10-K/A")]
            tenk_text = (td / "sec" / tenk[-1]["file"]).read_text(encoding="utf-8") if tenk else None
            gics = load_gics(ctx.paths.state / "cache" / "universe" / "sp500.csv")
            res = build_peer_section(sec, job.ticker, cik, job.asof, universe, tenk_text, td / "sec", gics)
            section = res["markdown"]
            _write_json(td / "dossier" / "_peers.json", {k: v for k, v in res.items() if k != "markdown"})
        except Exception as e:  # noqa: BLE001
            section = f"\n## 同業資料\n\n缺：框架補算失敗（{type(e).__name__}: {str(e)[:200]}）\n"
        md.write_text(_insert_before_text_section(md.read_text(encoding="utf-8"), section), encoding="utf-8")

    def _missing_once(self, job, ctx, why):
        if not (_td(job, ctx) / "decision.json").exists():
            _missing(job, ctx, why)

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        sec_files = sorted(f for f in (td / "sec").glob("*.txt"))
        items = _skill_inputs(ctx) + [(td / "dossier" / f"{job.ticker}.md", f"dossier/{job.ticker}.md")]
        items += [(f, f"sec/{f.name}") for f in sec_files]
        stage_inputs(sb, items, job.job_type)
        return (_skill_sections(ctx, job.job_type)
                + _file_sections(sb, [f"dossier/{job.ticker}.md"])
                + _sec_sections(sb, [f"sec/{f.name}" for f in sec_files]))

    def prompt_values(self, job, ctx, backend):
        v = _vals(job, backend, {})
        v["inputs_dir"] = f"{v['work']}/sec"
        return v

    def post(self, job, result, ctx):
        from ..analysis.dossier_prep import scrub_price_text
        td = _td(job, ctx)
        md = Path(job.sandbox) / "dossier" / f"{job.ticker}.md"
        text, removed = scrub_price_text(md.read_text(encoding="utf-8"))
        (td / "dossier" / f"{job.ticker}.md").write_text(text, encoding="utf-8")
        result["framework_price_lines_removed"] = removed
        _write_json(td / "dossier" / f"{job.ticker}_dq.json", result)
        return [next_job(job, "FULL_A", ctx, _carry(job))]

    def on_invalid(self, job, errors, ctx):
        return _missing(job, ctx, "DOSSIER 輸出不合格：" + "；".join(errors))


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
        stage_inputs(sb, _skill_inputs(ctx) + [(md, f"dossier/{job.ticker}.md")], job.job_type)
        (sb / job.ticker).mkdir(exist_ok=True)
        return _skill_sections(ctx, job.job_type) + _file_sections(sb, [f"dossier/{job.ticker}.md"])

    def prompt_values(self, job, ctx, backend):
        sc = scrub_prices(job.params.get("screen_context") or {})
        return _vals(job, backend, {"screen_context": "```json\n" + json.dumps(sc, ensure_ascii=False, indent=1)
                                    + "\n```"})

    def post(self, job, result, ctx):
        from ..analysis.checks import run_gate3
        td = _td(job, ctx)
        collect_outputs(Path(job.sandbox) / job.ticker, td, TD_OUT_A)
        _write_json(td / "phaseA.json", result)
        if result.get("stopped") in (True, "true"):
            notes = [f"FULL_A 停在 {result.get('stopped_at_gate')}"]
            _stop(job, ctx, result, notes)
            if _holding(job, ctx):   # 持股：買進閘門停止不是賣出理由，改走 H2
                return [next_job(job, "HOLD_REVIEW", ctx, _carry(job, {
                    "full_stage": "A", "stopped_at_gate": result.get("stopped_at_gate"),
                    "reason_code": result.get("reason_code")}))]
            return []
        if not (td / "report_A.md").is_file():
            return _missing(job, ctx, "FULL_A 未寫 report_A.md", "G6")
        g3 = run_gate3(td / "gate3_answers.json")
        _write_json(td / "gate3_result.json", g3)
        return [next_job(job, "FULL_B", ctx, _carry(job))]

    def on_invalid(self, job, errors, ctx):
        return _missing(job, ctx, "FULL_A 輸出不合格：" + "；".join(errors), "STEP0")


# ---------------------------------------------------------------- FULL_B

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
        names = stage_inputs(sb, items, job.job_type)
        (sb / "dossier").mkdir(exist_ok=True)
        # 只有 gate3 答覆檔存檔早於本工作建立時間才開價格檔
        open_price_file(sb / "dossier", td / "dossier" / f"{T}_price.json", td / "gate3_answers.json",
                        now=job.params.get("created_ts"))
        price_rel = [f"dossier/{T}_price.json"]
        inputs = [n for n in names if not n.startswith("pkg/")] + price_rel
        return _skill_sections(ctx, job.job_type) + _file_sections(sb, inputs)

    def prompt_values(self, job, ctx, backend):
        pe = job.params.get("largecap_pe")
        pe_txt = json.dumps(pe, ensure_ascii=False) if pe else "缺（P4′ 不適用）"
        ps = job.params.get("portfolio_state") or {}
        return _vals(job, backend, {"portfolio_state": "```json\n" + json.dumps(ps, ensure_ascii=False, indent=1)
                                    + "\n```", "largecap_pe_median": pe_txt})

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
        collect_outputs(Path(job.sandbox) / T, td, TD_OUT_B)
        missing = [n for n in TD_OUT_B if not (td / n).is_file()]
        if missing:
            return self._retry_or_stop(job, ctx, [f"缺輸出檔 {missing}"])
        card = _read_json(td / "signal_card.json")
        notes: list[str] = []
        g3 = _read_json(td / "gate3_result.json") if (td / "gate3_result.json").is_file() else None
        grade_capped = _cap_grade_by_c1(td, g3, card, notes)
        try:
            cap = run_cap_calc(td / "cap_input.json")
        except CheckError as e:
            return self._retry_or_stop(job, ctx, [f"cap_input.json 無法計算：{e}"])
        _write_json(td / "cap_result.json", cap)
        diffs = compare_signal_with_cap(card, cap)
        if diffs:
            if not job.params.get("cap_retry") and not grade_capped:
                return [self._redo(job, ctx, ["訊號卡數字與 cap_calc 不一致（以腳本為準）：" + "；".join(diffs)],
                                   {"cap_retry": 1})]
            card = apply_cap_override(card, cap)
            _write_json(td / "signal_card.json", card)
            notes.append("cap_calc 覆核兩次不一致，訊號卡上限與 IV 以腳本覆寫：" + "；".join(diffs))
        signal = card.get("signal") or result.get("signal")
        if result.get("signal") and result.get("signal") != signal:
            notes.append(f"摘要訊號 {result.get('signal')} 與訊號卡 {signal} 不同，以訊號卡為準")
        sd = (job.params.get("screen_context") or {}).get("screen_decision")
        signal, why = cap_signal_by_gates(signal, g3, sd, cap)
        notes += why
        if signal == "BUY":
            return [next_job(job, "RED_TEAM", ctx, _carry(job, {
                "redteam_round": int(job.params.get("redteam_round", 0)), "notes": notes}))]
        _finish(job, ctx, signal, notes)
        return _hold_review_after(job, ctx, signal)

    def _redo(self, job, ctx, feedback, extra):
        p = _carry(job, {k: job.params.get(k) for k in ("redteam_round", "cap_retry")})
        p.update(extra)
        p["feedback"] = feedback
        p["prior_files"] = TD_OUT_B + ["cap_result.json", "red_team.json"]
        return next_job(job, "FULL_B", ctx, p)

    def _retry_or_stop(self, job, ctx, why):
        if not job.params.get("output_retry"):
            return [self._redo(job, ctx, why, {"output_retry": 1})]
        return _missing(job, ctx, "FULL_B 無法產出完整輸出：" + "；".join(why), "G7")

    def on_invalid(self, job, errors, ctx):
        return _missing(job, ctx, "FULL_B 輸出不合格：" + "；".join(errors), "G7")


# ---------------------------------------------------------------- RED_TEAM

def _cap_grade_by_c1(td: Path, g3: dict | None, card: dict, notes: list[str]) -> bool:
    """閘門 3 的 C1（可預見年限）決定等級上限（gate3_check max_grade_from_C1）：cap_input 與訊號卡等級較高時壓低。
    回傳是否有壓低（有則 cap_calc 與訊號卡的差異是框架造成，直接以腳本覆寫、不退回重做）。"""
    from ..analysis.checks import worse_grade
    mg = (g3 or {}).get("max_grade_from_C1")
    path = td / "cap_input.json"
    if mg is None or not path.is_file():
        return False
    x = _read_json(path)
    capped = False
    if x.get("grade") in ("A", "B", "C", "D") and worse_grade(x["grade"], mg) != x["grade"]:
        notes.append(f"閘門 3 C1 限等級最高 {mg}：cap_input 等級 {x['grade']} → {mg}")
        x["grade"] = mg
        _write_json(path, x)
        capped = True
    if card.get("certainty_grade") in ("A", "B", "C", "D") and worse_grade(card["certainty_grade"], mg) != card["certainty_grade"]:
        card["certainty_grade"] = mg
        _write_json(td / "signal_card.json", card)
        capped = True
    return capped


class RedTeamHandler(Handler):
    """擬 BUY 才跑。不過 → 退回 FULL_B 修正一次；仍不過 → WATCH。"""
    job_type = "RED_TEAM"

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        T = job.ticker
        items = _skill_inputs(ctx) + [(td / "dossier" / f"{T}.md", f"dossier/{T}.md"),
                                      (td / "report.md", f"{T}/report.md"),
                                      (td / "signal_card.json", f"{T}/signal_card.json")]
        names = stage_inputs(sb, items, job.job_type)
        return _skill_sections(ctx, job.job_type) + _file_sections(sb, [n for n in names if not n.startswith("pkg/")])

    def prompt_values(self, job, ctx, backend):
        return _vals(job, backend, {})

    def post(self, job, result, ctx):
        td = _td(job, ctx)
        _write_json(td / "red_team.json", result)
        notes = list(job.params.get("notes") or [])
        bad = result.get("fatal") in (True, "true") or bool(result.get("number_errors")) \
            or result.get("verdict") == "退回"
        rnd = int(job.params.get("redteam_round", 0))
        if not bad:
            result["final_signal"] = "BUY"
            _finish(job, ctx, "BUY", notes)
            return []
        if rnd == 0:
            fb = ["紅隊退回：" + json.dumps({k: result.get(k) for k in ("fatal_paths", "number_errors",
                                                                       "rule_violations")}, ensure_ascii=False)]
            return [FullBHandler()._redo(job, ctx, fb, {"redteam_round": 1, "cap_retry": None})]
        result["final_signal"] = "WATCH"
        _finish(job, ctx, "WATCH", notes + ["紅隊修正後仍不過，維持 WATCH"])
        return _hold_review_after(job, ctx, "WATCH")

    def on_invalid(self, job, errors, ctx):
        # 紅隊答不出來：不得放行 BUY，保守降為 WATCH
        _finish(job, ctx, "WATCH", ["RED_TEAM 輸出不合格，保守降為 WATCH：" + "；".join(errors)])
        return _hold_review_after(job, ctx, "WATCH")
