"""深度分析結果回寫：決策日誌（decisions）、訊號卡（signals）、登錄表（registry）、論點檔（凍結）。

欄位名依 `berkshire-deep-merged/assets/` 的三個 schema（signal_card／registry／decision_log）。
三張表的寫入在同一個交易內完成；論點檔是檔案系統寫入，先寫，若交易失敗則刪除本次新建的檔案。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from trader import db
from trader.ledger.corp_actions import PER_SHARE_REGISTRY
from trader.paths import Paths


class WritebackError(Exception):
    """必要檔缺漏、輸入不合法或寫入失敗。"""


SIGNALS = ("BUY", "WATCH", "NO_ACTION")
# registry_schema.json 的 status 枚舉中，NO_ACTION 的停止碼可直接作為 status
_STATUS_FROM_REASON = ("MISSING_DATA", "TOO_HARD", "REJECT")
ACTION_BY_SIGNAL = {"BUY": "BUY", "WATCH": "WATCH", "NO_ACTION": "NO_ACTION"}  # decision_log action 枚舉
STOP_MODE = "A_THESIS_CHECK"
FRAMEWORK_HOLD = "FRAMEWORK_HOLD"   # A3-M5B-01：FULL_B 判 BUY、框架降成 WATCH（G8／P-32／G9／D26／價格盲缺口／持股前半／紅隊未過）
# 已作廢的決策／訊號卡不參與（`trader decisions void`，M8-D4-02）；與 portfolio.sizing.NOT_VOIDED 同口徑
# （sizing 經 correlation 匯入本模組，不反向匯入）
NOT_VOIDED = "json_extract(data, '$.voided_at') IS NULL"


# ---------------------------------------------------------------- 小工具

def _std_ticker(ticker: str) -> str:
    """代號標準形：去頭尾空白、大寫，`-`、`/` 換成 `.`（與 trader.data.universe.normalize_ticker 相同）。"""
    t = str(ticker).strip().upper().replace("-", ".").replace("/", ".")
    if not t:
        raise WritebackError("代號為空")
    return t


def _ymd(asof: str) -> str:
    """`YYYY-MM-DD` → `YYYYMMDD`；格式不合法拋 WritebackError。"""
    try:
        return datetime.strptime(str(asof), "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError as e:
        raise WritebackError(f"asof 須為 YYYY-MM-DD：{asof!r}") from e


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise WritebackError(f"缺必要檔：{path}") from e
    except (OSError, ValueError) as e:
        raise WritebackError(f"檔案讀取或 JSON 解析失敗：{path}：{e}") from e
    if not isinstance(data, dict):
        raise WritebackError(f"檔案頂層不是 JSON 物件：{path}")
    return data


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise WritebackError(f"缺必要檔：{path}") from e
    except OSError as e:
        raise WritebackError(f"檔案讀取失敗：{path}：{e}") from e


def _conclusion(signal: str, reason: str | None) -> str:
    """結論欄（signal_card_schema：買 | 觀察 | 觀察（缺資料） | 不買）。"""
    if signal == "BUY":
        return "買"
    if signal == "WATCH":
        return "觀察"
    if reason == "MISSING_DATA":
        return "觀察（缺資料）"
    return "不買"


def _status_for(signal: str, reason: str | None) -> str:
    """訊號 → 登錄表 status。BUY 尚未成交，先記 WATCH（成交後由帳本改 HOLDING）。"""
    if signal in ("BUY", "WATCH"):
        return "WATCH"
    if signal == "NO_ACTION":
        if reason in _STATUS_FROM_REASON:
            return reason
        if reason == "RED_LIGHT":   # P-64 紅燈＝延後，不是淘汰：留觀察名單，兩週後重跑（M2_D7_08；不進 X5，D24）
            return "WATCH"
        return "NO_ACTION"
    raise WritebackError(f"不認得的訊號：{signal!r}")


RED_LIGHT_RERUN_DAYS = 14   # P-64「下週期重跑」：觀察名單排程週期
HOLD_STOP_RETRY_DAYS = 91   # A3-M3B-01：持股 FULL_A 違規停止（沒有估值）→ 約一季後再排年度完整分析（推論）


def red_light_context(tdir: Path | None, asof: str) -> dict:
    """D92：紅燈的致命路徑／理由（下次重跑時 deep 附進提示，反方角色）：紅隊 red_team.json 的 fatal_paths（fatal 才取）、
    P-64 紅燈 red_light.json 的理由（deep._p64_red_light）。"""
    ctx: dict[str, Any] = {"date": asof, "fatal_paths": [], "reasons": []}
    for name, key in (("red_team.json", "fatal_paths"), ("red_light.json", "reason")):
        try:
            obj = json.loads((Path(tdir) / name).read_text(encoding="utf-8")) if tdir is not None else None
        except (OSError, ValueError):
            obj = None
        if not isinstance(obj, dict):
            continue
        if key == "fatal_paths" and obj.get("fatal") in (True, "true"):
            ctx["fatal_paths"] = [p for p in obj.get("fatal_paths") or [] if isinstance(p, dict)]
        elif key == "reason" and str(obj.get("reason") or "").strip():
            ctx["reasons"].append(str(obj["reason"]))
    return ctx


def _red_light_rerun(entry: dict, reason: str | None, asof: str, notes: list[str], prev_reason: str | None = None,
                     context: dict | None = None) -> None:
    """紅燈（RED_LIGHT）：next_full_review_due＝asof+14 天，review_due 到期排 FULL（M2_D7_08）。
    D92（A3-M2B-03）：前次結論（登錄表 last_reason_code）已是 RED_LIGHT＝連續第二次紅燈 → 不再設 14 天重跑，回一般節奏
    （14 天內的到期日清掉）並記註；狀態維持 WATCH，不自動減半／降級。紅燈理由存 red_light_context（下次重跑附進提示）；
    非紅燈結論清掉 red_light_context。"""
    if reason != "RED_LIGHT":
        entry.pop("red_light_context", None)
        return
    if context is not None:
        entry["red_light_context"] = context
    soon = (date.fromisoformat(asof[:10]) + timedelta(days=RED_LIGHT_RERUN_DAYS)).isoformat()
    if prev_reason == "RED_LIGHT":
        due = entry.get("next_full_review_due")
        if due and str(due)[:10] <= soon:
            entry["next_full_review_due"] = None
        notes.append("連續第二次紅燈（RED_LIGHT；D92）：不再設 14 天重跑，回一般完整分析節奏"
                     f"（next_full_review_due＝{entry.get('next_full_review_due')}）；登錄表維持 WATCH，"
                     "不自動減半或降級（連續紅燈後的處置待使用者決定）")
        return
    entry["next_full_review_due"] = soon
    notes.append(f"紅燈延後（P-64）：登錄表留 WATCH，{soon} 重跑完整分析（以反方角色，附本次致命路徑）")


def _resolve_status(conn: sqlite3.Connection, ticker: str, status: str, notes: list[str]) -> str:
    """已持有（登錄表 HOLDING）的檔不因重新分析而降為 WATCH 等狀態；保留 HOLDING 並記註。"""
    row = conn.execute("SELECT status FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is not None and row["status"] == "HOLDING" and status != "HOLDING":
        notes.append(f"登錄表既有 HOLDING，本次分析結果 {status} 未覆寫持有狀態（持有狀態由帳本管理）")
        return "HOLDING"
    if row is not None and row["status"] == "RETIRED" and status != "RETIRED":   # M8-R4-03
        notes.append(f"登錄表為 RETIRED（已人工停止追蹤），本次分析結果 {status} 不恢復追蹤；"
                     "要恢復請先人工改回登錄表狀態再分析")
        return "RETIRED"
    return status


def _next_free_id(conn: sqlite3.Connection, ticker: str, asof: str) -> str:
    day = _ymd(asof)
    n = int(conn.execute(
        "SELECT COUNT(*) AS c FROM decisions WHERE ticker = ? AND decision_date = ?", (ticker, asof)
    ).fetchone()["c"]) + 1
    # 計數法在列被刪除後可能撞號；逐號確認未被使用
    # signals 表另有 decision_id UNIQUE，decisions 缺列但 signals 仍在時也要避開
    while any(conn.execute(f"SELECT 1 FROM {tb} WHERE decision_id = ?", (f"{ticker}-{day}-{n}",)).fetchone()
              for tb in ("decisions", "signals")):
        n += 1
    return f"{ticker}-{day}-{n}"


def _done_for_job(conn: sqlite3.Connection, job_id: str | None) -> str | None:
    """回寫冪等（M6-D9-03）：本工作（post 路徑）已寫過的決策編號；沒有或未給 job_id 回 None。
    鍵記在日誌 data.source_job_id；decisions.job_id 欄維持「佔位列」的既有語意（_newer_state、doctor 依它判斷）。"""
    if not job_id:
        return None
    row = conn.execute("SELECT decision_id FROM decisions WHERE json_extract(data, '$.source_job_id') = ? "
                       "ORDER BY rowid LIMIT 1", (job_id,)).fetchone()
    return row["decision_id"] if row is not None else None


def next_decision_id(conn: sqlite3.Connection, ticker: str, asof: str) -> str:
    """決策編號 `{TICKER}-{YYYYMMDD}-{n}`，n 從 1 起，為 decisions 表中同代號同決策日已有數量加 1。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    return _next_free_id(conn, t, asof)


def _write_rows(conn: sqlite3.Connection, rows: list[tuple[str, dict, list[str] | None]],
                sql: list[tuple[str, tuple]] | None = None) -> None:
    """在單一交易內寫入多列；key 不為 None 時為 upsert。任何錯誤都回滾。

    與 db.insert／db.upsert 相同的 SQL 與 JSON 編碼，但不各自提交，確保三表原子寫入。
    sql：同一交易內另外執行的 (語句, 參數)（例：取消未成交買單）。
    """
    if conn.in_transaction:
        raise WritebackError("連線已有未提交的交易，拒絕寫入以免部分提交")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, row, key in rows:
            db._check_ident(table)
            cols = list(row)
            for c in cols:
                db._check_ident(c)
            values = [db._encode(row[c]) for c in cols]
            sql_text = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            if key:
                updates = [c for c in cols if c not in key]
                sql_text += f" ON CONFLICT({', '.join(key)}) "
                sql_text += ("DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in updates)) if updates else "DO NOTHING"
            conn.execute(sql_text, values)
        for stmt, args in sql or []:
            conn.execute(stmt, args)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _write_thesis(paths: Paths, ticker: str, asof: str, text: str) -> Path:
    """寫論點檔。`{ticker}_{asof}.md` 已存在則不覆寫，改寫 `{ticker}_{asof}_addendum_{n}.md`（n 從 1 起）。

    以獨占模式建立檔案，避免併發時互相覆寫。回傳實際寫入的路徑。
    """
    paths.theses.mkdir(parents=True, exist_ok=True)
    base = paths.theses / f"{ticker}_{asof}.md"
    try:
        with open(base, "x", encoding="utf-8") as f:
            f.write(text)
        return base
    except FileExistsError:
        pass
    n = 1
    while True:
        p = paths.theses / f"{ticker}_{asof}_addendum_{n}.md"
        try:
            with open(p, "x", encoding="utf-8") as f:
                f.write(text)
            return p
        except FileExistsError:
            n += 1


# 估值字句（M6-D6-01／M6-D6-02）：論點檔與持股增補常寫 IV、買價上限、安全邊際、P/E；dossier_prep 的價格樣式不含這些詞，
# 這組只用在論點檔（review_jobs._stage_thesis 與 H2 增補），不改 dossier_prep 共用樣式。
# 只抓股價／估值寫法（P54R）：「價格」只在接價位數字（非百分比／區間）時算；「購買價格」不算買價；「定價權、價格競爭、等級上限、產能上限、公債殖利率」等業務論述不算。
_VALUATION_RE = re.compile(r"(?<![A-Za-z])IV(?![A-Za-z])|內在價值|安全邊際|(?<!購)買價|預期(?:年化)?報酬|"
                           # D79（M6_S6-NF-3）：P／E、獨立 PE、保守／基準／悲觀／樂觀價值、荒謬價格線、英文估值詞；
                           # 殖利率接數字算（前接公債／國債／美債／債券／國庫券的利率論述不算）；單獨 fair value 不算
                           r"(?<![A-Za-z])P\s*[/／]\s*E|(?-i:(?<![A-Za-z])PE(?![A-Za-z]))|本益比|"
                           r"(?:保守|基準|悲觀|樂觀)價值|荒謬(?:價格)?線|荒謬價|intrinsic\s+value|margin\s+of\s+safety|"
                           r"buy[\s_]cap|FCF\s*yield|fair\s+value\s+estimate|"
                           r"(?<!公債)(?<!國債)(?<!美債)(?<!債券)(?<!國庫券)殖利率\s*[:：]?\s*(?:約|為|達)?\s*[$＄]?\d|"
                           r"(?:自動買進|買進|價格)上限|價格\s*[:：]?\s*[$＄]?\s*\d[\d,.]*(?![\d,.]|\s*[%％–~～-])|"
                           r"(?:股利|盈餘|自由現金流|FCF)殖利率|auto_buy_cap|sell_review|荒謬級|"
                           # 估值區間／目標價寫法緊接價位數字（M5_D6-04／RM5-6R／RM5-6RR）：詞與數字間只容空白、冒號、「約／為／在／至／每股」
                           # 與「落在／設定為／設在／訂為／調整為」；數字後接億／萬／兆／千／百萬／K/M/B／million／billion／%（營收、會計金額、占比）不算；
                           # 「年」或兩端皆年份且不接美元／元的區間（2026–2028）不算，1950–2050 美元這類千元股價位仍算；
                           # 「合理價格、目標買進區域、檢視線上、公允價值變動」不算。
                           r"(?:價值區間|(?:賣出|買進)檢視線|建倉區間|目標(?:賣出|買進)區(?!域)|合理價(?:位|值)?(?!格)"
                           r"|公允價值(?:估計)?|估值(?:區間)?|(?:買進|賣出)區間|目標(?:賣出|買進)?價位?(?!格))"
                           r"(?:[\s:：約為在至每股]|落在|設定?[為在於]|訂[為在於]|調整?[為至到]){0,6}"
                           r"(?!(?:19|20)\d\d\s*(?:年|[–~～-]\s*(?:19|20)?\d\d(?![\d,.])(?!\s*(?:美元|元))))"   # 2026–27 年亦屬年份（RM5-6R3）
                           r"[$＄]?\d[\d,.]*(?![\d,.]|\s*(?:[億萬兆千%％]|百萬|[KMB]\b|million|billion|bn|mn))|"
                           r"上檔空間(?:[\s到至約達為]|高達|[還仍約]?有){0,4}[$＄]?\d|"
                           # 「N 至 N 美元之間」只在接每股／股價（非盈餘、股利）或接建倉／買賣動作時算（每股盈餘、客單價、時薪區間不算）
                           r"(?:每股|股價)(?!盈餘|股利|EPS)[\s在約為介於]{0,4}\d[\d,.]*\s*(?:至|到|[–~～-])\s*\d[\d,.]*\s*美元之間|"
                           r"\d[\d,.]*\s*(?:至|到|[–~～-])\s*\d[\d,.]*\s*美元之間\s*(?:建倉|買進|賣出|加碼|減碼)", re.IGNORECASE)


def scrub_valuation_lines(text: str) -> str:
    """逐行刪除同時含估值詞與數字的行（價格隔離工作讀論點檔前用）。"""
    return "\n".join(ln for ln in text.split("\n") if not (_VALUATION_RE.search(ln) and re.search(r"\d", ln)))


# 論點檔標題（D79）：Markdown #、整行粗體、「一、」短標題
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+\S.*|\*\*[^*\n]+\*\*\s*[:：]?|[一二三四五六七八九十]+、\S.{0,28})\s*$")
_VALUATION_HEAD_RE = re.compile(r"估值|估價|價值區間|價值評估|合理價|保守\S{0,4}價值|安全邊際|買價|買進上限|目標價|valuation",
                                re.IGNORECASE)


def _heading_kind(ln: str) -> tuple[str, int]:
    """標題種類與層級：Markdown（層級＝# 數）、粗體、「一、」編號。"""
    s = ln.lstrip()
    if s.startswith("#"):
        return "md", len(s) - len(s.lstrip("#"))
    return ("bold", 0) if s.startswith("**") else ("num", 0)


def drop_valuation_sections(text: str) -> str:
    """估值類標題（保守價值區間、估值、安全邊際、買價…）之後到下一個標題之間的含數字行刪除（D79，M6_S6-NF-3）。
    標題行與無數字行保留；「失效條件」行不刪（證偽核對要用）。價格隔離寧嚴：段內業務數字一併刪可接受。
    段落結束＝同級或更高級標題（Markdown 依 # 層級；粗體遇粗體、編號遇編號）；段內較深的子標題（### 熊市情境、
    **熊市**、一、保守情境 85）延續估值段，子標題含數字也刪（RM6-4R）。"""
    out, val = [], None   # val＝估值段開頭的 (種類, 層級)
    for ln in text.split("\n"):
        if _HEADING_RE.match(ln):
            kind = _heading_kind(ln)
            if val is not None and not (kind[0] == val[0] and kind[1] <= val[1]
                                        or kind[0] == "md" and val[0] != "md"):
                if re.search(r"\d", ln) and "失效條件" not in ln:   # 估值段內子標題
                    continue
                out.append(ln)
                continue
            val = kind if _VALUATION_HEAD_RE.search(ln) else None
        elif val is not None and re.search(r"\d", ln) and "失效條件" not in ln:
            continue
        out.append(ln)
    return "\n".join(out)


def _load_schema(paths: Paths, name: str) -> dict:
    return _load_json(paths.skill_deep / "assets" / name)


def _blank_from_fields(fields: dict) -> dict:
    """依 schema 的欄位名建立空白列（非 `_` 開頭的鍵皆為 None）。"""
    return {k: None for k in fields if not str(k).startswith("_")}


