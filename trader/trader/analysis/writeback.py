"""深度分析結果回寫：決策日誌（decisions）、訊號卡（signals）、登錄表（registry）、論點檔（凍結）。

欄位名依 `berkshire-deep-merged/assets/` 的三個 schema（signal_card／registry／decision_log）。
三張表的寫入在同一個交易內完成；論點檔是檔案系統寫入，先寫，若交易失敗則刪除本次新建的檔案。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trader import db
from trader.paths import Paths


class WritebackError(Exception):
    """必要檔缺漏、輸入不合法或寫入失敗。"""


SIGNALS = ("BUY", "WATCH", "NO_ACTION")
# registry_schema.json 的 status 枚舉中，NO_ACTION 的停止碼可直接作為 status
_STATUS_FROM_REASON = ("MISSING_DATA", "TOO_HARD", "REJECT")
ACTION_BY_SIGNAL = {"BUY": "BUY", "WATCH": "WATCH", "NO_ACTION": "NO_ACTION"}  # decision_log action 枚舉
STOP_MODE = "A_THESIS_CHECK"


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
        if reason == "RED_LIGHT":
            return "REJECT"
        return "NO_ACTION"
    raise WritebackError(f"不認得的訊號：{signal!r}")


def _resolve_status(conn: sqlite3.Connection, ticker: str, status: str, notes: list[str]) -> str:
    """已持有（登錄表 HOLDING）的檔不因重新分析而降為 WATCH 等狀態；保留 HOLDING 並記註。"""
    row = conn.execute("SELECT status FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is not None and row["status"] == "HOLDING" and status != "HOLDING":
        notes.append(f"登錄表既有 HOLDING，本次分析結果 {status} 未覆寫持有狀態（持有狀態由帳本管理）")
        return "HOLDING"
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


def next_decision_id(conn: sqlite3.Connection, ticker: str, asof: str) -> str:
    """決策編號 `{TICKER}-{YYYYMMDD}-{n}`，n 從 1 起，為 decisions 表中同代號同決策日已有數量加 1。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    return _next_free_id(conn, t, asof)


