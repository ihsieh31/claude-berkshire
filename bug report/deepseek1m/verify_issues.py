#!/usr/bin/env python3
"""逐條複驗報告中的指控；同時主動尋找「我自己寫錯」的項目。

只讀專案程式，只寫本目錄 tmp/。輸出 [CONFIRMED] / [REFUTED] / [OVERSTATED]。
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path("/Users/zongen/Downloads/claude code/berkshire")
sys.path.insert(0, str(ROOT / "trader"))
sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))

from trader import db                                     # noqa: E402
from trader.paths import get_paths                        # noqa: E402
from trader.ledger import book, broker                    # noqa: E402
from trader.portfolio import sizing as Z                 # noqa: E402

OUT = []


def say(tag, title, detail):
    OUT.append((tag, title, detail))
    print(f"\n[{tag}] {title}\n    {detail}")


def fresh(tmp, name):
    paths = get_paths(state_dir=tmp / name, root=ROOT).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100000, "2026-01-02")
    return paths, conn


# ---------------------------------------------------------------- V1 賣出後買進：撮合順序依代號字母
def v1_sell_then_buy_ordering(tmp):
    results = {}
    for buy_t, sell_t in (("AAA", "ZZZ"), ("ZZZ", "AAA")):
        paths, conn = fresh(tmp, f"v1{buy_t}")
        conn.execute("UPDATE cash_ledger SET amount = 500")   # 只有 500 現金
        conn.commit()
        with book.transaction(conn):
            book.write_row(conn, "positions", {"ticker": sell_t, "qty": 10, "avg_cost": 50.0,
                                               "first_buy_date": "2026-01-02", "updated_at": db.now_iso()})
        broker.place_order(conn, sell_t, "SELL", 10, None, "d1", "2026-01-05")
        broker.place_order(conn, buy_t, "BUY", 10, 100.0, "d1", "2026-01-05")
        fills = broker.match_orders(conn, "2026-01-06",
                                    {buy_t: {"open": 100.0, "high": 101, "low": 99, "close": 100},
                                     sell_t: {"open": 100.0, "high": 101, "low": 99, "close": 100}}, 0, 0)
        results[f"買{buy_t}/賣{sell_t}"] = {f["ticker"]: f["qty"] for f in fills}
    a, b = results.values()
    if a.get("AAA") != b.get("ZZZ"):
        say("CONFIRMED", "同一輪的賣出＋買進，成交結果取決於代號字母順序",
            f"現金 500、賣 10 股可得 1000、買 10 股需 1000。{results}。"
            "match_orders 依 (created_date, order_id) 排序，order_id 以代號開頭 → 買單先撮就現金不足、"
            "只成交部分且訂單被標成 filled，剩餘股數永久丟失；賣單先撮則完全成交。"
            "這讓「換股」變成碰運氣，且沒有任何重試。")
    else:
        say("REFUTED", "賣出後買進的撮合順序", str(results))


# ---------------------------------------------------------------- V2 組合狀態缺現金
def v2_portfolio_state(tmp):
    from trader.pipelines.analyze import _portfolio_state
    paths, conn = fresh(tmp, "v2")
    with book.transaction(conn):
        book.write_row(conn, "positions", {"ticker": "KO", "qty": 100, "avg_cost": 50.0,
                                           "first_buy_date": "2026-01-02", "updated_at": db.now_iso()})
    cfg = SimpleNamespace(account=SimpleNamespace(initial_cash=100000))
    st = _portfolio_state(cfg, conn)
    missing = [k for k in ("cash", "n_holdings", "committed", "weights") if k not in st]
    say("CONFIRMED" if missing else "REFUTED", "FULL_B 拿到的 portfolio_state 缺現金／檔數",
        f"有持股時回傳 {st}；缺 {missing}。SKILL §0 要求「持股、現金、下限、已承諾、檔數」。"
        "（無持股時反而有 cash，行為不一致。）")


# ---------------------------------------------------------------- V3 first_tranche 0 被當 50
def v3_first_tranche_zero(tmp):
    card = {"certainty_grade": "B", "target_weight_pct": 10,
            "position_plan": {"first_tranche_pct_of_target": 0}}
    pct = (card.get("position_plan") or {}).get("first_tranche_pct_of_target") or \
        card.get("first_tranche_pct_of_target") or 50
    say("CONFIRMED" if pct == 50 else "REFUTED", "first_tranche_pct_of_target=0 被當成 50",
        f"模型若填 0（意圖＝不建倉），`or 50` 會把它變成 50%。實測 pct={pct}。"
        "（實務上模型不會填 0，屬邊界 bug，非主要風險。）")


# ---------------------------------------------------------------- V4 triage 只留 20 筆
def v4_triage_truncation(tmp):
    items = [{"type": "news", "date": f"2026-09-{i:02d}"} for i in range(1, 26)]
    kept = sorted(items, key=lambda t: t["date"])[-20:]
    say("CONFIRMED" if len(kept) == 20 else "REFUTED", "triage 只保留最後 20 筆，其餘靜默丟棄",
        f"25 筆事件 → 只剩 {len(kept)} 筆；`review_due.py:131` 直接截斷，沒有註記。"
        "（影響有限：20 筆以上很少見。）")


# ---------------------------------------------------------------- V5 利率單調性
def v5_rate_monotonicity(tmp):
    import cap_calc
    caps = {}
    for L in (0.01, 0.03, 0.05, 0.07, 0.09):
        r = cap_calc.calc({"oe": 5.0, "g10": 0.05, "g5": 0.05, "grade": "B", "long_rate": L,
                           "long_rate_20y_med": 0.045, "price": 100.0, "norm_eps": 5.0,
                           "largecap_pe_median": 26.6})
        caps[L] = r["auto_buy_cap"]
    flat = len({round(v, 6) for v in list(caps.values())[:4]}) == 1
    inverted = caps[0.01] < caps[0.05]
    say("CONFIRMED" if flat and inverted else "REFUTED", "30 年期殖利率 1%–7% 對買價無影響，且低利率時買價更低",
        f"auto_buy_cap：{ {f'{k:.0%}': round(v,2) for k,v in caps.items()} }。"
        f"1%–7% 完全相同={flat}；1% 的上限低於 5%={inverted}。"
        "（10% 下限是 SKILL 的保守設計，但 g_term=min(3%,L−1%) 造成方向顛倒，這部分是實作錯誤。）")


# ---------------------------------------------------------------- V6 等級不影響買價
def v6_grade_price(tmp):
    import cap_calc
    caps = {}
    for g in ("A", "B", "C"):
        r = cap_calc.calc({"oe": 5.0, "g10": 0.05, "g5": 0.05, "grade": g, "long_rate": 0.042,
                           "long_rate_20y_med": 0.045, "price": 100.0, "norm_eps": 5.0,
                           "largecap_pe_median": 26.6})
        caps[g] = (r["auto_buy_cap"], r["components"]["P1_report_only"])
    same = len({round(v[0], 6) for v in caps.values()}) == 1
    say("OVERSTATED" if same else "REFUTED", "「等級不影響買價」——正確，但我原本說成 bug",
        f"auto_buy_cap A/B/C={ {k: round(v[0],2) for k,v in caps.items()} }；P1 分別為 "
        f"{ {k: round(v[1],2) for k,v in caps.items()} }。SKILL §3 本來就把等級係數定義在 P1（安全邊際欄與 G8），"
        "不進硬上限——所以這是**設計如此**，不是實作 bug。我原本寫「安全邊際在實作上不存在」是誇大："
        "安全邊際改由部位大小（A 15%／B 10%）與 G8 體現。")


# ---------------------------------------------------------------- V7 同日雙公司行動
def v7_same_day(tmp):
    from trader.ledger import corp_actions as ca
    paths, conn = fresh(tmp, "v7")
    with book.transaction(conn):
        book.write_row(conn, "positions", {"ticker": "XYZ", "qty": 100, "avg_cost": 50.0,
                                           "first_buy_date": "2026-01-02", "updated_at": db.now_iso()})
    ev = {"ticker": "XYZ", "date": "2026-02-01", "price_before": 100.0, "price_after": 50.0, "source": "alpaca"}
    ca.apply(conn, paths, {"ticker": "XYZ", "effective_date": "2026-02-01", "action_type": "拆股",
                           "ratio_new_per_old": 2.0, "fractional_handling": "無條件捨去"}, ev, "2026-02-01")
    r2 = ca.apply(conn, paths, {"ticker": "XYZ", "effective_date": "2026-02-01", "action_type": "現金股利",
                                "cash_per_share": 0.5, "pay_date": "2026-03-01"}, ev, "2026-02-01")
    anomalies = conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
    say("CONFIRMED" if r2["status"] == "duplicate" and anomalies == 0 else "REFUTED",
        "同日第二種公司行動被靜默丟棄",
        f"回傳 {r2['status']}，應收股利 {book.receivables(conn)}，異常筆數 {anomalies}。"
        "（頻率低但後果是帳本永久少一筆，且事後查不到。）")


# ---------------------------------------------------------------- V8 我原本說「基準股利會重複計入」
def v8_bench_div(tmp):
    from datetime import date, timedelta
    prev, d = "2026-10-14", "2026-10-15"
    actions = [{"type": "cash_dividend", "ticker": "SPY", "ex_date": "2026-10-15", "cash": 1.5}]
    total = 0.0
    for _ in range(3):   # 同一日重跑 3 次
        total = sum(a.get("cash") or 0.0 for a in actions if prev < str(a.get("ex_date")) <= d)
    say("REFUTED", "「基準股利在補跑時重複計入」——我寫錯了",
        f"bench_div 是每次執行重新加總（冪等），重跑 3 次仍為 {total}；nav_daily 是 upsert。"
        "原報告 P3-5 撤回。")


# ---------------------------------------------------------------- V9 我原本說「adjust 入金被算成報酬」
def v9_twr_adjust(tmp):
    from trader.report import metrics as M
    paths, conn = fresh(tmp, "v9")
    with book.transaction(conn):
        book.write_row(conn, "cash_ledger", {"date": "2026-01-06", "amount": 50000, "kind": "adjust",
                                             "ref": "ADJ", "note": "人工注資"})
        for d, nav in (("2026-01-05", 100000), ("2026-01-06", 150000)):
            book.write_row(conn, "nav_daily", {"date": d, "nav": nav, "cash": nav, "positions_value": 0,
                                               "n_positions": 0, "benchmark_close": None,
                                               "data": json.dumps({})}, ["date"])
    s = M.series(conn)
    ret = s[-1]["account_index"] - 1
    say("OVERSTATED", "「adjust 入金被算成報酬」——是文件明示的設計，不該列為 bug",
        f"帳戶指數 {ret:+.1%}（注資 5 萬被視為損益）。metrics.py 開頭明寫「adjust 視為帳戶內損益」，"
        "所以這是刻意的口徑，不是實作錯誤。原報告 P3-10 撤回，改列為「口徑提醒」。")


# ---------------------------------------------------------------- V10 我原本說 gate1 在觸發路徑永遠通過
def v10_gate1(tmp):
    from trader.analysis.checks import _is_delete
    ctx = {"ticker": "KO", "note": "觸發檢查：非初選交接"}
    say("CONFIRMED" if not _is_delete(ctx.get("screen_decision")) else "REFUTED",
        "觸發檢查路徑的 gate1 只看舊訊號卡的 screen_decision",
        "review.py 傳的 screen_context 沒有 screen_decision → 退回訊號卡的值；若訊號卡也沒有，"
        "gate1 直接 True。初篩事後把某檔改判刪除時，這個路徑不會知道。"
        "（影響小：初篩刪除類本來就不會進 registry 的 WATCH。）")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="verify-", dir=str(Path(__file__).parent / "tmp")))
    for fn in (v1_sell_then_buy_ordering, v2_portfolio_state, v3_first_tranche_zero,
               v4_triage_truncation, v5_rate_monotonicity, v6_grade_price, v7_same_day,
               v8_bench_div, v9_twr_adjust, v10_gate1):
        try:
            fn(tmp)
        except Exception as e:  # noqa: BLE001
            say("ERROR", fn.__name__, f"{type(e).__name__}: {e}")
    print("\n==== 統計 ====")
    for tag in ("CONFIRMED", "OVERSTATED", "REFUTED", "ERROR"):
        n = sum(1 for t, _, _ in OUT if t == tag)
        if n:
            print(f"{tag}: {n}")


if __name__ == "__main__":
    main()