def skill_hashes_file(paths: Paths) -> str:
    """兩個 skill 目錄與 routing/（提示、schema、路由表；M3_D9-02）逐檔 sha256 凍結成 state/skill_hashes/{摘要}.json
    （同內容同檔）；回傳相對 state 的路徑。月報以「期間內出現幾種」判斷規則是否變動。"""
    files = {}
    for root in (paths.skill_deep, paths.skill_screen, paths.schemas_json.parent):
        for f in sorted(Path(root).rglob("*")) if Path(root).is_dir() else []:
            if f.is_file() and "__pycache__" not in f.parts and f.name != ".DS_Store":
                files[f"{Path(root).name}/{f.relative_to(root).as_posix()}"] = hashlib.sha256(f.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
    out = paths.state / "skill_hashes" / f"{digest}.json"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(files, ensure_ascii=False, indent=1), encoding="utf-8")
    return out.relative_to(paths.state).as_posix()


def _framework_log_fields(paths: Paths, tdir: Path | None) -> dict:
    """完整分析日誌中由框架填的欄位：skill_hashes；fy_last_filed（資料包用到的最新 10-K 申報日，sec/_index.json）；
    price_file_opened_at_file（FULL_B 開價格檔時間的留存檔，相對 state；問卷先於看價由框架以檔案時間保證，M2-D10-04）。"""
    out: dict[str, Any] = {"skill_hashes": skill_hashes_file(paths)}
    opened = Path(tdir) / "price_file_opened_at.txt" if tdir is not None else None
    if opened is not None and opened.is_file():
        try:
            out["price_file_opened_at_file"] = opened.resolve().relative_to(paths.state.resolve()).as_posix()
        except ValueError:
            out["price_file_opened_at_file"] = str(opened)
    idx = Path(tdir) / "sec" / "_index.json" if tdir is not None else None
    if idx is not None and idx.is_file():
        try:
            dates = [str(x.get("filingDate")) for x in json.loads(idx.read_text(encoding="utf-8"))
                     if isinstance(x, dict) and x.get("form") in ("10-K", "10-K/A") and x.get("filingDate")]
        except ValueError:
            dates = []
        if dates:
            out["fy_last_filed"] = max(dates)
    return out


def _chain_stamp(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str) -> dict:
    """本 run 本檔已送出工作（engine 在 jobs.params 記的 rules_hash、model_cutoff）：
    skill_hashes 取 FULL_A 送出時的版本；鏈內（含回寫當下）不只一版 → rule_note 警示（D49＝B，不阻擋）；
    決策日早於模型訓練截止日 → hindsight 註記（D25）。沒有紀錄（例如未經引擎）時各欄為 None。"""
    rows = conn.execute("SELECT job_type, params FROM jobs WHERE run_id=? AND ticker=? AND started_at IS NOT NULL "
                        "ORDER BY rowid", (run_id, ticker)).fetchall()
    stamps = [(r["job_type"], json.loads(r["params"] or "{}")) for r in rows]
    stamps = [(jt, p) for jt, p in stamps if isinstance(p, dict)]
    seen: list[str] = []
    for _, p in stamps:
        if p.get("rules_hash") and p["rules_hash"] not in seen:
            seen.append(p["rules_hash"])
    first = next((p["rules_hash"] for jt, p in stamps if jt == "FULL_A" and p.get("rules_hash")), None) \
        or (seen[0] if seen else None)
    out: dict[str, Any] = {"skill_hashes": first, "rule_note": None, "hindsight": None}
    if seen:
        now = skill_hashes_file(paths)
        if now not in seen:
            seen.append(now)
        if len(seen) > 1:
            out["rule_note"] = (f"規則版本變動警示：本分析鏈期間規則檔有 {len(seen)} 版（{'、'.join(seen)}），"
                                f"skill_hashes 記 FULL_A 送出時的版本 {first}")
    cut = max((str(p["model_cutoff"]) for _, p in stamps if p.get("model_cutoff")), default=None)
    if cut and asof < cut:
        out["hindsight"] = f"可能有後見之明：決策日 {asof} 早於模型訓練資料截止日 {cut}（D25）"
    return out


def _apply_stamp(stamp: dict, log: dict, notes: list[str]) -> None:
    if stamp["skill_hashes"]:
        log["skill_hashes"] = stamp["skill_hashes"]
    if stamp["rule_note"]:
        notes.append(stamp["rule_note"])
    if stamp["hindsight"]:
        log["hindsight_flag"] = True
        notes.append(stamp["hindsight"])


_CAP_LINES = ("auto_buy_cap", "sell_review_above", "absurd_price_line")


def _reconcile_with_cap(tdir: Path, card: dict, entry: dict, notes: list[str]) -> None:
    """登錄表的每股價格線與等級以 cap_calc 為準（不照抄模型的 registry_entry.json）：
    auto_buy_cap、sell_review_above（＝IV_base）、absurd_price_line（＝50×E）；等級取訊號卡與 cap_calc 較保守者，
    訊號卡 certainty_grade 同步改成同一等級。訊號卡同步寫入同樣的三條線。
    沒有 cap_result.json（未走到估值）時：登錄表三條線為 None（失敗關閉），等級取訊號卡。"""
    from .checks import absurd_line, worse_grade
    cap_path = Path(tdir) / "cap_result.json"
    if not cap_path.is_file():
        for k in _CAP_LINES:
            entry[k] = None
        entry["grade"] = worse_grade(card.get("certainty_grade"))
        return
    cap = _load_json(cap_path)
    cin_path = Path(tdir) / "cap_input.json"
    cin = _load_json(cin_path) if cin_path.is_file() else {}
    want = {"auto_buy_cap": cap.get("auto_buy_cap"), "sell_review_above": cap.get("IV_base"),
            "absurd_price_line": absurd_line(cin)}
    for k, v in want.items():
        if entry.get(k) != v:
            notes.append(f"登錄表 {k} 以 cap_calc 為準：{entry.get(k)!r} → {v!r}")
        entry[k] = v
        card[k] = v
    g = worse_grade(card.get("certainty_grade"), cap.get("grade_used"))
    entry["grade"] = g
    # 訊號卡等級同步（M6_D7_04）：plan_buys 讀訊號卡、plan_sells 讀登錄表，兩者等級不同會建倉後自動減碼
    if card.get("certainty_grade") != g:
        notes.append(f"訊號卡與登錄表等級取訊號卡與 cap_calc 較保守者：{card.get('certainty_grade')!r} → {g!r}")
        card["certainty_grade"] = g


# ---------------------------------------------------------------- 模型輸出 → 框架欄位（M6 B1）

# 登錄表中由模型給、框架只做正規化的鍵；其餘決策欄位一律由框架產生（設計決策 D31）
_SPLIT_TYPES = ("拆股", "合股", "股票股利")   # 公司行動換算每股欄位的類型（ledger.corp_actions.RATIO_TYPES）
# last_trim：設計決策 D03；peak_qty／entry_norm_pe：broker 買進成交時寫的持有側紀錄（M2_D4_06，X40）
# observe_until：P-50 觀察期限，完整分析非 BUY 不清掉（觀察中不得延後、到期仍須結案，RM2-13R2）；判 BUY 即結案清除（D76）
_HELD_KEYS = ("cost", "first_buy_date", "weight", "build_plan", "last_trim", "peak_qty", "entry_norm_pe",
              "observe_until")
# 資料日之後已有寫過價格線的決策（PRICE 或完整分析）時不覆寫的狀態欄（M6-D6-07）
_NEWER_STATE_KEYS = ("review_price", "t30_at_valuation", *_CAP_LINES, "grade", "target_weight",
                     "target_weight_basis", "cap_input_file")
# 資料日之後只有較新的 PRICE（沒有較新的完整分析）時只保留的欄位（A3-M6D-03）：PRICE 只寫價格線與 t30，
# 不寫等級、目標權重、cap_inputs → 舊資料日的完整分析（等待同意數日）照寫這些欄
_PRICE_STATE_KEYS = ("review_price", "t30_at_valuation", *_CAP_LINES)
# 資料日之後有已套用的拆合股時只保留的欄位（M7-D10-01）：已換算的每股欄位與 cap_inputs；等級、目標權重照新結果寫
_SPLIT_KEEP_KEYS = (*PER_SHARE_REGISTRY, "cap_input_file")
# 非持股停止時從既有列帶入的鍵（M6-D9-05）：P-31 判定只能因新事實改判，清掉會讓模型可不附事實改判
_STOP_CARRY_KEYS = ("cik", "asset_class", "external_drivers", "external_drivers_basis",
                    "external_drivers_determined_on", "external_drivers_change_reason", "corr_links",
                    "thesis_file", "integrity_reject")   # external_drivers_change_reason：M6-RS5-01
# integrity_reject（A3-M6E-01）：停止結果從未通過前半，不得清掉誠信淘汰標記（D27；只有 write_full_result 非 REJECT 才清）
# 誠信淘汰規則編號（NEVER-04；validate._RULE_ID 允許 NEVER-04a 等字尾）：REJECT 停止引用 → 登錄表 integrity_reject（D27）
_INTEGRITY_RULE = re.compile(r"^NEVER-04(?!\d)")


def _unwrap(obj: dict, ticker: str, label: str, notes: list[str]) -> dict:
    """模型照 schema 檔頂層形狀輸出（{schema, entries:[…]} 或 {schema, fields:{…}}）時拆包（M6-D1-03）。"""
    ents = obj.get("entries")
    if isinstance(ents, list) and ents:
        mine = [e for e in ents if isinstance(e, dict)
                and str(e.get("ticker") or "").strip().upper().replace("-", ".").replace("/", ".") == ticker]
        if not mine and len(ents) == 1 and isinstance(ents[0], dict):
            mine = [ents[0]]
        if mine:
            notes.append(f"{label} 為 schema 包裝形狀（entries），已取出 {ticker} 那一列")
            return dict(mine[0])
    if isinstance(obj.get("fields"), dict):
        notes.append(f"{label} 為 schema 包裝形狀（fields），已拆包")
        return dict(obj["fields"])
    return obj


