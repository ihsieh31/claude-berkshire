#!/usr/bin/env python3
"""對抗式審查用探針：只讀 cap_calc.py，不改專案。輸出各項可疑行為的實測數字。

執行：
  python3 "bug report/deepseek/probe_cap_calc.py"
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAP = ROOT / "berkshire-deep-merged" / "scripts" / "cap_calc.py"

spec = importlib.util.spec_from_file_location("cap_calc", CAP)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


def show(title, x):
    r = cc.calc(dict(x))
    print(f"\n=== {title} ===")
    print("  input :", json.dumps(x, ensure_ascii=False))
    for k in ("r_d", "g0", "IV_cons", "IV_base", "IV_opt", "width_opt_over_cons", "grade_used",
              "components", "cap", "auto_buy_cap", "price_to_cons", "g_impl", "conclusion_test",
              "conclusion_hint", "g_impl_gt_cap", "W_PE", "norm_pe"):
        v = r.get(k)
        if isinstance(v, float):
            v = round(v, 4)
        print(f"  {k:22s}= {v}")
    for n in r.get("notes") or []:
        print("   note:", n)
    return r


base = {"oe": 5.0, "g10": 0.07, "g5": 0.06, "grade": "B", "long_rate": 0.047,
        "long_rate_20y_med": 0.035, "price": 40.0, "norm_eps": 5.2, "largecap_pe_median": 19.0,
        "financial": False, "roe": 0.22, "eps_cagr5": 0.06}

# 1. P2′ 與 IV_base 的關係：高成長股（g0b 被 10% 上限夾住）
r1 = show("1) g10=g5=15%（g0b 被夾在 10%）", {**base, "g10": 0.15, "g5": 0.14})
print("  → P2' =", round(r1["components"]["P2_prime"], 4), " IV_base =", round(r1["IV_base"], 4),
      " auto_buy_cap =", round(r1["auto_buy_cap"], 4),
      " 相等?", abs(r1["components"]["P2_prime"] - r1["IV_base"]) < 1e-9)

# 2. 低成長股：上限是否等於 IV_base
r2 = show("2) g10=g5=3%", {**base, "g10": 0.03, "g5": 0.03})
print("  → auto_buy_cap == IV_base ?", abs(r2["auto_buy_cap"] - r2["IV_base"]) < 1e-9)

# 3. P1（等級安全邊際價）與 auto_buy_cap 的差距
for g, label in (((0.07, 0.06), "7%/6%"), ((0.15, 0.14), "15%/14%"), ((0.02, 0.01), "2%/1%")):
    rr = cc.calc({**base, "g10": g[0], "g5": g[1]})
    p1 = rr["components"]["P1_report_only"]
    print(f"  成長 {label:8s}: P1={p1:8.3f}  IV_cons={rr['IV_cons']:8.3f}  "
          f"IV_base={rr['IV_base']:8.3f}  auto_buy_cap={rr['auto_buy_cap']:8.3f}  "
          f"上限/P1={rr['auto_buy_cap']/p1:.2f}x")

# 4. iv_cons 覆寫時，結論測試的 E×0.9 是否為空操作
x4 = {**base, "iv_cons": 55.0, "iv_base": 70.0, "iv_opt": 90.0, "price": 69.0}
r4 = show("4) 給定 iv_cons（銀行／覆寫路徑）＋現價貼近上限", x4)
ct = r4.get("conclusion_test")
if ct:
    print("  → P2_prime_with_E_x0.9 =", round(ct["P2_prime_with_E_x0.9"], 4),
          " vs P1 =", round(r4["components"]["P1_report_only"], 4),
          "（相同 → E×0.9 無效果）")

# 5. 明確傳 null 的 base_g0/opt_g0 是否讓 P-21 區間檢查消失
x5a = {**base, "g10": 0.07, "g5": 0.06, "base_g0": None, "opt_g0": None}
r5a = show("5a) base_g0/opt_g0 顯式為 null", x5a)
x5b = {k: v for k, v in {**base, "g10": 0.07, "g5": 0.06}.items()}
r5b = show("5b) 同輸入但不含 base_g0/opt_g0 鍵", x5b)
print("  → 5a IV_opt =", r5a.get("IV_opt"), " width =", r5a.get("width_opt_over_cons"),
      " grade_used =", r5a.get("grade_used"))
print("  → 5b IV_opt =", round(r5b.get("IV_opt") or 0, 4), " width =", round(r5b.get("width_opt_over_cons") or 0, 4),
      " grade_used =", r5b.get("grade_used"))

# 6. 單位錯誤（成長率以百分數給）是否被靜默接受
r6 = show("6) g10=7, g5=6（百分數而非小數）", {**base, "g10": 7, "g5": 6})
print("  → g0.cons =", r6["g0"]["cons"], " IV_cons =", r6["IV_cons"], " auto_buy_cap =", r6["auto_buy_cap"])

# 7. 負成長 / 近零成長：cap 是否仍以 10% 成長定價
r7 = show("7) g10=-0.05, g5=-0.02（歷史衰退）", {**base, "g10": -0.05, "g5": -0.02, "price": 20.0})
print("  → P2' =", round(r7["components"]["P2_prime"], 4), " IV_base =", round(r7["IV_base"], 4),
      " auto_buy_cap =", round(r7["auto_buy_cap"], 4))

# 8. 完全沒有大型股 PE 中位（金融股）→ cap 退化成 P1
x8 = {"oe": 4.0, "g10": 0.06, "g5": 0.05, "grade": "B", "long_rate": 0.047,
      "long_rate_20y_med": 0.035, "price": 50.0, "norm_eps": 4.2, "financial": True,
      "iv_cons": 60.0, "iv_base": 75.0, "iv_opt": 95.0}
r8 = show("8) 金融股、缺 largecap_pe_median", x8)
print("  → cap =", r8["components"]["P2_prime"], " P4' =", r8["components"]["P4p"],
      " auto_buy_cap =", r8["auto_buy_cap"], "（cap 應為 P1）")

# 9. 敏感度：r_d 對 cap 的影響（10% 下限是否常態綁定）
for L in (0.02, 0.03, 0.04, 0.05, 0.07):
    rr = cc.calc({**base, "long_rate": L})
    print(f"  L={L:.0%}: r_d={rr['r_d']:.2%} r_stress={rr['r_stress']:.2%} "
          f"g_term_impl={rr['g_term_impl']:.2%} P2'={rr['components']['P2_prime']:.2f} "
          f"IV_base={rr['IV_base']:.2f} cap={rr['auto_buy_cap']:.2f}")

# 10. g_impl 求解邊界
r10 = show("10) 價格極高（g_impl 不可解）", {**base, "price": 500.0})
print("  → g_impl =", r10.get("g_impl"), " g_impl_gt_cap =", r10.get("g_impl_gt_cap"),
      " auto_buy_cap =", r10.get("auto_buy_cap"))
