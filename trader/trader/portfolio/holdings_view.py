"""G8 機會成本比較檔（procedure/06 §2）：加碼現有最佳持股、宿主持股 ≥10 檔時的最弱持股、觀察名單次佳、
短期國庫券、大盤（後兩列見 g8_benchmarks.py）。

只給 FULL_B 與 HOLD_REVIEW（兩者都看得到價格）；DOSSIER、FULL_A、RED_TEAM 的沙箱不放這個檔。
每檔持股與觀察名單以 state/cap_inputs/{T}.json（已隨拆合股換算）換上決策日收盤價與當日長債後重跑 cap_calc：
  預期報酬 = 業主盈餘殖利率（oe ÷ 收盤價）＋ 保守 g0；調整後 = 預期報酬 × 確定性係數（A 0.9／B 0.75／C 0.5）。
比較對象是「決策當日實際可得選項」，所以用收盤價，不用自身買價（CORE-07）；成本權重只列作參考。
"""
from __future__ import annotations

import json
import math
from typing import Any

from ..analysis.checks import _round, _skill_module, cap_input_problems
from ..analysis.writeback import latest_signal
from ..ledger import book
from ..paths import Paths
from .sizing import MAX_HOLDINGS, NO_ADD_ABOVE, add_blocked

SCHEMA = "trader/holdings-g8-v2"
TIE = 0.01   # procedure/06 §2：差 <1pp 視為同分，依品質（交模型判斷）
CERTAINTY_COEF = {"A": 0.9, "B": 0.75, "C": 0.5}   # SKILL G8（推論，±0.1）；D 級不估值

NOTE = ("G8 用：預期報酬＝oe_per_share ÷ close ＋ g0_cons（cap_calc 保守 g0），adjusted＝預期報酬 × 等級係數；"
        "close 為決策日收盤價，比較對象限決策當日可得選項，不得以自身買價作門檻（CORE-07）；"
        "cost_weight 為成本基礎權重，只供參考；market_weight 以收盤價計（缺價用平均成本）。"
        "best_addable＝可加碼者中 adjusted 最高；weakest 只在持股 ≥10 檔時給（須勝過最弱持股）；"
        "best_watch＝觀察名單中今天可買者（A／B 級且收盤 < 重算的自動買進上限）adjusted 最高；"
        "benchmarks 的 treasury／market 由框架計算（market 依 P-66／P-27，區內取值與摩擦為推論）。")


def _num(v: Any) -> float | None:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else None


def _valued_card(conn, ticker: str) -> dict:
    """最近一張有保守價值的訊號卡（論點檢查等快速工作寫的卡沒有估值，跳過）。"""
    rows = conn.execute("SELECT data FROM signals WHERE ticker = ? ORDER BY created_at DESC, rowid DESC",
                        (ticker,)).fetchall()
    for r in rows:
        d = json.loads(r["data"] or "{}")
        if _num((d.get("value_per_share") or {}).get("conservative")) is not None:
            return d
    return (latest_signal(conn, ticker) or {}).get("data") or {}