def _empty_val(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _valid_date(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v.strip()[:10]).isoformat()
    except ValueError:
        return None


def _kill_criteria_due(phase_a: dict, model_entry: dict) -> list[dict]:
    """失效條件 [{text, due}]：FULL_A thesis_draft.kill_criteria（價格盲階段寫的、帶期限）全部在前，
    再接模型登錄表新增的條件（FULL_B 只能新增、不能刪改，設計決策 D29；同文字略過）；
    非 dict 元素轉成 {text, due: None}；文字刪價格句（M6-D1-01、M6-D6-06、M3-D10-02）。"""
    from ..jobs.validate import _norm_text
    from .dossier_prep import scrub_price_sentences
    td = phase_a.get("thesis_draft")
    src_a = td.get("kill_criteria") if isinstance(td, dict) else None
    src_b = model_entry.get("kill_criteria_due")
    out: list[dict] = []
    seen: set[str] = set()
    for src in (src_a, src_b):
        if src is None:
            continue
        for k in src if isinstance(src, list) else [src]:
            text, due = (k.get("text") or k.get("criterion"), k.get("due")) if isinstance(k, dict) else (k, None)
            text = scrub_price_sentences(str(text or "").strip())[0].strip()
            if text and _norm_text(text) not in seen:
                seen.add(_norm_text(text))
                out.append({"text": text, "due": _valid_date(due)})
    return out


def _registry_entry(model_entry: dict, phase_a: dict, cin: dict, prev: dict | None) -> dict:
    """登錄表列以白名單＋框架值建立（M6-D1-01）：模型只給 cik、next_full_review_due、kill_criteria_due（正規化）；
    review_price／t30_at_valuation 取 cap_input；三條價格線與等級由 _reconcile_with_cap 填；
    cost／first_buy_date／weight／build_plan 只取既有持股列（呼叫端處理），模型值一律丟棄。"""
    cik = model_entry.get("cik")
    cik = str(cik).strip() if isinstance(cik, (str, int)) and not isinstance(cik, bool) and str(cik).strip() else None
    out: dict[str, Any] = {
        "cik": cik or (prev["data"].get("cik") if prev else None),
        "next_full_review_due": _valid_date(model_entry.get("next_full_review_due")),
        "kill_criteria_due": _kill_criteria_due(phase_a, model_entry),
        "review_price": _finite(cin.get("price")),
        "t30_at_valuation": _finite(cin.get("long_rate")),
    }
    if prev and prev["data"].get("corr_links") is not None:
        out["corr_links"] = prev["data"]["corr_links"]
    return out


def _card_numbers(card: dict, notes: list[str]) -> None:
    """訊號卡會驅動部位計算的數字欄（M6-D1-02）：不是有限數字 → None 並記註；max_weight_pct 不是物件 → {}。
    單位（百分比 vs 小數）不在此判斷，由 sizing.target_weight 處理。"""
    from ..portfolio.sizing import _num

    def fix(d: dict, k: str, where: str) -> None:
        if d.get(k) is not None and _num(d[k]) is None:
            notes.append(f"訊號卡 {where} 不是數字，視為缺：{d[k]!r}")
            d[k] = None
    fix(card, "target_weight_pct", "target_weight_pct")
    fix(card, "first_tranche_pct_of_target", "first_tranche_pct_of_target")
    if isinstance(card.get("position_plan"), dict):
        pp = card["position_plan"] = dict(card["position_plan"])
        for k in ("target_weight_pct", "first_tranche_pct_of_target"):
            fix(pp, k, f"position_plan.{k}")
    mw = card.get("max_weight_pct")
    if mw is not None and not isinstance(mw, dict):
        notes.append(f"訊號卡 max_weight_pct 不是物件，視為空：{mw!r}")
        card["max_weight_pct"] = {}
    elif isinstance(mw, dict):
        card["max_weight_pct"] = mw = dict(mw)
        for k in list(mw):
            fix(mw, k, f"max_weight_pct.{k}")


def _apply_summary(card: dict, summary: dict, phase_a: dict, model_entry: dict, notes: list[str],
                   final_signal: str | None = None) -> None:
    """設計決策 D31／D29／D13：驅動下單的欄位取驗證過的 FULL_B 摘要（phaseB.json）與前半結果，模型訊號卡只當報告文字。
    - signal／stopped_at_gate／reason_code：摘要有值時以摘要為準（三者同源，不會出現 BUY＋非 PASS_ALL）；
      stopped_at_gate 只在摘要非 null、且框架 final_signal 未改成別的訊號時覆寫（保留框架設的 G8／G11 等閘）；
      final_signal 與賣光日等框架降級在之後照舊。
    - 意願權重 intended_weight：只取摘要（訊號卡自填的不採用）；摘要沒有 → 移除，sizing 退回舊算法並記註。
    - 確定性等級：取摘要（缺時訊號卡）、訊號卡與 FULL_A 結束等級中最低者（後半只能降級）；全都不是 A–D 時不動。
    - 失效條件：模型登錄表漏列 FULL_A 的條件記註（登錄表由 _kill_criteria_due 補回）。"""
    from ..jobs.validate import missing_kill_criteria, phase_a_grade
    from ..portfolio.sizing import INTENT_FRAC
    from .checks import GRADE_ORDER, worse_grade
    before = (card.get("signal"), card.get("reason_code"))
    sig = summary.get("signal")
    if sig in SIGNALS:
        if sig != card.get("signal"):
            notes.append(f"訊號卡 signal 以驗證過的摘要為準：{card.get('signal')!r} → {sig!r}")
            card["signal"] = sig
        # P26R2：摘要閘為 null 不覆寫（框架在 deep.post 設的 G8／G11 等停止閘要留著，與 deep.py `or` 一致）；
        # 框架已把訊號降成與摘要不同（final_signal）時，卡上的閘是框架的，也不覆寫
        gate = summary.get("stopped_at_gate")
        if gate is not None and gate != card.get("stopped_at_gate") and final_signal in (None, sig):
            notes.append(f"訊號卡 stopped_at_gate 以驗證過的摘要為準：{card.get('stopped_at_gate')!r} → {gate!r}")
            card["stopped_at_gate"] = gate
    rc = summary.get("reason_code")
    if isinstance(rc, str) and rc and rc != card.get("reason_code"):
        notes.append(f"訊號卡 reason_code 以驗證過的摘要為準：{card.get('reason_code')!r} → {rc!r}")
        card["reason_code"] = rc
    if (card.get("signal"), card.get("reason_code")) != before and card.get("signal") in SIGNALS:
        card["conclusion"] = _conclusion(card["signal"], card.get("reason_code"))
    iw = summary.get("intended_weight")
    if isinstance(iw, dict) and iw.get("level") in INTENT_FRAC:
        card["intended_weight"] = {"level": iw["level"], "reason": iw.get("reason")}
    elif card.pop("intended_weight", None) is not None:
        notes.append("訊號卡自填的 intended_weight 不採用（只取驗證過的摘要）")
    base = summary.get("certainty_grade") if summary.get("certainty_grade") in GRADE_ORDER else card.get("certainty_grade")
    if base in GRADE_ORDER:
        ga = phase_a_grade(phase_a)
        g = worse_grade(base, card.get("certainty_grade"), ga)
        if g != card.get("certainty_grade"):
            notes.append(f"訊號卡等級取摘要、訊號卡與 FULL_A 結束等級（{ga}）中較嚴者：{card.get('certainty_grade')!r} → {g!r}")
            card["certainty_grade"] = g
    kc = model_entry.get("kill_criteria_due")
    texts = [(k.get("text") or k.get("criterion")) if isinstance(k, dict) else k
             for k in (kc if isinstance(kc, list) else [kc] if kc is not None else [])]
    dropped = missing_kill_criteria(phase_a, texts)
    if dropped:
        notes.append("模型登錄表漏列 FULL_A 失效條件，以 FULL_A 補回（後半只能新增）：" + "；".join(dropped))


def _framework_target(card: dict, user_hard_cap: float | None = None) -> tuple[float, list[str]]:
    """框架目標權重 T（sizing.target_weight，小數；0＝不建倉）。訊號卡數值不合法 → 0 並記註。
    user_hard_cap：config account.user_hard_cap（D01），與 plan_buys 同口徑（M6-D10-01）。"""
    from ..portfolio.sizing import target_weight
    try:
        return target_weight(card, user_hard_cap)
    except (TypeError, ValueError) as e:
        return 0.0, [f"訊號卡目標權重或權重上限不是數字，T＝0：{e}"]


_GATE_FIELDS = (("gate1", "gate1_not_deleted"), ("gate2", "gate2_price_below_cap"), ("gate3", "gate3_pass"))
_GATE_COPY = ("questionnaire_version", "gate3_answers_file", "screen_decision", "flag_codes", "distress_codes",
              "price_date", "time_zone")


def _apply_gates(log: dict, gates: dict, notes: list[str]) -> None:
    """三閘門與其來源欄以框架判定覆寫模型日誌（M6_D7_02、M6-D5-02）。"""
    for k, f in _GATE_FIELDS:
        v = gates.get(k) is True
        if log.get(f) != v:
            notes.append(f"日誌 {f} 以框架判定為準：{log.get(f)!r} → {v!r}")
        log[f] = v
    log["gate3_failures"] = [] if log["gate3_pass"] else \
        ([str(x) for x in gates.get("gate3_failures") or []] or ["閘門 3 未過"])
    if not log["gate2_price_below_cap"] and gates.get("gate2_failures"):
        notes.append("閘門 2 未過：" + "、".join(str(x) for x in gates["gate2_failures"]))
    for f in _GATE_COPY:
        if f in gates:
            log[f] = gates[f]


def _open_buy_ids(conn: sqlite3.Connection, ticker: str) -> list[str]:
    return [r["order_id"] for r in conn.execute(
        "SELECT order_id FROM orders WHERE ticker = ? AND side = 'BUY' AND status = 'open' ORDER BY order_id",
        (ticker,))]


def _cancel_buys_sql(ticker: str, reason: str) -> tuple[str, tuple]:
    return ("UPDATE orders SET status = 'cancelled', cancel_reason = ?, updated_at = ? "
            "WHERE ticker = ? AND side = 'BUY' AND status = 'open'", (reason, db.now_iso(), ticker))


def _newer_state(conn: sqlite3.Connection, ticker: str, asof: str) -> tuple[tuple[str, ...], str] | None:
    """登錄表已有比本次資料日新的狀態 → (不覆寫的欄位, 理由)（M6-D6-07、M7-D10-01）：
    - asof 之後已有寫過價格線的決策：PRICE 或完整分析（日誌 review_type 為 PRICE 或 null）。只算回寫函式寫的列
      （job_id 為 null）；不合格輸出的佔位列（on_invalid，job_id 非 null）沒寫登錄表，不算。
      TRIAGE（touch_review）、THESIS、HOLD_REVIEW 只推遲複查日期、不寫價格線，不擋新估值。已作廢的決策不算。
    - asof 之後有已套用的拆合股：只保留已換算的每股欄位與 cap_inputs。
    依較新決策的種類回欄位（A3-M6D-03）：有較新的完整分析（含停止結果）→ _NEWER_STATE_KEYS；只有較新的 PRICE →
    _PRICE_STATE_KEYS（PRICE 從不寫等級、T、cap_inputs，舊資料日的完整分析照寫這三者）。"""
    row = conn.execute(
        f"SELECT decision_id, decision_date FROM decisions WHERE ticker = ? AND job_id IS NULL AND {NOT_VOIDED} "
        "AND substr(decision_date, 1, 10) > ? AND json_extract(data, '$.review_type') IS NULL "
        "ORDER BY decision_date DESC LIMIT 1", (ticker, asof)).fetchone()
    if row is not None:
        return _NEWER_STATE_KEYS, (f"資料日 {asof} 之後已有寫過價格線的決策 {row['decision_id']}（{row['decision_date']}）："
                                   "未覆寫登錄表價格線、等級、目標權重與 cap_inputs")
    row = conn.execute(
        f"SELECT decision_id, decision_date FROM decisions WHERE ticker = ? AND job_id IS NULL AND {NOT_VOIDED} "
        "AND substr(decision_date, 1, 10) > ? AND json_extract(data, '$.review_type') = 'PRICE' "
        "ORDER BY decision_date DESC LIMIT 1", (ticker, asof)).fetchone()
    if row is not None:
        return _PRICE_STATE_KEYS, (f"資料日 {asof} 之後已有較新的 PRICE 決策 {row['decision_id']}（{row['decision_date']}）："
                                   "未覆寫登錄表價格線與 t30_at_valuation（等級、目標權重、cap_inputs 照本次分析寫；A3-M6D-03）")
    row = conn.execute(
        f"SELECT action_type, effective_date FROM corporate_actions WHERE ticker = ? AND status = 'applied' "
        f"AND action_type IN ({','.join('?' * len(_SPLIT_TYPES))}) AND effective_date > ? "
        "ORDER BY effective_date LIMIT 1", (ticker, *_SPLIT_TYPES, asof)).fetchone()
    if row is not None:
        return _SPLIT_KEEP_KEYS, (f"資料日後有{row['action_type']}（{row['effective_date']}），每股欄位已換算："
                                  "未覆寫登錄表每股欄位與 cap_inputs")
    return None


def _later(a: Any, b: str) -> str:
    return a if isinstance(a, str) and a[:10] > b else b


def _newer_review(conn: sqlite3.Connection, ticker: str, asof: str) -> dict | None:
    """資料日之後已有回寫函式寫的 THESIS／HOLD_REVIEW 決策（A3-M6D-02、A3-M6B-01）→ 最新一筆 {decision_id, decision_date,
    review_type, suspect, grade_after}；suspect＝其中任一筆論點疑似或開了 P-50 觀察期（observe_until）；grade_after＝其中
    HOLD_REVIEW H2 降等的最嚴等級（THESIS 不改等級）。沒有回 None。
    佔位列（job_id 非 null）、run 收尾保守結論（fallback）與已作廢者不算。"""
    out = None
    for r in conn.execute(
            f"SELECT decision_id, decision_date, data FROM decisions WHERE ticker = ? AND job_id IS NULL AND {NOT_VOIDED} "
            "AND substr(decision_date, 1, 10) > ? AND json_extract(data, '$.review_type') IN ('THESIS', 'HOLD_REVIEW') "
            "ORDER BY decision_date DESC, created_at DESC, rowid DESC", (ticker, asof)).fetchall():
        log = json.loads(r["data"] or "{}")
        if log.get("fallback"):
            continue
        if out is None:
            out = {"decision_id": r["decision_id"], "decision_date": r["decision_date"],
                   "review_type": log.get("review_type"), "suspect": False, "grade_after": None}
        out["suspect"] = out["suspect"] or log.get("thesis_check") == "疑似" or bool(log.get("observe_until"))
        if log.get("review_type") == "HOLD_REVIEW" and log.get("grade_after"):
            from .checks import worse_grade
            out["grade_after"] = worse_grade(out["grade_after"], log["grade_after"]) or out["grade_after"]
    return out


def _merge_kill_due(new: Any, keep: Any) -> Any:
    """舊資料日的完整分析遇較新的 THESIS／HOLD_REVIEW（A3-M6D-02）：既有失效條件條目（含 checked_on／status／due）原樣保留，
    本次分析只新增既有清單沒有的條件。"""
    from ..jobs.validate import _norm_text
    if not isinstance(keep, list) or not keep:
        return new
    seen = {_norm_text(k.get("text")) for k in keep if isinstance(k, dict)}
    add = [k for k in (new if isinstance(new, list) else []) if isinstance(k, dict) and _norm_text(k.get("text")) not in seen]
    return list(keep) + add


def _newer_thesis_price(conn: sqlite3.Connection, ticker: str, asof: str) -> str | None:
    """review_price 是否已有比本次資料日新的值 → 理由；沒有回 None（F05R2，check-F05R E4）。
    較新者：asof 之後回寫函式寫的 PRICE、完整分析（含停止結果）、帶價格的 THESIS，或已套用的拆合股（每股價已換算）。
    THESIS、PRICE、完整分析回寫 review_price 前都用它，舊資料日不蓋回較新的價格。已作廢的決策不算。"""
    row = conn.execute(
        f"SELECT decision_id, decision_date FROM decisions WHERE ticker = ? AND job_id IS NULL AND {NOT_VOIDED} "
        "AND substr(decision_date, 1, 10) > ? "
        "AND (json_extract(data, '$.review_type') IS NULL OR json_extract(data, '$.review_type') = 'PRICE' "
        "OR (json_extract(data, '$.review_type') = 'THESIS' AND json_extract(data, '$.price') IS NOT NULL)) "
        "ORDER BY decision_date DESC LIMIT 1", (ticker, asof)).fetchone()
    if row is not None:
        return (f"資料日 {asof} 之後已有帶價格的決策 {row['decision_id']}（{row['decision_date']}）："
                "review_price 保留較新值，不寫本次價格")
    newer = _newer_state(conn, ticker, asof)
    return newer[1] if newer is not None and "review_price" in newer[0] else None


# D24：閘門 3 問卷與 FULL_A 的對帳表。問卷停止級答案 → (停止閘門, 停止碼)；FULL_A 判通過卻有這些答案 → 前後不一致，
# 取較嚴的停止碼寫回、不給 auto_buy_cap、不重跑
# 停止級答案（M2-RS2-02，SKILL §2 與 gate3_questionnaire.json pass_if）：能力圈題 → G1 TOO_HARD；護城河題 → G3 REJECT。
# 另：護城河三測（M1–M3）強 <2 → G3 REJECT（見 gate3_verdict）；「不知道」→ TOO_HARD（SKILL §0A）
GATE3_STOP_ANSWERS = (("C1", "寫不到5年", "G1", "TOO_HARD"), ("C2", "否", "G1", "TOO_HARD"), ("C3", "否", "G1", "TOO_HARD"),
                      ("C4", "否", "G1", "TOO_HARD"), ("C5", "屬於且加嚴條件未全部成立", "G1", "TOO_HARD"),
                      ("M4", "否", "G3", "REJECT"), ("M5", "變窄", "G3", "REJECT"), ("M6", "可怕", "G3", "REJECT"))
MOAT_STRONG_IDS, MOAT_MIN_STRONG = ("M1", "M2", "M3"), 2
_STOP_RANK = {"TOO_HARD": 1, "REJECT": 2}


def gate3_verdict(tdir: Path) -> tuple[str, str, str | None, str] | None:
    """閘門 3 問卷（tdir/gate3_result.json、gate3_answers.json）→ 結果上界 (signal, reason_code, stopped_at_gate, 理由)；
    問卷通過 → None（D24）。停止級答案 → NO_ACTION＋TOO_HARD／REJECT（有 REJECT 取 REJECT）；
    有題答「不知道」→ NO_ACTION＋TOO_HARD（SKILL §0A）；其他不過（含缺結果）→ 最多 WATCH，reason_code＝GATE3_FAIL。"""
    tdir = Path(tdir)
    res = _load_json(tdir / "gate3_result.json") if (tdir / "gate3_result.json").is_file() else {}
    if res.get("gate3_pass") is True:
        return None
    try:
        raw = _load_json(tdir / "gate3_answers.json").get("answers")
    except WritebackError:
        raw = None
    ans = {str(q): str((a or {}).get("answer") or "").strip() for q, a in (raw or {}).items()
           if isinstance(a, dict) or a is None} if isinstance(raw, dict) else {}
    stops = [(code, gate, f"{q}＝{v}") for q, v, gate, code in GATE3_STOP_ANSWERS if ans.get(q) == v]
    strong = sum(ans.get(q) == "強" for q in MOAT_STRONG_IDS)
    if all(ans.get(q) in ("強", "弱") for q in MOAT_STRONG_IDS) and strong < MOAT_MIN_STRONG:
        stops.append(("REJECT", "G3", f"護城河三測強 {strong} 項＜{MOAT_MIN_STRONG}"))   # M2-RS2-02
    if stops:
        code, gate, _ = max(stops, key=lambda x: _STOP_RANK[x[0]])
        return ("NO_ACTION", code, gate, "閘門 3 問卷有停止級答案（" + "、".join(x[2] for x in stops)
                + f"），而 FULL_A 判通過：前後不一致，取較嚴的停止碼 {code}，不給 auto_buy_cap（D24）")
    unknown = sorted(q for q, v in ans.items() if v == "不知道")
    if unknown:
        return ("NO_ACTION", "TOO_HARD", "G1" if unknown[0].startswith("C") else "G3",
                f"閘門 3 問卷 {'、'.join(unknown)} 答「不知道」→ 太難（SKILL §0A；D24）")
    return ("WATCH", "GATE3_FAIL", None, "閘門 3 問卷不過 → 最多 WATCH（reason_code GATE3_FAIL；D24）")


def _apply_gate3_verdict(tdir: Path, card: dict, log: dict, signal: str, reason: str,
                         notes: list[str]) -> tuple[str, str, bool]:
    """依 gate3_verdict 收緊訊號與停止碼（只會更嚴）；回傳 (signal, reason, 是否撤銷 auto_buy_cap)。"""
    v = gate3_verdict(tdir)
    if v is None:
        return signal, reason, False
    vs, vr, gate, why = v
    if vs == "NO_ACTION":
        if signal == "NO_ACTION" and _STOP_RANK.get(reason, 0) >= _STOP_RANK[vr]:
            return signal, reason, True
        signal, reason = "NO_ACTION", vr
        card["stopped_at_gate"] = gate
        if "stopped_at_gate" in log:
            log["stopped_at_gate"] = gate
    else:
        if signal == "NO_ACTION":
            return signal, reason, False
        if signal == "BUY":
            signal = "WATCH"
        if reason == "PASS_ALL":
            reason = "GATE3_FAIL"
    if (signal, reason) != (card.get("signal"), card.get("reason_code")):
        notes.append(why + f"：{card.get('signal')}／{card.get('reason_code')} → {signal}／{reason}")
    card["signal"], card["reason_code"] = signal, reason
    card["conclusion"] = _conclusion(signal, reason)
    log["action"] = ACTION_BY_SIGNAL[signal]
    return signal, reason, vs == "NO_ACTION"


# ---------------------------------------------------------------- 對外函式

def write_full_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str,
                      tdir: Path, final_signal: str | None = None, notes: list[str] | None = None,
                      gates: dict | None = None, job_id: str | None = None,
                      user_hard_cap: float | None = None, framework_hold: bool = False) -> str:
    """FULL_B（RED_TEAM 若有）完成後回寫。回傳 decision_id。
    `framework_hold`：FULL_B 判 BUY、框架降成 WATCH（deep.FullBHandler.post）→ reason_code PASS_ALL 改 FRAMEWORK_HOLD（A3-M5B-01）。
    `user_hard_cap`：config account.user_hard_cap，登錄表目標權重 T 一併夾入（M6-D10-01）。

    讀 `tdir/signal_card.json`、`registry_entry.json`、`decision_log.json`、`thesis.md`；全部讀取與檢查通過後才寫入。
    `final_signal` 給定時覆寫訊號卡與日誌的訊號（日誌以 action 表示）；`notes` 併入日誌 framework_notes。
    `gates`：框架判定的三閘門（deep._framework_gates：gate1／gate2／gate3 bool、gate3_failures、gate2_failures，
    以及 questionnaire_version、gate3_answers_file、screen_decision、flag_codes、distress_codes、price_date、time_zone），
    給定時覆寫模型日誌的同名欄。登錄表的決策欄位由框架產生（白名單見 _registry_entry），模型檔只當報告文字。
    `job_id`：post 路徑的工作編號（冪等鍵，M6-D9-03）；同一工作已回寫過 → 直接回傳該決策編號，不再寫。
    持股（登錄表 HOLDING）的論點檔另立新版 `{T}_{asof}_v{n}.md`（含 predecessor），舊檔不動（D16）。
    """
    t = _std_ticker(ticker)
    _ymd(asof)
    done = _done_for_job(conn, job_id)
    if done is not None:   # 重跑：提交後步驟（cap_inputs 換上、同群判定）冪等補做（A3-M6E-02）
        _finish_full_replay(conn, paths, t, asof, done)
        return done
    tdir = Path(tdir)
    notes = list(notes or [])
    card = _unwrap(_load_json(tdir / "signal_card.json"), t, "signal_card.json", notes)
    model_entry = _unwrap(_load_json(tdir / "registry_entry.json"), t, "registry_entry.json", notes)
    log = _unwrap(_load_json(tdir / "decision_log.json"), t, "decision_log.json", notes)
    thesis = _read_text(tdir / "thesis.md")
    _card_numbers(card, notes)
    pa = _load_json(tdir / "phaseA.json") if (tdir / "phaseA.json").is_file() else {}
    summary = _load_json(tdir / "phaseB.json") if (tdir / "phaseB.json").is_file() else {}
    card_signal = card.get("signal")
    _apply_summary(card, summary, pa, model_entry, notes, final_signal)

    signal = card.get("signal")
    reason = card.get("reason_code")
    if signal != card_signal and signal in SIGNALS:   # 摘要改了訊號：日誌動作跟著改
        log["action"] = ACTION_BY_SIGNAL[signal]
    if final_signal is not None:
        if final_signal not in SIGNALS:
            raise WritebackError(f"final_signal 不合法：{final_signal!r}")
        if final_signal != signal:
            notes.append(f"final_signal 覆寫：{signal!r} → {final_signal!r}")
        card["signal"] = final_signal
        card["conclusion"] = _conclusion(final_signal, reason)
        log["action"] = ACTION_BY_SIGNAL[final_signal]
        signal = final_signal
    if signal not in SIGNALS:
        raise WritebackError(f"訊號卡 signal 不合法：{signal!r}")
    if not isinstance(reason, str) or not reason:
        raise WritebackError("訊號卡缺 reason_code")
    no_cap = False
    if gates is not None and gates.get("gate3") is not True:   # D24：問卷不過的結果上界（框架判閘門 3 未過時才對帳）
        signal, reason, no_cap = _apply_gate3_verdict(tdir, card, log, signal, reason, notes)
    if framework_hold and signal == "WATCH" and reason == "PASS_ALL":
        # A3-M5B-01：FULL_B 判 BUY、框架降成 WATCH（G8、P-32、G9、D26、價格盲缺口、持股前半）時 reason_code 不得仍是 PASS_ALL，
        # 否則 PRICE 快速通道讀「全過」又把它升回 ADD／BUY；改 FRAMEWORK_HOLD（PRICE_NO_BUY_REASONS，須重跑完整分析）。閘門值不改
        notes.append("框架降級 BUY → WATCH：reason_code PASS_ALL → FRAMEWORK_HOLD（PRICE 不得升成買進，須重跑完整分析；A3-M5B-01）")
        reason = card["reason_code"] = FRAMEWORK_HOLD
        card["conclusion"] = _conclusion(signal, reason)
    # 資料日不晚於賣光日（持有期間排入、賣光後才跑完）：上限與殖利率不回寫、BUY 當 WATCH（check-F08R 裁定）
    prev = _registry_get(conn, t)
    newer = _newer_state(conn, t, asof)
    if newer is not None and newer[0] == _NEWER_STATE_KEYS and signal == "BUY":
        # A3-M6B-02（D86 第二道防線）：資料日之後已有較新的完整分析 → 舊資料日的 BUY 不得與較新卡拼成可下單決策，當 WATCH
        notes.append(f"資料日 {asof} 之後已有較新的完整分析：舊資料日 BUY 當作 WATCH，不建立買進（D86、A3-M6B-02）")
        card["signal"] = "WATCH"
        card["conclusion"] = _conclusion("WATCH", reason)
        log["action"] = ACTION_BY_SIGNAL["WATCH"]
        signal = "WATCH"
    sold = str(((prev or {}).get("data") or {}).get("sold_out_at") or "")[:10]
    stale_sold = bool(sold) and sold >= asof
    if stale_sold and signal == "BUY":
        notes.append(f"{sold} 已賣光（資料日 {asof} 不晚於賣光日）：BUY 當作 WATCH，不建立建倉計畫")
        card["signal"] = "WATCH"
        card["conclusion"] = _conclusion("WATCH", reason)
        log["action"] = ACTION_BY_SIGNAL["WATCH"]
        signal = "WATCH"

    status = _resolve_status(conn, t, _status_for(signal, reason), notes)
    decision_id = next_decision_id(conn, t, asof)
    now = db.now_iso()

    # 論點檔（先寫；交易失敗時刪除）。持股的論點檔凍結不改（procedure/07 §2）：持有中完整複查另立新版，舊檔原封不動（D16）
    holding = prev is not None and prev["status"] == "HOLDING"
    open_buys = _open_buy_ids(conn, t)
    held = prev is not None and (holding or bool(open_buys))   # 未成交買單成交後即持股（M6-D9-01）
    stamp = _chain_stamp(conn, paths, t, asof, run_id)
    if stamp["hindsight"]:   # D25：報告（論點稿）與決策日誌都加註
        thesis = f"> 注意：{stamp['hindsight']}\n\n{thesis}"
        card["hindsight_flag"] = True
    main = main_thesis_path(paths, t, prev["data"].get("thesis_file")) if holding else None
    if main is not None:
        thesis_path = _write_thesis_version(paths, t, asof, main, thesis)
    else:
        thesis_path = _write_thesis(paths, t, asof, thesis)
    try:
        card["ticker"] = t
        card["decision_log_id"] = decision_id
        if not str(card.get("as_of") or "").startswith(asof):   # 最新卡依資料日排序（latest_signal）
            card["as_of"] = asof
        log["decision_id"] = decision_id
        log["ticker"] = t
        log["decision_date"] = asof
        if log.get("review_type") is not None:   # 完整分析的日誌沒有 review_type（M6-D10-05）
            notes.append(f"日誌 review_type 由模型填 {log['review_type']!r}，完整分析一律為 null")
        log["review_type"] = None
        if job_id:
            log["source_job_id"] = job_id
        log.update(_framework_log_fields(paths, tdir))
        _apply_stamp(stamp, log, notes)
        hs = pa.get("holding_stop") if isinstance(pa.get("holding_stop"), dict) else None
        if hs and hs.get("gate"):   # 設計決策 D30：持股版前半的未過閘門記為持有側旗標（與停止結果同格式）
            log["hold_flags"] = {"stopped_at_gate": hs["gate"], "reason_code": hs.get("reason_code")}
        cin = _load_json(tdir / "cap_input.json") if (tdir / "cap_input.json").is_file() else {}
        _reconcile_with_cap(tdir, card, model_entry, notes)  # 三條線與等級寫進 model_entry 與訊號卡
        entry = _registry_entry(model_entry, pa, cin, prev)
        for k in (*_CAP_LINES, "grade"):
            entry[k] = model_entry.get(k)
        t_w, tnotes = _framework_target(card, user_hard_cap)   # 訊號卡等級已對帳（M6-D10-01）
        if isinstance(card.get("intended_weight"), dict):   # D13：訊號卡目標權重改寫成框架 T（百分數），模型值只留在報告
            card["target_weight_pct"] = round(t_w * 100, 4)
            if isinstance(card.get("position_plan"), dict) and "target_weight_pct" in card["position_plan"]:
                card["position_plan"]["target_weight_pct"] = card["target_weight_pct"]
        entry["target_weight"] = t_w
        entry["target_weight_basis"] = {"decision_id": decision_id, "grade": card.get("certainty_grade"),
                                        "notes": tnotes}
        log.update({"grade": entry["grade"], "target_weight": t_w, "price": entry["review_price"]})
        if (tdir / "cap_result.json").is_file():   # 估值欄與 PRICE 日誌、訊號卡同口徑：取 cap_calc（M2_D9_06）
            cr = _load_json(tdir / "cap_result.json")
            log.update({"g_impl": cr.get("g_impl"), "r_d": cr.get("r_d"), "buy_price_cap": cr.get("cap"),
                        "price_to_cap": cr.get("price_to_auto_buy_cap"), "auto_buy_cap": cr.get("auto_buy_cap"),
                        "iv_base": cr.get("IV_base")})
        if no_cap or stale_sold:   # 登錄表不寫上限：這次 WATCH 不是價位判斷，review_jobs 90 天節流不採（A3-M6C-03）
            log["cap_not_applied"] = True
        if gates is not None:
            _apply_gates(log, gates, notes)
            # 訊號卡的閘門欄與日誌一致（M5_D7_01；訊號卡 schema 的閘門 3 欄名為 gate3_questionnaire_pass）
            abg = dict(card["auto_buy_gates"]) if isinstance(card.get("auto_buy_gates"), dict) else {}
            abg.update({"gate1_not_deleted": log["gate1_not_deleted"],
                        "gate2_price_below_cap": log["gate2_price_below_cap"],
                        "gate3_questionnaire_pass": log["gate3_pass"], "gate3_failures": log["gate3_failures"]})
            if "gate3_answers_file" in gates:
                abg["gate3_answers_file"] = gates["gate3_answers_file"]
            if gates.get("gate1_warning"):   # D17：12 個月內無初篩判定 → 閘門 1 通過但記警示（訊號卡＋日誌）
                abg["gate1_warning"] = card["gate1_warning"] = gates["gate1_warning"]
                notes.append(gates["gate1_warning"])
            card["auto_buy_gates"] = abg
        # P-31：外部變數判定寫進登錄表（之後只能因新事實改判）；金融與否供 P-32 減損用
        from ..portfolio import correlation as corr
        vocab = corr.load_vocab(paths.skill_deep)
        entry["asset_class"] = pa.get("asset_class") or card.get("asset_class") or \
            (prev["data"].get("asset_class") if prev else None)
        dnotes: list[str] = []   # deep._corr_writeback 已判過一次：同一則註記不重複（M6-D10-04）
        card["external_drivers"] = corr.apply_drivers_to_entry(entry, prev["data"] if prev else None, card, asof,
                                                               vocab, dnotes)
        notes += [n for n in dnotes if n not in notes]

        entry["ticker"] = t
        entry["status"] = status
        entry["last_reason_code"] = reason   # 初篩 build_exclusions 讀（NOT_THIS_SKILL → X1，M1-D2-12）
        if prev is not None and prev["data"].get("integrity_reject"):   # 誠信淘汰標記：仍 REJECT 才保留，否則清（D27）
            if reason == "REJECT":
                entry["integrity_reject"] = True
                notes.append("既有誠信淘汰標記 integrity_reject 保留：本次仍為 REJECT（D27）")
            else:
                notes.append(f"完整分析結論 {reason}（非 REJECT）：清除誠信淘汰標記 integrity_reject（D27）")
        entry["last_review_date"] = asof
        entry["last_full_review_date"] = asof
        # 存相對 state 的路徑：state 目錄搬移或複製後仍可解析（main_thesis_path）；持股為新版論點檔（D16）
        tf = thesis_path
        try:
            entry["thesis_file"] = Path(tf).resolve().relative_to(paths.state.resolve()).as_posix()
        except ValueError:
            entry["thesis_file"] = str(tf)
        extra_sql: list[tuple[str, tuple]] = []
        if held:
            for k in _HELD_KEYS:
                if prev["data"].get(k) is not None:
                    entry[k] = prev["data"][k]
            if signal == "BUY" and entry.get("observe_until"):   # D76：完整分析判 BUY 即 P-50 結案；非 BUY 維持觀察期
                notes.append(f"完整分析判 BUY：P-50 觀察結案，清除 observe_until {entry.pop('observe_until')}（D76）")
            entry["full_review_weight"] = entry.get("weight")   # review_due 權重 >25% 邊緣觸發的基準（M6-D4-03）
            bp = entry.get("build_plan")
            if isinstance(bp, dict):   # 舊建倉計畫：目標不得高於新 T；結論非 BUY → 剩餘批次歸 0（M6-D4-05）
                bp = dict(bp)
                old_t = _finite(bp.get("target_weight"))
                if old_t is not None and old_t > t_w:
                    notes.append(f"建倉計畫目標權重以新框架 T 為上限：{old_t} → {t_w}")
                    bp["target_weight"] = t_w
                if signal != "BUY" and int(_finite(bp.get("tranches_left")) or 0) > 0:
                    notes.append(f"新結論 {signal} 非 BUY：建倉計畫剩餘批次歸 0")
                    bp["tranches_left"] = 0
                entry["build_plan"] = bp
                # 只在建倉進行中（尚有剩餘批次）才取較小值；已建完 → 登錄表用新框架 T，不被舊目標永久壓住（M3-R7-01）
                if _finite(bp.get("target_weight")) is not None and int(_finite(bp.get("tranches_left")) or 0) > 0:
                    entry["target_weight"] = min(t_w, float(bp["target_weight"]))
        if open_buys and signal != "BUY":   # 結論已不是 BUY：未成交買單取消（M6-D9-01）
            notes.append(f"新結論 {signal} 非 BUY：取消未成交買單 {open_buys}")
            extra_sql.append(_cancel_buys_sql(t, f"新分析結論非 BUY（{decision_id}）"))

        # 本次估值用的長債輸入（cap_calc 輸入）存進 state/cap_inputs，供 PRICE 快速通道重算
        cap_src = Path(tdir) / "cap_input.json"
        cap_dst = None
        if cap_src.is_file():
            cap_dst = paths.state / "cap_inputs" / f"{t}.json"
            entry["cap_input_file"] = f"cap_inputs/{t}.json"   # 相對 state，換機後仍可讀（M8-R4-01）
        keep = prev["data"] if prev else {}
        if sold:
            entry["sold_out_at"] = keep["sold_out_at"]   # 賣光紀錄保留（write_price_result 以它判斷是否回寫上限）
        if stale_sold:
            notes.append(f"{sold} 已賣光，資料日 {asof} 不晚於賣光日：上限不回寫（auto_buy_cap、t30_at_valuation 留空），"
                         "需新的完整分析")
            entry["auto_buy_cap"] = None
            entry["t30_at_valuation"] = None
        if no_cap:   # D24：問卷停止級／不知道 → 不給自動買進上限
            entry["auto_buy_cap"] = None
            card["auto_buy_cap"] = None
        _red_light_rerun(entry, reason, asof, notes, keep.get("last_reason_code"),
                         red_light_context(tdir, asof) if reason == "RED_LIGHT" else None)   # D92
        if reason == "GATE3_FAIL" and keep.get("last_reason_code") != "GATE3_FAIL":
            # D24／SKILL §0：問卷與 FULL_A 前後不一致 → 下個觀察週期重跑完整分析一次；連續兩次 GATE3_FAIL 不再自動重跑（M2-RS2-03）
            due = (date.fromisoformat(asof[:10]) + timedelta(days=RED_LIGHT_RERUN_DAYS)).isoformat()
            entry["next_full_review_due"] = due
            notes.append(f"閘門 3 問卷不過（GATE3_FAIL）：{due} 重跑完整分析一次（M2-RS2-03）")
        # 複查日期不往回改（TRIAGE／THESIS 可能已推遲 last_review_date）
        entry["last_review_date"] = _later(keep.get("last_review_date"), asof)
        entry["last_full_review_date"] = _later(keep.get("last_full_review_date"), asof)
        if newer is not None:   # 舊資料日：仍留決策紀錄，但不蓋掉較新的狀態（M6-D6-07、M7-D10-01）
            keys, why = newer
            notes.append(why)
            entry = {**{k: v for k, v in entry.items() if k not in keys}, **{k: keep[k] for k in keys if k in keep}}
            if "cap_input_file" in keys:   # 只有較新 PRICE 時 cap_inputs 照本次分析換上（A3-M6D-03）
                cap_dst = None
            stale_keys = (_NEWER_STATE_KEYS, _PRICE_STATE_KEYS)   # 只有較新 PRICE 時也保留建倉計畫、委託與狀態（A3-M6D-03 只放寬等級／T／cap_inputs）
            if keys in stale_keys and prev is not None and "build_plan" in keep:
                # 較新分析之後的建倉計畫不被舊資料日的結論改寫（剩餘批次、目標；M6-RS4-01）
                entry["build_plan"] = keep["build_plan"]
                notes[:] = [n for n in notes if not (n.startswith(f"新結論 {signal} 非 BUY：建倉計畫剩餘批次歸 0")
                                                     or n.startswith("建倉計畫目標權重以新框架 T 為上限"))]
            if keys in stale_keys and any(str(s[0]).lstrip().startswith("UPDATE orders") for s in extra_sql):
                notes[:] = [n for n in notes if not n.startswith(f"新結論 {signal} 非 BUY：取消未成交買單")]
                notes.append("資料日較舊：未成交買單屬較新結論，不取消（M6-RS4-01）")
                extra_sql = [s for s in extra_sql if not str(s[0]).lstrip().startswith("UPDATE orders")]
            if keys in stale_keys and prev is not None:   # 較新決策（含停止結果）的狀態與拒絕依據保留（F05R2）
                if prev["status"] != status:
                    notes.append(f"登錄表狀態保留較新決策的 {prev['status']}（本次結果 {status} 未覆寫）")
                status = prev["status"]
                entry["status"] = status
                for k in ("reject_basis", "last_reason_code", "integrity_reject"):
                    if k in keep:
                        entry[k] = keep[k]
                if keep.get("integrity_reject"):   # 標記實際保留：先前的「清除」註記改正
                    notes[:] = [n.replace("清除誠信淘汰標記 integrity_reject（D27）",
                                          "誠信淘汰標記 integrity_reject 保留、未清除（資料日較舊，較新決策的標記不動；D27）")
                                for n in notes]
                if main is not None and keep.get("thesis_file"):   # 舊資料日的新版論點檔留存，不取代較新分析的主檔（D16）
                    notes.append(f"資料日 {asof} 較舊：登錄表論點檔維持 {keep['thesis_file']}"
                                 f"（本次新版 {Path(thesis_path).name} 留存）")
                    entry["thesis_file"] = keep["thesis_file"]
        nrev = _newer_review(conn, t, asof) if prev is not None else None
        if nrev is not None:
            # A3-M6D-02（D86 第二道防線）：資料日之後已有 THESIS／HOLD_REVIEW → 舊資料日的完整分析不清掉其後開的 P-50 觀察期
            # （D76 只在資料日不早於最近疑似時結案）、等級取較嚴者、失效條件既有條目（checked_on／status）保留只新增
            src = f"{nrev['review_type']} {nrev['decision_id']}（{nrev['decision_date']}）"
            if held and nrev["suspect"] and keep.get("observe_until") and entry.get("observe_until") != keep["observe_until"]:
                entry["observe_until"] = keep["observe_until"]
                notes[:] = [n for n in notes if not n.startswith("完整分析判 BUY：P-50 觀察結案")]
                notes.append(f"資料日 {asof} 之後有 {src}：P-50 觀察期 {keep['observe_until']} 保留，不結案（D76、A3-M6D-02）")
            from .checks import worse_grade
            g = worse_grade(entry.get("grade"), nrev["grade_after"]) if nrev["grade_after"] else None
            if g is not None and g != entry.get("grade"):
                notes.append(f"資料日 {asof} 之後有 HOLD_REVIEW 降等（{nrev['grade_after']}）：等級取較嚴者 "
                             f"{entry.get('grade')!r} → {g!r}（A3-M6D-02）")
                entry["grade"] = g
            kd = _merge_kill_due(entry.get("kill_criteria_due"), keep.get("kill_criteria_due"))
            if kd != entry.get("kill_criteria_due"):
                entry["kill_criteria_due"] = kd
                notes.append(f"資料日 {asof} 之後有 {src}：既有失效條件的檢查紀錄保留，本次只新增條件（A3-M6D-02）")
        why_price = _newer_thesis_price(conn, t, asof)
        if why_price is not None and "review_price" in keep and entry.get("review_price") != keep["review_price"]:
            notes.append(why_price)
            entry["review_price"] = keep["review_price"]
        if notes:
            log["framework_notes"] = list(log.get("framework_notes") or []) + notes

        reg_row: dict[str, Any] = {"ticker": t, "status": status, "data": entry, "updated_at": now}
        if entry.get("first_buy_date"):
            reg_row["first_buy_date"] = entry["first_buy_date"]

        # cap_inputs（M6-D9-02）：交易前先複製到暫存檔；交易失敗刪暫存；提交後才 os.replace 換上（移出 try：
        # 換檔失敗只記異常，不刪已被決策與登錄表引用的論點檔）
        cap_tmp = None
        if cap_dst is not None:
            cap_dst.parent.mkdir(parents=True, exist_ok=True)
            cap_tmp = cap_dst.with_name(cap_dst.name + ".tmp")
            shutil.copyfile(cap_src, cap_tmp)
        try:
            _write_rows(conn, [
                ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                               "job_id": None, "signal": signal, "reason_code": reason, "data": log,
                               "created_at": now}, None),
                ("signals", {"decision_id": decision_id, "ticker": t, "signal": signal, "data": card,
                             "created_at": now}, None),
                ("registry", reg_row, ["ticker"]),
            ], extra_sql)
        except BaseException:
            if cap_tmp is not None:
                cap_tmp.unlink(missing_ok=True)
            raise
    except BaseException:
        thesis_path.unlink(missing_ok=True)
        raise
    if cap_tmp is not None:
        try:
            os.replace(cap_tmp, cap_dst)
        except OSError as e:
            db.insert(conn, "anomalies", {"ticker": t, "kind": "cap_input_replace_failed", "status": "open",
                                          "pause_trading": 0, "created_at": db.now_iso(),
                                          "detail": json.dumps({"decision_id": decision_id, "dst": str(cap_dst),
                                                                "tmp": str(cap_tmp), "error": f"{type(e).__name__}: {e}"},
                                                               ensure_ascii=False)})
    _record_links_safely(conn, paths, t, asof)
    return decision_id


