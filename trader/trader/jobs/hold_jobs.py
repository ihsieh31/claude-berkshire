"""持股完整複查的 H2 賣出決策（HOLD_REVIEW）。

觸發：持股（帳本有股數或登錄表 HOLDING）的完整分析結束且未得出 BUY——FULL_A 停止、FULL_B 非 BUY、RED_TEAM 降為 WATCH／RED_LIGHT。
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
from ..analysis import writeback
from ..ledger import book
from .deep import _skill_inputs, _td, _vals, _file_sections, _read_json, prior_drivers_text, stage_g8_view, trigger_text
from .handlers import Handler, next_job
from .review_jobs import _last_full_card, _stage_thesis
from .sandbox import create_sandbox, stage_inputs

SELL_ACTIONS = ("TRIM", "SELL_ALL")
HUMAN_CATEGORIES = ("制度", "事件", "SWITCH", "FUNDING")
VALUATION_CATEGORIES = ("估值", "估值＋業務")
TWO_YEARS_DAYS = 730
ONE_YEAR_DAYS = 365     # 建倉未滿一年不因價格因素賣出（SKILL §6；設計決策 D01）


def held_years(first_buy: str | None, asof: str) -> int | None:
    """建倉至決策日的滿年數（日曆比較，A3-M5C-07）：跨閏日時 365 天不一定滿一年；2/29 建倉者以 3/1 為滿年日。
    建倉日缺或格式不合 → None（呼叫端視為未滿一年／未滿兩年）。"""
    if not first_buy:
        return None
    try:
        f, a = date.fromisoformat(str(first_buy)[:10]), date.fromisoformat(str(asof)[:10])
    except ValueError:
        return None
    return a.year - f.year - ((a.month, a.day) < (f.month, f.day))


NO_IV_STAGES = ("A", "FAIL", "THESIS")   # 沒走到估值（FULL_A 停止）、深度鏈中途失敗，或 THESIS 證偽被降為疑似直接交複查（D02）：
#                                         td 的 cap_result 不是本次完整分析的（M5_D2-08）
NOT_NAMED = ("null", "none", "無", "缺")
INPUT_FILES = ("phaseA.json", "report_A.md", "report.md", "signal_card.json", "cap_result.json", "red_team.json")


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


def valuation_numbers(td: Path, params: dict | None = None, *, with_date: bool = False) -> tuple:
    """(IV_base, price)：IV_base 只取 cap_calc 結果（完整分析走到估值、或 PRICE 以 cap_inputs 重算才有）；
    價格取價格檔，沒有則取工作參數（PRICE 觸發時帶入）。
    full_stage＝FAIL（深度鏈中途失敗）沒有完整分析結論：不取 IV_base（前一輪 FULL_B 留下的 cap_result 不算），Q3 不適用。
    full_stage＝A（FULL_A 停在 G7 之前）同樣不取：同 run 的 PRICE 前置可能已寫過 cap_result（M5_D2-08）。
    full_stage＝THESIS（論點證偽未過核對、降為疑似後直接交複查，D02）也不取：td 的 cap_result 是舊分析的。
    with_date＝True → (IV_base, price, price_date)：價格日與價格同源（同一價格檔的 price_date〔頂層，缺則 price_meta〕，
    沒有則取工作參數 price_date）；完整分析鏈的 CARRY 不帶 price_date，不能只看參數（RM6-3／M6-D5-08）。"""
    iv = price = None
    cap = td / "cap_result.json"
    if cap.is_file() and (params or {}).get("full_stage") not in NO_IV_STAGES:
        v = _read_json(cap).get("IV_base")
        iv = float(v) if isinstance(v, (int, float)) else None
    pdate = None
    for p in sorted((td / "dossier").glob("*_price.json")) if (td / "dossier").is_dir() else []:
        pj = _read_json(p)
        v = pj.get("price")
        price = float(v) if isinstance(v, (int, float)) else None
        d = pj.get("price_date")
        if not isinstance(d, str) or not d:   # 頂層非字串 → 改看 price_meta（check-RM6-3）
            d = (pj.get("price_meta") if isinstance(pj.get("price_meta"), dict) else {}).get("price_date")
        pdate = d if price is not None and isinstance(d, str) and d else None
    if price is None and isinstance((params or {}).get("price"), (int, float)):
        price = float(params["price"])   # 價格取自參數 → 價格日也取參數（同源）
        pdate = params["price_date"] if isinstance((params or {}).get("price_date"), str) else None
    return (iv, price, pdate) if with_date else (iv, price)


def _input_files(td: Path, params: dict) -> list[str]:
    """td 內要給 HOLD_REVIEW 的檔名；沒走到估值的階段不給 cap_result.json（M5_D2-08：提示寫無 IV_base）。
    深度鏈失敗／THESIS 降級（沒有完整分析結論）只給前半 phaseA.json、report_A.md：td 的 report.md、signal_card.json、
    red_team.json 可能是失敗那一版 FULL_B 未經框架覆核的模型原版（M5-R3-02）。"""
    stage = params.get("full_stage")
    return [n for n in INPUT_FILES if (td / n).is_file()
            and not (n == "cap_result.json" and stage in NO_IV_STAGES)
            and not (stage in ("FAIL", "THESIS") and n not in ("phaseA.json", "report_A.md"))]


def check(result: dict, *, iv_base: float | None, price: float | None, held_days: int | None,
          years: int | None = None) -> tuple[list[str], bool]:
    """回傳 (錯誤清單, 是否需轉人工)。錯誤 → 退回；需轉人工 → HOLD＋停止加碼＋異常。
    held_days＝None（帳本、登錄表都沒有建倉日）視為未滿一年、未滿兩年（M5_D2-10：不得失敗開放）。
    years：held_years 算的滿年數（日曆比較，A3-M5C-07）；給定時一年／兩年以它判斷，未給退回天數 365／730。"""
    if years is not None and held_days is not None:
        under1, under2 = years < 1, years < 2
    else:
        under1 = held_days is None or held_days < ONE_YEAR_DAYS
        under2 = held_days is None or held_days < TWO_YEARS_DAYS
    act, cat = result.get("action"), result.get("sell_category")
    if act not in SELL_ACTIONS:
        return [], False
    errs: list[str] = []
    named = str(result.get("falsified_assumption") or "").strip()
    if named.lower() in NOT_NAMED:  # 佔位字不算具名（validate 已把 "null" 轉 None，這裡再擋一次）
        named = ""
    if not cat:
        return ["賣出（TRIM／SELL_ALL）必須填 sell_category"], False
    if cat in HUMAN_CATEGORIES:
        return [], True
    if act == "TRIM":
        if cat != "估值":
            errs.append(f"TRIM 只用於估值類（Q3c／荒謬級）；{cat} 類不得只賣一部分")
        f = result.get("trim_fraction")
        if not isinstance(f, (int, float)) or isinstance(f, bool) or not 0 < f <= 1:
            errs.append(f"TRIM 的 trim_fraction 須介於 0 與 1：{f!r}")
    if cat in ("業務", "估值＋業務") and not named:
        errs.append("業務類賣出必須具名：哪條假設或失效條件被哪個事實否定（falsified_assumption）")
    if cat in VALUATION_CATEGORIES:
        if iv_base is None:
            errs.append("估值類賣出需要完整分析的 IV_base；本次完整分析未走到估值")
        elif price is None or price < iv_base:
            errs.append(f"估值類賣出需價格 ≥ IV_base（V 的條件之一）：價格 {price}，IV_base {iv_base}")
    if cat == "估值" and under1:   # 估值＋業務屬 3a 業務類（D01），不在此擋
        errs.append(f"建倉未滿一年（持有 {held_days if held_days is not None else '天數缺'} 日）：不因價格因素賣出"
                    "（SKILL §6；設計決策 D01）")
    if under2 and not named:
        errs.append("建倉未滿 2 年（Q5）：須具名被否定的假設，寫不出 → 持有")
    return errs, False


def _position_text(conn, ticker: str, asof: str) -> tuple[str, int | None]:
    """持股描述與持有天數；建倉日帳本優先、登錄表次之（writeback.first_buy_date，M5_D2-10），都缺 → None。"""
    pos = book.positions(conn).get(ticker)
    first, days = writeback.first_buy_date(conn, ticker), writeback.held_days(conn, ticker, asof)
    held = f"首次建倉 {first or '缺'}（持有 {days if days is not None else '缺'} 日；缺＝視為未滿一年）"
    peak = _reg_data(conn, ticker).get("peak_qty")   # broker 買進成交時記（P-47 累計保留 ≥ 峰值 2/3；M2_D4_06）
    held += f"，本持有期峰值股數 {peak if peak is not None else '缺'}"
    if not pos:
        return f"帳本無股數（登錄表為 HOLDING），{held}", days
    return f"{pos['qty']} 股，每股成本 {pos['avg_cost']:.4f}，{held}", days


def _reg_data(conn, ticker: str) -> dict:
    row = conn.execute("SELECT data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    return json.loads(row["data"] or "{}") if row else {}


class HoldReviewHandler(Handler):
    job_type = "HOLD_REVIEW"

    def prepare(self, job, ctx):
        td = _td(job, ctx)
        sb = create_sandbox(ctx.sandbox_base, job)
        T = job.ticker
        items = _skill_inputs(ctx)
        items += [(td / n, f"{T}/{n}") for n in _input_files(td, job.params)]
        for n in (f"{T}.md", f"{T}_dq.json"):   # 資料包與核對結果（M5-D5-01、M5-D5-04）
            if (td / "dossier" / n).is_file():
                items.append((td / "dossier" / n, f"dossier/{n}"))
        items.append((stage_g8_view(ctx, job, td), "portfolio/holdings_g8.json"))   # Q3「更好的用途」
        names = stage_inputs(sb, items, job.job_type)
        if job.params.get("full_stage") == "PRICE":
            # PRICE 觸發：td 只有 PRICE 的 cap 檔 → 附最近一次完整分析的訊號卡與登錄表列（M5-D5-04）
            last = _last_full_card(ctx.conn, T, job.asof)
            reg = ctx.conn.execute("SELECT data FROM registry WHERE ticker = ?", (T,)).fetchone()
            _write_json(sb / "last_full_signal_card.json", {"decision_date": last[0], **last[1]} if last else {})
            _write_json(sb / "registry_entry.json", json.loads(reg["data"] or "{}") if reg else {})
            names += ["last_full_signal_card.json", "registry_entry.json"]
        names += _stage_thesis(sb, job, ctx.paths)
        # D48：段落只在 API 後端才組（代理式不讀全文）
        return lambda: _file_sections(sb, [n for n in names if not n.startswith("pkg/")])

    def prompt_values(self, job, ctx, backend):
        td = _td(job, ctx)
        iv, price = valuation_numbers(td, job.params)
        pos, _ = _position_text(ctx.conn, job.ticker, job.asof)
        stage = job.params.get("full_stage")
        hs = _read_json(td / "phaseA.json").get("holding_stop") if stage == "B" and (td / "phaseA.json").is_file() \
            else None
        hs_txt = ("（持股版前半在 {g} 未過〔{c}〕，未停止、續做到估值；買進側不加碼。D30）".format(
                      g=hs.get("gate") or "?", c=hs.get("reason_code") or "?") if isinstance(hs, dict) else "")
        full = {"A": "FULL_A 停在 {g}（{c}），未走到估值；無 IV_base".format(
                    g=job.params.get("stopped_at_gate") or "?", c=job.params.get("reason_code") or "?"),
                "B": f"走完 FULL_B，最終訊號 {job.params.get('final_signal') or '?'}；IV_base {iv}{hs_txt}{_red_team_text(td)}",
                "PRICE": f"價格複查（PRICE）觸發賣出檢視：現價 {price} 對 IV_base {iv}（以最近完整分析的 cap_inputs 重算）；"
                         "依 SKILL §6 第 2 條判斷是否有更好用途（減碼或換股）",
                # 深度鏈中途失敗（deep._missing_hold）：沒有完整分析結論，不得說成 FULL_A 停在閘門（Q3 不適用）
                "FAIL": "深度分析在 {g} 失敗，未能完成（{w}）；本次沒有完整分析結論，無 IV_base".format(
                    w=str(job.params.get("fail_reason") or "原因缺")[:300],
                    g=job.params.get("stopped_at_gate") or "?"),
                # THESIS 證偽未過框架核對（D02(b)）：同 run 直接交複查，完整分析另排（SKILL §7），Q3 不適用
                "THESIS": "論點檢查判證偽但框架核對未過，已降為疑似（{w}）；本次沒有完整分析結論，無 IV_base"
                          "（完整分析另排）".format(
                    w=str((job.params.get("thesis_result") if isinstance(job.params.get("thesis_result"), dict) else {})
                          .get("falsify_downgrade") or "理由缺")[:300])}.get(stage, "缺")
        tr = job.params.get("thesis_result")
        T = job.ticker
        files = [f"{T}/{n}" for n in _input_files(td, job.params)]
        if (td / "dossier" / f"{T}.md").is_file():
            files.append(f"dossier/{T}.md（資料包）")
        if (td / "dossier" / f"{T}_dq.json").is_file():
            files.append(f"dossier/{T}_dq.json（資料包核對：dq_checks 標數字有誤者以更正值為準）")
        if stage == "PRICE":
            files += ["last_full_signal_card.json（最近一次完整分析的訊號卡）", "registry_entry.json（登錄表列）"]
        return _vals(job, backend, {
            "position": pos,
            "price": f"{price}" if price is not None else "缺",
            "full_status": full,
            "valuation_inputs": _valuation_inputs(ctx.conn, td, job),
            "thesis_result": "```json\n" + json.dumps(tr, ensure_ascii=False, indent=1) + "\n```" if tr
                             else "（無：本次不是由論點檢查觸發）",
            "files": "、".join(files + ["portfolio/holdings_g8.json（G8 比較檔）", "thesis/（論點檔與增補）"]),
            "sell_tree": sell_tree_text(ctx.paths.skill_deep),
            "last_trim": _last_trim_text(ctx.conn, job.ticker),
            "prior_external_drivers": prior_drivers_text(ctx.conn, job.ticker),
            "trigger": trigger_text(job.params, price_blind=False),   # RM2-3：觸發理由（P-50 到期結案等）
        })

    def prompt_suffix(self, job):
        fb = job.params.get("feedback")
        if not fb:
            return ""
        return "\n\n## 框架退回（本次為修正版）\n" + "\n".join(f"- {x}" for x in fb)

    def post(self, job, result, ctx):
        from ..analysis.writeback import write_hold_review_result
        if writeback._done_for_job(ctx.conn, job.job_id) is not None:
            # 重跑：本工作已回寫決策 → 不再記異常、不覆寫 td/hold_review.json（與已提交結論不一致，A3-M5E-06）；
            # 交回寫函式做冪等收尾（同群判定，A3-M6E-02）
            write_hold_review_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result, job.params,
                                     job_id=job.job_id)
            return []
        td = _td(job, ctx)
        iv, price, pdate = valuation_numbers(td, job.params, with_date=True)   # 價格日同源（RM6-3）
        _, days = _position_text(ctx.conn, job.ticker, job.asof)
        errs, human = check(result, iv_base=iv, price=price, held_days=days,
                            years=held_years(writeback.first_buy_date(ctx.conn, job.ticker), job.asof))
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
        # 疑似觀察中（有 observe_until，或觸發的論點檢查為疑似／證偽，或登錄表觀察期未到期）仍 HOLD
        # → 觀察期內不得加碼（M3_D4-02；登錄表條件補 check-P18 殘餘缺口）
        tv = (job.params.get("thesis_result") or {}).get("verdict")
        why = ("有觀察期限" if result.get("observe_until") else f"論點檢查為{tv}" if tv in ("疑似", "證偽")
               else _registry_observing(ctx.conn, job.ticker, job.asof))
        if result.get("action") == "HOLD" and not result.get("stop_adding") and why:
            notes.append(f"HOLD 但{why}：框架改為停止加碼")
            result = {**result, "stop_adding": True}
        _write_json(td / "hold_review.json", result)
        write_hold_review_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result,
                                 job.params, notes, price=price, iv_base=iv, job_id=job.job_id,   # 冪等鍵（M6-D9-03）
                                 price_date=pdate,
                                 record_price=True)   # 成功才記 review_price：失敗／不合格時邊緣觸發下期重排（D46）
        return []

    def on_invalid(self, job, errors, ctx):
        from ..analysis.writeback import write_hold_review_result
        why = "HOLD_REVIEW 輸出不合格：" + "；".join(errors)
        result = {"ticker": job.ticker, "decision_date": job.asof, "path": [], "action": "HOLD",
                  "sell_category": None, "trim_fraction": None, "stop_adding": True, "observe_until": None,
                  "falsified_assumption": None, "reason": why}
        _anomaly(ctx.conn, job.ticker, {"run_id": job.run_id, "why": why})
        iv, price, pdate = valuation_numbers(_td(job, ctx), job.params, with_date=True)   # 保守結論也記當時價格、價格日與 IV_base（M6-D5-08、RM6-3）
        write_hold_review_result(ctx.conn, ctx.paths, job.ticker, job.asof, job.run_id, result,
                                 job.params, [why + "；保守 HOLD＋停止加碼"], price=price, iv_base=iv,
                                 price_date=pdate)
        return []


def _last_trim_text(conn, ticker: str) -> str:
    """登錄表 last_trim（設計決策 D03：每次估值減碼的紀錄；荒謬級回到線下前只減一次、3c 累計保留 ≥ 峰值 2/3）。"""
    lt = writeback._prior_trim(conn, ticker, writeback._registry_get(conn, ticker))
    return json.dumps(lt, ensure_ascii=False) if lt else "（無：本持有期未曾估值減碼）"


def _registry_observing(conn, ticker: str, asof: str) -> str | None:
    """登錄表 observe_until 晚於決策日 → 理由字串；否則 None。"""
    row = conn.execute("SELECT data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    ou = str(json.loads(row["data"] or "{}").get("observe_until") or "")[:10] if row else ""
    return f"登錄表觀察期至 {ou}" if ou > asof else None


def _red_team_text(td: Path) -> str:
    """FULL_B 的 BUY 被紅隊否決或紅隊未完成而未成立時標明（M5-D5-04）：本 run 有 red_team.json，
    或回寫註記提到 RED_TEAM（紅隊輸出不合格／無法完成時不會有 red_team.json）。"""
    dec = td / "decision.json"
    notes = [str(n) for n in (_read_json(dec).get("notes") or [])] if dec.is_file() else []
    hit = [n for n in notes if "RED_TEAM" in n or "紅隊" in n]
    if not hit and not (td / "red_team.json").is_file():
        return ""
    return "；本 run 的 BUY 未通過紅隊（RED_TEAM）審查（見 red_team.json）" + (f"：{'；'.join(hit)[:300]}" if hit else "")


def _valuation_inputs(conn, td: Path, job) -> str:
    """估值輸入（M5-D5-04）：本次 cap_calc 用的長債與 P/E 中位，對照登錄表上次估值時的長債與複查價；
    附 P-45 倍數警報中框架能機械算的項目（M2_D4_06）。"""
    on = job.params.get("full_stage") in ("B", "PRICE")
    cin = _read_json(td / "cap_input.json") if (td / "cap_input.json").is_file() and on else {}
    cap = _read_json(td / "cap_result.json") if (td / "cap_result.json").is_file() and on else {}
    reg = _reg_data(conn, job.ticker)
    f = lambda v: "缺" if v is None else str(v)   # noqa: E731
    return (f"本次長債 t30 {f(cin.get('long_rate'))}、20 年中位 {f(cin.get('long_rate_20y_med'))}、"
            f"大型股 P/E 中位 {f(cin.get('largecap_pe_median'))}（{cin.get('pe_field') or '來源缺'}）、"
            f"金融 P/E 中位 {f(cin.get('fin_pe_median'))}；登錄表 t30_at_valuation {f(reg.get('t30_at_valuation'))}、"
            f"review_price {f(reg.get('review_price'))}\n" + multiple_alarms(cin, cap, reg))


def _pos(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def multiple_alarms(cin: dict, cap: dict, reg: dict) -> str:
    """P-45 倍數警報（rulebook P-45）中框架能機械算的部分（M2_D4_06）：數字交給模型，判斷仍由模型做；缺就標缺。
    正常化 P/E 取本次 cap_calc 的 norm_pe（現價 ÷ norm_eps，缺用 oe）；建倉時 P/E 取登錄表 entry_norm_pe（broker 成交時記）。"""
    pe, entry = _pos(cap.get("norm_pe")), _pos(reg.get("entry_norm_pe"))
    med, lr, rd = _pos(cin.get("largecap_pe_median")), _pos(cin.get("long_rate")), _pos(cap.get("r_d"))
    ey = 1 / pe if pe else None
    yn = lambda c: "缺" if c is None else ("是" if c else "否")   # noqa: E731
    gi = cap.get("g_impl")
    hist = [g for g in (cin.get("g10"), cin.get("g5")) if isinstance(g, (int, float)) and not isinstance(g, bool)]
    g_hi = (gi > max(hist)) if isinstance(gi, (int, float)) and hist else None
    if gi is None and cap.get("g_impl_gt_cap") and hist and rd and max(hist) <= min(0.5, rd - 1e-4):
        g_hi = True   # cap_calc 解不出 g_impl：隱含成長 > solve_g 上界 min(0.5, r_d)，已證明成長在上界以下 → 是
    return ("倍數警報（框架機械算；P-45）："
            f"正常化 P/E {f'{pe:.2f}' if pe else '缺'} ≥25：{yn(pe >= 25 if pe else None)}；"
            f"建倉時 P/E {f'{entry:.2f}' if entry else '缺'}，現值 ≥ 1.5 倍：{yn(pe >= 1.5 * entry if pe and entry else None)}；"
            f"≥ 大型股中位 1.2 倍：{yn(pe >= 1.2 * med if pe and med else None)}"
            "（規則另有『且 ≥ 自身 5 年中位 1.2 倍』：本系統不提供自身 5 年 P/E 中位，此項只以大型股中位判定，"
            "結果為『是』即計 1 項警報，不視為缺；M2-R4-03）；"
            f"盈餘殖利率 < 長債一半：{yn(ey < lr / 2 if ey and lr else None)}；"
            f"盈餘殖利率 < r_d：{yn(ey < rd if ey and rd else None)}，"
            f"g_impl > 已證明成長（g10、g5 較高者）：{yn(g_hi)}；"
            "非金融 P/B ≥6 且正常化 ROE <25%：框架未算，依資料包判斷")


def _anomaly(conn, ticker: str, detail: dict[str, Any]) -> None:
    db.insert(conn, "anomalies", {"ticker": ticker, "kind": "hold_review_needs_human", "detail": detail,
                                  "status": "open", "pause_trading": 0, "created_at": db.now_iso()})


def _write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