def _write_rows(conn: sqlite3.Connection, rows: list[tuple[str, dict, list[str] | None]]) -> None:
    """在單一交易內寫入多列；key 不為 None 時為 upsert。任何錯誤都回滾。

    與 db.insert／db.upsert 相同的 SQL 與 JSON 編碼，但不各自提交，確保三表原子寫入。
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
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            if key:
                updates = [c for c in cols if c not in key]
                sql += f" ON CONFLICT({', '.join(key)}) "
                sql += ("DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in updates)) if updates else "DO NOTHING"
            conn.execute(sql, values)
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


def _load_schema(paths: Paths, name: str) -> dict:
    return _load_json(paths.skill_deep / "assets" / name)


def _blank_from_fields(fields: dict) -> dict:
    """依 schema 的欄位名建立空白列（非 `_` 開頭的鍵皆為 None）。"""
    return {k: None for k in fields if not str(k).startswith("_")}


def skill_hashes_file(paths: Paths) -> str:
    """兩個 skill 目錄逐檔 sha256 凍結成 state/skill_hashes/{摘要}.json（同內容同檔）；回傳相對 state 的路徑。
    月報以「期間內出現幾種」判斷規則是否變動。"""
    files = {}
    for root in (paths.skill_deep, paths.skill_screen):
        for f in sorted(Path(root).rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.name != ".DS_Store":
                files[f"{Path(root).name}/{f.relative_to(root).as_posix()}"] = hashlib.sha256(f.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
    out = paths.state / "skill_hashes" / f"{digest}.json"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(files, ensure_ascii=False, indent=1), encoding="utf-8")
    return out.relative_to(paths.state).as_posix()


def _framework_log_fields(paths: Paths, tdir: Path | None) -> dict:
    """完整分析日誌中由框架填的欄位：skill_hashes；fy_last_filed（資料包用到的最新 10-K 申報日，sec/_index.json）。"""
    out: dict[str, Any] = {"skill_hashes": skill_hashes_file(paths)}
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


def _reconcile_with_cap(tdir: Path, card: dict, entry: dict, notes: list[str]) -> None:
    """登錄表的每股價格線與等級以 cap_calc 為準（不照抄模型的 registry_entry.json）：
    auto_buy_cap、sell_review_above（＝IV_base）、absurd_price_line（＝50×E）；等級取訊號卡與 cap_calc 較保守者。
    訊號卡同步寫入同樣的三條線。沒有 cap_result.json（未走到估值）時不動。"""
    from .checks import absurd_line, worse_grade
    cap_path = Path(tdir) / "cap_result.json"
    if not cap_path.is_file():
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
    if g is not None and entry.get("grade") != g:
        notes.append(f"登錄表 grade 取訊號卡與 cap_calc 較保守者：{entry.get('grade')!r} → {g!r}")
        entry["grade"] = g


# ---------------------------------------------------------------- 對外函式

def write_full_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str,
                      tdir: Path, final_signal: str | None = None, notes: list[str] | None = None) -> str:
    """FULL_B（RED_TEAM 若有）完成後回寫。回傳 decision_id。

    讀 `tdir/signal_card.json`、`registry_entry.json`、`decision_log.json`、`thesis.md`；全部讀取與檢查通過後才寫入。
    `final_signal` 給定時覆寫訊號卡與日誌的訊號（日誌以 action 表示）；`notes` 併入日誌 framework_notes。
    """
    t = _std_ticker(ticker)
    _ymd(asof)
    tdir = Path(tdir)
    card = _load_json(tdir / "signal_card.json")
    entry = _load_json(tdir / "registry_entry.json")
    log = _load_json(tdir / "decision_log.json")
    thesis = _read_text(tdir / "thesis.md")

    notes = list(notes or [])
    signal = card.get("signal")
    reason = card.get("reason_code")
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

    status = _resolve_status(conn, t, _status_for(signal, reason), notes)
    decision_id = next_decision_id(conn, t, asof)
    now = db.now_iso()

    # 論點檔（先寫；交易失敗時刪除）。持股的論點檔凍結，只能增補（procedure/07 §2）：寫成主檔的增補，不換主檔
    prev = _registry_get(conn, t)
    held = prev is not None and prev["status"] == "HOLDING"
    main = main_thesis_path(paths, t, prev["data"].get("thesis_file")) if held else None
    if main is not None:
        thesis_path = _write_addendum(main, f"# {t} 持有中完整複查 {asof}（FULL_B 論點稿，增補）\n\n{thesis}")
    else:
        thesis_path = _write_thesis(paths, t, asof, thesis)
    try:
        card["ticker"] = t
        card["decision_log_id"] = decision_id
        log["decision_id"] = decision_id
        log["ticker"] = t
        log["decision_date"] = asof
        log.update(_framework_log_fields(paths, tdir))
        _reconcile_with_cap(tdir, card, entry, notes)  # 先對帳：註記要進日誌
        if notes:
            log["framework_notes"] = list(log.get("framework_notes") or []) + notes

        entry = dict(entry)
        entry["ticker"] = t
        entry["status"] = status
        entry["last_review_date"] = asof
        entry["last_full_review_date"] = asof
        # 存相對 state 的路徑：state 目錄搬移或複製後仍可解析（main_thesis_path）
        tf = main if main is not None else thesis_path
        try:
            entry["thesis_file"] = Path(tf).resolve().relative_to(paths.state.resolve()).as_posix()
        except ValueError:
            entry["thesis_file"] = str(tf)
        if held:
            for k in ("cost", "first_buy_date", "weight", "build_plan"):
                if prev["data"].get(k) is not None:
                    entry[k] = prev["data"][k]

        # 本次估值用的長債輸入（cap_calc 輸入）存進 state/cap_inputs，供 PRICE 快速通道重算
        cap_src = Path(tdir) / "cap_input.json"
        cap_dst = None
        if cap_src.is_file():
            cap_dst = paths.state / "cap_inputs" / f"{t}.json"
            entry["cap_input_file"] = str(cap_dst)

        reg_row: dict[str, Any] = {"ticker": t, "status": status, "data": entry, "updated_at": now}
        if entry.get("first_buy_date"):
            reg_row["first_buy_date"] = entry["first_buy_date"]

        _write_rows(conn, [
            ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                           "job_id": None, "signal": signal, "reason_code": reason, "data": log,
                           "created_at": now}, None),
            ("signals", {"decision_id": decision_id, "ticker": t, "signal": signal, "data": card,
                         "created_at": now}, None),
            ("registry", reg_row, ["ticker"]),
        ])
        if cap_dst is not None:  # 交易已提交後才覆寫 state 檔
            cap_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cap_src, cap_dst)
    except BaseException:
        thesis_path.unlink(missing_ok=True)
        raise
    return decision_id


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


_REPORT_ONE_LINER = re.compile(r"^一句話理由[：:]\s*(.+?)\s*$", re.M)


def _stop_one_liner(phase_a: dict, tdir: Path | None = None) -> str | None:
    """停止理由一句話：reason_one_liner（舊呼叫端）→ thesis_draft.one_liner（FULL_A 格式）→ report_A.md 結論卡。"""
    td = phase_a.get("thesis_draft")
    for s in (phase_a.get("reason_one_liner"), td.get("one_liner") if isinstance(td, dict) else None):
        if isinstance(s, str) and s.strip():
            return s.strip()
    if tdir is not None and (tdir / "report_A.md").is_file():
        m = _REPORT_ONE_LINER.search(_read_text(tdir / "report_A.md"))
        if m:
            return m.group(1)
    return None


def write_stop_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str,
                      phase_a: dict, notes: list[str] | None = None, tdir: Path | None = None) -> str:
    """FULL_A 停止（stopped=true）或 DOSSIER／FULL_A 無法完成時回寫最小三件。回傳 decision_id。

    訊號 NO_ACTION；reason_code＝phase_a["reason_code"]（缺則 MISSING_DATA）；stopped_at_gate＝phase_a["stopped_at_gate"]；
    certainty_grade＝null。欄位以三個 schema 為骨架，無法得出的欄位為 null。不寫論點檔。
    """
    t = _std_ticker(ticker)
    _ymd(asof)
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

    log = _blank_from_fields(log_schema.get("fields") or {})
    log.update({
        "decision_id": decision_id,
        "decision_date": asof,
        "ticker": t,
        "action": "NO_ACTION",
        "stopped_at_gate": gate,
        "reason_one_liner": _stop_one_liner(phase_a, tdir) or f"停止於 {gate or '未知關卡'}，停止碼 {reason}",
        "stop_mode": STOP_MODE,
        "reject_basis": reject_basis(phase_a),
    })
    log.update(_framework_log_fields(paths, tdir))
    status = _resolve_status(conn, t, _status_for("NO_ACTION", reason), notes)
    if notes:
        log["framework_notes"] = notes

    entries = reg_schema.get("entries") or [{}]
    prev = _registry_get(conn, t)
    if prev is not None and prev["status"] == "HOLDING":
        # 持股：保留既有登錄表資料（上限、論點檔、建倉日、失效條件），只更新複查日期；動作由 HOLD_REVIEW 決定
        entry = {**prev["data"], "ticker": t, "status": status, "last_review_date": asof,
                 "last_full_review_date": asof}
    else:
        entry = _blank_from_fields(entries[0] if isinstance(entries[0], dict) else {})
        entry.update({
            "ticker": t,
            "status": status,
            "grade": None,
            "last_review_date": asof,
            "last_full_review_date": asof,
            "reject_basis": log["reject_basis"],
        })
    reg_row: dict[str, Any] = {"ticker": t, "status": status, "data": entry, "updated_at": now}

    _write_rows(conn, [
        ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                       "job_id": None, "signal": "NO_ACTION", "reason_code": reason, "data": log,
                       "created_at": now}, None),
        ("signals", {"decision_id": decision_id, "ticker": t, "signal": "NO_ACTION", "data": card,
                     "created_at": now}, None),
        ("registry", reg_row, ["ticker"]),
    ])
    return decision_id


# ---------------------------------------------------------------- 觸發檢查（M4）

THESIS_VERDICTS = ("完好", "疑似", "證偽")
PRICE_ACTIONS = ("BUY", "ADD", "TRIM", "HOLD", "WATCH")
_PRICE_SIGNAL = {"BUY": "BUY", "ADD": "BUY", "WATCH": "WATCH"}  # 其餘動作沿用上一張訊號卡的訊號


def _registry_get(conn: sqlite3.Connection, ticker: str) -> dict | None:
    row = conn.execute("SELECT status, data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return None
    return {"status": row["status"], "data": json.loads(row["data"] or "{}")}


def _reg_row(ticker: str, status: str | None, data: dict) -> dict[str, Any]:
    return {"ticker": ticker, "status": status, "data": data, "updated_at": db.now_iso()}


def latest_signal(conn: sqlite3.Connection, ticker: str) -> dict | None:
    """該檔最近一張訊號卡：{signal, data}；沒有回 None。"""
    row = conn.execute(
        "SELECT signal, data FROM signals WHERE ticker = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    return {"signal": row["signal"], "data": json.loads(row["data"] or "{}")}


def main_thesis_path(paths: Paths, ticker: str, thesis_file: str | None = None) -> Path | None:
    """主論點檔：thesis_file 存在就用它；否則取 `{T}_YYYY-MM-DD.md` 中日期最新者（排除 addendum）。沒有回 None。"""
    if thesis_file:
        p = Path(thesis_file)
        p = p if p.is_absolute() else paths.state / p
        if p.is_file():
            return p
        if (paths.theses / p.name).is_file():  # 舊紀錄的絕對路徑已失效（state 搬移）：同名檔
            return paths.theses / p.name
    t = _std_ticker(ticker)
    rx = re.compile(rf"^{re.escape(t)}_(\d{{4}}-\d{{2}}-\d{{2}})\.md$")
    cands: list[tuple[str, Path]] = []
    for p in paths.theses.glob(f"{t}_*.md") if paths.theses.is_dir() else []:
        m = rx.match(p.name)
        if m:
            cands.append((m.group(1), p))
    return max(cands)[1] if cands else None


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
    data = {**reg["data"], "last_review_date": asof}
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


def write_thesis_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str,
                        result: dict, params: dict | None = None) -> str:
    """THESIS 完成後回寫：決策日誌（HOLDING：完好／疑似→HOLD、證偽→SELL_ALL；其他→WATCH）、
    論點增補檔、登錄表只更新 last_review_date 與 review_price（合併既有 data）。回傳 decision_id。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    if not isinstance(result, dict):
        raise WritebackError("THESIS 結果須為 dict")
    verdict = result.get("verdict")
    if verdict not in THESIS_VERDICTS:
        raise WritebackError(f"THESIS verdict 不合法：{verdict!r}")
    params = params or {}
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
        rows: list[tuple[str, dict, list[str] | None]] = [
            ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                           "job_id": None, "signal": action, "reason_code": None, "data": log,
                           "created_at": now}, None),
        ]
        if reg is not None:
            data = {**reg["data"], "last_review_date": asof}
            if params.get("price") is not None:
                data["review_price"] = params["price"]
            rows.append(("registry", _reg_row(t, reg["status"], data), ["ticker"]))
        _write_rows(conn, rows)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return decision_id