def _finish_full_replay(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, decision_id: str) -> None:
    """A3-M6E-02：完整分析已提交、os.replace／_record_links_safely 之前被中斷（SIGTERM）後 jobs retry →
    cap_inputs/{T}.json.tmp 仍在、登錄表 cap_input_file 指向 {T}.json、且之後沒有較新的完整分析 → 換上；再補同群判定（已去重）。"""
    tmp = paths.state / "cap_inputs" / f"{ticker}.json.tmp"
    reg = _registry_get(conn, ticker)
    if tmp.is_file() and reg is not None and reg["data"].get("cap_input_file") == f"cap_inputs/{ticker}.json":
        me = conn.execute("SELECT rowid FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
        later = me is not None and conn.execute(
            f"SELECT 1 FROM decisions WHERE ticker = ? AND rowid > ? AND job_id IS NULL AND {NOT_VOIDED} "
            "AND json_extract(data, '$.review_type') IS NULL", (ticker, me["rowid"])).fetchone() is not None
        if me is not None and not later:
            try:
                os.replace(tmp, tmp.with_name(f"{ticker}.json"))
            except OSError as e:
                db.insert(conn, "anomalies", {"ticker": ticker, "kind": "cap_input_replace_failed", "status": "open",
                                              "pause_trading": 0, "created_at": db.now_iso(),
                                              "detail": json.dumps({"decision_id": decision_id, "tmp": str(tmp),
                                                                    "replay": True, "error": f"{type(e).__name__}: {e}"},
                                                                   ensure_ascii=False)})
    _record_links_safely(conn, paths, ticker, asof)


def _record_links_safely(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str) -> None:
    """同群判定寫進兩檔論點檔與登錄表（P-31）；失敗只記異常，不讓已提交的回寫失敗。"""
    from ..portfolio import correlation as corr
    try:
        corr.record_group_links(conn, paths, ticker, asof)
    except Exception as e:  # noqa: BLE001
        if conn.in_transaction:
            conn.rollback()
        db.insert(conn, "anomalies", {"ticker": ticker, "kind": "corr_link_failed",
                                      "detail": json.dumps({"date": asof, "error": f"{type(e).__name__}: {e}"},
                                                           ensure_ascii=False),
                                      "status": "open", "pause_trading": 0, "created_at": db.now_iso()})


_MOAT_KEYS = ("pricing_power", "attack_copy", "price_cut")


def reject_basis(phase_a: dict) -> str | None:
    """G3 REJECT 的依據（只做紀錄，不影響 status／X5／重評）。

    已有 ≥2 項強 → 「其他」（擋下的是生存篩或方向）；強＋缺資料 ≥2 → 「證據不足」；其餘 → 「有反證」。
    非 G3 REJECT、或沒有 moat_evidence（舊格式）→ None。
    """
    if phase_a.get("reason_code") != "REJECT" or phase_a.get("stopped_at_gate") != "G3":
        return None
    tests, ev = phase_a.get("moat_tests") or {}, phase_a.get("moat_evidence")
    if not isinstance(ev, dict) or not all(ev.get(k) in ("足夠", "缺資料") for k in _MOAT_KEYS):
        return None
    strong = sum(1 for k in _MOAT_KEYS if tests.get(k) == "強")
    if strong >= 2:
        return "其他"
    unproven = sum(1 for k in _MOAT_KEYS if tests.get(k) != "強" and ev.get(k) == "缺資料")
    return "證據不足" if strong + unproven >= 2 else "有反證"


# 一句話理由可能換行續寫（下一行以空白縮排開頭），只取第一行會截斷（2026-10-18 ALL 案例）
_REPORT_ONE_LINER = re.compile(r"^一句話理由[：:][ \t]*(.+(?:\n[ \t]+\S.*)*)", re.M)


def _stop_one_liner(phase_a: dict, tdir: Path | None = None) -> str | None:
    """停止理由一句話：reason_one_liner（舊呼叫端）→ thesis_draft.one_liner（FULL_A 格式）→ report_A.md 結論卡。"""
    td = phase_a.get("thesis_draft")
    for s in (phase_a.get("reason_one_liner"), td.get("one_liner") if isinstance(td, dict) else None):
        if isinstance(s, str) and s.strip():
            return s.strip()
    if tdir is not None and (tdir / "report_A.md").is_file():
        m = _REPORT_ONE_LINER.search(_read_text(tdir / "report_A.md"))
        if m:
            return " ".join(x.strip() for x in m.group(1).split("\n") if x.strip())
    return None


_STOP_LOG_FIELDS = ("screen_decision", "flag_codes", "distress_codes", "time_zone")   # 停止結果由框架補的日誌欄（M8-D2-04）


def write_stop_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str,
                      phase_a: dict, notes: list[str] | None = None, tdir: Path | None = None, *,
                      log_fields: dict | None = None, job_id: str | None = None,
                      hold_no_advance: bool = False, non_analytic: bool | None = None) -> str:
    """FULL_A 停止（stopped=true）或 DOSSIER／FULL_A 無法完成時回寫最小三件。回傳 decision_id。

    訊號 NO_ACTION；reason_code＝phase_a["reason_code"]（缺則 MISSING_DATA）；stopped_at_gate＝phase_a["stopped_at_gate"]；
    certainty_grade＝null。欄位以三個 schema 為骨架，無法得出的欄位為 null。不寫論點檔。
    rule_ids（卡與日誌；缺 → []，RM2-9）、phase_a 已有的 missing（→ missing_data）、inferred_thresholds_used、asset_class 照寫（M6-D5-01）；
    log_fields：框架已知的 screen_decision、flag_codes、distress_codes、time_zone（M8-D2-04）。
    job_id：post 路徑的冪等鍵（M6-D9-03），同一工作已回寫過 → 回傳該決策編號。
    hold_no_advance：持股 FULL_A 退回後仍違規停止（A3-M3B-01；沒有估值）→ 不推進 last_full_review_date，
    next_full_review_due＝決策日＋HOLD_STOP_RETRY_DAYS（下季再做年度完整複查）。
    non_analytic（D80、A3-M5A-01）：呼叫端明示本停止是否為非分析性（使用者拒絕強模型、模型輸出不合格／缺輸出檔）；
    True 時不論 stopped_at_gate 記在哪一關（G6、G7 等）都不否決先前閘門、不撤買單（reason 須為 MISSING_DATA）；
    False＝分析性（D20 金融前置檢查、資料包建立失敗等資料檢查不過）；None＝舊判斷（STEP0＋MISSING_DATA）。
    非分析性停止日誌另記 non_analytic_stop=true（sizing.nonanalytic_stop 讀）；非持股觀察檔依 D87 以既有登錄表為底。
    """
    t = _std_ticker(ticker)
    _ymd(asof)
    done = _done_for_job(conn, job_id)
    if done is not None:
        return done
    if not isinstance(phase_a, dict):
        raise WritebackError("phase_a 須為 dict")
    reason = phase_a.get("reason_code") or "MISSING_DATA"
    gate = phase_a.get("stopped_at_gate")
    notes = list(notes or [])
    now = db.now_iso()

    sig_schema = _load_schema(paths, "signal_card_schema.json")
    reg_schema = _load_schema(paths, "registry_schema.json")
    log_schema = _load_schema(paths, "decision_log_schema.json")

    decision_id = next_decision_id(conn, t, asof)
    conclusion = _conclusion("NO_ACTION", reason)

    card = _blank_from_fields(sig_schema)
    card.update({
        "schema": sig_schema.get("schema"),
        "stop_mode": STOP_MODE,
        "as_of": asof,
        "ticker": t,
        "conclusion": conclusion,
        "signal": "NO_ACTION",
        "reason_code": reason,
        "stopped_at_gate": gate,
        "certainty_grade": None,
        "red_light": reason == "RED_LIGHT",
        "decision_log_id": decision_id,
    })
    rule_ids = list(phase_a.get("rule_ids") or []) if isinstance(phase_a.get("rule_ids"), list) else []
    card["rule_ids"] = rule_ids   # 停止卡必填規則編號（SKILL §4）；缺 → []，處理器另記警示（RM2-9）
    for src, dst in (("missing", "missing_data"),
                     ("inferred_thresholds_used", "inferred_thresholds_used"), ("asset_class", "asset_class")):
        if not _empty_val(phase_a.get(src)):   # 前半已有的資訊不丟（M6-D5-01、M2-D5-09）
            card[dst] = phase_a[src]
    one_liner = _stop_one_liner(phase_a, tdir) or f"停止於 {gate or '未知關卡'}，停止碼 {reason}"
    card["one_liner"] = one_liner   # 結論卡一句話理由與日誌同一句（RM2-9）

    log = _blank_from_fields(log_schema.get("fields") or {})
    log.update({
        "decision_id": decision_id,
        "decision_date": asof,
        "ticker": t,
        "action": "NO_ACTION",
        "stopped_at_gate": gate,
        "reason_one_liner": one_liner,
        "stop_mode": STOP_MODE,
        "reject_basis": reject_basis(phase_a),
    })
    log["rule_ids"] = rule_ids
    for k in _STOP_LOG_FIELDS:
        if (log_fields or {}).get(k) is not None:
            log[k] = log_fields[k]
    if job_id:
        log["source_job_id"] = job_id
    log.update(_framework_log_fields(paths, tdir))
    _apply_stamp(_chain_stamp(conn, paths, t, asof, run_id), log, notes)
    # D80（M6_S7_N01）：STEP0 MISSING_DATA（使用者拒絕強模型、模型輸出不合格）不是對公司的判斷：三閘門寫 None
    # （sizing._latest_gate_decision、review_jobs._gate3_info 遇 None 跳過、往前找），不取消未成交買單、不歸 0 建倉批次。
    if non_analytic is None:
        non_analytic = gate == "STEP0" and reason == "MISSING_DATA"
    else:   # 呼叫端明示（A3-M5A-01）：不靠 gate 字串判斷；非分析性只限缺資料停止
        non_analytic = bool(non_analytic) and reason == "MISSING_DATA"
    if non_analytic:
        log.update({"gate1_not_deleted": None, "gate2_price_below_cap": None, "gate3_pass": None,
                    "gate3_failures": [], "non_analytic_stop": True})
        notes.append(f"{gate or 'STEP0'} 缺資料停止（非分析性，D80）：三閘門未判定（null），不否決先前閘門紀錄；"
                     "不取消未成交買單、不歸 0 建倉批次")
    else:
        # 完整分析停止＝本次三閘門未過：明確寫 False，加碼許可與 PRICE 的閘門 3 不得回頭沿用較早的「通過」（M6_D7_01）。
        # 閘門 1 本次未判定（停止結果沒有初篩判定）→ null；不寫 gate3_failures，以免與問卷不過原因混在一起統計
        log.update({"gate1_not_deleted": None, "gate2_price_below_cap": False, "gate3_pass": False,
                    "gate3_failures": []})
        notes.append(f"完整分析停在 {gate or '未知關卡'}（{reason}）：三閘門視為未過，停止加碼直到下一次完整分析通過")
    status = _resolve_status(conn, t, _status_for("NO_ACTION", reason), notes)

    entries = reg_schema.get("entries") or [{}]
    prev = _registry_get(conn, t)
    # 舊資料日（M6_S6-NF-1）：資料日之後已有寫過價格線的決策 → 登錄表維持較新狀態，只留決策紀錄
    newer = _newer_state(conn, t, asof) if prev is not None else None
    # 較新的 PRICE 也算（停止結果的登錄表沒有價格線可留，維持只留決策紀錄；A3-M6D-03 只放寬完整分析）
    stale = newer is not None and newer[0] in (_NEWER_STATE_KEYS, _PRICE_STATE_KEYS)
    open_buys = _open_buy_ids(conn, t)
    extra_sql: list[tuple[str, tuple]] = []
    if open_buys and not stale and not non_analytic:   # 停止結論不是 BUY：未成交買單取消（M6-D9-01）
        notes.append(f"完整分析停止：取消未成交買單 {open_buys}")
        extra_sql.append(_cancel_buys_sql(t, f"新分析結論非 BUY（{decision_id}）"))
    if prev is not None and (prev["status"] == "HOLDING" or open_buys):
        # 持股（或有未成交買單）：保留既有登錄表資料（上限、論點檔、建倉日、失效條件），只更新複查日期；
        # 動作由 HOLD_REVIEW 決定。持股的剩餘批次由上面寫入的未過閘門擋住；有未成交買單時批次歸 0（M6-D9-01）
        entry = {**prev["data"], "ticker": t, "status": status,
                 "last_review_date": _later(prev["data"].get("last_review_date"), asof)}
        if reason != "MISSING_DATA" and not hold_no_advance:   # 缺資料沒做成完整分析：不推進 last_full_review_date（M3-D10-01）
            entry["last_full_review_date"] = _later(prev["data"].get("last_full_review_date"), asof)
            entry["full_review_weight"] = entry.get("weight")   # review_due 權重 >25% 邊緣觸發的基準（M6-D4-03）
        bp = entry.get("build_plan")
        if open_buys and not stale and not non_analytic and isinstance(bp, dict) \
                and int(_finite(bp.get("tranches_left")) or 0) > 0:
            entry["build_plan"] = {**bp, "tranches_left": 0}
            notes.append("完整分析停止：建倉計畫剩餘批次歸 0")
        if not non_analytic:   # 本機做成的停止結論：他機來源標記 origin 清除（A3-M8B-07；舊資料日分支下面整段還原 prev）
            entry.pop("origin", None)
        if prev["status"] == "HOLDING":   # 設計決策 D30：持股的停止點記為持有側旗標，不清空登錄表
            log["hold_flags"] = {"stopped_at_gate": gate, "reason_code": reason}
        if not _empty_val(phase_a.get("asset_class")):
            entry["asset_class"] = phase_a["asset_class"]
    elif non_analytic and prev is not None:
        # D87（A3-M6E-04）：非分析性停止不是對公司的判斷 → 非持股觀察檔以既有登錄表為底，只更新 status、last_reason_code、
        # last_review_date；估值欄、價格線、等級、T、kill_criteria_due、integrity_reject 保留，不推進 last_full_review_date
        entry = {**prev["data"], "ticker": t, "status": status,
                 "last_review_date": _later(prev["data"].get("last_review_date"), asof)}
        notes.append("非分析性停止（D87）：觀察檔估值欄、價格線、等級、目標權重、失效條件保留，不推進完整複查日")
    else:
        entry = _blank_from_fields(entries[0] if isinstance(entries[0], dict) else {})
        if prev is not None:   # P-31 判定、論點檔、CIK 不因停止而清掉（M6-D9-05）
            entry.update({k: prev["data"][k] for k in _STOP_CARRY_KEYS if prev["data"].get(k) is not None})
        entry.update({
            "ticker": t,
            "status": status,
            "grade": None,
            "last_review_date": _later((prev or {}).get("data", {}).get("last_review_date"), asof),
            "last_full_review_date": ((prev or {}).get("data", {}).get("last_full_review_date") if hold_no_advance
                                      else _later((prev or {}).get("data", {}).get("last_full_review_date"), asof)),
            "reject_basis": log["reject_basis"],
        })
        if not _empty_val(phase_a.get("asset_class")):
            entry["asset_class"] = phase_a["asset_class"]
    n0 = len(notes)   # 以下為登錄表寫入的註記；舊資料日（stale）整段捨棄
    entry["last_reason_code"] = reason   # 初篩 build_exclusions 讀（NOT_THIS_SKILL → X1，M1-D2-12）
    # 誠信淘汰（NEVER-04(a)）：review_due 現任決策者在位期間只對 8-K 5.02 排 FULL（D27）。
    # 仍為 REJECT 時沿用既有標記；只有通過前半的完整分析（write_full_result 非 REJECT）才清（M6-RS1-01）
    if reason == "REJECT" and any(_INTEGRITY_RULE.match(r.strip()) for r in rule_ids if isinstance(r, str)):
        entry["integrity_reject"] = True
        notes.append("誠信淘汰（NEVER-04(a)）：登錄表標 integrity_reject，現任決策者在位期間只看 8-K 5.02（D27）")
    elif reason == "REJECT" and prev is not None and prev["data"].get("integrity_reject"):
        entry["integrity_reject"] = True
        notes.append("既有誠信淘汰標記 integrity_reject 保留：本次仍為 REJECT（D27）")
    if not non_analytic:   # D87：非分析性停止不動紅燈重跑排程與紅燈理由
        _red_light_rerun(entry, reason, asof, notes, ((prev or {}).get("data") or {}).get("last_reason_code"),
                         red_light_context(tdir, asof) if reason == "RED_LIGHT" else None)   # D92
    if hold_no_advance:   # A3-M3B-01：沒有估值的持股複查不算完成，下季再排年度完整分析
        entry["next_full_review_due"] = (date.fromisoformat(asof[:10]) + timedelta(days=HOLD_STOP_RETRY_DAYS)).isoformat()
        notes.append(f"持股 FULL_A 違規停止（應填 holding_stop）：last_full_review_date 不推進，"
                     f"next_full_review_due＝{entry['next_full_review_due']}（A3-M3B-01）")
    if stale:   # 較新決策的登錄表狀態（status、上限、等級、T、建倉計畫、拒絕依據、誠信標記、紅燈重跑）原樣保留
        del notes[n0:]
        entry = {**prev["data"]}
        status = prev["status"]
        if reason != "MISSING_DATA" and not hold_no_advance:   # 本次仍是做成的完整分析：完整複查日不往回改地推進（M3-D10-01）
            entry["last_full_review_date"] = _later(prev["data"].get("last_full_review_date"), asof)
        notes.append(f"{newer[1]}；本次停止結果只留決策紀錄，登錄表狀態保留較新決策（M6_S6-NF-1）")
    elif newer is not None:   # 只有拆合股：保留已換算的每股欄位與 cap_inputs（M7-D10-01）
        entry.update({k: prev["data"][k] for k in newer[0] if k in prev["data"]})
        notes.append(newer[1])
    if notes:
        log["framework_notes"] = notes
    reg_row: dict[str, Any] = {"ticker": t, "status": status, "data": entry, "updated_at": now}

    _write_rows(conn, [
        ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                       "job_id": None, "signal": "NO_ACTION", "reason_code": reason, "data": log,
                       "created_at": now}, None),
        ("signals", {"decision_id": decision_id, "ticker": t, "signal": "NO_ACTION", "data": card,
                     "created_at": now}, None),
        ("registry", reg_row, ["ticker"]),
    ], extra_sql)
    return decision_id


