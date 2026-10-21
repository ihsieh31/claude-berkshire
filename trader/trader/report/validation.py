"""前瞻紙上交易檢討（M6，`trader validate-report`）：依 skill procedure/13 §3 的固定格式，事先寫定、不看結果改格式。

1. 流程合規：日誌筆數 vs 實際決策次數、欄位缺漏率、BUY 缺閘門／問卷紀錄（與 daily 下單同一判定 sizing.gate_failures；
   建倉批次等由非 BUY 日誌支撐的買單也查，M8-D10-04）、決策日後申報的資料、價格停損類賣出（應為 0；只看理由與賣出類別欄，
   M8-D2-07）、skill 凍結雜湊是否一致（列出各值、決策數與起訖日）。
2. 對大盤：紙上組合 vs 基準（含股利，與儀表板同算法；取期間內的淨值序列，M8-D1-05）；每筆買進自成交日起 vs 基準同期
   （Alpaca 還原權值收盤）。基準＝config account.benchmark。
3. 對初篩母體：期初初選的各道（核心、特殊情境、統計排除、刪除）中未被買進者，同期等權平均與中位報酬。
4. 閘門診斷：停在各關的數量、閘門 3 不過的題目分布、閘門 3 通過者的同期報酬（只記錄，不改題）。
5. 誠實標示（固定文字）。
價格只用於事後檢討，不回饋任何決策。
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

import pandas as pd

from .. import db
from ..models import JobStatus
from ..portfolio.sizing import _truthy, gate_failures
from . import metrics as M

DEEP_JOBS = ("DOSSIER", "TRIAGE", "PRICE", "THESIS", "HOLD_REVIEW")
# 價格停損類賣出（M8-D2-07）：只看理由與賣出類別欄，逐句比對；否定句（「非價格停損」等）不算
STOP_WORDS = ("停損", "stop loss", "stop-loss", "trailing stop")
STOP_NEGATIONS = ("not a stop", "no stop")
# 否定詞與「停損」之間可夾同句 8 字內的修飾（「非因股價下跌觸發停損」「並未觸發停損」「不以價格停損」，A3-M8A-03）
STOP_NEG_RE = re.compile(r"(非|不是|並非|不屬於?|沒有|未|無|不設|不採|不)[^。；;，,]{0,8}停損")   # M8-RS2-01
STOP_FIELDS_TEXT = ("reason_one_liner", "reason", "sell_category")
# 缺漏率（M8-D2-04）：停止類（stop_mode 有值）只計停止結果該有的欄位。
# 成交類欄位（M7-D5-02）：紙上成交記在 fills 表、不回填決策日誌，改由 fills（orders.decision_id）推得——
# 該決策有已成交委託才計入分母，有對應 fills 列即算已記錄；未成交不計。滑價取 config account.slippage_bps 當時值（fills 不存）。
FILL_FIELDS = ("fill_price", "fill_date", "slippage_assumed", "tranche_weight", "portfolio_weight_after")
STOP_FIELDS = ("decision_id", "decision_date", "time_zone", "ticker", "action", "skill_hashes", "screen_decision",
               "stopped_at_gate", "reason_one_liner", "stop_mode")
STOP_ONLY_FIELDS = ("stop_mode", "stopped_at_gate")   # 非停止列不適用（M8-D2-04 返工）
NONE_MISSING_FIELDS = ("gate3_failures", "failure_conditions_hit", "review_trigger_hit")   # 只有 None 算缺；[] 是「已作答：無」
LANE_GROUPS = (("核心道", "核心道"), ("特殊情境", "特殊情境"), ("排除", "統計排除"), ("刪除", "刪除"))
# 這些公司行動之後，還原權值收盤不含換得的新股／現金（Alpaca 只還原拆股與股利）→ 報酬不可比
CA_NONCOMPARABLE = ("分拆", "併購換股", "現金併購", "其他")
HONEST = ("樣本小、受市場整體行情左右；任何結論只到「流程可用／不可用」，報酬差只作參考，"
          "不得據此放寬或收緊門檻。要改規則，須另寫測試規格、在新的期間再驗證。")
MISSING_PRICE = "缺價（Alpaca 無資料）"
SHORT_PERIOD = "期間太短（只有 1 根日線）"   # 成交日＝期末等：有價但算不出報酬，不是缺價（A3-M8C2-08）


def honest_text(start: str, end: str) -> str:
    """誠實標示：期間依實際檢討區間組字（使用者已取消固定期限、排程每月出累積報告，M8-D2-11）。"""
    from datetime import date
    days = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
    if days < 45:   # 不到一個半月寫天數，免得「約 0／1 個月」（M8-RS2-02）
        return f"自 {start} 起約 {max(0, days)} 天，" + HONEST
    return f"自 {start} 起約 {max(0, round(days / 30.44))} 個月，" + HONEST


def _logs(conn, start: str, end: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT decision_id, ticker, decision_date, run_id, signal, data FROM decisions "
                          f"WHERE decision_date >= ? AND decision_date <= ? AND {M.NOT_VOIDED} "   # 已作廢不計（M8-D4-02）
                          "ORDER BY decision_date, rowid", (start, end)):
        log = json.loads(r["data"]) if r["data"] else {}
        out.append({**log, "_id": r["decision_id"], "_ticker": r["ticker"], "_date": r["decision_date"],
                    "_run": r["run_id"], "_action": log.get("action") or r["signal"]})
    return out


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _stop_sell(x: dict) -> bool:
    """理由／賣出類別有價格停損字樣（逐句；含否定字樣的句子略過）。"""
    for f in STOP_FIELDS_TEXT:
        for part in re.split(r"[。；;，,\n]", str(x.get(f) or "").lower()):
            if STOP_NEG_RE.search(part) or any(n in part for n in STOP_NEGATIONS):
                continue
            if any(w in part for w in STOP_WORDS):
                return True
    return False


def _missing_rates(full: list[dict], fields: list[str]) -> dict[str, float]:
    """完整分析日誌的欄位缺漏率：分母只算該欄適用的列（停止類只適用 STOP_FIELDS；成交類另由 _fill_rates 算）。沒有適用列的欄不列。"""
    out: dict[str, float] = {}
    for f in fields:
        if f in FILL_FIELDS:
            continue
        rows = [x for x in full if _applies(x, f)]
        if rows:
            out[f] = round(sum(_field_missing(f, x.get(f)) for x in rows) / len(rows), 3)
    return out


def _applies(x: dict, f: str) -> bool:
    """該欄是否適用此列（M8-D2-04 返工）：停止列只看 STOP_FIELDS；非停止列不看 stop_mode／stopped_at_gate；
    delete_reason 只適用 screen_decision=DELETE 的列。"""
    if x.get("stop_mode"):
        return f in STOP_FIELDS
    if f in STOP_ONLY_FIELDS:
        return False
    if f == "delete_reason":
        return str(x.get("screen_decision") or "").strip().upper() == "DELETE"
    return True


def _field_missing(f: str, v: Any) -> bool:
    """缺漏判定：清單型作答欄只有 None 算缺（[] 是已作答），其餘同 _empty。"""
    return v is None if f in NONE_MISSING_FIELDS else _empty(v)


def _fill_rates(conn, logs: list[dict], fields: list[str]) -> tuple[dict[str, float], dict[str, int]]:
    """成交類欄位缺漏率（M7-D5-02）：分母＝有已成交委託的決策，缺漏＝這些委託沒有任何 fills 列。"""
    ids = {x["_id"] for x in logs}
    filled: dict[str, bool] = {}
    for r in conn.execute("SELECT o.decision_id, EXISTS (SELECT 1 FROM fills f WHERE f.order_id = o.order_id) AS rec "
                          "FROM orders o WHERE o.status = 'filled' AND o.decision_id IS NOT NULL"):
        if r[0] in ids:
            filled[r[0]] = filled.get(r[0], False) or bool(r[1])
    basis = {"with_orders": len(ids & {r[0] for r in conn.execute("SELECT DISTINCT decision_id FROM orders")}),
             "filled": len(filled), "no_fill_rows": sum(not v for v in filled.values())}
    if not filled:
        return {}, basis
    rate = round(basis["no_fill_rows"] / len(filled), 3)
    return {f: rate for f in fields if f in FILL_FIELDS}, basis


def compliance(conn, paths, start: str, end: str, logs: list[dict]) -> dict:
    # 人工下單（D12，`trader trade`）另列，不算進分析決策、不查閘門
    manual = [{"decision_id": x["_id"], "ticker": x["_ticker"], "date": x["_date"], "action": x["_action"],
               "anomaly_id": x.get("anomaly_id"), "order_id": x.get("order_id"), "note": x.get("note")}
              for x in logs if x.get("review_type") == "MANUAL"]
    # 框架自發單（PORTFOLIO：制度類硬上限減碼等，daily 自寫決策列）另列，不算分析決策（A3-M8C2-06）
    portfolio = [{"decision_id": x["_id"], "ticker": x["_ticker"], "date": x["_date"], "action": x["_action"],
                  "qty": x.get("qty"), "order_id": x.get("order_id"), "reason": x.get("reason_one_liner") or x.get("reason")}
                 for x in logs if x.get("review_type") == "PORTFOLIO"]
    stop_logs = [x for x in logs if x.get("review_type") != "MANUAL"]   # 停損類賣出照查框架自發單
    logs = [x for x in logs if x.get("review_type") not in ("MANUAL", "PORTFOLIO")]
    jobs = conn.execute(
        f"SELECT DISTINCT run_id, ticker FROM jobs WHERE asof >= ? AND asof <= ? AND status='done' "
        f"AND job_type IN ({','.join('?' * len(DEEP_JOBS))})", (start, end, *DEEP_JOBS)).fetchall()
    job_keys = {(r["run_id"], r["ticker"]) for r in jobs}
    dec_keys = {(x["_run"], x["_ticker"]) for x in logs}
    # 同 (run, 代號) 還有未結束的工作（等待同意、待執行、執行中）：決策尚未做出，不算「沒有日誌」，另列進行中（M8-D2-08）
    unfinished = {(r["run_id"], r["ticker"]) for r in conn.execute(
        f"SELECT DISTINCT run_id, ticker FROM jobs WHERE asof >= ? AND asof <= ? "
        f"AND status NOT IN ({','.join('?' * len(JobStatus.TERMINAL))})", (start, end, *sorted(JobStatus.TERMINAL)))}
    in_progress = (job_keys & unfinished) - dec_keys
    job_keys -= in_progress
    schema = json.loads((paths.skill_deep / "assets" / "decision_log_schema.json").read_text(encoding="utf-8"))
    fields = [k for k in schema.get("fields", {}) if not k.startswith("_")]
    full = [x for x in logs if not x.get("review_type")]
    missing = _missing_rates(full, fields)
    fill_missing, fill_basis = _fill_rates(conn, logs, fields)
    missing.update(fill_missing)
    bad_buys = []
    audited = set()
    for x in logs:
        if x["_action"] not in ("BUY", "ADD"):
            continue
        audited.add(x["_id"])
        why = gate_failures(x)   # 與 daily 下單同一判定（M8-D10-04）
        af = x.get("gate3_answers_file")
        if x["_action"] == "BUY" and (not af or not (paths.state / af).exists() and not Path(str(af)).exists()):
            why.append("問卷作答檔缺")
        if why:
            bad_buys.append({"decision_id": x["_id"], "ticker": x["_ticker"], "problems": why})
    # 期間內的買單，其依據決策不是 BUY／ADD 日誌（建倉批次 ADD 指向最近的閘門決策）：同樣查閘門；人工下單不查
    for o in conn.execute("SELECT o.order_id, o.ticker, o.decision_id, d.data FROM orders o "
                          "LEFT JOIN decisions d ON d.decision_id = o.decision_id WHERE o.side='BUY' "
                          "AND o.created_date >= ? AND o.created_date <= ? ORDER BY o.created_date, o.order_id",
                          (start, end)).fetchall():
        log = json.loads(o["data"]) if o["data"] else {}
        if o["decision_id"] in audited or log.get("review_type") == "MANUAL":
            continue
        why = gate_failures(log) if o["data"] else ["委託沒有對應的決策"]
        if why:
            bad_buys.append({"decision_id": o["decision_id"], "ticker": o["ticker"], "order_id": o["order_id"],
                             "problems": why})
    late = [{"decision_id": x["_id"], "fy_last_filed": x.get("fy_last_filed")} for x in logs
            if isinstance(x.get("fy_last_filed"), str) and x["fy_last_filed"] >= x["_date"]]
    stop_sells = [{"decision_id": x["_id"], "ticker": x["_ticker"]} for x in stop_logs
                  if x["_action"] in ("SELL_ALL", "TRIM") and _stop_sell(x)]
    hd: dict[str, dict] = {}
    for x in full:
        if _empty(x.get("skill_hashes")):
            continue
        h = hd.setdefault(str(x.get("skill_hashes")), {"n": 0, "first": x["_date"], "last": x["_date"]})
        h["n"] += 1
        h["first"], h["last"] = min(h["first"], x["_date"]), max(h["last"], x["_date"])
    hashes = sorted(hd)
    return {"n_decisions": len(logs), "n_full": len(full), "n_actual": len(job_keys | dec_keys),
            "n_triage": sum(1 for x in logs if x.get("review_type") == "TRIAGE"),   # 觸發初判判否（D59）
            "without_decision": sorted(f"{r}:{t}" for r, t in job_keys - dec_keys),
            "in_progress": sorted(f"{r}:{t}" for r, t in in_progress),
            "missing_rate": missing, "fill_basis": fill_basis, "bad_buys": bad_buys, "late_filings": late, "price_stop_sells": stop_sells,
            "skill_hash_values": hashes, "skill_hash_detail": [{"value": k, **hd[k]} for k in hashes],
            "manual_trades": manual, "portfolio_trades": portfolio}


def _returns(alpaca, tickers: list[str], start: str, end: str) -> tuple[dict[str, float], list[str], list[str]]:
    """還原權值（拆股＋股利）收盤：首個 ≥ start 的收盤 → 最後一個 ≤ end 的收盤。
    回傳 (報酬, 缺資料代號（0 根）, 期間太短代號（只有 1 根，A3-M8C2-08）)。"""
    if not tickers:
        return {}, [], []
    df = alpaca.bars(sorted(set(tickers)), start=start, end=end, adjustment="all")
    out, miss, short = {}, [], []
    for t in sorted(set(tickers)):
        s = df[df["ticker"] == t].sort_values("date") if not df.empty else df
        if s is None or len(s) == 0:
            miss.append(t)
            continue
        if len(s) < 2:
            short.append(t)
            continue
        out[t] = float(s["close"].iloc[-1]) / float(s["close"].iloc[0]) - 1
    return out, miss, short


def _stats(xs: list[float]) -> dict:
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 4) if xs else None,
            "median": round(statistics.median(xs), 4) if xs else None}


def _screen_for(paths, start: str) -> Path | None:
    d = paths.state / "screens"
    if not d.is_dir():
        return None
    # 只收純日期目錄：初選 --force／殘檔改名的 {日期}.bak-{時間} 排序在 {日期} 之後，不得被挑中（M1-D9-01）
    dates = sorted(p.name for p in d.iterdir()
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) and (p / "screen.csv").is_file())
    before = [x for x in dates if x <= start]
    pick = before[-1] if before else (dates[0] if dates else None)
    return d / pick if pick else None


def _restructured(conn, ticker: str, after: str, end: str) -> str | None:
    r = conn.execute(f"SELECT action_type, effective_date FROM corporate_actions WHERE ticker=? AND effective_date > ? "
                     f"AND effective_date <= ? AND action_type IN ({','.join('?' * len(CA_NONCOMPARABLE))}) "
                     "ORDER BY effective_date LIMIT 1", (ticker, after, end, *CA_NONCOMPARABLE)).fetchone()
    return f"{r['effective_date']} {r['action_type']}" if r else None


def _period_returns(conn, start: str, end: str) -> tuple[float | None, float | None]:
    """期間報酬（M8-D1-05）：淨值序列中 start（含）之前最後一點（沒有則期間第一點）→ end（含）之前最後一點。
    帳戶用 TWR 指數、基準用含股利指數，與儀表板同算法。"""
    s = M.series(conn)
    base = [x for x in s if x["date"] <= start] or [x for x in s if x["date"] <= end][:1]
    last = [x for x in s if x["date"] <= end]
    if not base or not last:
        return None, None
    b, e = base[-1], last[-1]
    acct = e["account_index"] / b["account_index"] - 1 if b["account_index"] else None
    spy = (e["spy_index"] / b["spy_index"] - 1) if b["spy_index"] and e["spy_index"] is not None else None
    return acct, spy


def market(conn, alpaca, start: str, end: str, bench: str) -> dict:
    acct, spy = _period_returns(conn, start, end)
    out: dict[str, Any] = {"account": M._r(acct), "spy": M._r(spy), "bench": bench,
                           "excess_pp": M._r((acct - spy) * 100, 4) if acct is not None and spy is not None else None,
                           "buys": []}
    nav = conn.execute("SELECT data FROM nav_daily WHERE date <= ? ORDER BY date DESC LIMIT 1", (end,)).fetchone()
    out.update(M._max_group(conn, (json.loads(nav["data"]) if nav and nav["data"] else {}).get("prices") or {}))
    fills = [dict(r) for r in conn.execute("SELECT ticker, fill_date, price, qty FROM fills WHERE side='BUY' "
                                           "AND fill_date >= ? AND fill_date <= ? ORDER BY fill_date", (start, end))]
    if alpaca is None:
        out["note"] = "未連 Alpaca：略過每筆買進 vs SPY"
        return out
    for f in fills:
        r, miss, short = _returns(alpaca, [f["ticker"], bench], f["fill_date"], end)
        ca = _restructured(conn, f["ticker"], f["fill_date"], end)
        ok = f["ticker"] in r and bench in r and ca is None
        out["buys"].append({**f, "return": None if ca else r.get(f["ticker"]), "spy": r.get(bench),
                            "diff": (r[f["ticker"]] - r[bench]) if ok else None, "noncomparable": ca,
                            "missing": miss, "short": short})
    return out


def population(paths, alpaca, start: str, end: str, bought: set[str]) -> dict:
    sdir = _screen_for(paths, start)
    if sdir is None:
        return {"note": "沒有初選結果（state/screens/*/screen.csv）"}
    if alpaca is None:
        return {"screen": sdir.name, "note": "未連 Alpaca：略過母體報酬"}
    p_start = max(sdir.name, start)
    if p_start >= end:
        return {"screen": sdir.name, "note": f"期初初選（{sdir.name}）不早於期末（{end}），沒有可比較的期間"}
    df = pd.read_csv(sdir / "screen.csv", usecols=["ticker", "lane"])
    groups: dict[str, list[str]] = {}
    for _, r in df.iterrows():
        lane = str(r["lane"])
        for prefix, name in LANE_GROUPS:
            if lane.startswith(prefix):
                if r["ticker"] not in bought:
                    groups.setdefault(name, []).append(str(r["ticker"]))
                break
    allt = [t for ts in groups.values() for t in ts]
    rets, miss, short = _returns(alpaca, allt, p_start, end)
    res = {name: _stats([rets[t] for t in ts if t in rets]) for name, ts in groups.items()}
    retained = [rets[t] for n in ("核心道", "特殊情境") for t in groups.get(n, []) if t in rets]
    return {"screen": sdir.name, "groups": res, "retained_not_bought": _stats(retained), "missing": miss, "short": short}


def gates(logs: list[dict], rets: dict[str, float], conn=None, end: str | None = None) -> dict:
    stops: dict[str, int] = {}
    fails: dict[str, int] = {}
    passed = []
    for x in logs:
        if x.get("review_type"):
            continue
        g = x.get("stopped_at_gate") or ("未停止" if x["_action"] in ("BUY", "ADD", "WATCH") else "未記錄")
        stops[g] = stops.get(g, 0) + 1
        for f in x.get("gate3_failures") or []:
            k = f if isinstance(f, str) else json.dumps(f, ensure_ascii=False)
            fails[k] = fails.get(k, 0) + 1
        if _truthy(x.get("gate3_pass")):
            ca = _restructured(conn, x["_ticker"], x["_date"], end) if conn is not None else None
            # rets 以決策編號為鍵（自各決策日起算，M8-D1-05）；舊呼叫端以代號為鍵
            passed.append({"ticker": x["_ticker"], "decision_id": x["_id"],
                           "return": None if ca else rets.get(x["_id"], rets.get(x["_ticker"])), "noncomparable": ca})
    return {"stopped_at": dict(sorted(stops.items())), "gate3_failures": dict(sorted(fails.items(), key=lambda kv: -kv[1])),
            "gate3_passed": passed}


def build(conn, paths, start: str | None, end: str | None, alpaca=None, bench: str = "SPY") -> dict:
    rows = conn.execute("SELECT MIN(date), MAX(date) FROM nav_daily").fetchone()
    first_dec = conn.execute("SELECT MIN(decision_date), MAX(decision_date) FROM decisions").fetchone()
    start = start or rows[0] or first_dec[0]
    end = end or rows[1] or first_dec[1]
    if not start or not end:
        raise ValueError("沒有淨值或決策資料，無法檢討")
    logs = _logs(conn, start, end)
    bought = {r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM fills WHERE side='BUY'")}
    pop = population(paths, alpaca, start, end, bought)
    g3_rets: dict[str, float] = {}
    if alpaca is not None:
        for x in logs:   # 閘門 3 通過者：自各決策日起算（M8-D1-05）
            if _truthy(x.get("gate3_pass")) and not x.get("review_type"):
                r = _returns(alpaca, [x["_ticker"]], x["_date"], end)[0]
                if x["_ticker"] in r:
                    g3_rets[x["_id"]] = r[x["_ticker"]]
    return {"start": start, "end": end, "generated_at": db.now_iso(), "bench": bench,
            "prices": "Alpaca 還原權值收盤" if alpaca is not None else "未連 Alpaca（略過需要價格的段落）",
            "compliance": compliance(conn, paths, start, end, logs),
            "market": market(conn, alpaca, start, end, bench), "population": pop, "gates": gates(logs, g3_rets, conn, end),
            "honest": honest_text(start, end)}


# ---------------------------------------------------------------- markdown

def _p(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def render(r: dict) -> str:
    c, m, p, g = r["compliance"], r["market"], r["population"], r["gates"]
    bench = m.get("bench") or r.get("bench") or "SPY"
    L = [f"# 前瞻紙上交易檢討（{r['start']} → {r['end']}）", "",
         f"產出時間 {r.get('generated_at') or '—'}｜基準 {bench}｜價格來源 {r.get('prices') or '—'}", "",
         "> 格式依 procedure/13 §3 事先寫定；價格只用於事後檢討，不回饋決策。", "",
         "## 1. 流程合規", "",
         f"- 決策日誌 {c['n_decisions']} 筆（完整分析 {c['n_full']}；觸發初判判否 {c.get('n_triage', 0)}）；"
         f"實際決策（run×代號）{c['n_actual']} 次"
         + (f"；沒有日誌的：{'、'.join(c['without_decision'])}" if c["without_decision"] else "；全部有日誌"),
         *([f"- 尚未完成（等待同意或待執行，不計入）：{'、'.join(c['in_progress'])}"] if c.get("in_progress") else []),
         f"- BUY／ADD 缺閘門或問卷紀錄：{len(c['bad_buys'])} 筆" + "".join(
             f"\n  - {b['decision_id']}：{'、'.join(b['problems'])}" for b in c["bad_buys"]),
         f"- 使用決策日（含）之後申報的資料：{len(c['late_filings'])} 筆" + "".join(   # 明細（A3-M8C2-07）
             f"\n  - {x['decision_id']}：申報日 {x.get('fy_last_filed')}" for x in c["late_filings"]),
         f"- 價格停損／移動停損類賣出：{len(c['price_stop_sells'])} 筆（應為 0）" + "".join(
             f"\n  - {x['decision_id']}（{x['ticker']}）" for x in c["price_stop_sells"]),
         f"- 人工下單（trader trade）：{len(c.get('manual_trades') or [])} 筆" + "".join(
             f"\n  - {x['date']} {x['ticker']} {x['action']}（{x['decision_id']}；委託 {x.get('order_id') or '—'}；"
             f"異常 {x.get('anomaly_id') or '—'}）：{x.get('note') or ''}" for x in c.get("manual_trades") or []),
         f"- 框架自發委託（制度類硬上限減碼等，不計入決策筆數）：{len(c.get('portfolio_trades') or [])} 筆" + "".join(
             f"\n  - {x['date']} {x['ticker']} {x['action']}（{x['decision_id']}；委託 {x.get('order_id') or '—'}）："
             f"{x.get('reason') or ''}" for x in c.get("portfolio_trades") or []),
         f"- skill 凍結雜湊：{len(c['skill_hash_values'])} 種" + ("（期間內規則有變，需檢查修改紀錄）" if len(c["skill_hash_values"]) > 1 else "")
         + "".join(f"\n  - {h['value']}：{h['n']} 筆決策，{h['first']} → {h['last']}" for h in c.get("skill_hash_detail") or []),
         ""]
    worst = sorted(((k, v) for k, v in c["missing_rate"].items() if v > 0), key=lambda kv: -kv[1])[:12]
    if worst:
        L += ["欄位缺漏率（完整分析決策；前 12 高）：", "", "| 欄位 | 缺漏率 |", "|---|---|"]
        L += [f"| {k} | {v * 100:.0f}% |" for k, v in worst]
        L.append("")
    fb = c.get("fill_basis")
    if fb:
        L += [f"成交類欄位（fill_price、fill_date、slippage_assumed、tranche_weight、portfolio_weight_after）由 fills 表推得：" 
              f"有委託的決策 {fb['with_orders']} 筆，已成交 {fb['filled']} 筆（未成交不計），其中缺成交紀錄 {fb['no_fill_rows']} 筆；"
              "滑價為 config account.slippage_bps 當時值（fills 不存）。", ""]
    L += ["## 2. 對大盤", "",
          f"- 紙上組合 {_p(m['account'])}；{bench}（含股利）{_p(m['spy'])}；超額 "
          + ("—" if m["excess_pp"] is None else f"{m['excess_pp']:+.2f}pp"),
          "- 組合：最大相關群合計權重 " + ("—（沒有 2 檔以上的相關群）" if m.get("max_group_weight") is None else
                                  f"{m['max_group_weight'] * 100:.1f}%（{'、'.join(m['max_group']['members'])}；"
                                  f"共同變數 {'、'.join(m['max_group']['variables'])}）")
          + (f"；外部變數未知：{'、'.join(m['unknown_drivers'])}" if m.get("unknown_drivers") else "")
          + "（目前持股，M8-D10-07）", ""]
    if m.get("note"):
        L += [f"- {m['note']}", ""]
    if m["buys"]:
        def px(b: dict, key: str, t: str) -> str:
            if b.get(key) is None and t in (b.get("short") or []):
                return SHORT_PERIOD
            return MISSING_PRICE if b.get(key) is None and t in (b.get("missing") or []) else _p(b.get(key))
        L += [f"| 成交日 | 代號 | 股數 | 成交價 | 至期末 | {bench} 同期 | 差 |", "|---|---|---|---|---|---|---|"]
        L += [f"| {b['fill_date']} | {b['ticker']} | {b['qty']} | {b['price']:.2f} | "
              + (f"不可比（{b['noncomparable']}）" if b.get("noncomparable") else px(b, "return", b["ticker"]))
              + f" | {px(b, 'spy', bench)} | {_p(b['diff'])} |" for b in m["buys"]]
        L.append("")
    L += ["## 3. 對初篩母體（等權，未被買進者）", ""]
    if p.get("note"):
        L += [f"- {p['note']}", ""]
    elif p:
        L += [f"期初初選：{p['screen']}", "", "| 群組 | 檔數 | 平均 | 中位 |", "|---|---|---|---|"]
        rb = p["retained_not_bought"]
        L.append(f"| 保留未買（核心＋特殊） | {rb['n']} | {_p(rb['mean'])} | {_p(rb['median'])} |")
        L += [f"| {k} | {v['n']} | {_p(v['mean'])} | {_p(v['median'])} |" for k, v in p["groups"].items()]
        if p["missing"]:
            L.append(f"\n缺價格資料（未計入）：{len(p['missing'])} 檔：{'、'.join(p['missing'])}")
        if p.get("short"):
            L.append(f"\n期間太短（只有 1 根日線，未計入）：{'、'.join(p['short'])}")
        L.append("")
    L += ["## 4. 閘門診斷", "", "| 停在 | 次數 |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in g["stopped_at"].items()] or ["| — | 0 |"]
    L.append("")
    if g["gate3_failures"]:
        L += ["閘門 3 不過的題目：", ""] + [f"- {k}：{v}" for k, v in g["gate3_failures"].items()] + [""]
    if g["gate3_passed"]:
        L += ["閘門 3 通過者的同期報酬（只記錄，不改題）：", ""]
        L += [f"- {x['ticker']}（{x['decision_id']}）："
              + (f"不可比（{x['noncomparable']}）" if x.get("noncomparable") else _p(x["return"])) for x in g["gate3_passed"]]
        L.append("")
    L += ["## 5. 誠實標示", "", r.get("honest") or honest_text(r["start"], r["end"]), ""]
    return "\n".join(L)


def write(r: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"validation_{r['start']}_{r['end']}"
    md, js = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    md.write_text(render(r), encoding="utf-8")
    js.write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return md, js
