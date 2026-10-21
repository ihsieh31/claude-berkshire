"""觸發檢查的工作處理器：TRIAGE（初判）、PRICE（機械快速通道）、THESIS（論點檢查）。

價格隔離：TRIAGE、THESIS 的沙箱與提示都不含價格（PRICE_ISOLATED_JOBS）；PRICE 可讀價格與 cap_calc 結果。
規則來源：routing/prompts/{TRIAGE,PRICE,THESIS}.md、routing/model_routing.json、SKILL.md §0A／§6。
升級：一律帶原工作 params 的完整副本；PRICE、THESIS 以外的工作拿掉 created_ts（由 next_job 重設）。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import db
from ..analysis import writeback
from ..analysis.checks import ABSURD_PE, _is_delete, absurd_line, run_cap_calc, worse_grade
from ..analysis.dossier_prep import (earnings_exhibit, external_header, is_price_headline, sanitize_filing_text,
                                     scrub_price_sentences, split_10k_items)
from ..models import PRICE_ISOLATED_JOBS, JobSpec
from ..paths import Paths
from .context import ContextOverflowError, Section
from .deep import _file_sections, _pe_for_cap, _read_json, _skill_inputs, _td, _vals, _write_json, trigger_text
from .handlers import Handler, HandlerCtx, next_job
from .sandbox import create_sandbox, stage_inputs

TRIAGE_8K_CHARS = 30_000
THESIS_FILE_CHARS = 40_000
THESIS_TOTAL_CHARS = 150_000
THESIS_FORMS = ("10-Q", "10-Q/A", "10-K/A", "8-K", "8-K/A")  # review_due 對 10-K/A、8-K/A 也升 THESIS（M3-D5-03）
IGNORE_8K = {"9.01", "5.07"}   # 與 skill review_due.py 同：只附展品、股東會投票結果的 8-K 不需讀（M5-D5-06）
THESIS_STATUS = "_status.txt"    # 申報未抓取（無 SEC 客戶端／查無 CIK）時寫原因（M5_D10-07）
THESIS_OMITTED = "_omitted.txt"  # 因篇幅未附的申報清單（M5-D5-06）
GATE3_MAX_DAYS = 365    # 閘門 3 有效期（PRICE.md：≤ 12 個月）


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
    """論點檔寫進沙箱 thesis/（逐句刪價格字句，再逐行刪估值數字行：IV、上限、安全邊際、P/E…；M6-D6-01）；回傳沙箱內相對檔名。"""
    names = []
    for p in thesis_files(paths, job.ticker, job.params.get("thesis_file")):
        text = p.read_text(encoding="utf-8")
        if getattr(job, "job_type", None) in PRICE_ISOLATED_JOBS:
            text = h2_whitelist(text)   # A3-M6D-04：H2 增補只留白名單欄位（黑名單過濾漏估值倍數、G8 pp）
        text = writeback.drop_valuation_sections(text)   # 估值段先整段去數字（D79）
        text = writeback.scrub_valuation_lines(scrub_price_sentences(text)[0])
        names.append(_write_scrubbed(sb, f"thesis/{p.name}", text))
    return names


def _cut(text: str, n: int) -> str:
    """截到 n 字；有截斷時文末標明原長（M5-D5-06：不得靜默截斷）。"""
    return text if len(text) <= n else text[:n] + f"\n（原文已截斷，原長 {len(text)} 字，只附前 {n} 字）"


def _kill_criteria(params: dict) -> list[str]:
    """失效條件（價格盲工作用）：逐條刪價格字句，再套論點檔同一估值行過濾（IV、上限、安全邊際…；M3-RS6-02），
    整條被刪者標明已刪（M5_D6-04）。"""
    out = []
    for x in writeback.thesis_kill_criteria(params):   # 與證偽核對同一清單、同一順序（K1、K2…；D02）
        t = writeback.scrub_valuation_lines(scrub_price_sentences(str(x))[0]).strip()
        out.append(t if t else KC_DELETED)
    return out


KC_DELETED = "（含價格數字，已刪）"
_KC_ID = re.compile(r"\s*K(\d+)\s*[：:]")


def map_kill_status(result: dict, params: dict) -> dict:
    """A3-M5D-02：THESIS 模型照抄的是價格盲版失效條件（_kill_criteria 改寫過或整條刪掉），回寫端
    （writeback._thesis_kill_due、證偽核對）以登錄表原文比對 → 改寫過的條目永遠比對不到、不記 checked_on。
    這裡依序號把 kill_criteria_status 的 criterion 換回原文：K 編號前綴 → 價格盲版或原文全等 →
    （條數相同且已對上的都在原位時）依位置。整條被刪的價格型條目不換（THESIS 沒看到，不得記為已檢查；
    交看得到價格的 PRICE，見 price_kill_due）。不改傳入物件。"""
    ks = result.get("kill_criteria_status") if isinstance(result, dict) else None
    raw = writeback.thesis_kill_criteria(params)
    if not isinstance(ks, list) or not ks or not raw:
        return result
    shown = _kill_criteria(params)
    norm = writeback._norm_criterion
    idx: list[int | None] = []
    used: set[int] = set()
    for k in ks:
        c = str(k.get("criterion") or "") if isinstance(k, dict) else ""
        i = None
        m = _KC_ID.match(c)
        if m and 1 <= int(m.group(1)) <= len(raw):
            i = int(m.group(1)) - 1
        elif c:
            n = norm(c)
            i = next((j for j in range(len(raw)) if j not in used and n in (norm(shown[j]), norm(raw[j]))), None)
        if i in used:
            i = None
        if i is not None:
            used.add(i)
        idx.append(i)
    if len(ks) == len(raw) and all(i is None or i == j for j, i in enumerate(idx)):
        idx = [j if i is None and j not in used else i for j, i in enumerate(idx)]
    out = []
    for k, i in zip(ks, idx):
        if isinstance(k, dict) and i is not None and shown[i] != KC_DELETED and k.get("criterion") != raw[i]:
            k = {**k, "criterion": raw[i]}
        out.append(k)
    return {**result, "kill_criteria_status": out}


def price_kill_due(params: dict) -> list[str]:
    """本次因到期觸發、但價格盲版整條刪掉的失效條件原文（THESIS 看不到；A3-M5D-02）：THESIS 判完好後
    比照 also_price 續接 PRICE（PRICE 看得到價格與登錄表原文，g10_still_valid 以本次數字核失效條件）。"""
    raw, shown = writeback.thesis_kill_criteria(params), _kill_criteria(params)
    due = [str(r) for r in params.get("reasons") or [] if "失效條件到期檢查" in str(r)]
    return [x for x, s in zip(raw, shown) if s == KC_DELETED and any(x.strip() and x.strip() in r for r in due)]


def _thesis_value(job: JobSpec, backend: str, paths: Paths) -> str:
    rels = [f"thesis/{p.name}" for p in thesis_files(paths, job.ticker, job.params.get("thesis_file"))]
    return "、".join(_rel_or_abs(job, backend, r) for r in rels) or "（無論點檔）"


def _gate3_status(conn, ticker: str, asof: str) -> str:
    """閘門 3 狀態：取該檔最近一次 FULL 決策（排除 PRICE／THESIS 列）的 gate3_pass。
    回傳 通過 | 不通過 | 已過期。已作廢（data.voided_at，`trader decisions void`）的不算。"""
    return _gate3_info(conn, ticker, asof)[0]


def _gate3_info(conn, ticker: str, asof: str) -> tuple[str, str | None]:
    """(_gate3_status 的狀態, 來源決策編號；沒有 FULL 決策為 None)。PRICE 日誌記來源（M6-D5-03）。"""
    rows = conn.execute(
        "SELECT decision_id, decision_date, data FROM decisions WHERE ticker = ? AND decision_date <= ? "
        "AND json_extract(data, '$.voided_at') IS NULL ORDER BY decision_date DESC, created_at DESC",
        (ticker, asof),  # 時點上界：補跑過去日期時不得用到之後的決策
    ).fetchall()
    for r in rows:
        d = json.loads(r["data"] or "{}")
        if d.get("review_type") or d.get("gate3_pass") is None:
            continue
        if d.get("gate3_pass") not in (True, "true"):
            return "不通過", r["decision_id"]
        age = (date.fromisoformat(asof) - date.fromisoformat(r["decision_date"])).days
        return ("通過" if age <= GATE3_MAX_DAYS else "已過期"), r["decision_id"]
    return "不通過", None


RECENT_FULL_DAYS = 90   # 完整分析當時已看到「現價 < 上限」仍未判 BUY：90 天內不因 PRICE 再重跑（推論：約一季）


def _last_full_card(conn, ticker: str, asof: str) -> tuple[str, dict] | None:
    """最近一次完整分析（決策日誌無 review_type）的 (決策日, 訊號卡)；略過已作廢的決策。"""
    last = _last_full(conn, ticker, asof)
    return None if last is None else (last[0], last[1])


def _last_full(conn, ticker: str, asof: str) -> tuple[str, dict, dict] | None:
    """同 _last_full_card，另附該次決策日誌（框架寫的 price、auto_buy_cap；M5-RS7-02）。"""
    for r in conn.execute(
            "SELECT d.decision_date, d.data AS log, s.data AS card FROM decisions d JOIN signals s "
            "ON s.decision_id = d.decision_id WHERE d.ticker = ? AND d.decision_date <= ? "
            "AND json_extract(d.data, '$.voided_at') IS NULL ORDER BY d.decision_date DESC, d.created_at DESC",
            (ticker, asof)):
        log = json.loads(r["log"] or "{}")
        if not log.get("review_type"):
            return r["decision_date"], json.loads(r["card"] or "{}"), log
    return None


def _full_already_saw_buyable(conn, ticker: str, asof: str) -> str | None:
    """近 RECENT_FULL_DAYS 天的完整分析，當時現價已低於自動買進上限卻沒判 BUY → 回傳該日期。
    價格與上限讀框架寫的決策日誌（price＝cap_input 收盤、auto_buy_cap＝cap_calc），不讀模型填的訊號卡 price（D31；M5-RS7-02）。"""
    last = _last_full(conn, ticker, asof)
    if last is None:
        return None
    d, card, log = last
    if log.get("cap_not_applied"):   # 該次完整分析不給上限（no_cap／賣光日前資料）：非價位判斷，不節流（A3-M6C-03）
        return None
    px, cap = log.get("price"), log.get("auto_buy_cap")
    if card.get("signal") == "BUY" or not isinstance(px, (int, float)) or not isinstance(cap, (int, float)):
        return None
    if px < cap and (date.fromisoformat(asof) - date.fromisoformat(d)).days <= RECENT_FULL_DAYS:
        return d
    return None


def _held_under_year(first_buy: str | None, asof: str) -> bool:
    """建倉未滿一年（日曆比較，與 HOLD_REVIEW 覆核共用 hold_jobs.held_years，A3-M5C-07）；不知道建倉日：保守視為未滿一年。"""
    from .hold_jobs import held_years   # hold_jobs 匯入本模組，延後匯入避免循環
    n = held_years(first_buy, asof)
    return n is None or n < 1


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _framework_action(holding: bool, passed: bool, *, weight: float | None, target: float | None) -> tuple[str, list[str]]:
    """PRICE 的動作由框架依三閘門算出（設計決策 D28：模型的 proposed_action 只記日誌）。回傳 (action, 理由)。
    PRICE 沒有賣出動作：賣出側只決定要不要交 HOLD_REVIEW（_sell_route；設計決策 D03）。"""
    if not holding:
        return ("BUY", []) if passed else ("WATCH", [])
    if not passed:
        return "HOLD", []
    if not _num(weight):
        return "HOLD", ["權重缺，無法確認未達目標權重：不加碼"]
    if not _num(target) or target <= 0:
        # 目標權重未評（最近完整分析非 BUY、未走到 G11 → T＝0／缺）：比照非持股 BUY 交完整分析（M3-R7-02）
        return "ADD_CANDIDATE", ["目標權重未評（登錄表 target_weight 缺或 0，最近完整分析未走到 G11）：比照買進候選，"
                                 "交完整分析走 G11（近期完整分析已看過可買價位者除外）"]
    if weight >= target:
        return "HOLD", [f"權重 {weight:.2%} 已達目標權重 {target:.2%}，不加碼"]
    return "ADD", []


def _sell_route(price: float, cap: dict, prior_sell_review_above: Any, first_buy: str | None,
                asof: str) -> tuple[bool, list[str]]:
    """持股的賣出側（SKILL §6、規則書 P-25、P-48；設計決策 D01／D03）。PRICE 不下賣單，回傳 (是否交 HOLD_REVIEW, 理由)。
    - 現價 ≥ 荒謬級線 → 交 HOLD_REVIEW（第一年內也送：停止加碼＋完整複查，回寫端再擋價格類賣出；D01）。
    - 現價 ≥ IV_base：建倉未滿一年 → 不送（不因價格因素賣出）；現價 < 上次 sell_review_above（只因長債使 IV_base 下修）
      → 不送，只停止加碼（P-25）；其餘交 HOLD_REVIEW 依賣出決策樹判斷（SKILL §6(2)）。"""
    iv, absurd = cap.get("IV_base"), cap.get("absurd_price_line")
    if _num(absurd) and price >= absurd:
        return True, [f"現價 {price} ≥ 荒謬級線 {absurd}：PRICE 不下單，交 HOLD_REVIEW 決定（D03；建倉未滿一年只停止加碼並複查，D01）"]
    if not (_num(iv) and price >= iv):
        return False, []
    if _held_under_year(first_buy, asof):
        return False, [f"現價 {price} ≥ IV_base {iv}，但建倉未滿一年（首次建倉 {first_buy or '缺'}）：不因價格因素賣出，"
                       "只停止加碼（SKILL §6）"]
    if _num(prior_sell_review_above) and price < prior_sell_review_above:
        return False, [f"現價 {price} ≥ 新 IV_base {iv}，但低於上次 sell_review_above {prior_sell_review_above}（只因 IV_base 下修）："
                       "只停止加碼，不送賣出檢視（規則書 P-25）"]
    return True, [f"現價 {price} ≥ IV_base {iv}：PRICE 不下單，交 HOLD_REVIEW 依賣出決策樹判斷（含換股／資金用途；SKILL §6(2)）"]


# ---------------------------------------------------------------- TRIAGE

class TriageHandler(Handler):
    """8-K 原文（刪價格）＋ 新聞標題 → 是／否／不確定。escalate → THESIS；否則依 base_action 處理。"""
    job_type = "TRIAGE"

    def pre_script(self, job, ctx):
        td = _td(job, ctx)
        shutil.rmtree(td / "triage_sec", ignore_errors=True)   # 同 run 重跑（retry）：舊 8-K 檔不得殘留進沙箱（A3-M5C-03）
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
            except Exception:  # noqa: BLE001 - 單筆原文抓取失敗只影響該項；next_jobs 見 text_missing 一律升 THESIS（A3-M3C-01）
                it["text_missing"] = True
                continue
            text, _ = sanitize_filing_text(raw)  # 單一入口：刪段＋遮回購／每股金額＋逐句刪（M4-D6-4）
            base = f"8-K_{it.get('date')}"
            seen[base] = seen.get(base, 0) + 1
            name = f"{base}.txt" if seen[base] == 1 else f"{base}_{seen[base]}.txt"
            # 寫 td/triage_sec/（不寫 sec/：那是 DOSSIER 的原文區；M5_D10-01）；沙箱內仍放 sec/
            # 首行外部文件標頭（_COMMON.md 第 6 條；與資料包同一函式，M4-R8-04）
            _write_scrubbed(td, f"triage_sec/{name}",
                            f"{external_header('8-K', it.get('date'))}\n{_cut(text, TRIAGE_8K_CHARS)}")
            it["text_file"] = f"sec/{name}"
        job.params["triage"] = items  # 只在記憶體中標註，提示用

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        names = _stage_thesis(sb, job, ctx.paths)
        src = td / "triage_sec"
        sec_names = sorted(p.name for p in src.glob("*.txt")) if src.is_dir() else []
        stage_inputs(sb, [(src / n, f"sec/{n}") for n in sec_names], job.job_type)
        names += [f"sec/{n}" for n in sec_names]
        return _file_sections(sb, names)

    def prompt_values(self, job, ctx, backend):
        # 提示不帶 url（A3-M3D-01 (3)：新聞網址常含標題與價格字眼、模型不需要；8-K 原文已由 pre_script 依 url 抓入 sec/）
        # 新聞標題以 FULL_A 同一保守濾網（is_price_headline blind=True）過濾，命中改略記、保留項數（A3-M5B-04：
        # 漏項會被 next_jobs 判「漏答」強升 THESIS）
        items = [{k: (_blind_headline(v) if k == "headline" else v) for k, v in it.items() if k != "url"}
                 if isinstance(it, dict) else it for it in job.params.get("triage") or []]
        kc = _kill_criteria(job.params)
        return _vals(job, backend, {
            "thesis_file": _thesis_value(job, backend, ctx.paths),
            "kill_criteria": "\n".join(f"- {k}" for k in kc) or "（無）",
            "triage_items": "```json\n" + json.dumps(items, ensure_ascii=False, indent=1) + "\n```"
            if items else "（無）",
            "tickers": job.ticker,
        })

    def post(self, job, result, ctx):
        # 不收沙箱檔：TRIAGE 沒有檔案產出，收檔會讓模型植入 cap_result.json 等到同檔輸出區（M3-D8-03）
        return self.next_jobs(job, result, ctx)

    def next_jobs(self, job, result, ctx):
        # escalate 不只信模型：任一項答 是／不確定，或漏答（回答項數 < 輸入項數）也升級（M3_D1-01、M3_D4-04）
        answers = [it.get("answer") for it in result.get("items") or [] if isinstance(it, dict)]
        n_in = len([it for it in job.params.get("triage") or [] if isinstance(it, dict)])
        must = any(a in ("是", "不確定") for a in answers) or len(answers) < n_in
        # 8-K 原文沒抓到（text_missing）：模型只看標題答「否」不可信，推進複查日後該 8-K 落在新窗口外永不再讀
        # → 一律升 THESIS（THESIS 會重抓原文；A3-M3C-01）
        missing = [it for it in job.params.get("triage") or []
                   if isinstance(it, dict) and it.get("type") == "8-K" and it.get("text_missing")]
        if missing and result.get("escalate") is not True:
            result["framework_note"] = (f"{len(missing)} 份 8-K 原文未取得（text_missing），不能以標題判否："
                                        "框架改為升級 THESIS")
        elif must and result.get("escalate") is not True:
            result["framework_note"] = (f"escalate 與 items 不一致（{len(answers)}/{n_in} 項有答，含 是／不確定 或漏答）："
                                        "框架改為升級 THESIS")
        must = must or bool(missing)
        if result.get("escalate") is True or must:
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
        try:   # D59：判否也記一筆決策日誌；在工作完成處寫（retry、run-approved、續跑完成的 TRIAGE 都記，A3-M5B-05）
            _log_triage_hold(job, result, ctx)
        except Exception as e:  # noqa: BLE001 - 日誌補記失敗不影響初判結果（不因此升級）；記在工作結果供人工補
            ctx.conn.rollback()
            result["triage_log_error"] = f"{type(e).__name__}: {e}"
        return []

    def on_invalid(self, job, errors, ctx):
        # 初判答不出來視同「不確定」→ 升級 THESIS（deps 清空，避免被失敗的 TRIAGE 連帶跳過）
        nj = _upgrade(job, "THESIS", ctx, {"triage_invalid": True})
        nj.deps = []
        return [nj]

    def on_fail(self, job, error, ctx):
        # API 後端 8-K 過多 → 提示超出上下文（ContextOverflowError），重跑也一樣：視同「不確定」→ 升級 THESIS
        # （THESIS 有總量上限；A3-M5C-05）。傳輸等暫時性失敗維持 failed，留給 jobs retry（升級後原工作不再可重設）
        if not str(error).startswith(ContextOverflowError.__name__):
            return []
        nj = _upgrade(job, "THESIS", ctx, {"triage_failed": str(error)[:500]})
        nj.deps = []
        return [nj]


def _log_triage_hold(job: JobSpec, result: dict, ctx: HandlerCtx) -> str | None:
    """設計決策 D59（procedure/13 §1.3 持有也要記）：TRIAGE 判否、無後續工作 → 寫一筆決策日誌：持股 HOLD、
    觀察名單 WATCH、其他 NO_ACTION，review_type=TRIAGE。冪等鍵 data.source_job_id＝本工作（重跑不重寫）。回傳決策編號。"""
    conn, t = ctx.conn, job.ticker
    if writeback._done_for_job(conn, job.job_id) is not None:
        return None
    p = job.params
    reg = conn.execute("SELECT status FROM registry WHERE ticker=?", (t,)).fetchone()
    status = reg["status"] if reg else p.get("status")
    action = "HOLD" if status == "HOLDING" or p.get("holding") else ("WATCH" if status == "WATCH" else "NO_ACTION")
    why = "；".join(f"{it.get('ref', '')}：{it.get('answer', '')}（{it.get('reason', '')}）"
                   for it in result.get("items") or [] if isinstance(it, dict))
    fields = writeback._load_schema(ctx.paths, "decision_log_schema.json").get("fields") or {}
    did = writeback.next_decision_id(conn, t, job.asof)
    log = {k: None for k in fields if not str(k).startswith("_")}
    log.update({
        "decision_id": did, "decision_date": job.asof, "ticker": t, "action": action, "review_type": "TRIAGE",
        "thesis_check": "不適用", "review_trigger_hit": list(p.get("reasons") or []),
        "reason_one_liner": "觸發初判：不需深入檢查，沿用上次結論" + (f"。{why}" if why else ""),
        "price": p.get("price"), "price_date": p.get("price_date"),
        "framework_notes": [f"TRIAGE 工作 {job.job_id}：escalate=false，只更新複查日（D59）"],
        "source_job_id": job.job_id,
    })
    writeback._write_rows(conn, [("decisions", {
        "decision_id": did, "ticker": t, "decision_date": job.asof, "run_id": job.run_id, "job_id": None,
        "signal": action, "reason_code": None, "data": log, "created_at": db.now_iso()}, None)])
    return did


# ---------------------------------------------------------------- PRICE

def _g8_check_add(ctx, job: JobSpec, g8_self: float | None, notes: list[str]) -> str:
    """PRICE 加碼的 G8 勝負檢查（procedure/06 §2；與 FULL_B 同一判準）：明顯輸 → HOLD。
    比較檔用本次 run 的 holdings_g8.json（排除本標的自己）；沒有比較檔只記錄，不擋（與 FULL_B 缺列一致），
    但本標的自己算不出調整後預期報酬仍算輸。"""
    from ..portfolio.holdings_view import for_ticker, g8_verdict, missing_rows, view_path
    src = view_path(ctx.paths, job.run_id)
    if src.is_file():
        view = for_ticker(_read_json(src), job.ticker)
        notes.extend(missing_rows(view))
    else:
        view = {}
        notes.append("本次 run 沒有 G8 比較檔：加碼只檢查本標的能否算出調整後預期報酬")
    lost, why = g8_verdict(g8_self, view)
    if lost:
        notes.append("G8 機會成本未勝出，不加碼（改 HOLD）：" + "；".join(why))
        return "HOLD"
    return "ADD"


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
        stale: list[str] = []   # M5-D5-02：利率缺不得靜默沿用 cap_inputs 的舊值
        for key, src in (("long_rate", "t30"), ("long_rate_20y_med", "t30_20y_median")):
            if rates.get(src) is not None:
                x[key] = float(rates[src])
            else:
                stale.append(f"長債缺（rates.{src}）：沿用 cap_inputs 的 {key}={x.get(key)}（上次估值時的值）")
        # D60：與完整分析同一口徑（deep._pe_for_cap）——大型股中位取凍結檔 pe_ttm_median、金融取 fin_pe_ttm_median（舊凍結檔退回 fin_pe_norm_median）
        # 並記來源（pe_field）；凍結檔缺 → 兩欄 null（P4′ 不適用），不沿用 cap_inputs 裡舊的中位
        x["largecap_pe_median"], x["fin_pe_median"], x["pe_field"] = _pe_for_cap(p.get("largecap_pe"))
        # 等級取 cap_inputs 與登錄表較保守者（HOLD_REVIEW grade_after 只降登錄表；M3-RS2-01）：閘門 2 與上限用最新等級
        reg_g = ((writeback._registry_get(ctx.conn, job.ticker) or {}).get("data") or {}).get("grade")
        g = worse_grade(x.get("grade"), reg_g)
        if g is not None and g != x.get("grade"):
            stale.append(f"cap_inputs 等級 {x.get('grade')!r} 比登錄表 {reg_g!r} 寬：改用較保守的 {g!r}")
            x["grade"] = g
        # 只更新 price、long_rate、PE 中位（routing：PRICE pre_script）；寫在本次工作目錄，state 的原輸入不動
        td = _td(job, ctx)
        _write_json(td / "cap_input.json", x)
        cap = run_cap_calc(td / "cap_input.json")
        cap["absurd_price_line"] = absurd_line(x)
        # 模型看得到本次實際用的估值輸入；利率沿用舊值 → 交完整分析（post 不回寫 t30_at_valuation）
        cap["inputs_used"] = {k: x.get(k) for k in ("long_rate", "long_rate_20y_med", "largecap_pe_median",
                                                    "fin_pe_median", "pe_field")}
        cap["inputs_used"]["t30_date"] = rates.get("t30_date")
        if x.get("largecap_pe_median") is None and x.get("fin_pe_median") is None:
            stale.append("P/E 中位缺：P4′ 不適用（D60，不沿用舊中位）")
        if stale:
            cap["framework_notes"] = stale
            cap["rates_stale"] = rates.get("t30") is None   # 只有本次長債缺才升級；20 年中位缺只註記
        _write_json(td / "cap_result.json", cap)

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        stage_inputs(sb, [(td / "cap_result.json", "cap_result.json")], job.job_type)
        card = writeback.latest_signal(ctx.conn, job.ticker, job.asof)   # 資料日上界（A3-M6B-03）
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
        card = writeback.latest_signal(ctx.conn, T, asof)   # 資料日上界：補跑舊資料日不讀未來的卡（A3-M6B-03）
        screen = (p.get("screen_context") or {}).get("screen_decision")
        if not screen or str(screen).startswith("未篩"):   # 查不到初篩全池判定 → 沿用上次訊號卡（M1-D7-4）
            screen = ((card or {}).get("data") or {}).get("screen_decision")

        gate1 = not _is_delete(screen)
        auto = cap.get("auto_buy_cap")
        grade_ok = cap.get("grade_used") in ("A", "B")  # C 級最多觀察（cap_calc conclusion_hint）
        # D90（A3-M2B-01）：cap_calc 不再輸出 conclusion_test（E×0.9 腿已併入 auto_buy_cap＝0.95×原始上限），沒有翻轉條件
        if auto is None:
            gate2 = "無上限"
        else:
            gate2 = "通過" if (price < auto and grade_ok) else "不通過"
        gate3, gate3_src = _gate3_info(ctx.conn, T, asof)
        passed = gate1 and gate2 == "通過" and gate3 == "通過"

        action, notes = _framework_action(holding, passed, weight=rdata.get("weight"), target=rdata.get("target_weight"))
        # 建倉日：帳本優先、登錄表欄位與 data 次之（M7-D1-03：broker 只寫登錄表欄位）
        to_review, sell_notes = _sell_route(price, cap, rdata.get("sell_review_above"),
                                            writeback.first_buy_date(ctx.conn, T), asof) if holding else (False, [])
        proposed = str(result.get("proposed_action") or "") or None
        notes = ["auto_buy_cap、IV_base（sell_review_above）、荒謬級線以 cap_calc 覆寫；動作由框架算出"
                 f"（模型 proposed_action＝{proposed or '未填'}，只記日誌；設計決策 D28）"] + notes + sell_notes

        # 框架覆寫模型的數字（不信模型的數字）；result 會被存進 jobs.result
        result.update({
            "price": price, "auto_buy_cap": auto, "sell_review_above": cap.get("IV_base"),
            "absurd_price_line": cap.get("absurd_price_line"), "framework_action": action,
            "framework_gates": {"gate1": gate1, "gate2": gate2, "gate3": gate3}, "model_proposed_action": proposed,
        })

        # G9、G10 不得跳過（P-63；D28 只採信 needs_full、理由、G9 反方簡答與 G10 是否仍成立）：G10 不再成立或 G9 空白 → 完整分析
        g9g10 = result.get("g10_still_valid") is False or \
            ("g9_contrarian" in result and not str(result.get("g9_contrarian") or "").strip())
        if g9g10:
            result["framework_g9g10"] = "G10 三件不再成立或 G9 反方簡答空白：交完整分析（P-63、設計決策 D28）"
        # PRICE.md 的機械條件框架亦自行檢查，不只靠模型 needs_full（M3-RS5-01、M3-RS2-01）：
        # cap_calc 所用等級／上次訊號卡等級與登錄表等級不同（如 HOLD_REVIEW grade_after 只降登錄表）。
        # 舊「結論測試翻轉 → 完整分析」路徑已刪（D90：cap_calc 不再輸出 conclusion_test，翻轉恆不發生）。
        # grade_used 已在 pre_script 取與登錄表較保守者；訊號卡等級在完整分析回寫時與登錄表同步，故不會每次 PRICE 都升級
        reg_g = rdata.get("grade")
        card_g = ((card or {}).get("data") or {}).get("certainty_grade")
        grade_diff = bool(reg_g) and any(g and g != reg_g for g in (cap.get("grade_used"), card_g))
        if grade_diff:
            result["framework_mechanical_full"] = (
                f"等級不同（cap_calc {cap.get('grade_used')}、訊號卡 {card_g}、登錄表 {reg_g}）：框架交完整分析")
        needs_full = bool(result.get("needs_full")) or g9g10 or gate3 == "已過期" or bool(cap.get("rates_stale")) \
            or grade_diff
        if cap.get("rates_stale"):
            result["framework_note"] = "長債缺、cap_calc 沿用舊利率：不回寫，交完整分析（M5-D5-02）"
        if needs_full:
            return [_upgrade(job, "DOSSIER", ctx)]  # 需要判斷的題目：交給完整分析，不在此寫結論
        if action in ("BUY", "ADD_CANDIDATE"):
            # 新部位：觀察卡沒有走到 G8–G11（目標權重、尾部與單事件上限），也沒經過 RED_TEAM（SKILL §4、§5）
            # → 交給完整分析決定；持股目標權重未評同理（M3-R7-02）。
            # 近期完整分析已看過可買價位仍未判 BUY 者，不重跑（避免每兩週燒強模型）
            seen = _full_already_saw_buyable(ctx.conn, T, asof)
            if seen is None:
                result["framework_action"] = ("BUY_CANDIDATE" if action == "BUY" else "ADD_CANDIDATE") + " → 完整分析"
                return [_upgrade(job, "DOSSIER", ctx)]
            action = "WATCH" if action == "BUY" else "HOLD"
            notes.append(f"{seen} 的完整分析當時現價已低於上限仍未判 BUY：{RECENT_FULL_DAYS} 天內不重跑，維持 {action}")
            result["framework_action"] = action

        from ..portfolio.holdings_view import self_adjusted
        x = _read_json(td / "cap_input.json")
        t30 = x.get("long_rate")
        g8_self = self_adjusted(x, cap)
        if action == "ADD":
            # 持股加碼同樣套 90 天節流（M5-RS7-01）：完整分析當時已見低於上限價卻未判 BUY（如 FULL_B 框架降級只改訊號、
            # 日誌閘門仍全過）→ PRICE 不得自行加碼
            seen = _full_already_saw_buyable(ctx.conn, T, asof)
            if seen is not None:
                action = "HOLD"
                notes.append(f"{seen} 的完整分析當時現價已低於上限仍未判 BUY：{RECENT_FULL_DAYS} 天內 PRICE 不加碼，維持 HOLD")
            else:
                action = _g8_check_add(ctx, job, g8_self, notes)
            result["framework_action"] = action
        p30_card = None
        if action in ("BUY", "ADD"):   # P-30 只在建倉與加碼時檢查，以下單時近 8 次財報計（M2-R4-02）
            from .deep import _p30_writeback
            p30_card = {}
            _p30_writeback(ctx, job, p30_card, notes)
        writeback.write_price_result(
            ctx.conn, ctx.paths, T, asof, job.run_id, action=action, gate1=gate1, gate2=gate2, gate3=gate3,
            p30_card=p30_card,
            cap=cap, price=price, price_date=p.get("price_date"), t30=t30,
            t30_date=(p.get("rates") or {}).get("t30_date"),   # 訊號卡 long_rate 值與日期同步（M5-D5-02）
            reason=str(result.get("reason") or ""),
            notes=notes + list(cap.get("framework_notes") or []),   # 20 年中位沿用等框架註記進日誌（M5-D5-02）
            g8_adjusted=g8_self,
            basis={"gate3_source": gate3_src, "weight": rdata.get("weight"), "target_weight": rdata.get("target_weight")},
            job_id=job.job_id,   # 冪等鍵（M6-D9-03）
            defer_review_price=bool(to_review),   # 交 HOLD_REVIEW：review_price 待其成功才寫，失敗時下期照常重排（D46、RS8-01）
        )
        # 賣出只由 HOLD_REVIEW 決定（設計決策 D03、D28）：荒謬級、≥ IV_base（扣掉第一年與 P-25）交 HOLD_REVIEW 走賣出決策樹；
        # 模型提不提 SELL_HALF／SELL_REVIEW 都不影響
        if to_review:
            return [_upgrade(job, "HOLD_REVIEW", ctx, {"full_stage": "PRICE", "price": price})]
        return []

    def on_invalid(self, job, errors, ctx):
        # 不合格的 PRICE 也要留一筆「閘門未過」的 PRICE 紀錄：否則加碼會沿用更早那筆全過的閘門紀錄（標準決策編號，M5-D1-08）
        writeback.write_price_invalid(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, job.job_id, list(errors),
                                      "PRICE 輸出不合格")
        return []

    def on_fail(self, job, error, ctx):
        # 非格式失敗（缺現價、缺 cap_inputs、傳輸錯誤…）同樣不可讓更早全過的閘門紀錄繼續生效（M5_D4-06）
        writeback.write_price_invalid(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, job.job_id, [error],
                                      "PRICE 無法完成")
        return []


# ---------------------------------------------------------------- THESIS

def _excerpt(sec, f: dict) -> str:
    """單份申報的節錄：sanitize_filing_text 隔離價格；10-Q 只留 MD&A（Item 2）及其後，找不到則取前段；
    每份至多 40,000 字（截斷時文末標明原長）。"""
    raw = sec.filing_text(f["url"])
    if str(f.get("form", "")).startswith("8-K"):
        raw += earnings_exhibit(sec, f["url"], f.get("items"))
    text, _ = sanitize_filing_text(raw)  # 單一入口（M4-D6-4）
    if str(f.get("form", "")).startswith("10-Q"):
        items = split_10k_items(text)
        codes = [c for c, _ in items]
        if "2" in codes:
            text = "".join(body for _, body in items[codes.index("2"):])
    return _cut(text, THESIS_FILE_CHARS)


def _routine_8k(f: dict) -> bool:
    """只有 9.01／5.07 項目碼的 8-K（review_due 也不送初判）：不佔 THESIS 篇幅。"""
    codes = {c.strip() for c in str(f.get("items") or "").split(",") if c.strip()}
    return str(f.get("form", "")).startswith("8-K") and bool(codes) and codes <= IGNORE_8K


class ThesisHandler(Handler):
    """論點檢查：論點檔＋上次分析後的新申報節錄（10-Q、8-K）＋ TRIAGE 結果 → 完好／疑似／證偽。"""
    job_type = "THESIS"

    def pre_script(self, job, ctx):
        td = _td(job, ctx)
        out = td / "thesis_sec"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        # 抓不到申報不得靜默：寫 _status.txt（提示據此要求至少疑似，回寫不推進 last_review_date；M5_D10-07）
        if ctx.sec is None:
            _write_scrubbed(out, THESIS_STATUS, "未抓取：未設定 SEC 客戶端，本次沒有讀任何新申報\n")
            return
        last = str(job.params.get("last_review_date") or "")[:10] or None
        cik = job.params.get("cik") or ctx.sec.cik_for(job.ticker)
        if not cik:
            _write_scrubbed(out, THESIS_STATUS, f"未抓取：查無 {job.ticker} 的 CIK，本次沒有讀任何新申報\n")
            return
        # 申報日＝上次分析日的文件不在上次分析內，與 review_due 同窗口 last ≤ 申報日 < asof（M2_D6_04）；
        # SecClient.filings 的 since 是嚴格晚於，故傳前一天
        since = (date.fromisoformat(last) - timedelta(days=1)).isoformat() if last else None
        found = ctx.sec.filings(cik, job.asof, since=since, forms=list(THESIS_FORMS))
        found = [f for f in found if f.get("url") and f["filingDate"] < job.asof
                 and (not last or f["filingDate"] >= last) and not _routine_8k(f)]
        # 總量有上限（M5-D5-06）：10-Q／10-K/A 優先，再 8-K；各自由舊到新、整份放得下才附（不附殘段）。
        # 放不下的列進 _omitted.txt；回寫只把 last_review_date 推進到最早未附者的申報日，下次從那裡續讀。
        budget = THESIS_TOTAL_CHARS
        chosen: list[tuple[dict, str]] = []
        omitted: list[dict] = []
        for f in sorted(found, key=lambda f: (str(f.get("form", "")).startswith("8-K"), f["filingDate"])):
            text = _excerpt(ctx.sec, f)
            if len(text) > budget:
                omitted.append(f)
                continue
            budget -= len(text)
            chosen.append((f, text))
        if omitted:
            _write_scrubbed(out, THESIS_OMITTED, "因篇幅未附（未讀，不得據以判完好）：\n" + "".join(
                f"- {f['form']} {f['filingDate']} {f['url']}\n" for f in sorted(omitted, key=lambda f: f["filingDate"])))
        for f, text in sorted(chosen, key=lambda ft: ft[0]["filingDate"]):
            tag = str(f["form"]).replace("/", "").replace(" ", "")
            acc_tail = str(f.get("accessionNumber") or "").replace("-", "")[-6:]
            name = f"{tag}_{f['filingDate']}_{acc_tail}.txt"
            _write_scrubbed(out, name, f"# {f['form']} {f['filingDate']} {f['url']} "
                                       f"{external_header(f['form'], f['filingDate'])}\n{text}")   # M4-R8-04

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
            _write_json(sb / "triage_result.json", blind_triage_result(tr))   # THESIS 價格盲（A3-M3D-01 (2)）
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
            "kill_criteria": "\n".join(f"- K{i}：{k}" for i, k in enumerate(kc, 1)) or "（無）",
            "last_review_date": job.params.get("last_review_date") or "（無紀錄）",
            "r_list": r_list_text(ctx.paths.skill_deep),
            "news": _news_text(job.params, blind=True),   # 價格盲：與 FULL_A 同濾網（RM4-2）
            "trigger": trigger_text(job.params),   # M5-D5-07（價格盲：不列 [PRICE]、含數字只留類別）
            "sec_status": _sec_status(_td(job, ctx)),
        })

    def post(self, job, result, ctx):
        adv = _review_advance(_td(job, ctx))
        result = map_kill_status(result, job.params)   # A3-M5D-02：價格盲版條文換回原文，回寫端才比對得到
        result = _reconcile_thesis(result, len(_kill_criteria(job.params)), adv)
        result = writeback.vet_falsification(result, writeback.thesis_kill_criteria(job.params), job.asof)   # D02
        writeback.write_thesis_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result, job.params,
                                      advance_review=adv, job_id=job.job_id)
        return self.next_jobs(job, result, ctx)

    def on_invalid(self, job, errors, ctx):
        # 答不出來當成「疑似」（停止加碼，並交給完整分析）；不可留下沒有 review_type 的紀錄讓更早的「完好」繼續生效
        return self._suspect(job, ctx, "THESIS 輸出不合格，視為疑似：" + "；".join(errors)[:500])

    def on_fail(self, job, error, ctx):
        # 非格式失敗（抓申報、準備、傳輸）比照不合格：記疑似、停止加碼、交完整分析（M5_D4-06）
        return self._suspect(job, ctx, f"THESIS 無法完成，視為疑似：{error[:500]}")

    def _suspect(self, job, ctx, reason):
        # 機械原因的疑似（M5-R5-01）：不開 P-50 觀察期；沒有可信的申報審閱 → last_review_date 不推進（M5-R3-01、M5-R5-02）
        result = {"verdict": "疑似", "reason": reason, "kill_criteria_status": [], "r_list_status": [], "new_facts": [],
                  "framework_suspect": True}
        writeback.write_thesis_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result, job.params,
                                      advance_review=False)
        nj = _upgrade(job, "DOSSIER", ctx, {"thesis_result": result})
        nj.deps = []
        return [nj]

    def next_jobs(self, job, result, ctx):
        from .hold_jobs import is_holding
        holding = bool(job.params.get("holding")) or is_holding(ctx.conn, job.ticker)
        if holding and result.get("verdict") == "證偽":
            return []   # D02(a)：核對通過已寫 SELL_ALL，自下一交易日執行；不升完整分析
        if holding and result.get("falsify_downgrade"):
            # D02(b)：證偽核對不過或屬判斷類 → 疑似（已停止加碼）；同 run 交 HOLD_REVIEW（中模型）由 Q2 決定，另升完整分析（SKILL §7）
            return [_upgrade(job, "HOLD_REVIEW", ctx, {"full_stage": "THESIS", "thesis_result": result}),
                    _upgrade(job, "DOSSIER", ctx, {"thesis_result": result})]
        if result.get("verdict") in ("疑似", "證偽"):
            return [_upgrade(job, "DOSSIER", ctx, {"thesis_result": result})]
        # 完好：同期被 THESIS 遮蔽的 PRICE 型觸發（買點、加碼、賣出檢視、荒謬級、長債）續跑 PRICE（M2_D7_01）；
        # TRIAGE 升級來的 THESIS 原本的 base_action=PRICE 同理。沒有 cap_inputs 無法快速重算 → 完整分析（同 TRIAGE）
        # 到期的價格型失效條件被價格盲版整條刪掉（THESIS 沒看到）→ 同 also_price 交 PRICE（A3-M5D-02）
        if job.params.get("also_price") or job.params.get("base_action") == "PRICE" or price_kill_due(job.params):
            if _cap_input_path(ctx.paths, job.ticker).is_file():
                return [_upgrade(job, "PRICE", ctx)]
            return [_upgrade(job, "DOSSIER", ctx)]
        return []


def _sec_status(td: Path) -> str:
    """提示用：申報抓取狀態（M5_D10-07、M5-D5-06）。"""
    out = td / "thesis_sec"
    if (out / THESIS_STATUS).is_file():
        return ((out / THESIS_STATUS).read_text(encoding="utf-8").strip()
                + f"（見 thesis_sec/{THESIS_STATUS}）：沒有申報檔不代表查無新申報，結論至少「疑似」")
    if (out / THESIS_OMITTED).is_file():
        n = sum(1 for ln in (out / THESIS_OMITTED).read_text(encoding="utf-8").splitlines() if ln.startswith("- "))
        return f"另有 {n} 份申報因篇幅未附（清單見 thesis_sec/{THESIS_OMITTED}）：未讀的申報不得據以判完好"
    return "已抓取，該期間的申報全部附上"


def _review_advance(td: Path) -> bool | str:
    """write_thesis_result 的 advance_review：未抓取 → False；有未附 → 最早未附者的申報日；否則 True。"""
    out = td / "thesis_sec"
    if (out / THESIS_STATUS).is_file():
        return False
    if (out / THESIS_OMITTED).is_file():
        dates = [ln.split()[2] for ln in (out / THESIS_OMITTED).read_text(encoding="utf-8").splitlines()
                 if ln.startswith("- ") and len(ln.split()) > 2]
        return min(dates) if dates else False
    return True


def _reconcile_thesis(result: dict, n_kc: int, advance: bool | str = True) -> dict:
    """「完好」與逐條狀態不一致 → 改「疑似」（不自動升成證偽；M3_D1-02）：任一失效條件已觸發或接近、任一 R 項惡化，
    失效條件回答列數少於輸入條數（M3_D4-04），或本次申報未抓取／未全附（advance＝_review_advance 結果不是 True；
    THESIS.md：結論至少疑似、未讀的申報不得據以判完好；M3-RS1-01）。"""
    if result.get("verdict") != "完好":
        return result
    ks = [k for k in result.get("kill_criteria_status") or [] if isinstance(k, dict)]
    why = []
    if any(k.get("status") == "已觸發" for k in ks):
        why.append("有失效條件已觸發")
    if any(k.get("status") == "接近" for k in ks):
        why.append("有失效條件接近觸發")
    if advance is not True:
        why.append("本次申報未抓取" if advance is False else "本次申報未全附")
    if any(isinstance(r, dict) and r.get("status") == "惡化" for r in result.get("r_list_status") or []):
        why.append("有 R 項惡化")
    if len(ks) < n_kc:
        why.append(f"失效條件只答 {len(ks)}/{n_kc} 條")
    if not why:
        return result
    # 只因申報未抓取／未全附而改判：機械原因，回寫不開 P-50 觀察期（仍交完整分析；M5-R5-01）
    mech = {"framework_suspect": True} if advance is not True and len(why) == 1 else {}
    return {**result, **mech, "verdict": "疑似",
            "reason": f"〔框架：模型判完好但{'、'.join(why)}，改為疑似〕" + str(result.get("reason") or "")}


TRIAGE_TEXT_KEYS = ("ref", "headline", "touches", "reason")
BLIND_OMITTED = "〔含價格／估值字眼，已略（價格盲）〕"


def _blind_text(v: Any) -> Any:
    """價格盲文字：整句命中 FULL_A 保守濾網（is_price_headline blind=True）→ 換成略記；否則逐句刪價格字句。"""
    if not isinstance(v, str) or not v.strip():
        return v
    if is_price_headline(v, blind=True):
        return BLIND_OMITTED
    return writeback.scrub_valuation_lines(scrub_price_sentences(v)[0]).strip() or BLIND_OMITTED


TRIAGE_HEADLINE_OMITTED = "（含價格字樣，略）"


def _blind_headline(v: Any) -> Any:
    """TRIAGE 提示的新聞標題（價格盲）：整則命中 is_price_headline(blind=True) → 略記；否則逐句刪價格字句（刪光也略記）。"""
    if not isinstance(v, str) or not v.strip():
        return v
    if is_price_headline(v, blind=True):
        return TRIAGE_HEADLINE_OMITTED
    return scrub_price_sentences(v)[0].strip() or TRIAGE_HEADLINE_OMITTED


_H2_HEAD = re.compile(r"^#\s*\S+\s+持股完整複查 H2\b")
_H2_ACTION = re.compile(r"^-\s*動作：\s*([A-Z_]+)")
_H2_KEEP = re.compile(r"^-\s*被否定的假設：|^-\s*Q2(?![0-9])")
_H2_STOP = re.compile(r"停止加碼\s*(是|否)")
_H2_OBS = re.compile(r"觀察至\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|無)")


def h2_whitelist(text: str) -> str:
    """A3-M6D-04：H2 論點增補（writeback._hold_review_text 寫的「# T 持股完整複查 H2 日期」檔）給價格盲工作的白名單版：
    只留標題、動作（不帶賣出類別）＋停止加碼＋觀察期限、被否定的假設、決策樹 Q2（論點／業務）列；
    理由與 Q0／Q1／Q3–Q6（估值倍數、G8 pp、較成本漲幅等）只在決策日誌。不是 H2 增補 → 原樣。"""
    lines = text.splitlines()
    if not lines or not _H2_HEAD.match(lines[0].strip()):
        return text
    out = [lines[0].strip(), ""]
    for ln in lines[1:]:
        s = ln.strip()
        m = _H2_ACTION.match(s)
        if m:
            stop, obs = _H2_STOP.search(s), _H2_OBS.search(s)
            out.append(f"- 動作：{m.group(1)}（停止加碼 {stop.group(1) if stop else '不明'}；"
                       f"觀察至 {obs.group(1) if obs else '無'}）")
        elif s.startswith("## 決策樹路徑"):
            out += ["", s]
        elif _H2_KEEP.match(s):
            out.append(s)
    out.append("（理由與其餘決策樹題目只記在決策日誌，價格盲工作不附）")
    return "\n".join(out) + "\n"


def blind_triage_result(tr: Any) -> Any:
    """THESIS（價格盲）沙箱的 triage_result.json（A3-M3D-01 (2)）：TRIAGE 模型轉述的標題／理由可能含評等、估值、
    股價字眼。items 的 ref／headline／touches／reason 以 FULL_A 同一濾網過濾（命中 → 略記，保留 answer 讓 THESIS 知道
    哪幾項被判「是／不確定」），其餘字句刪價格句；頂層 reason／framework_note 同。url 一律不帶。不改傳入物件。"""
    if not isinstance(tr, dict):
        return tr
    out = {k: v for k, v in tr.items() if k != "url"}
    for k in ("reason", "framework_note"):
        if k in out:
            out[k] = _blind_text(out[k])
    if isinstance(tr.get("items"), list):
        items = []
        for it in tr["items"]:
            if isinstance(it, dict):
                it = {k: (_blind_text(v) if k in TRIAGE_TEXT_KEYS else v) for k, v in it.items() if k != "url"}
            items.append(it)
        out["items"] = items
    return out


def _news_text(params: dict, blind: bool = False) -> str:
    """新聞標題（只有日期、標題、來源）：params.news（觸發檢查給）或 TRIAGE 升級帶來的 triage 新聞項。
    價格標題再濾一次（價格隔離雙保險；觸發檢查已濾）。blind=True（THESIS，價格盲）用與 FULL_A 相同的保守濾網（RM4-2）。"""
    items = params.get("news") or [x for x in params.get("triage") or [] if isinstance(x, dict) and x.get("type") == "news"]
    lines = [f"- {str(n.get('date', ''))[:10]} {n.get('headline', '')}（{n.get('source') or '來源不明'}）"
             for n in items if isinstance(n, dict) and n.get("headline") and not is_price_headline(n["headline"], blind=blind)]
    return "\n".join(lines) or "（無）"


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