# ---------------------------------------------------------------- 觸發檢查（M4）

THESIS_VERDICTS = ("完好", "疑似", "證偽")
PRICE_ACTIONS = ("BUY", "ADD", "HOLD", "WATCH")   # PRICE 不寫 TRIM：減碼只有 HOLD_REVIEW 能決定（設計決策 D03、D28）
# HOLD 沿用上一張卡的訊號，但持股訊號卡不寫 BUY（上一張是 BUY → WATCH；M6-D1-04）
_PRICE_SIGNAL = {"BUY": "BUY", "ADD": "BUY", "WATCH": "WATCH"}
# D24：PRICE 快速通道不得把這些結論直接升成 BUY／ADD；FRAMEWORK_HOLD＝FULL_B 框架降級（A3-M5B-01），同樣須重跑完整分析
PRICE_NO_BUY_REASONS = ("GATE3_FAIL", "RED_LIGHT", FRAMEWORK_HOLD)


def _registry_get(conn: sqlite3.Connection, ticker: str) -> dict | None:
    row = conn.execute("SELECT status, data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return None
    return {"status": row["status"], "data": json.loads(row["data"] or "{}")}


def _reg_row(ticker: str, status: str | None, data: dict) -> dict[str, Any]:
    return {"ticker": ticker, "status": status, "data": data, "updated_at": db.now_iso()}


def signal_rows(conn: sqlite3.Connection, ticker: str, asof: str | None = None, *, full_only: bool = False,
                limit: int | None = None) -> list[dict]:
    """該檔訊號卡（新到舊）[{signal, data}]：依資料日（as_of 前 10 字）再依寫入時間；已作廢的卡不算（M8-D4-02）。
    asof：只取資料日 ≤ asof 的卡（A3-M6B-03：補跑舊資料日不得用到未來的卡）。full_only：只取完整分析（含停止結果）
    寫的卡＝決策日誌 review_type 為 null（A3-M6D-03：PRICE 沿用的來源）。latest_signal、sizing.latest_card、
    holdings_view._valued_card 共用這一份查詢。"""
    sql = f"SELECT signal, data FROM signals WHERE ticker = ? AND {NOT_VOIDED}"
    args: list[Any] = [ticker]
    if asof is not None:
        sql += " AND (json_extract(data, '$.as_of') IS NULL OR substr(json_extract(data, '$.as_of'), 1, 10) <= ?)"
        args.append(str(asof)[:10])
    if full_only:
        sql += (" AND decision_id IN (SELECT d.decision_id FROM decisions d WHERE d.ticker = signals.ticker "
                "AND json_extract(d.data, '$.review_type') IS NULL)")
    sql += " ORDER BY substr(json_extract(data, '$.as_of'), 1, 10) DESC, created_at DESC, rowid DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [{"signal": r["signal"], "data": json.loads(r["data"] or "{}")} for r in conn.execute(sql, args).fetchall()]


def latest_signal(conn: sqlite3.Connection, ticker: str, asof: str | None = None, *,
                  full_only: bool = False) -> dict | None:
    """該檔最近一張訊號卡：{signal, data}；依資料日（as_of 前 10 字）再依寫入時間，沒有回 None。
    等待同意數日才跑完的舊資料日完整分析不得變成最新卡（M6-D6-07）。已作廢的卡不算（M8-D4-02）。
    asof／full_only 見 signal_rows（A3-M6B-03、A3-M6D-03）。"""
    rows = signal_rows(conn, ticker, asof, full_only=full_only, limit=1)
    return rows[0] if rows else None


def main_thesis_path(paths: Paths, ticker: str, thesis_file: str | None = None) -> Path | None:
    """主論點檔：thesis_file 存在就用它；否則取 `{T}_YYYY-MM-DD.md`／`{T}_YYYY-MM-DD_v{n}.md`（D16 新版）中
    日期、版次最新者（排除 addendum）。沒有回 None。"""
    if thesis_file:
        p = Path(thesis_file)
        p = p if p.is_absolute() else paths.state / p
        if p.is_file():
            return p
        if (paths.theses / p.name).is_file():  # 舊紀錄的絕對路徑已失效（state 搬移）：同名檔
            return paths.theses / p.name
    t = _std_ticker(ticker)
    rx = re.compile(rf"^{re.escape(t)}_(\d{{4}}-\d{{2}}-\d{{2}})(?:_v(\d+))?\.md$")
    cands: list[tuple[str, int, Path]] = []
    for p in paths.theses.glob(f"{t}_*.md") if paths.theses.is_dir() else []:
        m = rx.match(p.name)
        if m:
            cands.append((m.group(1), int(m.group(2) or 1), p))
    return max(cands)[2] if cands else None


_CORR_ADDENDUM_HEAD = "# 相關群"   # correlation.record_group_links 的增補標題（相關群判定／改判）


def _write_thesis_version(paths: Paths, ticker: str, asof: str, main: Path, text: str) -> Path:
    """持股完整複查另立新版論點檔 `{T}_{asof}_v{n}.md`（D16）：開頭記版次與 predecessor（前版相對 state 的路徑），
    前版的相關群判定增補（P-31）原文附在文末沿用；前版與其增補原封不動（H3 驗屍對照建倉當時的版本）。獨占建立。"""
    m = re.search(r"_v(\d+)$", main.stem)
    n = int(m.group(1)) + 1 if m else 2
    try:
        pred = main.resolve().relative_to(paths.state.resolve()).as_posix()
    except ValueError:
        pred = str(main)
    carried = [x for x in (_read_text(a) for a in addendum_paths(main)) if x.startswith(_CORR_ADDENDUM_HEAD)]
    tail = ("\n\n## 沿用的相關群判定（前版增補原文，P-31）\n\n" + "\n\n".join(carried)) if carried else ""
    paths.theses.mkdir(parents=True, exist_ok=True)
    while True:
        p = paths.theses / f"{ticker}_{asof}_v{n}.md"
        body = (f"# {ticker} 論點第 {n} 版（持有中完整複查 {asof}，FULL_B）\n\n"
                f"- version: v{n}\n- predecessor: {pred}\n\n{text}{tail}")
        try:
            with open(p, "x", encoding="utf-8") as f:
                f.write(body)
            return p
        except FileExistsError:
            n += 1


def addendum_paths(main: Path) -> list[Path]:
    """主檔的增補檔 `{主檔名去 .md}_addendum_n.md`，依 n 升冪。"""
    rx = re.compile(rf"^{re.escape(main.stem)}_addendum_(\d+)\.md$")
    found = []
    for p in main.parent.iterdir():
        m = rx.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return [p for _, p in sorted(found)]


def _write_addendum(main: Path, text: str) -> Path:
    """在主檔旁新增 `{主檔名去 .md}_addendum_n.md`（n 從 1 起，獨占建立）。"""
    n = 1
    while True:
        p = main.with_name(f"{main.stem}_addendum_{n}.md")
        try:
            with open(p, "x", encoding="utf-8") as f:
                f.write(text)
            return p
        except FileExistsError:
            n += 1


def touch_review(conn: sqlite3.Connection, ticker: str, asof: str) -> None:
    """只更新登錄表該檔的 last_review_date（與 updated_at）。登錄表無該列時什麼都不做。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    reg = _registry_get(conn, t)
    if reg is None:
        return
    data = {**reg["data"], "last_review_date": _later(reg["data"].get("last_review_date"), asof)}   # 不往回改
    _write_rows(conn, [("registry", _reg_row(t, reg["status"], data), ["ticker"])])


def _thesis_addendum_text(ticker: str, asof: str, verdict: str, action: str, result: dict) -> str:
    lines = [f"# {ticker} 論點檢查（THESIS）{asof}", "", f"判定：{verdict}　動作：{action}", "",
             f"理由：{result.get('reason') or ''}", "", "## 失效條件"]
    for k in result.get("kill_criteria_status") or []:
        if isinstance(k, dict):
            lines.append(f"- {k.get('criterion', '')}：{k.get('status', '')}（{k.get('evidence', '')}）")
    lines += ["", "## R 清單（SELL-011）"]
    for r in result.get("r_list_status") or []:
        if isinstance(r, dict):
            lines.append(f"- {r.get('item', '')}：{r.get('status', '')}（{r.get('evidence', '')}）")
    lines += ["", "## 新事實"] + [f"- {x}" for x in result.get("new_facts") or []]
    return "\n".join(lines) + "\n"


OBSERVE_DAYS = 183   # P-50 觀察期：兩次季報或 6 個月（取 6 個月）


def _p50_expired(rd: dict, asof: str) -> str | None:
    """P-50 觀察期已到期且未結案（原期限 ≤ 決策日、之後沒有完整分析）→ 回傳原期限；否則 None。
    HOLD_REVIEW（_p50_observe_until）與 THESIS 疑似共用（M6-RS3-01）；review_due 以同條件排 FULL 結案。"""
    old = _valid_date(str((rd or {}).get("observe_until") or "")[:10])
    if old is not None and old <= asof and str((rd or {}).get("last_full_review_date") or "")[:10] < old:
        return old
    return None


def _p50_observe_until(reg: dict | None, result: dict, asof: str, notes: list[str],
                       reasons: list | None = None) -> str | None:
    """HOLD_REVIEW 的 observe_until 由框架核對（RM2-3／M2_D4_04；rulebook P-50「到期必須結案」）：
    到期必須結案、不接受新期限：觸發理由（params.reasons，review_due 產生、沿深度鏈帶到 HOLD_REVIEW）含
    「P-50 觀察期到期」，或登錄表原期限 ≤ 決策日且 last_full_review_date < 原期限（到期後沒有完整分析）；
    原期限已過但之後有完整分析 → 已結案（本次 FULL／停止回寫推進 last_full 不算到期，RM2-13R2）；
    觀察中 → 不得晚於原期限；新開觀察（含已結案後的新疑似）→ 最長決策日＋OBSERVE_DAYS。不合法日期不採用。"""
    raw = result.get("observe_until")
    if not raw:
        return None
    new = _valid_date(raw)
    rd = (reg or {}).get("data") or {}
    old = _valid_date(str(rd.get("observe_until") or ""))
    if new is None:
        notes.append(f"observe_until {raw!r} 不是合法日期：不採用")
        return None
    due = any("P-50 觀察期到期" in str(r) for r in reasons or [])
    if old is not None and old <= asof:
        due = due or _p50_expired(rd, asof) is not None
        if not due:
            old = None   # 舊觀察期已結案：本次是新的觀察
    if due:
        notes.append(f"P-50 觀察期{f'已於 {old} ' if old else '已'}到期、必須結案：不接受新觀察期限 {new}（未證偽即解除觀察）")
        return None
    cap = old if old is not None else (date.fromisoformat(asof) + timedelta(days=OBSERVE_DAYS)).isoformat()
    if new > cap:
        notes.append(f"P-50 觀察期限 {new} 晚於上限 {cap}（{'原期限不得延後' if old else '最長 6 個月'}）：夾回 {cap}")
        return cap
    return new


# 設計決策 D02：證偽分流。前三類核對通過 → 直接 SELL_ALL（不升完整分析）；經濟特性／護城河惡化或核對不過 → 疑似＋HOLD_REVIEW
FALSIFY_TYPES = ("CONDITION_TRIGGERED", "TOP_MGMT_INTEGRITY", "ACCOUNTING", "ECONOMICS_DETERIORATION")
FALSIFY_DIRECT = FALSIFY_TYPES[:3]


def thesis_kill_criteria(params: dict | None) -> list[str]:
    """THESIS 的失效條件清單，依序編號 K1、K2…（提示與證偽核對同一來源）：params.kill_criteria（觸發檢查自登錄表
    kill_criteria_due 帶入；登錄表由凍結論點 FULL_A thesis_draft 與 FULL_B 增列寫成），缺則 params.kill_criteria_due 的 text。"""
    params = params or {}
    kc = params.get("kill_criteria")
    if not kc:
        kc = [x.get("text", "") for x in params.get("kill_criteria_due") or [] if isinstance(x, dict)]
    return [str(x) for x in kc if x]


def _norm_criterion(t: Any) -> str:
    """失效條件原文比對用：去空白，再去掉開頭的 K 編號（模型照抄提示清單「K1：原文」時仍比對得到）。"""
    from ..jobs.validate import _norm_text
    return re.sub(r"^K\d+[：:]", "", _norm_text(t))


def _falsify_problem(result: dict, kill_criteria: list[str], asof: str) -> str | None:
    """證偽的框架核對（D02）；通過回 None，否則回不通過的理由。
    誠信／會計類 condition_id 可為 null（D02 補充 22:45：凍結論點的失效條件幾乎不含這兩類），只核 evidence；有填仍須在清單內。"""
    from .dossier_prep import scrub_price_sentences
    ft = result.get("falsify_type")
    if ft not in FALSIFY_TYPES:
        return f"falsify_type 缺或不合法（{ft!r}）"
    if ft not in FALSIFY_DIRECT:
        return "理由是經濟特性或護城河惡化，屬需要判斷的理由"
    cid = str(result.get("condition_id") or "").strip().upper()
    m = re.fullmatch(r"K(\d+)", cid)
    if (cid or ft == "CONDITION_TRIGGERED") and (m is None or not 1 <= int(m.group(1)) <= len(kill_criteria)):
        return f"condition_id {result.get('condition_id')!r} 不在凍結論點的失效條件清單（K1–K{len(kill_criteria)}）"
    if ft == "CONDITION_TRIGGERED":
        raw = kill_criteria[int(m.group(1)) - 1]
        names = {_norm_criterion(raw), _norm_criterion(scrub_price_sentences(raw)[0].strip())}
        if not any(isinstance(k, dict) and k.get("status") == "已觸發" and _norm_criterion(k.get("criterion")) in names
                   for k in result.get("kill_criteria_status") or []):
            return f"{cid} 在 kill_criteria_status 未標「已觸發」"
    ev = result.get("evidence")
    if not isinstance(ev, list) or not ev or not all(
            isinstance(x, dict) and str(x.get("source") or "").strip() and _valid_date(x.get("date"))
            and _valid_date(x.get("date")) < asof for x in ev):
        return "evidence 須至少一筆，且每筆有來源、日期早於決策日"
    return None


def vet_falsification(result: dict, kill_criteria: list[str], asof: str) -> dict:
    """THESIS 證偽分流（D02）：核對通過或非證偽 → 原樣；否則改「疑似」並記 falsify_downgrade（理由），
    由呼叫端停止加碼、同 run 交 HOLD_REVIEW。已改過的結果再呼叫不變。"""
    if not isinstance(result, dict) or result.get("verdict") != "證偽":
        return result
    why = _falsify_problem(result, kill_criteria, asof)
    if why is None:
        return result
    return {**result, "verdict": "疑似", "falsify_downgrade": why,
            "reason": f"〔框架：模型判證偽但{why}，改為疑似，交持股複查（D02）〕" + str(result.get("reason") or "")}


def _thesis_kill_due(cur: Any, result: dict, asof: str, notes: list[str]) -> list[dict] | None:
    """THESIS 檢查後更新登錄表失效條件（M2-D5-12）：依原文比對 kill_criteria_status，記 checked_on／status；
    模型給的 next_due 晚於 asof → 前移 due（下次到期再檢查）；null 或缺 → due 不動（邊緣觸發不會再因同一 due 觸發）。
    本次 asof 早於既有 checked_on（舊資料日補跑）的條目不動。無可更新回 None。"""
    if not isinstance(cur, list) or not cur:
        return None
    by_text = {}
    for k in result.get("kill_criteria_status") or []:
        if isinstance(k, dict) and k.get("criterion"):
            by_text[_norm_criterion(k["criterion"])] = k
    out, changed = [], False
    for item in cur:
        k = by_text.get(_norm_criterion(item.get("text"))) if isinstance(item, dict) else None
        if k is None or str(item.get("checked_on") or "") > asof:
            out.append(item)
            continue
        new = {**item, "checked_on": asof, "status": k.get("status")}
        nd = _valid_date(k.get("next_due"))
        if nd and nd > asof:
            if nd != item.get("due"):
                notes.append(f"失效條件到期日前移：{str(item.get('text'))[:40]} {item.get('due')} → {nd}")
            new["due"] = nd
        out.append(new)
        changed = True
    return out if changed else None


def write_thesis_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str,
                        result: dict, params: dict | None = None, advance_review: bool | str = True, *,
                        job_id: str | None = None) -> str:
    """THESIS 完成後回寫：決策日誌（HOLDING：完好／疑似→HOLD、證偽→SELL_ALL；其他→WATCH）、
    證偽先經 vet_falsification 核對（D02）：不通過改疑似（停止加碼，HOLD_REVIEW 由 ThesisHandler 排）；
    論點增補檔、登錄表只更新 last_review_date 與 review_price（合併既有 data）。回傳 decision_id。
    advance_review：True → last_review_date 推進到 asof；False → 不動（申報未抓取，M5_D10-07）；
    日期字串 → 只推進到該日（有申報因篇幅未附：最早未附者的申報日，下次從該日起重讀，M5-D5-06）。
    job_id：post 路徑的冪等鍵（M6-D9-03），同一工作已回寫過 → 回傳該決策編號。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    done = _done_for_job(conn, job_id)
    if done is not None:
        return done
    if not isinstance(result, dict):
        raise WritebackError("THESIS 結果須為 dict")
    if result.get("verdict") not in THESIS_VERDICTS:
        raise WritebackError(f"THESIS verdict 不合法：{result.get('verdict')!r}")
    params = params or {}
    result = vet_falsification(result, thesis_kill_criteria(params), asof)   # D02：證偽須框架核對通過
    verdict = result.get("verdict")
    reg = _registry_get(conn, t)
    holding = (reg is not None and reg["status"] == "HOLDING") or bool(params.get("holding"))
    action = ("SELL_ALL" if verdict == "證偽" else "HOLD") if holding else "WATCH"
    hit = [k.get("criterion") for k in result.get("kill_criteria_status") or []
           if isinstance(k, dict) and k.get("status") == "已觸發"]

    decision_id = next_decision_id(conn, t, asof)
    now = db.now_iso()
    text = _thesis_addendum_text(t, asof, verdict, action, result)
    main = main_thesis_path(paths, t, params.get("thesis_file"))
    path = _write_addendum(main, text) if main is not None else _write_thesis(paths, t, asof, text)
    try:
        log = _blank_from_fields(_load_schema(paths, "decision_log_schema.json").get("fields") or {})
        log.update({
            "decision_id": decision_id, "decision_date": asof, "ticker": t, "action": action,
            "thesis_check": verdict, "failure_conditions_hit": hit,
            "review_trigger_hit": list(params.get("reasons") or []),
            "reason_one_liner": result.get("reason") or "", "price": params.get("price"),
            "price_date": params.get("price_date"), "review_type": "THESIS",
        })
        if job_id:
            log["source_job_id"] = job_id
        if verdict == "證偽" or result.get("falsify_downgrade"):   # D02 證偽分流的依據與核對結果
            log["falsify"] = {"type": result.get("falsify_type"), "condition_id": result.get("condition_id"),
                              "evidence": result.get("evidence"), "verified": verdict == "證偽",
                              "downgrade_reason": result.get("falsify_downgrade")}
        rows: list[tuple[str, dict, list[str] | None]] = [
            ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                           "job_id": None, "signal": action, "reason_code": None, "data": log,
                           "created_at": now}, None),
        ]
        if reg is not None:
            data = dict(reg["data"])
            fnotes: list[str] = []
            if advance_review is True:
                data["last_review_date"] = _later(reg["data"].get("last_review_date"), asof)
            elif advance_review:
                data["last_review_date"] = _later(reg["data"].get("last_review_date"), str(advance_review)[:10])
                fnotes.append(f"有申報因篇幅未附：last_review_date 只推進到 {data['last_review_date']}（下次從該日起重讀）")
            else:
                fnotes.append("申報未抓取：last_review_date 不推進")
            if params.get("also_price") or params.get("base_action") == "PRICE":
                # 續接 PRICE 時 review_price 交 PRICE（或其後 HOLD_REVIEW）成功才寫：下游失敗時邊緣觸發下期照常重排（D46）
                fnotes.append("續接 PRICE：review_price 不寫，待 PRICE／HOLD_REVIEW 回寫")
            elif params.get("price") is not None:
                why = _newer_thesis_price(conn, t, asof)   # 舊資料日不蓋回較新的 review_price（F05R2）
                if why is None:
                    data["review_price"] = params["price"]
                else:
                    fnotes.append(why)
            kc = _thesis_kill_due(reg["data"].get("kill_criteria_due"), result, asof, fnotes)
            if kc is not None:
                data["kill_criteria_due"] = kc
            if holding and verdict == "疑似" and result.get("framework_suspect"):
                # 機械原因（申報未抓取／未全附、輸出不合格、傳輸失敗）的疑似：不開 P-50 觀察期，缺口由升級的完整分析補（M5-R5-01）
                fnotes.append("疑似屬機械原因（框架改判）：不開 P-50 觀察期，交完整分析")
            elif holding and verdict == "疑似" and _p50_expired(data, asof):
                # 觀察期已到期未結案：保留原期限，review_due 照排 FULL 結案（P-50 到期必須結案；M6-RS3-01）
                fnotes.append(f"論點疑似：P-50 觀察期已於 {data['observe_until']} 到期未結案，保留原期限、不重開（待完整分析結案）")
            elif holding and verdict == "疑似" and str(data.get("last_full_review_date") or "")[:10] > asof:
                # A3-M6B-01（D86 第二道防線）：資料日之後已有完整分析（較新的判斷）→ 舊資料日的疑似不重開 P-50 觀察期
                fnotes.append(f"論點疑似：資料日 {asof} 之後已有完整分析（{data['last_full_review_date']}），不開 P-50 觀察期"
                              "（A3-M6B-01）")
            elif holding and verdict == "疑似":   # P-50：疑似 → 觀察 6 個月（兩次季報），到期 review_due 排 FULL 結案（M2_D4_04）
                ou = str(data.get("observe_until") or "")[:10]
                if not (_valid_date(ou) and ou > asof):
                    data["observe_until"] = (date.fromisoformat(asof) + timedelta(days=OBSERVE_DAYS)).isoformat()
                    fnotes.append(f"論點疑似：P-50 觀察期至 {data['observe_until']}")
            if fnotes:
                log["framework_notes"] = list(log.get("framework_notes") or []) + fnotes
            rows.append(("registry", _reg_row(t, reg["status"], data), ["ticker"]))
        _write_rows(conn, rows)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return decision_id


