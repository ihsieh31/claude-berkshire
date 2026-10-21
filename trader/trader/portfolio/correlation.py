"""相關群上限（P-31）與共同情境永久損失（P-32）。只算數字與建議，不下單。

規則（rulebook §5.4；rule_index POS-046、RISK-016）：
- P-31：兩檔的失效條件中有任一條描述同一外部變數 → 同群；判定記入兩檔論點檔，之後只能因新事實改判。
  相關群上限＝min（2 × 單檔等級上限，10pp ÷ 群最壞相對損失）。
- P-32：Σ（權重 × 情境減損；金融 50%、非金融 30%）>5% → 該群停止新買；>10% → 從最弱者（POS-046：確定性最低者）減碼到 ≤10%。
- rulebook 第 147 行：相關群與共同情境損失（P-31、P-32）……任何動作不得違反。

實作（標「推論」者為規則書未寫明、由本模組補的口徑）：
- 外部變數：訊號卡／登錄表 external_drivers（受控詞彙 assets/external_drivers.json 的 id）；分群以詞彙的 variable 為鍵，
  方向不分（推論：照 P-31「同一外部變數」字面，油價崩與油價漲同群），連通分量（A、B 共享油價，B、C 共享利率 → 三檔同群）。
- 群最壞相對損失（設計決策 D11）：各成員 P-29 最壞損失（1 − 悲觀價值÷現價，下限 25%）的權重加權平均
  Σ w·max(25%, worst) ÷ Σ w；成員的 worst 由最近訊號卡 max_weight_pct.tail（P-29：10pp ÷ 最壞損失）反推，
  卡上缺 tail 時退回 P-32 減損（金融 50%、非金融 30%）。報表標出綁定的是 P-31（群權重上限）或 P-32（5% 損失額度）。
- 2 × 單檔等級上限：成員等級不同時取群內 A／B 級成員與本標的等級上限的最小者（推論，從嚴）。
- 建倉／加碼額度：買進後群合計不得超過 P-31 上限，且 P-32 損失不得超過 5%（推論：「>5% 停止新買」＋「任何動作不得違反」
  → 單筆買進也不得把群推過 5%）。
- 金融與否：登錄表 asset_class（FULL_A 的 asset_class；銀行／保險／其他金融＝金融）；缺 → 視為金融（50%，推論，從嚴）。
- 外部變數未知（舊卡沒有 external_drivers）→ 不分群、不擋，回傳 warning 並列入報表（使用者要求）。
- 減碼順序：等級低者先（D<C<B<A，缺等級最先），同等級 G8 調整後預期報酬低者先（缺值最先）。只產生建議。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

IMPAIR_FIN = 0.50          # P-32 金融預設減損
IMPAIR_NONFIN = 0.30       # P-32 非金融預設減損
TAIL_FLOOR = 0.25          # P-29：最壞損失下限 25%
STOP_NEW_LOSS = 0.05       # P-32：>5% 該群停止新買
TRIM_LOSS = 0.10           # P-32：>10% 從最弱者減碼到 ≤10%
GROUP_LOSS_BUDGET = 0.10   # P-31：10pp ÷ 群最壞相對損失
GRADE_CAP = {"A": 0.15, "B": 0.10}    # P-28 宿主上限（與 sizing.GRADE_CAP 相同）
FIN_CLASSES = ("銀行", "保險", "其他金融")
GRADE_ORDER = {"A": 3, "B": 2, "C": 1, "D": 0}
VOCAB_FILE = "external_drivers.json"


# ---------------------------------------------------------------- 詞彙與外部變數

def vocab_path(skill_deep: Path | None = None) -> Path:
    if skill_deep is None:
        from ..paths import get_paths
        skill_deep = get_paths().skill_deep
    return Path(skill_deep) / "assets" / VOCAB_FILE


def load_vocab(skill_deep: Path | None = None) -> dict[str, str]:
    """{id: variable}。檔案缺 → 空表（所有 id 都視為自訂）。"""
    p = vocab_path(skill_deep)
    if not p.is_file():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    return {str(x["id"]): str(x.get("variable") or x["id"]) for x in d.get("drivers") or [] if x.get("id")}


def normalize_drivers(raw: Any, vocab: dict[str, str]) -> tuple[list[str] | None, list[dict], list[str]]:
    """模型輸出 → (id 清單或 None, 依據 [{driver, kill_criterion}], 警示)。
    raw 為 None（舊卡、未作答）→ (None, [], [])：未知。空清單 → 沒有描述外部變數的失效條件。"""
    if raw is None:
        return None, [], []
    if not isinstance(raw, list):
        return None, [], [f"external_drivers 格式不是陣列（{type(raw).__name__}），視為未知"]
    ids: list[str] = []
    basis: list[dict] = []
    warns: list[str] = []
    for x in raw:
        if isinstance(x, dict):
            d, kc = str(x.get("driver") or "").strip(), str(x.get("kill_criterion") or "").strip()
        else:
            d, kc = str(x or "").strip(), ""
        if not d:
            continue
        d = d.lower()
        if d not in vocab:
            warns.append(f"外部變數 {d!r} 不在受控詞彙（assets/{VOCAB_FILE}）：只與完全相同的字串分群，請擴充詞彙")
        if d not in ids:
            ids.append(d)
        basis.append({"driver": d, "kill_criterion": kc})
    return ids, basis, warns


def variables(ids: list[str] | None, vocab: dict[str, str]) -> set[str]:
    return {vocab.get(i, i) for i in ids or []}


def impairment(financial: bool | None) -> float:
    return IMPAIR_NONFIN if financial is False else IMPAIR_FIN   # 缺 → 金融（推論，從嚴）


def is_financial(asset_class: Any) -> bool | None:
    if asset_class in FIN_CLASSES:
        return True
    if asset_class == "非金融":
        return False
    return None


def tail_worst_loss(card: dict | None) -> float | None:
    """P-29 最壞損失：由訊號卡 max_weight_pct.tail（＝10pp ÷ 最壞損失，百分比）反推，下限 25%；缺或不合法 → None。"""
    v = ((card or {}).get("max_weight_pct") or {}).get("tail")
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
    return max(TAIL_FLOOR, GROUP_LOSS_BUDGET / (float(v) / 100.0)) if ok else None


# ---------------------------------------------------------------- 資料庫讀取

def _reg(conn, ticker: str) -> dict:
    row = conn.execute("SELECT data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    return json.loads(row["data"]) if row and row["data"] else {}


# 已作廢的訊號卡不參與（`trader decisions void`，M8-D4-02）；與 sizing.NOT_VOIDED 同口徑（sizing 匯入本模組，不能反向匯入）
_SIGNALS_SQL = ("SELECT data FROM signals WHERE ticker = ? AND json_extract(data, '$.voided_at') IS NULL "
                "ORDER BY substr(json_extract(data, '$.as_of'), 1, 10) DESC, created_at DESC, rowid DESC")   # M6_S6-NF-2


def _latest_card_with(conn, ticker: str, key: str) -> dict | None:
    for r in conn.execute(_SIGNALS_SQL, (ticker,)).fetchall():
        d = json.loads(r["data"] or "{}")
        if d.get(key) is not None:
            return d
    return None


def member_info(conn, ticker: str, vocab: dict[str, str]) -> dict:
    """{ticker, drivers(None＝未知), variables, financial, grade, g8}。登錄表優先（判定的保存處），其次最近一張有欄位的訊號卡。"""
    reg = _reg(conn, ticker)
    drivers = reg.get("external_drivers")
    if drivers is None:
        card = _latest_card_with(conn, ticker, "external_drivers")
        drivers = (card or {}).get("external_drivers")
    card = _latest_card_with(conn, ticker, "g8_adjusted_expected_return") or {}
    fin = is_financial(reg.get("asset_class"))
    worst = None
    for r in conn.execute(_SIGNALS_SQL, (ticker,)).fetchall():   # 最近一張有 P-29 尾部上限的卡
        worst = tail_worst_loss(json.loads(r["data"] or "{}"))
        if worst is not None:
            break
    return {"ticker": ticker, "drivers": list(drivers) if isinstance(drivers, list) else None,
            "variables": sorted(variables(drivers, vocab)) if isinstance(drivers, list) else None,
            "financial": fin, "impairment": impairment(fin), "worst_loss": worst, "grade": reg.get("grade"),
            "g8": card.get("g8_adjusted_expected_return")}


# ---------------------------------------------------------------- 分群與群指標（純函式）

def build_groups(members: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """連通分量：共享任一 variable 即相連。回傳 (群清單〔≥2 檔或單檔有變數者都列〕, 外部變數未知的代號)。
    每群：{members: [代號], variables: [共享的變數], links: [(a, b, [變數])]}。"""
    unknown = sorted(t for t, m in members.items() if m.get("variables") is None)
    known = {t: set(m["variables"]) for t, m in members.items() if m.get("variables")}
    parent = {t: t for t in known}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    ts = sorted(known)
    links = []
    for i, a in enumerate(ts):
        for b in ts[i + 1:]:
            shared = known[a] & known[b]
            if shared:
                links.append((a, b, sorted(shared)))
                parent[find(a)] = find(b)
    comps: dict[str, list[str]] = {}
    for t in ts:
        comps.setdefault(find(t), []).append(t)
    groups = []
    for mem in comps.values():
        lk = [x for x in links if x[0] in mem]
        shared = sorted({v for x in lk for v in x[2]})
        groups.append({"members": sorted(mem), "variables": shared, "links": lk})
    groups.sort(key=lambda g: (-len(g["members"]), g["members"]))
    return groups, unknown


def grade_cap_for(grades: list[str | None]) -> float:
    """2 × 單檔等級上限的「單檔等級上限」：群內 A／B 級的最小上限（推論，從嚴）；都不是 A／B → B 級上限。"""
    caps = [GRADE_CAP[g] for g in grades if g in GRADE_CAP]
    return min(caps) if caps else GRADE_CAP["B"]


def _member_worst(m: dict) -> float:
    """成員的 P-29 最壞損失（下限 25%）；缺 tail 用 P-32 減損（D11）。"""
    return max(TAIL_FLOOR, m["worst_loss"]) if m.get("worst_loss") is not None else m["impairment"]


def group_metrics(members: list[dict], weights: dict[str, float], extra_grade: str | None = None) -> dict:
    """群合計權重、P-32 共同情境損失、P-31 上限。members：member_info 列；weights：{代號: 權重}。
    P-31 的群最壞相對損失＝成員 P-29 最壞損失（下限 25%；缺 tail 用 P-32 減損）的權重加權平均（D11）。
    binding：群權重先碰到哪一條——P31（cap）或 P32（損失到 5% 時的群權重＝5% ÷ 加權減損）。"""
    w = {m["ticker"]: float(weights.get(m["ticker"]) or 0.0) for m in members}
    tw = sum(w.values())
    loss = sum(w[m["ticker"]] * m["impairment"] for m in members)
    wl = {m["ticker"]: _member_worst(m) for m in members}
    if tw > 0:
        worst = sum(w[t] * x for t, x in wl.items()) / tw
        imp = loss / tw
    elif members:   # 群尚無權重：以成員的簡單平均（推論）
        worst = sum(wl.values()) / len(members)
        imp = sum(m["impairment"] for m in members) / len(members)
    else:
        worst = imp = IMPAIR_FIN
    gcap = grade_cap_for([m.get("grade") for m in members] + ([extra_grade] if extra_grade else []))
    cap = min(2 * gcap, GROUP_LOSS_BUDGET / worst) if worst > 0 else 2 * gcap
    cap_32 = STOP_NEW_LOSS / imp if imp > 0 else math.inf
    return {"weight": round(tw, 6), "loss": round(loss, 6), "worst_relative_loss": round(worst, 4),
            "worst_basis": {m["ticker"]: "P-29" if m.get("worst_loss") is not None else "P-32 減損" for m in members},
            "grade_cap": gcap, "cap": round(cap, 6), "binding": "P31" if cap < cap_32 else "P32",
            "stop_new": loss > STOP_NEW_LOSS + 1e-12, "trim": loss > TRIM_LOSS + 1e-12}


def trim_recommendations(members: list[dict], weights: dict[str, float], closes: dict[str, float] | None = None,
                         equity: float | None = None) -> list[dict]:
    """P-32 >10%：從最弱者減碼到 ≤10%（只建議）。最弱＝等級最低（缺等級最先），同等級 G8 調整後預期報酬最低（缺值最先）。"""
    loss = sum(float(weights.get(m["ticker"]) or 0) * m["impairment"] for m in members)
    if loss <= TRIM_LOSS + 1e-12:
        return []

    def weak_key(m):
        g8 = m.get("g8")
        return (GRADE_ORDER.get(m.get("grade"), -1), -math.inf if g8 is None else float(g8), m["ticker"])
    recs = []
    for m in sorted(members, key=weak_key):
        if loss <= TRIM_LOSS + 1e-12:
            break
        t, wt = m["ticker"], float(weights.get(m["ticker"]) or 0)
        if wt <= 0:
            continue
        sell_w = min(wt, (loss - TRIM_LOSS) / m["impairment"])
        loss -= sell_w * m["impairment"]
        rec = {"ticker": t, "grade": m.get("grade"), "g8_adjusted_expected_return": m.get("g8"),
               "weight": round(wt, 6), "sell_weight": round(sell_w, 6), "weight_after": round(wt - sell_w, 6),
               "reason": "P-32 共同情境損失 >10%：從最弱者減碼到 ≤10%（POS-046：確定性最低者先）"}
        px = (closes or {}).get(t)
        if px and equity:
            rec["sell_qty_approx"] = math.ceil(sell_w * equity / px)
        recs.append(rec)
    return recs


# ---------------------------------------------------------------- 組合層

def holdings_weights(conn, closes: dict[str, float], pending: dict[str, float] | None = None,
                     include_committed: bool = True) -> tuple[dict[str, float], float]:
    """持股市價權重（缺價用平均成本），加未成交買單（已承諾）與當日已排計畫 pending（權重）。回傳 (權重, 權益)。
    未成交買單以 min(限價, 收盤) 估權重（與持股以收盤計同口徑；D04 後限價＝上限×0.9999，可遠高於收盤，
    以限價計會高估群合計、少給同群後一檔額度；缺收盤才用限價，X38）。"""
    from ..ledger import book
    pos = book.positions(conn)
    px = {t: float(closes[t]) for t in pos if closes.get(t) is not None}
    equity = book.equity(conn, px)
    w: dict[str, float] = {}
    if equity > 0:
        for t, p in pos.items():
            w[t] = p["qty"] * (px.get(t) or p["avg_cost"]) / equity
        if include_committed:
            for o in book.open_orders(conn):
                if o["side"] == "BUY" and o.get("limit_price"):
                    p = float(o["limit_price"])
                    if closes.get(o["ticker"]) is not None:
                        p = min(p, float(closes[o["ticker"]]))
                    w[o["ticker"]] = w.get(o["ticker"], 0.0) + o["qty"] * p / equity
    for t, x in (pending or {}).items():
        w[t] = w.get(t, 0.0) + float(x)
    return w, equity


def portfolio_groups(conn, closes: dict[str, float], vocab: dict[str, str] | None = None,
                     include_committed: bool = True) -> dict:
    """組合現況：各群合計、P-32 損失、P-31 上限、>10% 的減碼建議、外部變數未知的持股（報表用）。"""
    vocab = load_vocab() if vocab is None else vocab
    w, equity = holdings_weights(conn, closes, include_committed=include_committed)
    tickers = sorted(t for t, x in w.items() if x > 0)
    infos = {t: member_info(conn, t, vocab) for t in tickers}
    groups, unknown = build_groups(infos)
    out_groups, recs = [], []
    for g in groups:
        mem = [infos[t] for t in g["members"]]
        met = group_metrics(mem, w)
        rec = trim_recommendations(mem, w, closes, equity) if met["trim"] and len(mem) >= 2 else []
        recs += [{**r, "group": g["members"], "variables": g["variables"]} for r in rec]
        out_groups.append({**g, "links": [list(x) for x in g["links"]], **met,
                           "member_detail": [{k: m[k] for k in ("ticker", "variables", "financial", "impairment",
                                                                 "grade", "g8")} | {"weight": round(w.get(m["ticker"], 0), 6)}
                                             for m in mem]})
    multi = [g for g in out_groups if len(g["members"]) >= 2]
    return {"schema": "trader/corr-groups-v1", "equity": equity, "groups": out_groups,
            "max_group_weight": max((g["weight"] for g in multi), default=0.0),
            "max_group_loss": max((g["loss"] for g in multi), default=0.0),
            "unknown_drivers": unknown, "trim_recommendations": recs,
            "warnings": [f"{t} 沒有外部變數判定（舊訊號卡缺 external_drivers）：不分群、不擋，需於下次完整分析補判定"
                         for t in unknown],
            "note": "P-31 上限＝min(2×單檔等級上限, 10pp÷群最壞相對損失)；群最壞相對損失＝成員 P-29 最壞損失（下限 25%，"
                    "缺 tail 時用 P-32 減損：金融 50%、非金融 30%）權重加權；binding 標出 P-31 或 P-32 先綁定；"
                    "P-32 >5% 停止新買、>10% 從最弱者減碼（只建議，不下單）"}


def buy_room(conn, ticker: str, closes: dict[str, float], vocab: dict[str, str] | None = None,
             pending: dict[str, float] | None = None, info: dict | None = None,
             weights: dict[str, float] | None = None) -> dict:
    """本標的買進（新建或加碼）時的相關群剩餘額度。
    回傳 {status: unknown|none|blocked|ok, room(權重或 None), group, metrics, reason, warning}。
    room 為 None 代表 P-31／P-32 不限制（外部變數未知或沒有共享變數）。"""
    vocab = load_vocab() if vocab is None else vocab
    if weights is None:
        weights, _ = holdings_weights(conn, closes, pending)
    me = info or member_info(conn, ticker, vocab)
    if me.get("variables") is None:
        return {"status": "unknown", "room": None, "group": [ticker],
                "warning": f"{ticker} 沒有外部變數判定：P-31／P-32 無法分群，不擋（列入報表）"}
    others = {t: member_info(conn, t, vocab) for t, x in weights.items() if x > 0 and t != ticker}
    infos = {**others, ticker: me}
    groups, unknown = build_groups(infos)
    g = next((x for x in groups if ticker in x["members"]), {"members": [ticker], "variables": []})
    mem = [infos[t] for t in g["members"]]
    met = group_metrics(mem, weights, extra_grade=me.get("grade"))
    base = {"group": g["members"], "variables": g["variables"], "metrics": met}
    if unknown:   # 「沒檢查到」不等於「確認不同群」：列出外部變數未知的持股
        base["warning"] = (f"持股 {'、'.join(unknown)} 外部變數未知，未納入分群（P-31／P-32 可能低估；"
                           "需於下次完整分析補判定）")
    if len(g["members"]) < 2:   # P-31 的群是兩檔以上：沒有共享變數的持股 → 不受本條限制（單檔尾部由 P-29 管）
        return {**base, "status": "none", "room": None, "reason": "沒有與持股共享的外部變數"}
    if met["stop_new"]:
        return {**base, "status": "blocked", "room": 0.0, "binding": "P32",
                "reason": f"相關群 {'、'.join(g['members'])}（共同變數 {'、'.join(g['variables'])}）"
                          f"P-32 共同情境損失 {met['loss']:.2%} > 5%：該群停止新買"}
    # P-31 以買後的群計：Σw·l＋x·l_c ≤ 10pp（候選自身尾部也算進加權最壞損失），且群權重 ≤ 2×單檔等級上限（A3-M6C-06）
    wsum = sum(float(weights.get(m["ticker"]) or 0.0) * _member_worst(m) for m in mem)
    lc = _member_worst(me)
    room_31 = min(2 * met["grade_cap"] - met["weight"], (GROUP_LOSS_BUDGET - wsum) / lc if lc > 0 else math.inf)
    room_32 = (STOP_NEW_LOSS - met["loss"]) / me["impairment"]
    room = max(0.0, min(room_31, room_32))
    binding = "P31" if room_31 < room_32 else "P32"
    why = (f"相關群 {'、'.join(g['members'])}（共同變數 {'、'.join(g['variables'])}）合計 {met['weight']:.2%}、"
           f"P-31 上限 {met['cap']:.2%}（群最壞相對損失 {met['worst_relative_loss']:.0%}）、P-32 損失 {met['loss']:.2%}："
           f"剩餘額度 {room:.2%}（{'P-31' if binding == 'P31' else 'P-32'} 綁定）")
    return {**base, "status": "ok", "room": room, "binding": binding, "reason": why}


# ---------------------------------------------------------------- 判定的保存（P-31：記入兩檔論點檔，之後只能因新事實改判）

def decide_drivers(prior: list[str] | None, proposed: list[str] | None,
                   change_reason: Any) -> tuple[list[str] | None, str | None]:
    """既有判定 prior 與本次提出的 proposed → (最終判定, 註記)。
    沒有既有判定 → 採用本次；相同 → 不變；不同 → 有新事實（change_reason 非空字串）才改判，否則維持既有。"""
    if proposed is None:
        return prior, None
    if prior is None:
        return list(proposed), None
    if set(prior) == set(proposed):
        return list(prior), None
    reason = change_reason.strip() if isinstance(change_reason, str) else ""
    if reason:
        return list(proposed), f"外部變數改判：{prior} → {proposed}（新事實：{reason}）"
    return list(prior), (f"本次提出的外部變數 {proposed} 與既有判定 {prior} 不同，但未附新事實："
                         "P-31 只能因新事實改判，維持既有判定")


def apply_drivers_to_entry(entry: dict, prev: dict | None, card_or_result: dict, asof: str,
                           vocab: dict[str, str], notes: list[str]) -> list[str] | None:
    """把外部變數判定寫進登錄表 entry（external_drivers、external_drivers_basis、external_drivers_determined_on），
    保留既有 corr_links。card_or_result 讀 external_drivers（已正規化的 id 或模型原始格式）與 external_drivers_change_reason。
    回傳最終判定（None＝未知）。"""
    prev = prev or {}
    ids, basis, warns = normalize_drivers(card_or_result.get("external_drivers"), vocab)
    cb = card_or_result.get("external_drivers_basis")
    if cb and not any(b.get("kill_criterion") for b in basis) and isinstance(cb, list) and ids is not None \
            and {b.get("driver") for b in cb if isinstance(b, dict)} == set(ids):   # 卡上依據須與判定同一組（M6-D10-04）
        basis = list(cb)
    notes += warns
    final, note = decide_drivers(prev.get("external_drivers"), ids,
                                 card_or_result.get("external_drivers_change_reason"))
    if note:
        notes.append(note)
    entry["external_drivers"] = final
    changed = final is not None and final != prev.get("external_drivers")
    entry["external_drivers_determined_on"] = asof if changed else prev.get("external_drivers_determined_on")
    # 改判的新事實（P-31：只能因新事實改判）存進登錄表，record_group_links 的改判增補引用；首次判定不算改判
    reason = card_or_result.get("external_drivers_change_reason")
    if changed:
        entry["external_drivers_change_reason"] = (reason.strip() if isinstance(reason, str) and reason.strip()
                                                   and prev.get("external_drivers") is not None else None)
    elif prev.get("external_drivers_change_reason") is not None:
        entry["external_drivers_change_reason"] = prev["external_drivers_change_reason"]
    # 本次提案被駁回（維持既有判定）→ 依據也維持既有，不得把被駁回提案的依據配上舊判定（M6-D10-04）
    rejected = ids is not None and final is not None and set(final) != set(ids)
    entry["external_drivers_basis"] = prev.get("external_drivers_basis") if rejected else \
        basis if (changed or not prev.get("external_drivers_basis")) and basis else prev.get("external_drivers_basis")
    if prev.get("corr_links") is not None:
        entry["corr_links"] = prev["corr_links"]
    return final


def _holding_tickers(conn) -> set[str]:
    from ..ledger import book
    held = set(book.positions(conn))
    held |= {r["ticker"] for r in conn.execute("SELECT ticker FROM registry WHERE status = 'HOLDING'")}
    return held


def _kc(reg: dict, variables_: set[str], vocab: dict[str, str]) -> str:
    out = [f"「{b.get('kill_criterion') or b.get('driver')}」" for b in reg.get("external_drivers_basis") or []
           if vocab.get(b.get("driver"), b.get("driver")) in variables_]
    return "、".join(out) or "（論點檔未存失效條件原文）"


def record_group_links(conn, paths, ticker: str, asof: str, vocab: dict[str, str] | None = None) -> list[dict]:
    """本標的與持股的同群判定：新判定或因新事實改判時，寫進兩檔登錄表 corr_links 與兩檔論點檔增補。
    已有且未變的判定不重寫（之後只能因新事實改判）。回傳本次新增／變更的連結。"""
    from ..analysis.writeback import _registry_get, _reg_row, _write_addendum, _write_rows, main_thesis_path
    vocab = load_vocab(paths.skill_deep) if vocab is None else vocab
    me_reg = _registry_get(conn, ticker)
    if me_reg is None or not isinstance(me_reg["data"].get("external_drivers"), list):
        return []
    mine = variables(me_reg["data"]["external_drivers"], vocab)
    why = me_reg["data"].get("external_drivers_change_reason")   # 本標的這次改判的新事實（M6-D5-06）
    fact = (f"- 改判依據的新事實（{ticker}，{me_reg['data'].get('external_drivers_determined_on')}）：{why}\n"
            if why and me_reg["data"].get("external_drivers_determined_on") == asof else "")
    changes = []
    for u in sorted(_holding_tickers(conn) - {ticker}):
        u_reg = _registry_get(conn, u)
        if u_reg is None or not isinstance(u_reg["data"].get("external_drivers"), list):
            continue
        shared = sorted(mine & variables(u_reg["data"]["external_drivers"], vocab))
        links = dict(me_reg["data"].get("corr_links") or {})
        old = links.get(u) or {}
        active = old and not old.get("removed_on")
        if shared and (not active or sorted(old.get("variables") or []) != shared):
            kind, rec = "同群", {"variables": shared, "determined_on": asof}
            text = (f"# 相關群判定（P-31）{asof}\n\n- {ticker} 與 {u} 同一相關群：共同外部變數 {'、'.join(shared)}\n"
                    f"- {ticker} 失效條件：{_kc(me_reg['data'], set(shared), vocab)}\n"
                    f"- {u} 失效條件：{_kc(u_reg['data'], set(shared), vocab)}\n"
                    + fact +
                    "- 判定記入兩檔論點檔與登錄表；之後只能因新事實改判（rulebook P-31）。\n")
        elif not shared and active:
            kind, rec = "改判不同群", {**old, "removed_on": asof}
            text = (f"# 相關群改判（P-31）{asof}\n\n- {ticker} 與 {u} 不再同群：外部變數判定已因新事實改判"
                    f"（{ticker}：{me_reg['data'].get('external_drivers')}；{u}：{u_reg['data'].get('external_drivers')}）\n"
                    + fact)
        else:
            continue
        if fact:
            rec = {**rec, "change_reason": why}
        rows = []
        for a, b, reg in ((ticker, u, me_reg), (u, ticker, u_reg)):
            data = {**reg["data"], "corr_links": {**(reg["data"].get("corr_links") or {}), b: rec}}
            rows.append(("registry", _reg_row(a, reg["status"], data), ["ticker"]))
            reg["data"] = data
            main = main_thesis_path(paths, a, data.get("thesis_file"))
            if main is not None:
                _write_addendum(main, text)
        _write_rows(conn, rows)
        changes.append({"a": ticker, "b": u, "kind": kind, **rec})
    return changes


def record_trim_anomalies(conn, report: dict, asof: str) -> int:
    """P-32 >10% 的減碼建議寫成不暫停的異常 corr_group_trim。同檔未結 → 更新 detail（新數字＋日期，保留 first_date）；
    本次報告（完整組合報告）不再建議的未結 corr_group_trim → 結案（條件已消失）。回傳新增筆數。"""
    from .. import db
    from ..ledger import book
    with book.transaction(conn):   # 自成一筆交易，不留隱式交易給後續寫入（A3-M6E-03）
        n = 0
        for a in conn.execute("SELECT detail FROM anomalies WHERE kind='corr_group_trim' AND status='open'").fetchall():
            try:
                last = str((json.loads(a["detail"]) if a["detail"] else {}).get("date") or "")[:10]
            except ValueError:
                last = ""
            if last and asof < last:   # 舊日期（analyze --asof、bootstrap）的報告不倒退、不結案較新的異常（M6-RS4-02）
                return 0
        recs = report.get("trim_recommendations") or []
        for r in recs:
            old = conn.execute("SELECT anomaly_id, detail FROM anomalies WHERE ticker=? AND kind='corr_group_trim' "
                               "AND status='open' ORDER BY anomaly_id", (r["ticker"],)).fetchone()
            if old:
                od = json.loads(old["detail"]) if old["detail"] else {}
                first = od.get("first_date") or od.get("date")   # 舊版異常沒有 first_date：它記的 date 就是首次日期
                detail = {"date": asof, "first_date": first or asof, **r}
                conn.execute("UPDATE anomalies SET detail=? WHERE anomaly_id=?",
                             (json.dumps(detail, ensure_ascii=False, default=str), old["anomaly_id"]))
                continue
            book.write_row(conn, "anomalies", {"ticker": r["ticker"], "kind": "corr_group_trim",
                                          "detail": json.dumps({"date": asof, "first_date": asof, **r},
                                                               ensure_ascii=False, default=str),
                                          "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
            n += 1
        keep = {r["ticker"] for r in recs}
        for a in conn.execute("SELECT anomaly_id, ticker FROM anomalies WHERE kind='corr_group_trim' AND status='open'"
                              ).fetchall():
            if a["ticker"] not in keep:
                conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? WHERE anomaly_id=?",
                             (db.now_iso(), f"{asof} 條件已消失：相關群 P-32 損失已 ≤10% 或該檔不再是減碼對象",
                              a["anomaly_id"]))
    return n