def _valuation(conn, paths: Paths, t: str, close: float | None, rates: dict | None) -> dict:
    """以 cap_inputs 換上收盤與當日長債重跑 cap_calc：等級、保守價值、自動買進上限、預期報酬。"""
    card = _valued_card(conn, t)
    row: dict[str, Any] = {
        "grade": None, "card_date": card.get("as_of"), "card_signal": card.get("signal"),
        "oe_per_share": None, "g0_cons": None, "owner_earnings_yield": None, "expected_return": None,
        "certainty_coef": None, "adjusted_expected_return": None,
        "iv_cons": None, "price_to_conservative": None, "auto_buy_cap": None, "notes": [],
    }
    cap_path = paths.state / "cap_inputs" / f"{t}.json"
    x = json.loads(cap_path.read_text(encoding="utf-8")) if cap_path.is_file() else None
    row["grade"] = (x or {}).get("grade") or card.get("certainty_grade")
    if x is None:
        row["notes"].append("缺 cap_inputs，無法重算預期報酬")
        return row
    if close is None:
        row["notes"].append("缺決策日收盤價，無法計算預期報酬")
        return row
    x = {**x, "price": close}
    if rates and _num(rates.get("t30")) is not None:
        x["long_rate"] = float(rates["t30"])
    if rates and _num(rates.get("t30_20y_median")) is not None:
        x["long_rate_20y_med"] = float(rates["t30_20y_median"])
    bad = cap_input_problems(x)
    try:
        if bad:
            raise ValueError("；".join(bad))
        cap = _round(_skill_module("cap_calc").calc(x))
    except Exception as e:  # 輸入壞或缺欄：只標註，不讓整個比較檔失敗
        row["notes"].append(f"cap_calc 重算失敗：{type(e).__name__}: {e}")
        return row
    oe, g0 = _num(x.get("oe")), _num((cap.get("g0") or {}).get("cons"))
    row.update({"oe_per_share": oe, "g0_cons": g0, "iv_cons": _num(cap.get("IV_cons")),
                "auto_buy_cap": _num(cap.get("auto_buy_cap"))})
    if row["iv_cons"]:
        row["price_to_conservative"] = round(close / row["iv_cons"], 4)
    coef = CERTAINTY_COEF.get(row["grade"])
    if oe is None or g0 is None:
        row["notes"].append("缺 oe 或保守 g0（歷史成長不足）")
    elif coef is None:
        row["notes"].append(f"等級 {row['grade']!r} 無確定性係數")
    else:
        er = oe / close + g0
        row.update({"owner_earnings_yield": round(oe / close, 4), "expected_return": round(er, 4),
                    "certainty_coef": coef, "adjusted_expected_return": round(er * coef, 4)})
    return row


def _holding_row(conn, paths: Paths, t: str, pos: dict, close: float | None, price_date: str | None,
                 equity: float, basis: float, rates: dict | None, asof: str, paused: set[str]) -> dict:
    cost_value = pos["qty"] * pos["avg_cost"]
    mv = pos["qty"] * (close if close is not None else pos["avg_cost"])
    row: dict[str, Any] = {
        "ticker": t, "qty": pos["qty"], "avg_cost": pos["avg_cost"], "first_buy_date": pos["first_buy_date"],
        "close": close, "price_date": price_date,
        "market_weight": round(mv / equity, 4) if equity > 0 else None,
        "cost_weight": round(cost_value / basis, 4) if basis > 0 else None,
        **_valuation(conn, paths, t, close, rates), "addable": False, "not_addable": [],
    }
    if t in paused:
        row["not_addable"].append("異常暫停交易")
    why = add_blocked(conn, t, asof)
    if why:
        row["not_addable"].append(why)
    if row["market_weight"] is not None and row["market_weight"] > NO_ADD_ABOVE:
        row["not_addable"].append(f"權重 > {NO_ADD_ABOVE:.0%}（P-43）")
    if row["grade"] not in ("A", "B"):
        row["not_addable"].append("等級非 A／B，不加碼")
    row["addable"] = not row["not_addable"]
    return row


def _watch_row(conn, paths: Paths, t: str, close: float | None, price_date: str | None,
               rates: dict | None, paused: set[str]) -> dict:
    """觀察名單：只有今天真的買得到（A／B 級、收盤 < 重算的自動買進上限、未暫停）才算 G8 的可得選項。"""
    row: dict[str, Any] = {"ticker": t, "close": close, "price_date": price_date,
                           **_valuation(conn, paths, t, close, rates), "buyable": False, "not_buyable": []}
    if row["grade"] not in ("A", "B"):
        row["not_buyable"].append("等級非 A／B（最多觀察）")
    cap = row["auto_buy_cap"]
    if close is None or cap is None:
        row["not_buyable"].append("缺收盤價或自動買進上限")
    elif close >= cap:
        row["not_buyable"].append("收盤 ≥ 自動買進上限")
    if t in paused:
        row["not_buyable"].append("異常暫停交易")
    row["buyable"] = not row["not_buyable"]
    return row


def watchlist_tickers(conn) -> list[str]:
    """登錄表 status＝WATCH 且未持有的代號（BUY 未成交也記 WATCH）。"""
    held = set(book.positions(conn))
    rows = conn.execute("SELECT ticker FROM registry WHERE status = 'WATCH' ORDER BY ticker").fetchall()
    return [r["ticker"] for r in rows if r["ticker"] not in held]


