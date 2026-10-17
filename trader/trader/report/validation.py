"""前瞻紙上交易檢討（M6，`trader validate-report`）：依 skill procedure/13 §3 的固定格式，事先寫定、不看結果改格式。

1. 流程合規：日誌筆數 vs 實際決策次數、欄位缺漏率、BUY 缺閘門／問卷紀錄、決策日後申報的資料、價格停損類賣出（應為 0）、
   skill 凍結雜湊是否一致。
2. 對大盤：紙上組合 vs SPY（含股利，與儀表板同算法）；每筆買進自成交日起 vs SPY 同期（Alpaca 還原權值收盤）。
3. 對初篩母體：期初初選的各道（核心、特殊情境、統計排除、刪除）中未被買進者，同期等權平均與中位報酬。
4. 閘門診斷：停在各關的數量、閘門 3 不過的題目分布、閘門 3 通過者的同期報酬（只記錄，不改題）。
5. 誠實標示（固定文字）。
價格只用於事後檢討，不回饋任何決策。
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import pandas as pd

from . import metrics as M

DEEP_JOBS = ("DOSSIER", "TRIAGE", "PRICE", "THESIS", "HOLD_REVIEW")
GATE_KEYS = ("gate1_not_deleted", "gate2_price_below_cap", "gate3_pass")
STOP_WORDS = ("停損", "stop loss", "stop-loss", "移動停損", "trailing")
LANE_GROUPS = (("核心道", "核心道"), ("特殊情境", "特殊情境"), ("排除", "統計排除"), ("刪除", "刪除"))
# 這些公司行動之後，還原權值收盤不含換得的新股／現金（Alpaca 只還原拆股與股利）→ 報酬不可比
CA_NONCOMPARABLE = ("分拆", "併購換股", "現金併購", "其他")
HONEST = ("3 個月（使用者指定）樣本小、受市場整體行情左右；任何結論只到「流程可用／不可用」，報酬差只作參考，"
          "不得據此放寬或收緊門檻。要改規則，須另寫測試規格、在新的期間再驗證。")


def _logs(conn, start: str, end: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT decision_id, ticker, decision_date, run_id, signal, data FROM decisions "
                          "WHERE decision_date >= ? AND decision_date <= ? ORDER BY decision_date, rowid", (start, end)):
        log = json.loads(r["data"]) if r["data"] else {}
        out.append({**log, "_id": r["decision_id"], "_ticker": r["ticker"], "_date": r["decision_date"],
                    "_run": r["run_id"], "_action": log.get("action") or r["signal"]})
    return out


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _truthy(v: Any) -> bool:
    """與 sizing._truthy 相同：PRICE 回寫的閘門 2 是字串「通過」。"""
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "通過", "yes", "1"))


def compliance(conn, paths, start: str, end: str, logs: list[dict]) -> dict:
    jobs = conn.execute(
        f"SELECT DISTINCT run_id, ticker FROM jobs WHERE asof >= ? AND asof <= ? AND status='done' "
        f"AND job_type IN ({','.join('?' * len(DEEP_JOBS))})", (start, end, *DEEP_JOBS)).fetchall()
    job_keys = {(r["run_id"], r["ticker"]) for r in jobs}
    dec_keys = {(x["_run"], x["_ticker"]) for x in logs}
    schema = json.loads((paths.skill_deep / "assets" / "decision_log_schema.json").read_text(encoding="utf-8"))
    fields = [k for k in schema.get("fields", {}) if not k.startswith("_")]
    full = [x for x in logs if not x.get("review_type")]
    missing = {f: round(sum(_empty(x.get(f)) for x in full) / len(full), 3) for f in fields} if full else {}
    bad_buys = []
    for x in logs:
        if x["_action"] not in ("BUY", "ADD"):
            continue
        why = [k for k in GATE_KEYS if not _truthy(x.get(k))]
        af = x.get("gate3_answers_file")
        if x["_action"] == "BUY" and (not af or not (paths.state / af).exists() and not Path(str(af)).exists()):
            why.append("問卷作答檔缺")
        if why:
            bad_buys.append({"decision_id": x["_id"], "ticker": x["_ticker"], "problems": why})
    late = [{"decision_id": x["_id"], "fy_last_filed": x.get("fy_last_filed")} for x in logs
            if isinstance(x.get("fy_last_filed"), str) and x["fy_last_filed"] >= x["_date"]]
    stop_sells = [{"decision_id": x["_id"], "ticker": x["_ticker"]} for x in logs
                  if x["_action"] in ("SELL_ALL", "TRIM")
                  and any(w in json.dumps(x, ensure_ascii=False).lower() for w in STOP_WORDS)]
    hashes = sorted({str(x.get("skill_hashes")) for x in full if not _empty(x.get("skill_hashes"))})
    return {"n_decisions": len(logs), "n_full": len(full), "n_actual": len(job_keys | dec_keys),
            "without_decision": sorted(f"{r}:{t}" for r, t in job_keys - dec_keys),
            "missing_rate": missing, "bad_buys": bad_buys, "late_filings": late, "price_stop_sells": stop_sells,
            "skill_hash_values": hashes}


def _returns(alpaca, tickers: list[str], start: str, end: str) -> tuple[dict[str, float], list[str]]:
    """還原權值（拆股＋股利）收盤：首個 ≥ start 的收盤 → 最後一個 ≤ end 的收盤。回傳 (報酬, 缺資料代號)。"""
    if not tickers:
        return {}, []
    df = alpaca.bars(sorted(set(tickers)), start=start, end=end, adjustment="all")
    out, miss = {}, []
    for t in sorted(set(tickers)):
        s = df[df["ticker"] == t].sort_values("date") if not df.empty else df
        if s is None or len(s) < 2:
            miss.append(t)
            continue
        out[t] = float(s["close"].iloc[-1]) / float(s["close"].iloc[0]) - 1
    return out, miss


def _stats(xs: list[float]) -> dict:
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 4) if xs else None,
            "median": round(statistics.median(xs), 4) if xs else None}


def _screen_for(paths, start: str) -> Path | None:
    d = paths.state / "screens"
    if not d.is_dir():
        return None
    dates = sorted(p.name for p in d.iterdir() if (p / "screen.csv").is_file())
    before = [x for x in dates if x <= start]
    pick = before[-1] if before else (dates[0] if dates else None)
    return d / pick if pick else None


def _restructured(conn, ticker: str, after: str, end: str) -> str | None:
    r = conn.execute(f"SELECT action_type, effective_date FROM corporate_actions WHERE ticker=? AND effective_date > ? "
                     f"AND effective_date <= ? AND action_type IN ({','.join('?' * len(CA_NONCOMPARABLE))}) "
                     "ORDER BY effective_date LIMIT 1", (ticker, after, end, *CA_NONCOMPARABLE)).fetchone()
    return f"{r['effective_date']} {r['action_type']}" if r else None


def market(conn, alpaca, start: str, end: str, bench: str) -> dict:
    s = M.summary(conn)
    out: dict[str, Any] = {"account": s.get("return"), "spy": s.get("spy_return"), "excess_pp": s.get("excess_pp"),
                           "buys": []}
    fills = [dict(r) for r in conn.execute("SELECT ticker, fill_date, price, qty FROM fills WHERE side='BUY' "
                                           "AND fill_date >= ? AND fill_date <= ? ORDER BY fill_date", (start, end))]
    if alpaca is None:
        out["note"] = "未連 Alpaca：略過每筆買進 vs SPY"
        return out
    for f in fills:
        r, _ = _returns(alpaca, [f["ticker"], bench], f["fill_date"], end)
        ca = _restructured(conn, f["ticker"], f["fill_date"], end)
        ok = f["ticker"] in r and bench in r and ca is None
        out["buys"].append({**f, "return": None if ca else r.get(f["ticker"]), "spy": r.get(bench),
                            "diff": (r[f["ticker"]] - r[bench]) if ok else None, "noncomparable": ca})
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
    rets, miss = _returns(alpaca, allt, p_start, end)
    res = {name: _stats([rets[t] for t in ts if t in rets]) for name, ts in groups.items()}
    retained = [rets[t] for n in ("核心道", "特殊情境") for t in groups.get(n, []) if t in rets]
    return {"screen": sdir.name, "groups": res, "retained_not_bought": _stats(retained), "missing": miss}


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
            passed.append({"ticker": x["_ticker"], "decision_id": x["_id"],
                           "return": None if ca else rets.get(x["_ticker"]), "noncomparable": ca})
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
    g3 = [x["_ticker"] for x in logs if _truthy(x.get("gate3_pass"))]
    g3_rets = _returns(alpaca, g3, start, end)[0] if alpaca is not None and g3 else {}
    return {"start": start, "end": end, "compliance": compliance(conn, paths, start, end, logs),
            "market": market(conn, alpaca, start, end, bench), "population": pop, "gates": gates(logs, g3_rets, conn, end),
            "honest": HONEST}


# ---------------------------------------------------------------- markdown

def _p(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def render(r: dict) -> str:
    c, m, p, g = r["compliance"], r["market"], r["population"], r["gates"]
    L = [f"# 前瞻紙上交易檢討（{r['start']} → {r['end']}）", "",
         "> 格式依 procedure/13 §3 事先寫定；價格只用於事後檢討，不回饋決策。", "",
         "## 1. 流程合規", "",
         f"- 決策日誌 {c['n_decisions']} 筆（完整分析 {c['n_full']}）；實際決策（run×代號）{c['n_actual']} 次"
         + (f"；沒有日誌的：{'、'.join(c['without_decision'])}" if c["without_decision"] else "；全部有日誌"),
         f"- BUY／ADD 缺閘門或問卷紀錄：{len(c['bad_buys'])} 筆" + "".join(
             f"\n  - {b['decision_id']}：{'、'.join(b['problems'])}" for b in c["bad_buys"]),
         f"- 使用決策日（含）之後申報的資料：{len(c['late_filings'])} 筆",
         f"- 價格停損／移動停損類賣出：{len(c['price_stop_sells'])} 筆（應為 0）",
         f"- skill 凍結雜湊：{len(c['skill_hash_values'])} 種" + ("（期間內規則有變，需檢查修改紀錄）" if len(c["skill_hash_values"]) > 1 else ""),
         ""]
    worst = sorted(((k, v) for k, v in c["missing_rate"].items() if v > 0), key=lambda kv: -kv[1])[:12]
    if worst:
        L += ["欄位缺漏率（完整分析決策；前 12 高）：", "", "| 欄位 | 缺漏率 |", "|---|---|"]
        L += [f"| {k} | {v * 100:.0f}% |" for k, v in worst]
        L.append("")
    L += ["## 2. 對大盤", "",
          f"- 紙上組合 {_p(m['account'])}；SPY（含股利）{_p(m['spy'])}；超額 "
          + ("—" if m["excess_pp"] is None else f"{m['excess_pp']:+.2f}pp"), ""]
    if m.get("note"):
        L += [f"- {m['note']}", ""]
    if m["buys"]:
        L += ["| 成交日 | 代號 | 股數 | 成交價 | 至期末 | SPY 同期 | 差 |", "|---|---|---|---|---|---|---|"]
        L += [f"| {b['fill_date']} | {b['ticker']} | {b['qty']} | {b['price']:.2f} | "
              + (f"不可比（{b['noncomparable']}）" if b.get("noncomparable") else _p(b["return"]))
              + f" | {_p(b['spy'])} | {_p(b['diff'])} |" for b in m["buys"]]
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
            L.append(f"\n缺價格資料（未計入）：{len(p['missing'])} 檔")
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
    L += ["## 5. 誠實標示", "", r["honest"], ""]
    return "\n".join(L)


def write(r: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"validation_{r['start']}_{r['end']}"
    md, js = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    md.write_text(render(r), encoding="utf-8")
    js.write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return md, js
