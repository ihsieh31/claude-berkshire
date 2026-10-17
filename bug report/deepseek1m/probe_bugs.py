#!/usr/bin/env python3
"""對抗式審查用探針：只讀專案程式、只寫本目錄的暫存 state。

每個 probe 印出 [BUG]/[OK]，不修改 /Users/zongen/Downloads/claude code/berkshire 內任何檔案。
執行：python probe_bugs.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path("/Users/zongen/Downloads/claude code/berkshire")
sys.path.insert(0, str(ROOT / "trader"))

from trader import db                                    # noqa: E402
from trader.paths import get_paths                       # noqa: E402
from trader.ledger import book, broker, corp_actions     # noqa: E402
from trader.portfolio import sizing as Z                # noqa: E402
from trader.analysis import writeback                    # noqa: E402

RESULTS = []


def report(tag, title, detail):
    RESULTS.append((tag, title, detail))
    print(f"\n[{tag}] {title}\n    {detail}")


def fresh(tmp: Path):
    paths = get_paths(state_dir=tmp / "state", root=ROOT).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    return paths, conn


# ---------------------------------------------------------------- A：加碼批次在「下單」時就被扣掉
def probe_tranche_consumed_without_fill(tmp):
    from trader.pipelines import daily
    paths, conn = fresh(tmp / "A")
    book.ensure_initial_deposit(conn, 100000, "2026-01-02")
    with book.transaction(conn):
        book.write_row(conn, "positions", {"ticker": "KO", "qty": 100, "avg_cost": 50.0,
                                           "first_buy_date": "2026-01-02", "updated_at": db.now_iso()})
        book.write_row(conn, "registry", {"ticker": "KO", "status": "HOLDING", "first_buy_date": "2026-01-02",
                                          "data": json.dumps({"auto_buy_cap": 60.0,
                                                              "build_plan": {"target_weight": 0.10,
                                                                             "tranches_left": 3,
                                                                             "days_above_cap": 0,
                                                                             "started": "2026-01-02"}}),
                                          "updated_at": db.now_iso()})
        book.write_row(conn, "decisions", {"decision_id": "KO-20260102-1", "ticker": "KO",
                                           "decision_date": "2026-01-02", "run_id": "r", "job_id": None,
                                           "signal": "BUY", "reason_code": "PASS_ALL",
                                           "data": json.dumps({"action": "BUY", "review_type": "FULL",
                                                               "gate1_not_deleted": True,
                                                               "gate2_price_below_cap": True,
                                                               "gate3_pass": True}),
                                           "created_at": db.now_iso()})
    plans, cancels = Z.plan_tranches(conn, "2026-01-05", {"KO": 55.0}, {"KO": 1e9}, 10)
    p = [x for x in plans if x["qty"] > 0][0]
    # 下單（限價 55.05），但隔天開盤跳空到 58 → 高於限價 → 不成交
    oid = broker.place_order(conn, "KO", "BUY", p["qty"], p["limit_price"], p["decision_id"], "2026-01-05")
    out = {"orders": [], "plans_dry_run": [], "rejected": []}
    daily._consume(conn, "KO", "2026-01-05")     # _place 對 ADD 就是呼叫這個
    fills = broker.match_orders(conn, "2026-01-06", {"KO": {"open": 58.0, "high": 58.5, "low": 57.8,
                                                           "close": 58.0}}, 10, 0)
    reg = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])
    pos = book.positions(conn)["KO"]["qty"]
    tl = reg["build_plan"]["tranches_left"]
    if not fills and tl == 2 and pos == 100:
        report("BUG", "未成交的加碼批次仍被扣掉（批次在下單時扣、不是成交時扣）",
               f"下單 {p['qty']} 股 @ {p['limit_price']}，隔日開盤 58 高於限價 → 0 成交；"
               f"tranches_left 由 3 → {tl}，持股仍 {pos} 股。該批額度永久消失（21 日窗口內不會重試），"
               f"最終部位永遠達不到目標權重。")
    else:
        report("OK", "tranche accounting", f"fills={fills} tl={tl} pos={pos}")


# ---------------------------------------------------------------- B：同日兩種公司行動被當成重複
def probe_same_day_ca_collision(tmp):
    paths, conn = fresh(tmp / "B")
    with book.transaction(conn):
        book.write_row(conn, "positions", {"ticker": "XYZ", "qty": 100, "avg_cost": 50.0,
                                           "first_buy_date": "2026-01-02", "updated_at": db.now_iso()})
    split = {"ticker": "XYZ", "effective_date": "2026-02-01", "action_type": "拆股",
             "ratio_new_per_old": 2.0, "cash_per_share": None, "fractional_handling": "無條件捨去"}
    ev = {"ticker": "XYZ", "date": "2026-02-01", "price_before": 100.0, "price_after": 50.0, "source": "alpaca"}
    r1 = corp_actions.apply(conn, paths, split, ev, "2026-02-01")
    div = {"ticker": "XYZ", "effective_date": "2026-02-01", "action_type": "現金股利",
           "ratio_new_per_old": None, "cash_per_share": 0.5, "pay_date": "2026-03-01"}
    r2 = corp_actions.apply(conn, paths, div, {**ev, "alpaca": {"payable_date": "2026-03-01"}}, "2026-02-01")
    cash = book.receivables(conn)
    if r1["status"] == "applied" and r2["status"] == "duplicate" and cash == 0:
        report("BUG", "同一天有兩種公司行動時，第二種被靜默丟棄",
               "拆股與特別股利同日生效（實務常見）：_ca_exists 只看 (ticker, effective_date)，"
               f"股利回傳 {r2['status']}，應收股利 {cash}。沒有任何異常紀錄，股利永久漏記。")
    else:
        report("OK", "same-day CA", f"{r1} {r2} receivables={cash}")


# ---------------------------------------------------------------- C：registry 的 auto_buy_cap 不經 cap_calc 覆核
def probe_registry_cap_not_reconciled(tmp):
    paths, conn = fresh(tmp / "C")
    td = tmp / "C" / "work" / "KO"
    td.mkdir(parents=True)
    (td / "signal_card.json").write_text(json.dumps({
        "signal": "WATCH", "reason_code": "PASS_ALL", "auto_buy_cap": 60.0,
        "value_per_share": {"conservative": 55, "base": 60, "optimistic": 70, "zero_growth_IV0": 40},
    }), encoding="utf-8")
    # 模型自己在 registry_entry 寫了 3 倍的上限；framework 不做任何覆核
    (td / "registry_entry.json").write_text(json.dumps({
        "ticker": "KO", "status": "WATCH", "grade": "B", "auto_buy_cap": 180.0,
        "sell_review_above": 180.0, "absurd_price_line": 5000, "target_weight": 0.10,
        "next_full_review_due": "2027-10-15",
    }), encoding="utf-8")
    (td / "decision_log.json").write_text(json.dumps({"action": "WATCH"}), encoding="utf-8")
    (td / "thesis.md").write_text("# thesis\n", encoding="utf-8")
    did = writeback.write_full_result(conn, paths, "KO", "2026-10-15", "r", td, "WATCH", [])
    reg = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])
    if reg["auto_buy_cap"] == 180.0:
        report("BUG", "登錄表的 auto_buy_cap／sell_review_above 完全由模型填寫，未被 cap_calc 覆核",
               f"訊號卡寫 60（已與 cap_calc 對帳），registry_entry.json 寫 180 → 登錄表存 {reg['auto_buy_cap']}。"
               "plan_tranches／review_due.py 用的正是登錄表這個值，等於模型的一個數字錯誤可以讓加碼上限放大 3 倍，"
               "而『程式覆核數字』只涵蓋 signal_card.json。registry_entry.json／decision_log.json／thesis.md "
               "都沒有任何結構驗證。")
    else:
        report("OK", "registry cap", str(reg.get("auto_buy_cap")))


# ---------------------------------------------------------------- D：TWR 與 SPY 起算日不同 → 超額報酬失真
def probe_spy_index_misalignment(tmp):
    from trader.report import metrics as M
    paths, conn = fresh(tmp / "D")
    rows = [
        ("2026-01-05", 100000, 100000, 0, None),   # 基準當日缺收盤（Alpaca 漏一筆）
        ("2026-01-06", 110000, 100000, 0, 500.0),  # 帳戶 +10%，這天的 SPY 報酬永遠不會被計入
        ("2026-01-07", 112000, 100000, 0, 510.0),
    ]
    with book.transaction(conn):
        for d, nav, cash, pv, bench in rows:
            book.write_row(conn, "nav_daily", {"date": d, "nav": nav, "cash": cash, "positions_value": pv,
                                               "n_positions": 0, "benchmark_close": bench,
                                               "data": json.dumps({"benchmark_dividend": 0})}, ["date"])
        book.write_row(conn, "cash_ledger", {"date": "2026-01-05", "amount": 100000, "kind": "deposit",
                                             "ref": "INIT", "note": "x"})
    s = M.series(conn)
    sm = M.summary(conn)
    acct = s[-1]["account_index"] - 1
    spy = s[-1]["spy_index"] - 1
    if abs(acct - spy) > 0.015:
        report("BUG", "首日缺基準收盤時，帳戶與 SPY 用不同起算日算報酬",
               f"首日 SPY 收盤缺值 → spy_index 從第 2 天才 =1.0，第 1 天 +10% 的帳戶報酬沒有任何 SPY 對照。"
               f"帳戶 {acct:+.2%}（3 天）、SPY {spy:+.2%}（2 天），excess_pp={sm['excess_pp']}。"
               "summary 直接相減兩個不同期間的指數，超額報酬（小儀表板主數字）會失真；"
               "而且只在『首日剛好缺價』時發生，最難察覺。")
    else:
        report("OK", "spy alignment", f"{acct} {spy}")


# ---------------------------------------------------------------- D2：P2′ 幾乎從不生效
def probe_p2_inert(tmp):
    sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
    import cap_calc
    rows = []
    for g in [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12, 0.15]:
        r = cap_calc.calc({"oe": 5.0, "g10": g, "g5": g, "grade": "B", "long_rate": 0.042,
                           "long_rate_20y_med": 0.045, "price": 100.0, "norm_eps": 5.0,
                           "largecap_pe_median": 26.6})
        p2 = r["components"]["P2_prime"]
        rows.append((g, p2, r["IV_base"], r["auto_buy_cap"], abs(p2 - r["auto_buy_cap"]) < 1e-6))
    binding = [r for r in rows if r[4]]
    report("BUG" if len(binding) < len(rows) / 2 else "OK",
           "P2′（隱含成長 10% 上限）對多數公司不生效，真正的上限是 IV_base",
           "成長 0–15% 掃描：" + "；".join(f"g={g:.0%} P2′={p2:.1f} IV_base={iv:.1f}" for g, p2, iv, a, b in rows)
           + f"。P2′ 只在歷史成長 ≥10% 時才是上限（{len(binding)}/{len(rows)}）。"
             "SKILL §3 把上限寫成 min(P2′, IV_base)，實務上等於只用 IV_base（歷史成長折現），"
             "『隱含成長不得超過 10%』這道閘門對典型的 3–6% 成長股完全沒有作用。")


# ---------------------------------------------------------------- D3：例行董事異動就觸發 opus 完整重跑
def probe_review_due_officer_change(tmp):
    sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
    import review_due
    import pandas as pd
    e = {"ticker": "KO", "status": "HOLDING", "last_review_date": "2026-09-01",
         "last_full_review_date": "2026-09-01", "t30_at_valuation": 0.042, "cost": 60,
         "auto_buy_cap": 50, "sell_review_above": 80, "weight": 0.08, "target_weight": 0.10}
    filings = [{"form": "8-K", "filingDate": "2026-10-01", "items": "5.02",
                "url": "https://www.sec.gov/x"}]
    px = pd.DataFrame({"date": [date(2026, 9, 1), date(2026, 10, 15)], "close": [61.0, 62.0]})
    act, why, tri = review_due.check(e, filings, px, "2026-10-15", 0.042, None)
    report("BUG" if act == "FULL" else "OK",
           "MATERIAL_8K 把 8-K 5.02（董事／高階經理人異動）當成「重大」→ 直接排 opus 完整分析",
           f"一次例行的 CFO 退休公告 → action={act}（{why}）。大型股每年 5.02 申報很常見，"
           "每命中一次就是一次 opus FULL_A（實測 $1–2、需人工同意），且會把整條 DOSSIER→FULL_A→FULL_B 重跑。"
           "真正的重大性判斷（是否動搖論點）被跳過，交給最貴的模型從頭做一次。")


# ---------------------------------------------------------------- E：沙箱根目錄殘留 → 價格隔離可被繞過
def probe_sandbox_leak(tmp):
    from trader.jobs import isolation
    denies = isolation.deny_roots([], ROOT / "state")
    settings = json.loads(isolation.settings_json(denies))
    rules = settings["permissions"]["deny"]
    # 現在磁碟上真實存在的價格檔（同一台機器、先前 run 留下）
    victim = Path("/Users/Shared/berkshire-sandbox-zongen/analyze-2026-10-16/OTIS/_FULL_B_1/dossier/OTIS_price.json")
    leaked = [p for p in Path("/Users/Shared").glob("berkshire-sandbox-*/**/*_price.json")]
    covered = any(str(victim).startswith(r.split("(", 1)[1].rstrip("/**)")) for r in rules if "(" in r)
    total_mb = sum(f.stat().st_size for f in Path("/Users/Shared").glob("berkshire-sandbox-*/**/*") if f.is_file()) / 1e6
    if leaked and not covered:
        report("BUG", "價格隔離只擋『沙箱以外』，沒擋『同一個沙箱根目錄裡的舊沙箱』",
               f"deny 規則共 {len(rules)} 條，涵蓋家目錄、/private、/tmp、專案根、state；"
               f"但沙箱根 /Users/Shared/berkshire-sandbox-zongen 本身不在名單內。"
               f"目前磁碟上仍有 {len(leaked)} 個 *_price.json（例：{victim.name} 在 _FULL_B_1/），"
               f"任何 cwd 在同一根目錄下的 DOSSIER／FULL_A 只要 Read 相對路徑 ../../analyze-2026-10-16/OTIS/_FULL_B_1/dossier/OTIS_price.json 就拿到價格。"
               f"舊沙箱從不清除（{total_mb:.0f} MB 累積）。")
    else:
        report("OK", "sandbox leak", f"leaked={len(leaked)} covered={covered}")


# ---------------------------------------------------------------- F：gate3 的「沒看過價格」是模型自述
def probe_gate3_self_attested(tmp):
    sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
    import gate3_check
    q = json.loads((ROOT / "berkshire-deep-merged" / "assets" / "gate3_questionnaire.json").read_text(encoding="utf-8"))
    ans = {"decision_date": "2026-10-15", "answered_at": "2026-10-15 09:00 UTC",
           "price_seen_before_answering": False, "price_file_opened_at": "", "answers": {}}
    for item in q["questions"]:
        ans["answers"][item["id"]] = {"answer": item["pass_if"][0], "reason": "r",
                                      "evidence": [{"source": "10-K", "date": "2026-01-01"}]}
    r = gate3_check.evaluate(ans, q)
    # 真實開檔時間由框架寫在沙箱的 price_file_opened_at.txt，gate3_check 完全不讀它
    if r["gate3_pass"] and not ans["price_file_opened_at"]:
        report("BUG", "閘門 3 的價格隔離檢查是模型自述，不是框架事實",
               "gate3_check 只讀 answers.json 裡模型自己填的 price_seen_before_answering 與 price_file_opened_at；"
               "框架在 open_price_file() 寫下的真實開檔時間（沙箱內 price_file_opened_at.txt）從來沒有被比對過。"
               "模型只要填 false＋空字串就通過——而 FULL_A 本來就有 Read 權限，"
               "配合 E 的沙箱殘留可直接讀到價格。閘門 3 的 price isolation 條款形同虛設。")
    else:
        report("OK", "gate3", str(r["gate3_pass"]))


# ---------------------------------------------------------------- G：C 級 BUY 不會被框架擋下
def probe_grade_c_buy(tmp):
    from trader.analysis.checks import cap_signal_by_gates
    sig, why = cap_signal_by_gates("BUY", {"gate3_pass": True}, "PASS")
    if sig == "BUY":
        report("BUG", "C／D 級不得買，但框架層的閘門降級不看等級",
               "cap_signal_by_gates 只檢查 gate3 與初篩刪除；certainty_grade=C 的 BUY 仍會進 RED_TEAM、"
               "仍會寫進 decisions（signal=BUY）。目前只靠 sizing.target_weight 的 GRADE_CAP.get('C',0)=0 "
               "在最後一刻變成 0 股來擋——那是『剛好』而不是設計。"
               "另外 cap_calc 的等級係數只影響 P1（報告欄），A 級與 B 級的 auto_buy_cap 完全相同（見 cap_probe）。")
    else:
        report("OK", "grade gate", sig)


# ---------------------------------------------------------------- H：人工歸零持股不會取消未成交買單
def probe_adjust_leaves_open_buy(tmp):
    paths, conn = fresh(tmp / "H")
    book.ensure_initial_deposit(conn, 100000, "2026-01-02")
    with book.transaction(conn):
        book.write_row(conn, "positions", {"ticker": "QRVO", "qty": 100, "avg_cost": 80.0,
                                           "first_buy_date": "2026-01-02", "updated_at": db.now_iso()})
        book.write_row(conn, "registry", {"ticker": "QRVO", "status": "HOLDING", "first_buy_date": "2026-01-02",
                                          "data": "{}", "updated_at": db.now_iso()})
    oid = broker.place_order(conn, "QRVO", "BUY", 10, 70.0, "d1", "2026-01-05")
    book.adjust(conn, date="2026-01-06", ticker="QRVO", set_qty=0, note="併購下市歸零")
    left = book.open_orders(conn, "QRVO")
    fills = broker.match_orders(conn, "2026-01-07", {"QRVO": {"open": 65.0, "high": 66.0, "low": 64.0,
                                                             "close": 65.0}}, 10, 0)
    if left and fills:
        report("BUG", "人工把持股歸零（公司行動調帳）不會取消未成交買單，隔天又買回來",
               f"adjust(set_qty=0) 後仍有 open buy {oid}；次日撮合 {fills[0]['qty']} 股 @ {fills[0]['price']}，"
               f"持股變 {book.positions(conn).get('QRVO', {}).get('qty')} 股。"
               "這正是 QRVO 併購歸零的操作路徑：使用者以為已經清掉，程式卻用舊限價單重建部位。")
    else:
        report("OK", "adjust orders", f"{left} {fills}")


def main():
    base = Path(tempfile.mkdtemp(prefix="probe-", dir=str(Path(__file__).parent / "tmp")))
    for fn in (probe_tranche_consumed_without_fill, probe_same_day_ca_collision,
               probe_registry_cap_not_reconciled, probe_spy_index_misalignment,
               probe_sandbox_leak, probe_gate3_self_attested, probe_grade_c_buy,
               probe_adjust_leaves_open_buy, probe_p2_inert, probe_review_due_officer_change):
        sub = base / fn.__name__
        sub.mkdir(parents=True, exist_ok=True)
        try:
            fn(sub)
        except Exception as e:  # noqa: BLE001
            report("ERROR", fn.__name__, f"{type(e).__name__}: {e}")
    n = sum(1 for t, _, _ in RESULTS if t == "BUG")
    print(f"\n==== 合計 {n} 項 BUG／{len(RESULTS)} 項探針 ====")
    print("（暫存 state 在", base, "，可整個刪除）")


if __name__ == "__main__":
    main()