def _pick(r: dict | None) -> dict | None:
    return ({"ticker": r["ticker"], "grade": r["grade"], "expected_return": r["expected_return"],
             "adjusted_expected_return": r["adjusted_expected_return"]} if r else None)


def summarize(rows: list[dict], n_holdings: int, watch: list[dict] | None = None) -> dict:
    """best_addable（可加碼者 adjusted 最高）、weakest（持股 ≥ MAX_HOLDINGS 時 adjusted 最低）、
    best_watch（觀察名單今天可買者 adjusted 最高）。"""
    scored = [r for r in rows if r.get("adjusted_expected_return") is not None]
    add = [r for r in scored if r.get("addable")]
    wb = [r for r in watch or [] if r.get("buyable") and r.get("adjusted_expected_return") is not None]
    best = max(add, key=lambda r: r["adjusted_expected_return"]) if add else None
    weak = min(scored, key=lambda r: r["adjusted_expected_return"]) if scored and n_holdings >= MAX_HOLDINGS else None
    bw = max(wb, key=lambda r: r["adjusted_expected_return"]) if wb else None
    return {"best_addable": _pick(best), "weakest": _pick(weak), "best_watch": _pick(bw)}


def holdings_g8(conn, paths: Paths, asof: str, closes: dict[str, dict], rates: dict | None,
                fred=None) -> dict:
    """closes：{ticker: {price, price_date}}（決策日收盤，含持股與觀察名單）。rates：利率檔原始內容或 None。"""
    from .g8_benchmarks import benchmarks
    pos = book.positions(conn)
    wl = watchlist_tickers(conn)
    px = {t: _num((closes.get(t) or {}).get("price")) for t in list(pos) + wl}
    equity = book.equity(conn, {t: p for t, p in px.items() if p is not None and t in pos}) if pos else 0.0
    basis = book.cash(conn) + book.receivables(conn) + sum(p["qty"] * p["avg_cost"] for p in pos.values())
    paused = book.paused(conn)
    pdate = lambda t: d if isinstance(d := (closes.get(t) or {}).get("price_date"), str) else None
    rows = [_holding_row(conn, paths, t, p, px[t], pdate(t),
                         equity, basis, rates, asof, paused) for t, p in pos.items()]
    watch = [_watch_row(conn, paths, t, px[t], pdate(t), rates, paused) for t in wl]
    return {"schema": SCHEMA, "asof": asof, "n_holdings": len(rows), "max_holdings": MAX_HOLDINGS,
            "holdings": rows, "watchlist": watch, **summarize(rows, len(rows), watch),
            "benchmarks": benchmarks(fred, asof),
            "note": NOTE if rows or watch else "紙上帳本無持股、觀察名單空：加碼持股與觀察名單兩列不適用；"
                                              "國庫券與大盤兩列照 benchmarks"}


def for_ticker(view: dict, ticker: str) -> dict:
    """給某一檔：比較對象排除本標的自己（重看持股或觀察名單時，本標的不能跟自己比）；自己那列放 self。"""
    hs, ws = view.get("holdings") or [], view.get("watchlist") or []
    rows = [r for r in hs if r.get("ticker") != ticker]
    watch = [r for r in ws if r.get("ticker") != ticker]
    out = {**view, "holdings": rows, "watchlist": watch,
           **summarize(rows, int(view.get("n_holdings") or 0), watch)}
    mine = [r for r in hs + ws if r.get("ticker") == ticker]
    if mine:
        out["excluded_self"] = ticker
        out["self"] = mine[0]
    return out


def competitors(view: dict) -> list[tuple[str, float]]:
    """G8 的其他選項（調整後預期報酬）；缺值的列不列入。"""
    out: list[tuple[str, float]] = []
    for key, name in (("best_addable", "加碼持股"), ("best_watch", "觀察名單次佳")):
        r = view.get(key) or {}
        if _num(r.get("adjusted_expected_return")) is not None:
            out.append((f"{name} {r.get('ticker')}", float(r["adjusted_expected_return"])))
    b = view.get("benchmarks") or {}
    for key, name in (("treasury", "國庫券"), ("market", "大盤")):
        if _num((b.get(key) or {}).get("adjusted")) is not None:
            out.append((name, float(b[key]["adjusted"])))
    return out