def write_price_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str, *,
                       action: str, gate1: bool, gate2: str, gate3: str, cap: dict, price: float,
                       price_date: str | None, t30: float | None, reason: str, notes: list[str],
                       g8_adjusted: float | None = None, basis: dict | None = None, job_id: str | None = None,
                       defer_review_price: bool = False, t30_date: str | None = None,
                       p30_card: dict | None = None) -> str:
    """PRICE 完成後回寫（僅不需升級時）：決策日誌、訊號卡（沿用上一張、更新數字並加 framework_notes）、
    登錄表合併更新（last_review_date、review_price、t30_at_valuation、auto_buy_cap、sell_review_above、
    absurd_price_line）。三表在同一交易內寫入。回傳 decision_id。
    日誌另記閘門 2 實際比較的 auto_buy_cap、iv_base、long_rate，與 basis（PriceHandler 給的 gate3_source、weight、
    target_weight：閘門 3 來源決策與 ADD 判斷用權重；M6-D5-03）；decisions.signal 記動作本身（M6-D1-04）；
    訊號卡記 carried_from_decision_id（目標權重、等級等沿用欄位的來源完整分析，M6-D5-09）。
    job_id：post 路徑的冪等鍵（M6-D9-03）。
    defer_review_price：本次要交 HOLD_REVIEW → 不寫 review_price，由 HOLD_REVIEW 成功回寫才寫（下游失敗時邊緣觸發
    下期照常重排，D46、M2-RS8-01）。
    t30_date：本次長債的資料日；訊號卡 long_rate 的 value 與 date 一起更新（不留新值配舊日期，M5-D5-02）。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    done = _done_for_job(conn, job_id)
    if done is not None:
        return done
    if action not in PRICE_ACTIONS:
        raise WritebackError(f"PRICE 動作不合法：{action!r}")
    # 沿用資料日 ≤ asof 的最新完整分析卡（A3-M6B-03、A3-M6D-03）：舊資料日 PRICE 不得沿用未來的決策；
    # 先前 PRICE 卡沿用的可能是較舊的完整分析（之後才晚寫入較新的完整分析）。沒有完整分析卡時退回資料日 ≤ asof 的最新卡
    prev = latest_signal(conn, t, asof, full_only=True) or latest_signal(conn, t, asof)
    card: dict[str, Any] = copy.deepcopy(prev["data"]) if prev else {}
    if p30_card is not None and action in ("BUY", "ADD"):   # 下單時近 8 次財報重算的 P-30 單事件上限（M2-R4-02）
        mw = card.get("max_weight_pct") if isinstance(card.get("max_weight_pct"), dict) else {}
        card["max_weight_pct"] = {**mw, "single_event": (p30_card.get("max_weight_pct") or {}).get("single_event")}
    if action in ("BUY", "ADD") and card.get("reason_code") in PRICE_NO_BUY_REASONS:   # D24：要重跑完整分析才能買
        notes = list(notes) + [f"上次完整分析 reason_code＝{card['reason_code']}：PRICE 不得升成買進，{action} → "
                               f"{'WATCH' if action == 'BUY' else 'HOLD'}（須重跑完整分析；設計決策 D24）"]
        action = "WATCH" if action == "BUY" else "HOLD"
    prev_sig = prev["signal"] if prev else None
    signal = _PRICE_SIGNAL.get(action) or (prev_sig if prev_sig in ("WATCH", "NO_ACTION") else "WATCH")
    carried = card.get("carried_from_decision_id") or card.get("decision_log_id")   # 沿用欄位的來源（最早的完整分析）
    decision_id = next_decision_id(conn, t, asof)
    now = db.now_iso()

    vps = dict(card.get("value_per_share") or {})
    vps.update({"conservative": cap.get("IV_cons"), "base": cap.get("IV_base"),
                "optimistic": cap.get("IV_opt"), "zero_growth_IV0": cap.get("IV0")})
    lr = dict(card.get("long_rate") or {})
    lr.update({"tenor": "30Y", "value": t30, "date": t30_date})
    # price_to_cap＝現價 ÷ auto_buy_cap（P19 定義，與 checks.set_cap_report_fields 一致）；cap_calc 的 price_to_cap 是 ÷ P2′
    card.update({
        "as_of": asof, "ticker": t, "signal": signal, "conclusion": _conclusion(signal, card.get("reason_code")),
        "decision_log_id": decision_id, "value_per_share": vps, "long_rate": lr,
        "auto_buy_cap": cap.get("auto_buy_cap"), "buy_price_cap": cap.get("cap"), "r_d": cap.get("r_d"),
        "price": price, "price_date": price_date, "price_to_conservative": cap.get("price_to_cons"),
        "price_to_cap": cap.get("price_to_auto_buy_cap"), "implied_growth": cap.get("g_impl"),
        "g_impl_gt_cap": cap.get("g_impl_gt_cap"), "sell_review_above": cap.get("IV_base"),
        "absurd_price_line": cap.get("absurd_price_line"),
        "g8_adjusted_expected_return": g8_adjusted,   # 以本次價格重算；沿用舊卡的值會用到舊價格
        "carried_from_decision_id": carried,
        "framework_notes": list(card.get("framework_notes") or [])
        + [f"{asof} PRICE（{action}）：{n}" for n in notes],
    })

    log = _blank_from_fields(_load_schema(paths, "decision_log_schema.json").get("fields") or {})
    log.update({
        "decision_id": decision_id, "decision_date": asof, "ticker": t, "action": action,
        "review_type": "PRICE", "grade": cap.get("grade_used"), "g_impl": cap.get("g_impl"),
        "r_d": cap.get("r_d"), "buy_price_cap": cap.get("cap"), "price": price, "price_date": price_date,
        "price_to_cap": cap.get("price_to_auto_buy_cap"), "gate1_not_deleted": gate1, "gate2_price_below_cap": gate2,
        "gate3_pass": gate3 == "通過", "gate3_failures": [] if gate3 == "通過" else [gate3],
        "thesis_check": "不適用", "reason_one_liner": reason or "", "framework_notes": list(notes),
        # 閘門 2 實際比較的上限（＝min(P2′, k×IV_base)，buy_price_cap 是 P2′）與長債（M6-D5-03）
        "auto_buy_cap": cap.get("auto_buy_cap"), "iv_base": cap.get("IV_base"), "long_rate": t30,
    })
    for k in ("gate3_source", "weight", "target_weight"):
        if k in (basis or {}):
            log[k] = basis[k]
    if job_id:
        log["source_job_id"] = job_id

    rows: list[tuple[str, dict, list[str] | None]] = [
        ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                       "job_id": None, "signal": action, "reason_code": card.get("reason_code"), "data": log,
                       "created_at": now}, None),
        ("signals", {"decision_id": decision_id, "ticker": t, "signal": signal, "data": card,
                     "created_at": now}, None),
    ]
    reg = _registry_get(conn, t)
    if reg is not None:
        data = {**reg["data"]}
        # 舊資料日不蓋掉較新的狀態（M6-D6-07、F05R2）：資料日之後已有寫過價格線的決策 → review_price、t30、上限、
        # 兩條賣出線都不寫；只有拆合股 → 每股欄位不寫（cap 是拆合股前每股價），t30 照寫
        newer = _newer_state(conn, t, asof)
        skip = set(newer[0]) if newer is not None else set()
        if newer is not None:
            log["framework_notes"].append(newer[1])
        data["last_review_date"] = _later(data.get("last_review_date"), asof)   # 複查日不往回改
        why_price = _newer_thesis_price(conn, t, asof)
        if defer_review_price:
            log["framework_notes"].append("交 HOLD_REVIEW：review_price 待 HOLD_REVIEW 回寫")
        elif why_price is None:
            data["review_price"] = price
        elif newer is None:
            log["framework_notes"].append(why_price)
        sold = str(data.get("sold_out_at") or "")[:10]
        full = str(data.get("last_full_review_date") or "")[:10]
        if sold and sold >= asof:   # 持有期間排入、賣光後才跑完：不寫回上限與殖利率，免得再進 PRICE 快速通道
            log["framework_notes"].append(f"{sold} 已賣光（資料日 {asof} 不晚於賣光日）：登錄表不寫回 auto_buy_cap、"
                                          "t30_at_valuation")
        elif sold and full < sold:   # 賣光後尚無新的完整分析：上限須由新完整分析恢復（M7-D1-04、M7-S4-N01）
            log["framework_notes"].append(f"{sold} 已賣光、之後沒有完整分析（最近 {full or '無'}）：登錄表不寫回 "
                                          "auto_buy_cap、t30_at_valuation")
        else:
            for k, v in (("t30_at_valuation", t30),
                         ("auto_buy_cap", cap.get("auto_buy_cap"))):   # 買進上限缺 → None（不買，失敗關閉）
                if k not in skip:
                    data[k] = v
        # 賣出側的線缺值時保留舊值：缺值＝不觸發，蓋成 None 會讓賣出檢視與荒謬級永久失效
        for k, v in (("sell_review_above", cap.get("IV_base")), ("absurd_price_line", cap.get("absurd_price_line"))):
            if k in skip:
                continue
            if v is not None:
                data[k] = v
            elif data.get(k) is not None:
                log["framework_notes"].append(f"cap_calc 無 {k}，登錄表保留舊值 {data[k]!r}")
        rows.append(("registry", _reg_row(t, reg["status"], data), ["ticker"]))
    _write_rows(conn, rows)
    return decision_id


def write_price_invalid(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str, job_id: str,
                        errors: list[str], why: str = "PRICE 輸出不合格") -> str:
    """PRICE 不合格或無法完成（M5-D1-08、M5_D4-06）：寫一筆標準編號的「閘門未過」PRICE 決策，
    加碼不得沿用更早那筆全過的閘門紀錄。只寫 decisions（job_id 記本工作＝佔位列，_newer_state 不算它）；
    不寫訊號卡、不動登錄表（不推進 last_review_date）。回傳 decision_id。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    decision_id = next_decision_id(conn, t, asof)
    log = _blank_from_fields(_load_schema(paths, "decision_log_schema.json").get("fields") or {})
    log.update({
        "decision_id": decision_id, "decision_date": asof, "ticker": t, "action": "NO_ACTION",
        "review_type": "PRICE", "job_type": "PRICE", "errors": [str(e) for e in errors],
        "gate1_not_deleted": None, "gate2_price_below_cap": "不通過", "gate3_pass": False,
        "thesis_check": "不適用", "reason_one_liner": why,
        "framework_notes": [f"{why}：本次閘門視為未過，暫停加碼至下次複查"],
    })
    _write_rows(conn, [("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof,
                                      "run_id": run_id, "job_id": job_id, "signal": "NO_ACTION",
                                      "reason_code": "MISSING_DATA", "data": log, "created_at": db.now_iso()}, None)])
    return decision_id


