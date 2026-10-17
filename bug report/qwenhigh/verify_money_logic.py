#!/usr/bin/env python3
"""對抗式審查的佐證腳本（唯讀：只匯入 skill 純函式，不讀寫 state、不呼叫模型、不連網）。

用法：python3 verify_money_logic.py

印出五組可重現結果，對應報告編號：
  F1  cap_calc 未實作「歷史成長≤0 → 上限＝IV0×等級上限」（rulebook:271/276、procedure/05:38、VAL-050:250）
  F2  auto_buy_cap 不含 IV_cons 與等級係數 → 買價可高於保守價值，A/B 級上限相同
  F3  保守情境 g0 在負成長時 ×0.75 反而較不保守（規則本文如此，程式照做）
  F5  荒謬級線＝50×E 未檢查 E>0（review_jobs.py:253-254）
  F10 review_due 用未調整(raw)日價 vs 已調整的登錄表門檻 → 拆股日假觸發 THESIS
"""
import sys

ROOT = "/Users/zongen/Downloads/claude code/berkshire"
sys.path.insert(0, f"{ROOT}/berkshire-deep-merged/scripts")

import cap_calc  # noqa: E402

R_D = 0.10          # long_rate 0.045 → r_d = max(10%, 4.5%+3%) = 10%
GRADE_K = {"A": 0.85, "B": 0.75, "C": 0.40}


def rule_cap(iv0, grade):
    """規則書要求：歷史成長為負或近零 → 上限只能用 IV0 × 等級上限。"""
    return iv0 * GRADE_K[grade]


def show(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------- F1 / F2
show("F1｜歷史成長≤0：規則上限 vs cap_calc 实际上限（OE=10/股, r_d=10%）")
print(f"{'情境':<14}{'IV0':>9}{'規則上限(B)':>14}{'cap=auto_cap':>20}{'IV_cons':>10}{'超規則幅度':>12}")
for tag, hist in [("10年-2%/5年+1%", {"g10": -0.02, "g5": 0.01}),
                  ("零成長",          {"g10": 0.0, "g5": 0.0}),
                  ("兩期皆負",        {"g10": -0.02, "g5": -0.03}),
                  ("低成長",          {"g10": 0.03, "g5": 0.04})]:
    x = dict(oe=10.0, price=90.0, grade="B", long_rate=0.045, long_rate_20y_med=0.045,
             largecap_pe_median=20.0, norm_eps=10.0, **hist)
    r = cap_calc.calc(x)
    iv0, b = r["IV0"], r["auto_buy_cap"]
    rc = rule_cap(iv0, "B")
    print(f"{tag:<14}{iv0:>9.2f}{rc:>14.2f}   {r['cap']:>10.2f}{b:>10.2f}{r['IV_cons']:>10.2f}"
          f"{(b / rc - 1) * 100:>10.0f}%")
print("\n→ cap_calc 沒有任何一條路徑把上限改成 IV0×等級係數；min(hist)≤0 時只 append 一句 note（cap_calc.py:133-134）。")

show("F2｜A 級與 B 級的買進上限相同；IV_cons 不進硬上限")
x = dict(oe=10.0, price=90.0, grade="A", long_rate=0.045, long_rate_20y_med=0.045,
         largecap_pe_median=20.0, norm_eps=10.0, g10=0.06, g5=0.07)
a = cap_calc.calc(x)
x["grade"] = "B"
b = cap_calc.calc(x)
print(f"A 級：auto_buy_cap={a['auto_buy_cap']:.2f}  P1(0.85×IV_cons)={a['components']['P1_report_only']:.2f}"
      f"  IV_cons={a['IV_cons']:.2f}")
print(f"B 級：auto_buy_cap={b['auto_buy_cap']:.2f}  P1(0.75×IV_cons)={b['components']['P1_report_only']:.2f}"
      f"  IV_cons={b['IV_cons']:.2f}")
print(f"同一 auto_buy_cap 相對保守價值：A {a['auto_buy_cap']/a['IV_cons']:.2f}×  B {b['auto_buy_cap']/b['IV_cons']:.2f}×")
print("→ 等級只影響部位上限與 G8 折現，不影響可付價格；買價可高於保守情境價值而仍然『通過閘門 2』。")

# ---------------------------------------------------------------- F3
show("F3｜保守情境 g0：負成長 ×0.75 後變得比實際更樂觀")
for g10, g5 in ((-0.04, -0.01), (-0.10, 0.02)):
    x = dict(oe=10.0, price=90.0, grade="A", long_rate=0.045, long_rate_20y_med=0.045,
             g10=g10, g5=g5)
    r = cap_calc.calc(x)
    print(f"g10={g10}, g5={g5} → 較低者={min(g10,g5)}  保守 g0={r['g0']['cons']:+.4f}"
          f"（IV_cons={r['IV_cons']:.2f}）  → 保守 g0 高於較低的歷史成長 = 較不保守")

# ---------------------------------------------------------------- F5
show("F5｜荒謬級線＝50×(norm_eps or oe)，未檢查 E>0")
ABSURD_PE = 50
for e in (10.0, 0.05, 0.0, -2.0):
    line = round(ABSURD_PE * e, 4)
    price = 90.0
    print(f"E={e:>6} → 荒謬級線={line:>8}  現價 {price} ≥ 線？ {price >= line}  → PRICE 會提 SELL_HALF")
print("→ 公司正常化盈轉負（或極小）時，機械線變成 ≤0，任何正價格都『超過荒謬級線』；"
      "review_jobs.py 只算 50×E，沒有 E>0 或 IV 一致性檢查。")

# ---------------------------------------------------------------- F10
show("F10｜拆股日：raw 未調整價序列 vs 已調整的登錄表門檻")
try:
    import pandas as pd
    import review_due
except ImportError as ex:
    print(f"跳過（需要 pandas）：{ex}")
    sys.exit(0)

import datetime as dt

# 2:1 拆股，ex date = 2026-10-05。Alpaca raw 價：拆股前 100、拆股後 50。
d0 = dt.date(2026, 9, 1)
rows = []
for i in range(40):
    day = d0 + dt.timedelta(days=i)
    if day.weekday() >= 5:
        continue
    px = 100.0 if day < dt.date(2026, 10, 5) else 50.0
    rows.append({"ticker": "TST", "date": day, "close": px})
px = pd.DataFrame(rows)

entry = {  # 登錄表一列：每股欄位已由 corp_actions 依拆股調整（÷2）
    "ticker": "TST", "cik": "0000000001", "status": "HOLDING",
    "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
    "review_price": 50.0, "cost": 50.0, "auto_buy_cap": 60.0, "sell_review_above": 55.0,
    "absurd_price_line": 500.0, "weight": 0.08, "target_weight": 0.10,
}
asof = "2026-10-20"
act, why, tri = review_due.check(entry, [], px, asof, t30=None)
print(f"action={act}")
for w in why:
    print("  -", w)
print("→ 實際上什麼都沒發生（只 2:1 拆股），卻觸發『單日 ±4%』與『單季自高點跌 ≥25%』→ THESIS。")
print("→ trader/pipelines/review.py:132 用 adjustment=\"raw\" 產生 daily.csv，正是這個未調整序列。")