def g8_verdict(self_adj: float | None, view: dict) -> tuple[bool, list[str]]:
    """(是否明顯輸, 理由)。明顯輸＝某選項高出 ≥1pp，或持股 ≥10 檔時最弱持股高出 ≥1pp；差 <1pp 交模型依品質。
    本標的算不出調整後預期報酬 → 無法確認 G8 勝出，也算輸（BUY 須 G8 勝出）。"""
    if self_adj is None:
        return True, ["本標的算不出 G8 調整後預期報酬（缺 oe、保守 g0 或等級係數），無法確認 G8 勝出"]
    why = [f"{n} {v:.4f} 高於本標的 {self_adj:.4f} ≥1pp" for n, v in competitors(view) if v - self_adj >= TIE]
    weak = view.get("weakest") or {}
    wv = _num(weak.get("adjusted_expected_return"))
    if wv is not None and wv - self_adj >= TIE:
        why.append(f"持股 ≥{MAX_HOLDINGS} 檔，未勝過最弱持股 {weak.get('ticker')}（{wv:.4f} vs {self_adj:.4f}）")
    return bool(why), why


def missing_rows(view: dict) -> list[str]:
    """比較表缺的列（只記錄）。"""
    b = view.get("benchmarks") or {}
    return [f"G8 {n}一列缺：{(b.get(k) or {}).get('error')}" for k, n in (("treasury", "國庫券"), ("market", "大盤"))
            if (b.get(k) or {}).get("error")]


def view_path(paths: Paths, run_id: str):
    """每個 run 一份；FULL_B／HOLD_REVIEW prepare 依本標的過濾後放進沙箱。"""
    return paths.work / run_id / "_portfolio" / "holdings_g8.json"


def write_view(conn, paths: Paths, run_id: str, asof: str, closes: dict[str, dict], rates: dict | None,
               fred=None) -> dict:
    view = holdings_g8(conn, paths, asof, closes, rates, fred)
    p = view_path(paths, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(view, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return view


def self_adjusted(cap_input: dict, cap: dict) -> float | None:
    """本標的的 G8 調整後預期報酬（與持股同一算法）：(oe ÷ 價格 ＋ 保守 g0) × 等級係數；缺項回 None。"""
    oe, price = _num(cap_input.get("oe")), _num(cap_input.get("price"))
    g0 = _num((cap.get("g0") or {}).get("cons"))
    coef = CERTAINTY_COEF.get(cap.get("grade_used") or cap_input.get("grade"))
    if oe is None or not price or g0 is None or coef is None:
        return None
    return round((oe / price + g0) * coef, 4)


def g8_notes(model_g8: Any, framework_self: float | None, staged: dict) -> list[str]:
    """模型 G8 比較表與框架數字的落差（只記錄，不改訊號）。"""
    notes: list[str] = []
    if not isinstance(model_g8, dict):
        return notes
    ms = _num(model_g8.get("self_adjusted"))
    if ms is not None and framework_self is not None and abs(ms - framework_self) >= 0.005:
        notes.append(f"G8 本標的調整後預期報酬：模型 {ms:.4f}，腳本 {framework_self:.4f}（訊號卡以腳本為準）")
    best = staged.get("best_addable") or {}
    mb = _num(model_g8.get("best_holding_adjusted"))
    fb = _num(best.get("adjusted_expected_return"))
    if fb is not None and (model_g8.get("best_holding") != best.get("ticker") or mb is None or abs(mb - fb) >= 0.005):
        notes.append(f"G8 最佳持股：模型 {model_g8.get('best_holding')}／{mb}，比較檔 {best.get('ticker')}／{fb:.4f}")
    if (model_g8.get("winner") == "本標的" and framework_self is not None and fb is not None
            and fb - framework_self >= 0.01):
        notes.append(f"G8 模型判本標的勝出，但比較檔最佳持股 {best.get('ticker')} 高 ≥1pp（{fb:.4f} vs {framework_self:.4f}）")
    return notes