def write_fallback_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str, job_id: str,
                          holding: bool, why: str) -> str:
    """run 結束時的集中收尾（設計決策 D46，engine.finalize_run）：該檔本輪有失敗而沒有任何結論 → 一筆保守結論。
    持股：HOLD＋停止加碼（review_type＝HOLD_REVIEW、stop_adding，sizing.add_blocked 會擋加碼與建倉批次，
    直到新的 THESIS 完好或 HOLD_REVIEW 不停止加碼）；其他：WATCH（review_type＝FALLBACK，不影響閘門與估值判讀）。
    只寫 decisions（job_id 記最後失敗的工作）；不寫訊號卡、不動登錄表與論點檔，下次觸發照常重排。回傳 decision_id。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    decision_id = next_decision_id(conn, t, asof)
    action = "HOLD" if holding else "WATCH"
    log = _blank_from_fields(_load_schema(paths, "decision_log_schema.json").get("fields") or {})
    log.update({
        "decision_id": decision_id, "decision_date": asof, "ticker": t, "action": action,
        "review_type": "HOLD_REVIEW" if holding else "FALLBACK", "stop_adding": bool(holding),
        "fallback": True,   # 收尾標記與決策同一交易寫入（A3-M6B-05；sizing.add_blocked、_newer_state 依它判斷）
        "reason_one_liner": f"本輪工作未完成、沒有結論，保守{'持有並停止加碼' if holding else '觀察'}",
        "framework_notes": [f"run 收尾（D46）：{why}"[:2000]],
    })
    _write_rows(conn, [("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof,
                                      "run_id": run_id, "job_id": job_id, "signal": action,
                                      "reason_code": "MISSING_DATA", "data": log, "created_at": db.now_iso()}, None)])
    return decision_id


# ---------------------------------------------------------------- 持股完整複查（H2）

HOLD_REVIEW_ACTIONS = ("HOLD", "TRIM", "SELL_ALL")
ONE_YEAR_DAYS = 365
ABSURD_MIN_TRIM = 0.5      # 設計決策 D03：荒謬級（價格 ≥ 50×E）至少賣一半（P-48）
VALUATION_MAX_TRIM = 0.25  # 設計決策 D03：3c 頂級複利型每次至多 1/4（rulebook H2 Q3c）


def first_buy_date(conn: sqlite3.Connection, ticker: str) -> str | None:
    """首次建倉日：帳本 positions 優先，登錄表欄位、登錄表 data 次之（M5_D2-10、M7-D1-03：broker 只寫登錄表欄位）。
    PRICE、HOLD_REVIEW 覆核與回寫共用；都缺回傳 None（呼叫端一律視為未滿一年／未滿兩年）。"""
    row = conn.execute("SELECT first_buy_date FROM positions WHERE ticker = ?", (ticker,)).fetchone()
    reg = conn.execute("SELECT first_buy_date, data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    return ((row["first_buy_date"] if row is not None else None)
            or (reg["first_buy_date"] if reg is not None else None)
            or (json.loads(reg["data"] or "{}").get("first_buy_date") if reg is not None else None))


def held_days(conn: sqlite3.Connection, ticker: str, asof: str) -> int | None:
    """建倉至決策日的曆日數；建倉日缺或格式不合 → None。"""
    first = first_buy_date(conn, ticker)
    try:
        return (date.fromisoformat(asof) - date.fromisoformat(str(first)[:10])).days if first else None
    except ValueError:
        return None


def _prior_trim(conn: sqlite3.Connection, ticker: str, reg: dict | None) -> dict | None:
    """本持有期的上次減碼（last_trim.date ≥ 首次建倉日；建倉日缺時照用）。"""
    lt = ((reg or {}).get("data") or {}).get("last_trim")
    if not isinstance(lt, dict) or not lt.get("date"):
        return None
    if lt.get("unfilled") or lt.get("shares") == 0:   # 下單被拒／沒成交（broker／daily 標記）不算減過（A3-M7A-02）
        return None
    first = first_buy_date(conn, ticker)
    return lt if not first or str(lt["date"])[:10] >= str(first)[:10] else None


def _back_below_line(conn: sqlite3.Connection, ticker: str, since: str, line: float, asof: str,
                     rearm: Any = None) -> bool:
    """上次荒謬級減碼（since）之後、決策日之前，價格是否回到荒謬級線下（設計決策 D03：回到線下才可再減）。
    證據兩種：review_due 判定重新觸發時帶來的 absurd_rearm（第一筆線下收盤，經觸發參數 PRICE→HOLD_REVIEW 傳入；
    跌破期間通常沒有決策紀錄），或決策日誌記下的價格（PRICE、HOLD_REVIEW、完整分析）。都沒有 → 視為沒回到線下（有疑義時不賣）。"""
    if isinstance(rearm, dict):
        d, c = str(rearm.get("date") or "")[:10], _finite(rearm.get("close"))
        if c is not None and c < line and since < d <= asof:
            return True
    # A3-M6C-07：last_trim.line 已隨拆合股換算（corp_actions._scale_registry），之前的決策價也要換算；比例缺 → 該價不採
    splits = conn.execute(
        f"SELECT effective_date, ratio_new_per_old FROM corporate_actions WHERE ticker = ? AND status = 'applied' "
        f"AND action_type IN ({','.join('?' * len(_SPLIT_TYPES))}) AND effective_date > ? AND effective_date <= ?",
        (ticker, *_SPLIT_TYPES, since, asof)).fetchall()
    for r in conn.execute(f"SELECT decision_date, data FROM decisions WHERE ticker = ? AND decision_date > ? "
                          f"AND decision_date <= ? AND {NOT_VOIDED}", (ticker, since, asof)):   # 已作廢的決策不當證據
        px = _finite((json.loads(r["data"] or "{}")).get("price"))
        for s in splits:
            if px is None:
                break
            if str(s["effective_date"])[:10] > str(r["decision_date"])[:10]:
                ratio = _finite(s["ratio_new_per_old"])
                px = px / ratio if ratio and ratio > 0 else None
        if px is not None and px < line:
            return True
    return False


def _last_full_p08_quality(conn: sqlite3.Connection, ticker: str, asof: str) -> str | None:
    """最近一次完整分析（決策日誌無 review_type、未作廢；同 review_jobs._last_full_card）訊號卡的 p08_final.quality。"""
    for r in conn.execute(
            "SELECT d.data AS log, s.data AS card FROM decisions d JOIN signals s ON s.decision_id = d.decision_id "
            "WHERE d.ticker = ? AND d.decision_date <= ? AND json_extract(d.data, '$.voided_at') IS NULL "
            "ORDER BY d.decision_date DESC, d.created_at DESC", (ticker, asof)):
        if not json.loads(r["log"] or "{}").get("review_type"):
            p = json.loads(r["card"] or "{}").get("p08_final")
            return str(p.get("quality")).strip() if isinstance(p, dict) and p.get("quality") is not None else None
    return None


def _vet_valuation_sell(conn: sqlite3.Connection, ticker: str, asof: str, result: dict, reg: dict | None,
                        price: float | None, iv_base: float | None, notes: list[str],
                        rearm: Any = None) -> tuple[str, float | None, dict | None]:
    """估值類賣出的框架檢查（設計決策 D01、D03 的回寫部分）。回傳 (動作, 減碼比例, last_trim 或 None)。
    - 建倉未滿一年（建倉日缺視為未滿）：不因價格因素賣出 → HOLD（呼叫端另設停止加碼）。
    - SELL_ALL（估值類）：不採 rulebook 3b 全清 → HOLD；價格 ≥ 荒謬級線時當荒謬級 TRIM 下限 1/2，走下面荒謬級檢查。
    - TRIM 比例（三條分開）：價格 ≥ 荒謬級線 → 至少 1/2，且回到線下之前只減一次；H2 路徑 Q3c 答「是」（頂級複利型）
      → 每次至多 1/4、累計保留 ≥ 本持有期峰值股數 2/3（峰值＝max(現有股數, last_trim.peak_shares, 登錄表 peak_qty)；
        peak_qty 由 broker 買進成交時記，硬上限或人工減碼不寫 last_trim 也不會低估，X40）；
      其他（普通／良好企業，SKILL §6(2)）數量須由 G8 算出，目前沒有 G8 數量來源 → HOLD（呼叫端另設停止加碼）。"""
    action, frac = result.get("action"), result.get("trim_fraction") if result.get("action") == "TRIM" else None
    if result.get("sell_category") != "估值" or action not in ("TRIM", "SELL_ALL"):
        return action, frac, None
    first = first_buy_date(conn, ticker)
    days = held_days(conn, ticker, asof)
    if days is None or days < ONE_YEAR_DAYS:
        notes.append(f"建倉未滿一年（首次建倉 {first or '缺'}）：不因價格因素賣出，{action} 改為 HOLD 並停止加碼"
                     "（SKILL §6；設計決策 D01）")
        return "HOLD", None, None
    line = _finite(((reg or {}).get("data") or {}).get("absurd_price_line"))
    absurd = line is not None and price is not None and price >= line
    if action == "SELL_ALL":   # 估值類全清＝rulebook 3b，本系統不採（D01(3)、X15）；荒謬級併入 TRIM 規則（D01(2)、D03）
        if not absurd:
            notes.append("估值類 SELL_ALL（rulebook 3b 全清）本系統不採：改為 HOLD 並停止加碼；需換股走 SWITCH 人工"
                         "（SKILL §6(2)；設計決策 D01）")
            return "HOLD", None, None
        notes.append(f"荒謬級估值類 SELL_ALL 依荒謬級 TRIM 下限（比例 {ABSURD_MIN_TRIM}）處理：受只減一次與 last_trim 紀錄約束"
                     "（P-48、設計決策 D01(2)、D03）")
        action, frac = "TRIM", ABSURD_MIN_TRIM
    if action != "TRIM" or _finite(frac) is None:
        return action, frac, None
    top = any(isinstance(q, dict) and str(q.get("q") or "").strip() == "Q3c" and str(q.get("answer") or "").startswith("是")
              for q in result.get("path") or [])
    if top:   # A3-M3D-02：頂級複利型須最近一次完整分析訊號卡 p08_final.quality＝優（D01／ASK-3＝B、D77）
        q = _last_full_p08_quality(conn, ticker, asof)
        if q != "優":
            notes.append(f"H2 Q3c 答「是」但最近一次完整分析 P-08 最終品質為 {q or '缺'}（非「優」）：不算頂級複利型，"
                         "不走每次 ≤1/4 分支（A3-M3D-02；ASK-3＝B、D77）")
            top = False
    prev = _prior_trim(conn, ticker, reg)
    pos = conn.execute("SELECT qty FROM positions WHERE ticker = ?", (ticker,)).fetchone()
    qty = int(pos["qty"]) if pos is not None else None
    peak = max([v for v in (qty, _finite((prev or {}).get("peak_shares")),
                            _finite(((reg or {}).get("data") or {}).get("peak_qty"))) if v is not None], default=None)
    if absurd:
        if prev is not None and prev.get("reason_code") == "ABSURD" and \
                not _back_below_line(conn, ticker, str(prev["date"])[:10], _finite(prev.get("line")) or line, asof, rearm):
            notes.append(f"荒謬級在價格回到線下之前只減一次：{str(prev['date'])[:10]} 已依荒謬級減碼，之後沒有低於線的紀錄，"
                         f"TRIM（比例 {frac}）改為 HOLD 並停止加碼（設計決策 D03）")
            return "HOLD", None, None
        lo, hi, label = ABSURD_MIN_TRIM, 1.0, "荒謬級（≥1/2）"
    elif top:
        lo, hi, label = 0.0, VALUATION_MAX_TRIM, "3c 頂級複利型（每次 ≤1/4）"
        if qty and peak:   # 3c 累計保留 ≥ 峰值股數 2/3（設計決策 D03）
            room = 1 - (2 / 3) * peak / qty
            if room <= 0:
                notes.append(f"3c 累計已減到峰值股數 {peak:g} 的 2/3 以下（現有 {qty} 股）：TRIM 改為 HOLD 並停止加碼（設計決策 D03）")
                return "HOLD", None, None
            if room < hi:
                hi, label = room, f"3c 頂級複利型（每次 ≤1/4，累計保留 ≥ 峰值 {peak:g} 股的 2/3）"
    else:   # 普通／良好企業（SKILL §6(2)）：數量須由 G8 算出，沒有來源就不賣（有疑義時不賣）
        notes.append(f"估值減碼（SKILL §6(2)）數量須由 G8 算出，目前無 G8 數量來源：TRIM（比例 {frac}）改為 HOLD 並停止加碼"
                     "（設計決策 D03、HOLD_REVIEW 細則 6／8）")
        return "HOLD", None, None
    new = min(max(float(frac), lo), hi)
    if new != frac:
        notes.append(f"減碼比例超出{label}範圍，夾到 {new}（原 {frac}；設計決策 D03）")
    last_trim = {"date": asof, "reason_code": "ABSURD" if absurd else "VALUATION",
                 "line": line if absurd else iv_base, "fraction": new,
                 "shares": math.floor(qty * new) if qty is not None else None, "peak_shares": peak}
    return "TRIM", new, last_trim


def _hold_review_text(ticker: str, asof: str, result: dict) -> str:
    """H2 論點增補：框架註記只進決策日誌（常含價格與 IV_base）；全文刪估值數字行（M6-D6-02）。"""
    lines = [f"# {ticker} 持股完整複查 H2 {asof}", "",
             f"- 動作：{result.get('action')}（類別 {result.get('sell_category') or '無'}；"
             f"停止加碼 {'是' if result.get('stop_adding') else '否'}；觀察至 {result.get('observe_until') or '無'}）",
             f"- 被否定的假設：{result.get('falsified_assumption') or '無'}",
             f"- 理由：{result.get('reason') or ''}", "", "## 決策樹路徑"]
    for q in result.get("path") or []:
        if isinstance(q, dict):
            lines.append(f"- {q.get('q', '')}：{q.get('answer', '')}（{q.get('evidence', '')}）")
    return scrub_valuation_lines("\n".join(lines)) + "\n"


def write_hold_review_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str,
                             result: dict, params: dict | None = None, notes: list[str] | None = None, *,
                             price: float | None = None, iv_base: float | None = None,
                             job_id: str | None = None, record_price: bool = False,
                             price_date: str | None = None) -> str:
    """HOLD_REVIEW 完成後回寫：決策日誌（action＝HOLD／TRIM／SELL_ALL，review_type＝HOLD_REVIEW，含停止加碼與減碼比例、
    價格日與觸發來源 trigger，M6-D5-08）、論點增補、登錄表合併更新複查日期與觀察期限；
    grade_after（H2「確定性降一級」）較登錄表等級保守時更新登錄表 grade（只降不升，M6-D2-10）。回傳 decision_id。
    job_id：post 路徑的冪等鍵（M6-D9-03）。
    price_date：與 price 同源的價格日（hold_jobs.valuation_numbers 取自同一價格檔）；給了優先於 params["price_date"]
    （完整分析鏈的參數不帶 price_date，RM6-3）。
    record_price：成功回寫（post）→ 登錄表寫 review_price＝price（上游 PRICE／THESIS 交來時延後寫的，D46、M2-RS8-01）；
    不合格的保守結論不寫，邊緣觸發下期照常重排。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    done = _done_for_job(conn, job_id)
    if done is not None:   # 重跑：同群判定冪等補做（A3-M6E-02；corr_links 已去重）
        if _registry_get(conn, t) is not None:
            _record_links_safely(conn, paths, t, asof)
        return done
    action = result.get("action")
    if action not in HOLD_REVIEW_ACTIONS:
        raise WritebackError(f"HOLD_REVIEW 動作不合法：{action!r}")
    params = params or {}
    notes = list(notes or [])
    reg = _registry_get(conn, t)
    vetted, frac, last_trim = _vet_valuation_sell(conn, t, asof, result, reg, price, iv_base, notes,
                                                  params.get("absurd_rearm"))
    if vetted != action or frac != result.get("trim_fraction"):
        result = {**result, "action": vetted, "trim_fraction": frac,
                  "stop_adding": bool(result.get("stop_adding")) or vetted != action}
        action = vetted
    ou = _p50_observe_until(reg, result, asof, notes, params.get("reasons"))
    if ou != result.get("observe_until"):
        result = {**result, "observe_until": ou}
    decision_id = next_decision_id(conn, t, asof)
    now = db.now_iso()
    # A3-M6B-01（D86 第二道防線）：資料日之後已有較新的完整分析（含停止結果）或 THESIS／HOLD_REVIEW → 只留決策紀錄與
    # last_review_date，不改登錄表等級、last_trim、P-50 觀察期、外部變數（較新的判斷不被舊資料日蓋掉）
    stale_why = None
    if reg is not None:
        nst, nrv = _newer_state(conn, t, asof), _newer_review(conn, t, asof)
        if nst is not None and nst[0] == _NEWER_STATE_KEYS:
            stale_why = nst[1]
        elif nrv is not None:
            stale_why = f"資料日 {asof} 之後已有 {nrv['review_type']} {nrv['decision_id']}（{nrv['decision_date']}）"
    drivers_patch: dict[str, Any] = {}
    if reg is not None and stale_why is None:   # P-31：持有中複查重新檢視外部變數（改判須附新事實）
        from ..portfolio import correlation as corr
        corr.apply_drivers_to_entry(drivers_patch, reg["data"], result, asof, corr.load_vocab(paths.skill_deep), notes)
    text = _hold_review_text(t, asof, result)
    main = main_thesis_path(paths, t, (reg["data"] if reg else {}).get("thesis_file") or params.get("thesis_file"))
    path = _write_addendum(main, text) if main is not None else _write_thesis(paths, t, asof, text)
    try:
        log = _blank_from_fields(_load_schema(paths, "decision_log_schema.json").get("fields") or {})
        log.update({
            "decision_id": decision_id, "decision_date": asof, "ticker": t, "action": action,
            "review_type": "HOLD_REVIEW", "reason_one_liner": result.get("reason") or "",
            "price": price, "sell_category": result.get("sell_category"),
            "trim_fraction": result.get("trim_fraction") if action == "TRIM" else None,
            "stop_adding": bool(result.get("stop_adding")) or action in ("TRIM", "SELL_ALL"),
            "observe_until": result.get("observe_until"),
            "falsified_assumption": result.get("falsified_assumption"), "iv_base": iv_base,
            "h2_path": result.get("path") or [],
            "price_date": price_date if price_date is not None else params.get("price_date"),   # RM6-3
            "trigger": {k: params.get(k) for k in ("full_stage", "final_signal", "stopped_at_gate", "reason_code")},
        })
        if job_id:
            log["source_job_id"] = job_id
        from .checks import GRADE_ORDER, worse_grade
        ga = result.get("grade_after", result.get("_grade_after"))   # A3-M3C-03：schema 顯示的鍵名是 _grade_after
        ga = None if str(ga).strip().lower() in ("none", "null", "") else ga   # 選填欄不經驗證器正規化
        old_g = (reg["data"] if reg else {}).get("grade")
        new_g = ga if reg is not None and ga in GRADE_ORDER and worse_grade(old_g, ga) == ga and ga != old_g else None
        if ga is not None:
            log["grade_after"] = ga
            if new_g is None:
                notes.append(f"grade_after {ga!r} 不比登錄表等級 {old_g!r} 保守（或不合法），登錄表等級不變（只降不升）")
        if stale_why is not None:
            notes.append(f"{stale_why}：本次 HOLD_REVIEW 只留決策紀錄與複查日，登錄表等級、last_trim、觀察期、"
                         "外部變數不改（D86、A3-M6B-01）")
            new_g, last_trim = None, None
        if new_g is not None:
            notes.append(f"H2 確定性降級：登錄表等級 {old_g!r} → {new_g!r}")
        if notes:
            log["framework_notes"] = notes
        rows: list[tuple[str, dict, list[str] | None]] = [
            ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                           "job_id": None, "signal": action, "reason_code": result.get("sell_category"),
                           "data": log, "created_at": now}, None),
        ]
        if reg is not None:
            data = {**reg["data"], **drivers_patch,   # 複查日不往回改
                    "last_review_date": _later(reg["data"].get("last_review_date"), asof)}
            if params.get("full_stage") in ("A", "B") and params.get("reason_code") != "MISSING_DATA" \
                    and not params.get("hold_stop_unresolved"):   # A3-M3B-01：持股前半違規停止沒有估值，不算完整複查
                # 只有完整分析做成（前半停止或後半完成）才算完整複查；PRICE、FAIL、缺資料不算（M3-D10-01）
                data["last_full_review_date"] = _later(reg["data"].get("last_full_review_date"), asof)
            if result.get("observe_until") and stale_why is None:
                data["observe_until"] = result["observe_until"]
            if record_price and price is not None:
                why_price = _newer_thesis_price(conn, t, asof)   # 舊資料日不蓋回較新的 review_price（F05R2）
                if why_price is None:
                    data["review_price"] = price
                else:
                    log["framework_notes"] = list(log.get("framework_notes") or []) + [why_price]
            if last_trim is not None:   # 設計決策 D03：每次估值減碼都記下來（review_due／HOLD_REVIEW 之後讀）
                data["last_trim"] = last_trim
            if new_g is not None:
                data["grade"] = new_g
            rows.append(("registry", _reg_row(t, reg["status"], data), ["ticker"]))
        _write_rows(conn, rows)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if reg is not None:
        _record_links_safely(conn, paths, t, asof)
    return decision_id
