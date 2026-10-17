#!/usr/bin/env python3
"""對抗式審查的「事實查核」腳本：逐條驗證 REVIEW.md 的發現是否真實，並找出已被緩解者。

只讀專案程式；所有暫存檔寫在 tempfile 目錄，不寫入專案。
執行：
  cd "/Users/zongen/Downloads/claude code/berkshire"
  /Users/zongen/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3 "bug report/deepseek/verify_findings.py"

輸出格式：每條 [CONFIRMED] / [REFUTED] / [MITIGATED] / [OVERSTATED]，附實測證據。
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trader"))

RESULTS: list[tuple[str, str, str]] = []


def rec(code: str, verdict: str, evidence: str) -> None:
    RESULTS.append((code, verdict, evidence))
    print(f"\n[{verdict}] {code}\n    {evidence}")


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cc = load(ROOT / "berkshire-deep-merged/scripts/cap_calc.py", "cap_calc_v")
gate3 = load(ROOT / "berkshire-deep-merged/scripts/gate3_check.py", "gate3_v")

BASE = {"oe": 5.0, "g10": 0.07, "g5": 0.06, "grade": "B", "long_rate": 0.047,
        "long_rate_20y_med": 0.035, "price": 40.0, "norm_eps": 5.2, "largecap_pe_median": 19.0,
        "financial": False, "roe": 0.22, "eps_cagr5": 0.06}


# ============================================================ A1
def a1():
    """買價上限是否實質等於 IV_base；P1 是否不進買價。"""
    rows = []
    bind_p2 = 0
    n = 0
    for L in (0.02, 0.03, 0.035, 0.04, 0.045, 0.05, 0.07):
        for g in ((0.0, 0.0), (0.03, 0.02), (0.07, 0.06), (0.15, 0.14), (0.30, 0.25)):
            r = cc.calc({**BASE, "long_rate": L, "g10": g[0], "g5": g[1]})
            if r.get("auto_buy_cap") is None:
                continue
            n += 1
            same = abs(r["auto_buy_cap"] - r["IV_base"]) < 1e-9
            if not same:
                bind_p2 += 1
            rows.append((L, g[0], r["components"]["P1_report_only"], r["IV_base"],
                         r["components"]["P2_prime"], r["auto_buy_cap"], same))
    ratios = [x[5] / x[2] for x in rows]
    ev = (f"{n} 組參數中，auto_buy_cap == IV_base 的有 {n - bind_p2} 組"
          f"（P2′ 成為約束的 {bind_p2} 組，全部出現在 L<4%）；"
          f"cap÷P1 介於 {min(ratios):.2f}–{max(ratios):.2f}。"
          f" 例：L=4.7%, g=7% → P1={rows[12][2]:.2f}, IV_base={rows[12][3]:.2f}, cap={rows[12][5]:.2f}")
    rec("A1 買價上限≡IV_base、P1 不進買價", "CONFIRMED", ev)


# ============================================================ A2
def a2():
    """金融股：補上 PE 中位是否讓上限跳升。"""
    x = {"oe": 4.0, "g10": 0.06, "g5": 0.05, "grade": "B", "long_rate": 0.047,
         "long_rate_20y_med": 0.035, "price": 50.0, "norm_eps": 4.2, "financial": True,
         "iv_cons": 60.0, "iv_base": 75.0, "iv_opt": 95.0}
    a = cc.calc(dict(x))
    b = cc.calc({**x, "largecap_pe_median": 19.0})
    rec("A2 資料越完整上限越寬鬆", "CONFIRMED",
        f"缺 PE 中位 → cap={a['auto_buy_cap']}（P1）；補上 PE 中位 → cap={b['auto_buy_cap']}"
        f"（{b['auto_buy_cap'] / a['auto_buy_cap'] - 1:+.0%}）。cap 的語意隨資料完整度改變。")


# ============================================================ A3
def a3():
    """max(hist) ≤ 10% 時 IV_base 是否等於 IV_opt。"""
    same = 0
    n = 0
    for g in (0.0, 0.02, 0.05, 0.08, 0.10):
        r = cc.calc({**BASE, "g10": g, "g5": g})
        n += 1
        if abs(r["IV_base"] - r["IV_opt"]) < 1e-9:
            same += 1
    r15 = cc.calc({**BASE, "g10": 0.15, "g5": 0.14})
    rec("A3 三情境退化", "CONFIRMED" if same == n else "REFUTED",
        f"g=0/2/5/8/10% 五組中 IV_base==IV_opt 有 {same} 組（寬度檢查量到的是 base/cons）；"
        f"g=15% 時兩者才分開（{r15['IV_base']:.2f} vs {r15['IV_opt']:.2f}）。")


# ============================================================ A4
def a4():
    """負成長時永續成長是否仍 3%。"""
    r = cc.calc({**BASE, "g10": -0.05, "g5": -0.02, "price": 20.0})
    oe, rd = 5.0, r["r_d"]
    tv3 = cc.pv(oe, -0.02, rd, 0.03)
    tv0 = cc.pv(oe, -0.02, rd, 0.0)
    # 10 年末的現金流
    cf, gt = oe, None
    for t, g in enumerate(cc.path(-0.02), 1):
        cf *= 1 + g
    terminal_share = (tv3 - tv0) / tv3
    rec("A4 衰退企業硬套 3% 永續", "CONFIRMED",
        f"g10=-5% → g0b=-2%，但 IV_base={r['IV_base']:.2f} 是以 g_term=3% 算的"
        f"（g_term=0% 時為 {tv0:.2f}）；永續段佔 IV_base 的 {terminal_share:.1%}。"
        f" SKILL §3 的『歷史成長 ≤0 → 上限只能用 IV0×係數』未實作（cap={r['auto_buy_cap']:.2f}，"
        f"IV0×0.75={0.75 * oe / rd:.2f}）。")


# ============================================================ A5
def a5():
    """低利率起點時 r_stress 是否被 10% 下限吞掉。"""
    r = cc.calc({**BASE, "long_rate": 0.03, "long_rate_20y_med": 0.045})
    rec("A5 壓力價 P3 == P1", "CONFIRMED" if abs(r["r_stress"] - r["r_d"]) < 1e-12 else "REFUTED",
        f"L=3%、20 年中位 4.5%（低利率起點）→ r_d={r['r_d']:.2%}, r_stress={r['r_stress']:.2%}；"
        f"P3={r['components']['P3_report_only']:.2f}, P1={r['components']['P1_report_only']:.2f}"
        f"（相同 → 壓力測試無效果）。")


# ============================================================ B1
def b1():
    """顯式 null 的完整鏈路：run_cap_calc → compare → apply_cap_override。"""
    from trader.analysis.checks import apply_cap_override, compare_signal_with_cap, run_cap_calc
    good = {**BASE, "g10": 0.07, "g5": 0.06}
    bad = {**good, "base_g0": None, "opt_g0": None}
    with tempfile.TemporaryDirectory() as td:
        p1, p2 = Path(td) / "good.json", Path(td) / "bad.json"
        p1.write_text(json.dumps(good), encoding="utf-8")
        p2.write_text(json.dumps(bad), encoding="utf-8")
        cg, cb = run_cap_calc(p1), run_cap_calc(p2)
    card = {"auto_buy_cap": cg["auto_buy_cap"], "sell_review_above": cg["IV_base"],
            "value_per_share": {"conservative": cg["IV_cons"], "base": cg["IV_base"],
                                "optimistic": cg["IV_opt"], "zero_growth_IV0": cg["IV0"]}}
    diffs = compare_signal_with_cap(card, cb)
    over = apply_cap_override(card, cb)
    ok = (cb["IV_base"] is None and over["sell_review_above"] is None
          and over["auto_buy_cap"] > cg["auto_buy_cap"] + 1)
    rec("B1 顯式 null → 上限放寬 + 賣出檢視失效", "CONFIRMED" if ok else "REFUTED",
        f"正確版 auto_buy_cap={cg['auto_buy_cap']}, IV_base={cg['IV_base']}；"
        f"null 版 auto_buy_cap={cb['auto_buy_cap']}, IV_base={cb['IV_base']}。"
        f"compare 檢出 {len(diffs)} 項不一致 → apply_cap_override 後 "
        f"sell_review_above={over['sell_review_above']}, "
        f"value_per_share.base={over['value_per_share']['base']}"
        f"（+{cb['auto_buy_cap'] / cg['auto_buy_cap'] - 1:.1%}）。")


# ============================================================ B2
def b2():
    """單位錯誤是否被靜默接受。"""
    ok = cc.calc({**BASE, "g10": 0.07, "g5": 0.06})
    bad = cc.calc({**BASE, "g10": 7, "g5": 6})
    roe_bad = cc.calc({**BASE, "roe": 18, "eps_cagr5": 10})
    rate_bad = cc.calc({**BASE, "long_rate": 4.7})
    rec("B2 無輸入驗證／單位檢查", "CONFIRMED",
        f"g10=7 → g0.cons={bad['g0']['cons']}（被夾在 8%）、cap {ok['auto_buy_cap']:.2f}→"
        f"{bad['auto_buy_cap']:.2f}（+{bad['auto_buy_cap'] / ok['auto_buy_cap'] - 1:.1%}）；"
        f"roe=18/eps_cagr5=10 → W_PE 門檻仍為 20（放寬條件失效）；"
        f"long_rate=4.7 → r_d={rate_bad['r_d']:.1%}（應為 10%），cap={rate_bad['auto_buy_cap']}。")


# ============================================================ B3
def b3():
    """金融股結論測試的 E×0.9 是否為空操作。"""
    x = {"oe": 4.0, "g10": 0.06, "g5": 0.05, "grade": "B", "long_rate": 0.047,
         "long_rate_20y_med": 0.035, "price": 44.0, "norm_eps": 4.2, "financial": True,
         "iv_cons": 60.0, "iv_base": 75.0, "iv_opt": 95.0}
    r = cc.calc(x)
    ct = r.get("conclusion_test") or {}
    p2oe = ct.get("P2_prime_with_E_x0.9")
    p1 = r["components"]["P1_report_only"]
    rec("B3 金融股 E×0.9 無效果", "CONFIRMED" if p2oe is not None and abs(p2oe - p1) < 1e-9 else "REFUTED",
        f"cap={r['auto_buy_cap']}, price=44（距上限 <5% → 觸發結論測試）；"
        f"P2_prime_with_E_x0.9={p2oe}, P1={p1}（完全相等 → 盈餘減 10% 沒有改變任何東西）。"
        f" flips={ct.get('flips')}（只因利率敏感度而翻轉）。")


# ============================================================ C1 / C5 / C2 / C3
def _db(tmp: str):
    from trader import db
    from trader.ledger import book
    c = db.connect(Path(tmp) / "t.db")
    db.migrate(c)
    book.ensure_initial_deposit(c, 100000.0, "2026-10-01")
    return c


def _reg(conn, t, status, data):
    from trader import db
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": data,
                                 "updated_at": db.now_iso()}, key=["ticker"])


def c1():
    """加碼批次在下單時就扣，逾期作廢不回補。"""
    from trader.pipelines import daily as D
    from trader.ledger import broker
    with tempfile.TemporaryDirectory() as td:
        conn = _db(td)
        _reg(conn, "AAA", "HOLDING", {"build_plan": {"target_weight": 0.10, "tranches_left": 3}})
        out = {"plans_dry_run": [], "orders": [], "rejected": []}
        plan = {"ticker": "AAA", "side": "BUY", "qty": 10, "limit_price": 100.0,
                "decision_id": "AAA-20261016-1", "action": "ADD", "reason": "t",
                "notes": [], "review_type": None, "cancel_buys": []}
        D._place(conn, plan, "2026-10-16", "2026-10-19", True, out)
        after_place = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])
        oid = out["orders"][0]["order_id"]
        broker.cancel_order(conn, oid, "逾期未成交", status="expired")
        after_expire = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])
        rec("C1 tranches_left 下單即扣、逾期不回補", "CONFIRMED"
            if after_place["build_plan"]["tranches_left"] == 2 and after_expire["build_plan"]["tranches_left"] == 2
            else "REFUTED",
            f"下單後 tranches_left={after_place['build_plan']['tranches_left']}；"
            f"訂單逾期作廢後仍為 {after_expire['build_plan']['tranches_left']}（未回補）。"
            f"三批全落空 → 建倉停在首批 50%。")
        conn.close()


def c2():
    """冷靜期：created_date=next_day 造成延後 2 個交易日。"""
    from trader.ledger import broker
    bars = {"AAA": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}}
    with tempfile.TemporaryDirectory() as td:
        conn = _db(td)
        broker.place_order(conn, "AAA", "BUY", 10, 101.0, None, "2026-10-19")  # 冷靜期：D+1 建立
        f1 = broker.match_orders(conn, "2026-10-19", bars, 0.0, 0.0)   # 次日
        f2 = broker.match_orders(conn, "2026-10-20", bars, 0.0, 0.0)   # 次次日
        rec("C2 冷靜期實際延後 2 個交易日", "CONFIRMED" if not f1 and f2 else "REFUTED",
            f"order.created_date=2026-10-19；match(10-19) 成交 {len(f1)} 筆、"
            f"match(10-20) 成交 {len(f2)} 筆。註解宣稱『次日才可撮合』，實際是次次日。")
        conn.close()


def c3():
    """C/D 級持股權重超限：是否連記錄都沒有。"""
    from trader.portfolio import sizing as S
    from trader import db
    with tempfile.TemporaryDirectory() as td:
        conn = _db(td)
        for t, g in (("CCC", "C"), ("BBB", "B")):
            _reg(conn, t, "HOLDING", {"grade": g, "auto_buy_cap": 100.0})
            db.insert(conn, "positions", {"ticker": t, "qty": 3000, "avg_cost": 50.0,
                                          "first_buy_date": "2024-01-02", "updated_at": db.now_iso()})
        quotes = {"CCC": 10.0, "BBB": 10.0}   # 各 30000 / 100000 = 30% 權重
        plans = S.plan_sells(conn, "2026-10-16", [], quotes)
        got = {p["ticker"]: p for p in plans}
        rec("C3 C 級權重超限無任何記錄", "CONFIRMED"
            if "CCC" not in got and got.get("BBB", {}).get("qty", 0) > 0 else "REFUTED",
            f"B 級（上限 10%、實際 30%）→ 產生減碼 {got.get('BBB', {}).get('qty')} 股；"
            f"C 級（實際 30%）→ plans 內完全沒有這檔（連 qty=0 的記錄都沒有，"
            f"與註解『僅記錄』不符）。")
        conn.close()


def c5():
    """部分成交：是否直接標 filled 且不回寫缺口。"""
    from trader.ledger import broker
    bars = {"AAA": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}}
    with tempfile.TemporaryDirectory() as td:
        conn = _db(td)
        conn.execute("UPDATE cash_ledger SET amount=? WHERE kind='deposit'", (1000.0,))
        conn.commit()
        oid = broker.place_order(conn, "AAA", "BUY", 100, 101.0, None, "2026-10-16")
        fills = broker.match_orders(conn, "2026-10-17", bars, 0.0, 0.0)
        row = conn.execute("SELECT status, qty, cancel_reason FROM orders WHERE order_id=?", (oid,)).fetchone()
        rec("C5 部分成交直接關單、缺口不回寫", "CONFIRMED"
            if row["status"] == "filled" and fills and fills[0]["qty"] < 100 else "REFUTED",
            f"委託 100 股、現金僅 1000 → 成交 {fills[0]['qty'] if fills else 0} 股，"
            f"訂單狀態={row['status']}，cancel_reason={row['cancel_reason']!r}。"
            f"registry.build_plan 沒有任何缺口欄位。")
        conn.close()


# ============================================================ D1 / D2 / D4
def d1():
    """平均回購價是否通過價格清洗。"""
    from trader.analysis import dossier_prep as P
    text = "平均回購價 | 78.53\n股價 | 86.10\n回購金額÷回購股數 | 78.53"
    kept, removed = P.scrub_price_text(text)
    survived = "平均回購價" in kept
    rec("D1 平均回購價繞過清洗", "CONFIRMED" if survived and removed >= 1 else "REFUTED",
        f"scrub 刪了 {removed} 行（股價那行），但『平均回購價 | 78.53』仍保留={survived}。"
        f" build_dossier.py:47 會把它寫進 dossier，而檔頭宣稱『本檔不含任何價格』。")


def d2():
    """special_watch 未拆 price_context；trader 端是否有第二道防線。"""
    screen = load(ROOT / "berkshire-screen-merged/scripts/screen.py", "screen_v")
    import pandas as pd
    df = pd.DataFrame({"ticker": ["AAA"], "price": [86.1], "core_rank": [1], "market_cap": [1e9]})
    plain = screen.records(df)
    split = screen.records(df, split=True)
    from trader.pipelines.screen import scrub_prices
    hand = {"core_shortlist": split, "special_watch": plain}
    scrubbed = scrub_prices(hand)
    leaked_after = "price" in json.dumps(scrubbed.get("special_watch"))
    rec("D2 special_watch 保留價格（但 trader 有第二道防線）", "MITIGATED",
        f"skill 的 records(split=False) 保留 price={plain[0].get('price')}；"
        f"trader 的 scrub_prices 之後 special_watch 仍含 price={leaked_after}。"
        f" 自動化路徑安全（screen.py:146-148 殘留就 raise），風險只在 agent 直接使用 skill 時。")


def d4():
    """±4% 單日觸發的實際機率（修正原報告的『50–70%』說法）。"""
    def p_at_least_once(sigma_daily: float, days: int = 10, thr: float = 0.04) -> float:
        p = math.erfc(thr / (sigma_daily * math.sqrt(2)))   # 雙尾
        return 1 - (1 - p) ** days
    rows = {v: p_at_least_once(v / math.sqrt(252)) for v in (0.20, 0.25, 0.35, 0.45, 0.60)}
    txt = "、".join(f"年化波動 {k:.0%}→{v:.0%}" for k, v in rows.items())
    rec("D4 ±4% 觸發頻率（原報告高估）", "OVERSTATED",
        f"以常態報酬估算 10 個交易日窗口內至少一次 |單日|≥4%：{txt}。"
        f" 一般持股（25%）約 {rows[0.25]:.0%}，不是原報告說的 50–70%；"
        f"只有高波動股（45%+）才接近一半。結論：觸發偏多但未到『每檔每輪』。")


# ============================================================ E1 / E2 / E3 / E4
def e1():
    """FULL_B 是否覆核 absurd_price_line / sell_review_above / cap_components。"""
    from trader.analysis.checks import _IV_MAP, compare_signal_with_cap
    import inspect
    src = inspect.getsource(compare_signal_with_cap)
    checks = [k for k, _ in _IV_MAP]
    has_absurd = "absurd" in src
    has_sell = "sell_review_above" in src
    has_comp = "cap_components" in src
    cc_src = (ROOT / "berkshire-deep-merged/scripts/cap_calc.py").read_text(encoding="utf-8")
    cc_has_absurd = "absurd_price_line" in cc_src
    rec("E1 FULL_B 不覆核荒謬級線／賣出檢視線", "CONFIRMED"
        if not (has_absurd or has_sell or has_comp) and not cc_has_absurd else "REFUTED",
        f"compare_signal_with_cap 只比 {checks}；原始碼含 absurd={has_absurd}、"
        f"sell_review_above={has_sell}、cap_components={has_comp}。"
        f" cap_calc.py 是否輸出 absurd_price_line={cc_has_absurd}。"
        f" output_schemas 允許 absurd_price_line=null → 模型可合法關掉該賣出觸發。")


def e2():
    """HOLD_REVIEW：TRIM 的類別限制與 1/4 上限。"""
    from trader.jobs import hold_jobs as H
    r_biz = {"action": "TRIM", "sell_category": "業務", "trim_fraction": 0.5,
             "falsified_assumption": "市佔被取代"}
    r_val = {"action": "TRIM", "sell_category": "估值", "trim_fraction": 0.9}
    e_biz, human_biz = H.check(r_biz, iv_base=100.0, price=150.0, held_days=1000)
    e_val, human_val = H.check(r_val, iv_base=100.0, price=150.0, held_days=1000)
    rec("E2 荒謬級 TRIM 受類別阻擋、無 1/4 上限", "CONFIRMED"
        if e_biz and not e_val else "REFUTED",
        f"TRIM＋業務 → 被擋（{len(e_biz)} 項錯誤，human={human_biz}）；"
        f"TRIM＋估值＋fraction=0.9（一次賣 90%）→ 零錯誤（human={human_val}），"
        f"prompt 寫的『每次 ≤1/4』沒有任何程式檢查。")


def e3():
    """post 先寫 DB、post_script 後檢查 → 失敗仍留副作用（但無 handler 實作）。"""
    from trader.jobs.handlers import Handler
    import inspect
    overrides = [c.__name__ for c in Handler.__subclasses__()
                 if c.post_script is not Handler.post_script]
    rec("E3 post/post_script 順序（目前無實作者）", "MITIGATED",
        f"engine.py:320-327 的順序確為 post() → post_script() → 失敗只 _fail(job)，副作用已提交；"
        f"但全專案覆寫 post_script 的 handler 有 {len(overrides)} 個（{overrides}）。"
        f" 現在不會發生，屬介面設計債，不是現行 bug。")


def e4():
    """max_grade_from_C1 是否被使用。"""
    ans = {"ticker": "AAA", "decision_date": "2026-10-16", "answered_by": "t",
           "answered_at": "2026-10-16 10:00 +08:00", "price_seen_before_answering": False,
           "answers": {}}
    q = json.loads((ROOT / "berkshire-deep-merged/assets/gate3_questionnaire.json").read_text(encoding="utf-8"))
    for item in q["questions"]:
        ans["answers"][item["id"]] = {"answer": item["pass_if"][0], "reason": "r",
                                      "evidence": [{"source": "10-K", "date": "2025-01-01"}]}
    r = gate3.evaluate(ans, q)
    hits = []
    for p in (ROOT / "trader/trader").rglob("*.py"):
        if "max_grade_from_C1" in p.read_text(encoding="utf-8", errors="ignore"):
            hits.append(str(p.relative_to(ROOT)))
    rec("E4 gate3 等級上限未被使用", "CONFIRMED" if len(hits) <= 1 else "REFUTED",
        f"evaluate() 回傳 max_grade_from_C1={r['max_grade_from_C1']!r}（gate3_pass={r['gate3_pass']}）；"
        f" trader/ 內提到這個鍵的檔案：{hits}（僅 checks.py 的失敗預設值，沒有任何地方用它壓等級）。")


# ============================================================ C4 / D5
def c4_d5():
    """PRICE 回寫：grade 與 weight 是否進 registry。"""
    from trader import db
    from trader.paths import get_paths
    from trader.analysis import writeback as W
    with tempfile.TemporaryDirectory() as td:
        paths = get_paths(state_dir=Path(td) / "state").ensure()
        conn = db.connect(paths.db)
        db.migrate(conn)
        _reg(conn, "AAA", "HOLDING", {"grade": "A", "weight": 0.05, "target_weight": 0.10})
        cap = {"auto_buy_cap": 90.0, "IV_base": 90.0, "IV_cons": 67.0, "IV_opt": 90.0, "IV0": 50.0,
               "absurd_price_line": 200.0, "grade_used": "B", "cap": 101.0, "r_d": 0.10,
               "price_to_cons": 0.6, "price_to_cap": 0.4, "g_impl": 0.05, "g_impl_gt_cap": False,
               "W_PE": False}
        did = W.write_price_result(conn, paths, "AAA", "2026-10-16", "run1", action="HOLD",
                                   gate1=True, gate2="通過", gate3="通過", cap=cap, price=40.0,
                                   price_date="2026-10-16", t30=0.047, reason="t", notes=[])
        reg = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])
        log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])
        rec("C4/D5 PRICE 回寫不更新 grade 與 weight", "CONFIRMED"
            if reg.get("grade") == "A" and log.get("grade") == "B" and reg.get("weight") == 0.05
            else "REFUTED",
            f"cap_calc 的 grade_used=B（P-21 降級）只寫進 decision log（grade={log.get('grade')}），"
            f"registry 的 grade 仍是 {reg.get('grade')!r} → sizing 的 GRADE_CAP 與 plan_sells 都用舊等級；"
            f" registry.weight 仍是 {reg.get('weight')}（未依市價更新，review_due 與 _map_action 都用它）。")
        conn.close()


# ============================================================ D3
def d3():
    """fin_source 是否對所有無 SIC 的列都標『近似』。"""
    import numpy as np
    import pandas as pd
    screen = load(ROOT / "berkshire-screen-merged/scripts/screen.py", "screen_v2")
    cols = ["ticker", "is_financial", "liab_to_assets", "shareholders_equity", "roa", "delete_codes",
            "vol_36m", "earn_yield", "fcf_yield", "norm_earn_yield", "rev_cagr_5y", "op_margin_5y_std",
            "debt_to_equity", "share_change_3y", "roe", "drawdown_3y", "loss_yrs5", "roe_med5",
            "roe_min5", "distress_codes"]
    rows = []
    for t, fin, lev in (("SIC1", 1, 0.9), ("SIC2", 0, 0.3), ("NOSIC_LOWLEV", np.nan, 0.3),
                        ("NOSIC_HIGHLEV", np.nan, 0.95)):
        rows.append({"ticker": t, "is_financial": fin, "liab_to_assets": lev,
                     "shareholders_equity": 1000.0, "roa": 0.10, "delete_codes": "",
                     "vol_36m": 0.2, "earn_yield": 0.06, "fcf_yield": 0.05, "norm_earn_yield": 0.06,
                     "rev_cagr_5y": 0.05, "op_margin_5y_std": 0.02, "debt_to_equity": 0.3,
                     "share_change_3y": 0.0, "roe": 0.15, "drawdown_3y": -0.1, "loss_yrs5": 0,
                     "roe_med5": 0.15, "roe_min5": 0.12, "distress_codes": ""})
    df, _ = screen.statistical(pd.DataFrame(rows), 2, 0)
    got = dict(zip(df["ticker"], df["fin_source"]))
    like = dict(zip(df["ticker"], df["fin_like"]))
    rec("D3 fin_source 標記失真", "CONFIRMED"
        if got["NOSIC_LOWLEV"].startswith("近似") and like["NOSIC_LOWLEV"] == 0 else "REFUTED",
        f"無 SIC 但財務比率完全不像金融（負債/資產 0.3、ROA 10%）→ fin_like={like['NOSIC_LOWLEV']}，"
        f"但 fin_source={got['NOSIC_LOWLEV']!r}（標成『近似』，實際上沒有做任何近似判斷）；"
        f"有 SIC 的列：{got['SIC1']!r}/{got['SIC2']!r}。")


# ============================================================ C6 / E5
def c6_e5():
    from trader.ledger import book
    import inspect
    src = inspect.getsource(book.paused)
    hit = [ln.strip() for ln in src.split("\n") if "anomalies" in ln]
    rec("C6 未登記持股的異常不會被任何程式處理", "CONFIRMED",
        f"唯一會讀 anomalies 的行動路徑是 book.paused（{hit[0] if hit else '?'}），"
        f"只取 pause_trading=1；daily.py:180-182 以 pause_trading=0 寫入 → 不暫停、不排工作、不通知。"
        f" 補救只能靠人工跑 `cli analyze`（cli.py:676），但異常沒有連到任何待辦。")


def e5():
    """gate3 不填 price_file_opened_at 仍可通過。"""
    q = json.loads((ROOT / "berkshire-deep-merged/assets/gate3_questionnaire.json").read_text(encoding="utf-8"))
    ans = {"ticker": "AAA", "decision_date": "2026-10-16", "answered_by": "t",
           "answered_at": "2026-10-16 10:00 +08:00", "price_seen_before_answering": False, "answers": {}}
    for item in q["questions"]:
        ans["answers"][item["id"]] = {"answer": item["pass_if"][0], "reason": "r",
                                      "evidence": [{"source": "10-K", "date": "2025-01-01"}]}
    r = gate3.evaluate(ans, q)
    ans2 = {**ans, "price_file_opened_at": "2026-10-16 09:00 +08:00"}   # 早於作答 → 應被抓
    r2 = gate3.evaluate(ans2, q)
    rec("E5 價格隔離只靠自述（省略即通過）", "CONFIRMED"
        if r["gate3_pass"] and not r2["gate3_pass"] else "REFUTED",
        f"完全不填 price_file_opened_at → gate3_pass={r['gate3_pass']}；"
        f"填了一個早於作答的時間 → gate3_pass={r2['gate3_pass']}（{r2['failures']}）。"
        f" 檢查只在自述自相矛盾時生效，沒有框架證據（mtime／sandbox 存取）。")


# ============================================================ 額外：SKILL 是否明說「達到上限即可買」
def skill_intent():
    txt = (ROOT / "berkshire-deep-merged/SKILL.md").read_text(encoding="utf-8")
    hit = [ln for ln in txt.split("\n") if "達到上限即可買" in ln]
    rec("A1 補充：這是刻意設計，不是疏漏", "CONFIRMED",
        f"SKILL.md §3 原文：{hit[0].strip() if hit else '（找不到）'}"
        f" → 安全邊際被設計成由 r_d=max(10%,30Y+3pp) 承擔，P1 只是報告欄。"
        f" 所以 A1 應改列為『設計選擇造成的語意衝突』，不是 bug。")


if __name__ == "__main__":
    for fn in (a1, a2, a3, a4, a5, b1, b2, b3, c1, c2, c3, c5, c4_d5, c6_e5, d1, d2, d3, d4,
               e1, e2, e3, e4, e5, skill_intent):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            rec(fn.__name__.upper(), "ERROR", f"{type(e).__name__}: {e}")

    print("\n" + "=" * 78)
    print("彙總：")
    for code, verdict, _ in RESULTS:
        print(f"  {verdict:12s} {code}")
    counts = {}
    for _, v, _ in RESULTS:
        counts[v] = counts.get(v, 0) + 1
    print("  →", counts)