def write_price_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str, *,
                       action: str, gate1: bool, gate2: str, gate3: str, cap: dict, price: float,
                       price_date: str | None, t30: float | None, reason: str, notes: list[str],
                       g8_adjusted: float | None = None) -> str:
    """PRICE 完成後回寫（僅不需升級時）：決策日誌、訊號卡（沿用上一張、更新數字並加 framework_notes）、
    登錄表合併更新（last_review_date、review_price、t30_at_valuation、auto_buy_cap、sell_review_above、
    absurd_price_line）。三表在同一交易內寫入。回傳 decision_id。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    if action not in PRICE_ACTIONS:
        raise WritebackError(f"PRICE 動作不合法：{action!r}")
    prev = latest_signal(conn, t)
    card: dict[str, Any] = copy.deepcopy(prev["data"]) if prev else {}
    signal = _PRICE_SIGNAL.get(action) or (prev["signal"] if prev else None) or "WATCH"
    decision_id = next_decision_id(conn, t, asof)
    now = db.now_iso()

    vps = dict(card.get("value_per_share") or {})
    vps.update({"conservative": cap.get("IV_cons"), "base": cap.get("IV_base"),
                "optimistic": cap.get("IV_opt"), "zero_growth_IV0": cap.get("IV0")})
    lr = dict(card.get("long_rate") or {})
    lr.update({"tenor": "30Y", "value": t30})
    card.update({
        "as_of": asof, "ticker": t, "signal": signal, "conclusion": _conclusion(signal, card.get("reason_code")),
        "decision_log_id": decision_id, "value_per_share": vps, "long_rate": lr,
        "auto_buy_cap": cap.get("auto_buy_cap"), "buy_price_cap": cap.get("cap"), "r_d": cap.get("r_d"),
        "price": price, "price_date": price_date, "price_to_conservative": cap.get("price_to_cons"),
        "price_to_cap": cap.get("price_to_cap"), "implied_growth": cap.get("g_impl"),
        "g_impl_gt_cap": cap.get("g_impl_gt_cap"), "sell_review_above": cap.get("IV_base"),
        "absurd_price_line": cap.get("absurd_price_line"),
        "g8_adjusted_expected_return": g8_adjusted,   # 以本次價格重算；沿用舊卡的值會用到舊價格
        "framework_notes": list(card.get("framework_notes") or [])
        + [f"{asof} PRICE（{action}）：{n}" for n in notes],
    })

    log = _blank_from_fields(_load_schema(paths, "decision_log_schema.json").get("fields") or {})
    log.update({
        "decision_id": decision_id, "decision_date": asof, "ticker": t, "action": action,
        "review_type": "PRICE", "grade": cap.get("grade_used"), "g_impl": cap.get("g_impl"),
        "r_d": cap.get("r_d"), "buy_price_cap": cap.get("cap"), "price": price, "price_date": price_date,
        "price_to_cap": cap.get("price_to_cap"), "gate1_not_deleted": gate1, "gate2_price_below_cap": gate2,
        "gate3_pass": gate3 == "通過", "gate3_failures": [] if gate3 == "通過" else [gate3],
        "thesis_check": "不適用", "reason_one_liner": reason or "", "framework_notes": list(notes),
    })

    rows: list[tuple[str, dict, list[str] | None]] = [
        ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                       "job_id": None, "signal": signal, "reason_code": card.get("reason_code"), "data": log,
                       "created_at": now}, None),
        ("signals", {"decision_id": decision_id, "ticker": t, "signal": signal, "data": card,
                     "created_at": now}, None),
    ]
    reg = _registry_get(conn, t)
    if reg is not None:
        data = {**reg["data"]}
        data.update({"last_review_date": asof, "review_price": price, "t30_at_valuation": t30,
                     "auto_buy_cap": cap.get("auto_buy_cap")})   # 買進上限缺 → None（不買，失敗關閉）
        # 賣出側的線缺值時保留舊值：缺值＝不觸發，蓋成 None 會讓賣出檢視與荒謬級永久失效
        for k, v in (("sell_review_above", cap.get("IV_base")), ("absurd_price_line", cap.get("absurd_price_line"))):
            if v is not None:
                data[k] = v
            elif data.get(k) is not None:
                log["framework_notes"].append(f"cap_calc 無 {k}，登錄表保留舊值 {data[k]!r}")
        rows.append(("registry", _reg_row(t, reg["status"], data), ["ticker"]))
    _write_rows(conn, rows)
    return decision_id


# ---------------------------------------------------------------- 持股完整複查（H2）

HOLD_REVIEW_ACTIONS = ("HOLD", "TRIM", "SELL_ALL")


def _hold_review_text(ticker: str, asof: str, result: dict, notes: list[str]) -> str:
    lines = [f"# {ticker} 持股完整複查 H2 {asof}", "",
             f"- 動作：{result.get('action')}（類別 {result.get('sell_category') or '無'}；"
             f"停止加碼 {'是' if result.get('stop_adding') else '否'}；觀察至 {result.get('observe_until') or '無'}）",
             f"- 被否定的假設：{result.get('falsified_assumption') or '無'}",
             f"- 理由：{result.get('reason') or ''}", "", "## 決策樹路徑"]
    for q in result.get("path") or []:
        if isinstance(q, dict):
            lines.append(f"- {q.get('q', '')}：{q.get('answer', '')}（{q.get('evidence', '')}）")
    if notes:
        lines += ["", "## 框架註記"] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


def write_hold_review_result(conn: sqlite3.Connection, paths: Paths, ticker: str, asof: str, run_id: str,
                             result: dict, params: dict | None = None, notes: list[str] | None = None, *,
                             price: float | None = None, iv_base: float | None = None) -> str:
    """HOLD_REVIEW 完成後回寫：決策日誌（action＝HOLD／TRIM／SELL_ALL，review_type＝HOLD_REVIEW，含停止加碼與減碼比例）、
    論點增補、登錄表合併更新複查日期與觀察期限。回傳 decision_id。"""
    t = _std_ticker(ticker)
    _ymd(asof)
    action = result.get("action")
    if action not in HOLD_REVIEW_ACTIONS:
        raise WritebackError(f"HOLD_REVIEW 動作不合法：{action!r}")
    params = params or {}
    notes = list(notes or [])
    reg = _registry_get(conn, t)
    decision_id = next_decision_id(conn, t, asof)
    now = db.now_iso()
    text = _hold_review_text(t, asof, result, notes)
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
        })
        if notes:
            log["framework_notes"] = notes
        rows: list[tuple[str, dict, list[str] | None]] = [
            ("decisions", {"decision_id": decision_id, "ticker": t, "decision_date": asof, "run_id": run_id,
                           "job_id": None, "signal": action, "reason_code": result.get("sell_category"),
                           "data": log, "created_at": now}, None),
        ]
        if reg is not None:
            data = {**reg["data"], "last_review_date": asof, "last_full_review_date": asof}
            if result.get("observe_until"):
                data["observe_until"] = result["observe_until"]
            rows.append(("registry", _reg_row(t, reg["status"], data), ["ticker"]))
        _write_rows(conn, rows)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return decision_id
