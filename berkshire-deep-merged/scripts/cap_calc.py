#!/usr/bin/env python3
"""估值三情境與買價上限計算器（對應 規則書 第 6 節、P-14～P-22、VAL-046、VAL-050、VAL-051、VAL-081）。

  python cap_calc.py input.json [--out result.json]

輸入 JSON（每股數字；比率用小數）：
  oe                 正常化業主盈餘（每股，營業盈餘口徑，已扣維持性資本支出差額與股權薪酬）   必要
  g10, g5            10 年、5 年內生業主盈餘（或營業盈餘）年複合；缺填 null                    至少一個
  grade              確定性等級 A/B/C/D（P-21 區間 >3 會自動再降一級）                          必要
  long_rate          最近交易日 30 年期公債殖利率                                              必要
  long_rate_20y_med  過去 20 年 30 年期中位（判斷低利率起點；缺 → 視為非低利率並註記）
  price              現價                                                                        必要
  norm_eps           正常化營業 EPS（P4′ 與 W-PE 用；缺 → 用 oe）
  largecap_pe_median 大型股 P/E 中位（缺 → P4′ 不適用）
  fin_pe_median      金融同業 P/E 中位（金融類 P4′）
  financial          true/false（金融類不做 P2；可直接給 iv_cons/iv_base/iv_opt 或保險參數）
  insurance          {"tbv_ps":..,"float_ps":..,"float_cost":..}（VAL-081 可計算版）
  base_g0, opt_g0    基準／樂觀起始成長（缺：基準＝min(已證明成長,10%)；樂觀＝min(15%, 較高歷史成長)）
  moat_widening      true → 保守終值成長可 3%（護城河三測全強且變寬），預設 0%
  incr_roic_gt_rd    true/false（A 級 P4′ 1.2 倍條件之一）
  roe, eps_cagr5     W-PE 門檻放寬條件（ROE ≥18% 且 5 年 EPS 年複合 ≥10% → 25 倍）
  iv_cons, iv_base, iv_opt  直接覆寫（銀行等以 VAL-080 另算者）
路徑（推論，與 規則書 g_impl 同式）：第 t 年成長＝g＋(min(g,4%)−g)×(t−1)/9，t=1..10；保守終值 0%、基準與樂觀 3%。
輸出：三情境、IV0、r_d、r_stress、g_impl、P1～P4′、上限＝P2′（歷史成長 ≤0 → 不高於 IV0×等級係數；P4′ 軟上限、P1、P3 為報告欄；金融類無 P2′ → min(P1, P4′)）、
      現價比、結論測試、敏感度、W-PE、註記。數字欄位缺或為 null 一律視為「缺」（用預設）。
"""
import argparse
import json

GRADE_CAP = {"A": 0.85, "B": 0.75, "C": 0.40}
ORDER = ["A", "B", "C", "D"]


G_IMPL_MAX = 0.10  # 見 ../references/procedure/05_valuation_caps.md §4


def path(g, years=10):
    return [g + (min(g, 0.04) - g) * (t - 1) / 9 for t in range(1, years + 1)]


def pv(oe, g, r, g_term):
    cf, v = oe, 0.0
    for t, gt in enumerate(path(g), 1):
        cf *= 1 + gt
        v += cf / (1 + r) ** t
    if r <= g_term:
        return float("inf")
    return v + cf * (1 + g_term) / (r - g_term) / (1 + r) ** 10


def solve_g(oe, price, r, g_term=0.03):
    lo, hi = -0.5, min(0.5, r - 1e-4)
    if pv(oe, hi, r, g_term) < price:
        return None  # 價格高到起始成長逼近 r_d 也不足：隱含成長遠高於上限（輸出 g_impl_gt_cap=true）
    if pv(oe, lo, r, g_term) > price:
        return lo
    for _ in range(100):
        mid = (lo + hi) / 2
        if pv(oe, mid, r, g_term) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _val(x, key, default):
    """鍵缺或值為 null 都用預設（schema 慣例「數字或 null」：顯式 null 不得關掉預設值）。"""
    v = x.get(key)
    return default if v is None else v


def calc(x):
    x = dict(x)  # 不改呼叫端的 dict
    notes = []
    L = x["long_rate"]
    low = x.get("long_rate_20y_med") is not None and L < x["long_rate_20y_med"]
    if x.get("long_rate_20y_med") is None:
        notes.append("缺 20 年長債中位：視為非低利率起點")
    r_d = max(0.10, L + 0.03)
    r_stress = max(0.10, L + 0.06 + (0.01 if low else 0.0))
    r_sens = max(0.10, L + 0.07 + (0.01 if low else 0.0))
    oe, price = x["oe"], x["price"]
    eps = x.get("norm_eps") or oe
    hist = [g for g in (x.get("g10"), x.get("g5")) if g is not None]
    grade = x["grade"]
    fin = bool(x.get("financial"))
    # ---- 三情境 ----
    if hist:
        g0c = min(0.08, min(hist) * (0.75 if len(hist) == 2 else 0.6))
        if len(hist) == 1:
            notes.append("只有一個期間的歷史成長：保守 g0 ×0.6")
    else:
        g0c = None
    g_term_c = 0.03 if x.get("moat_widening") else 0.0
    g0b = _val(x, "base_g0", min(max(hist), 0.10) if hist else None)
    g0o = _val(x, "opt_g0", min(0.15, max(hist)) if hist else None)
    iv0 = oe / r_d
    if x.get("insurance"):
        ins = x["insurance"]
        iv_c = ins["tbv_ps"] + max(0.0, ins["float_ps"] * (L - ins["float_cost"] - 0.01) / r_d)
        if x.get("iv_cons") is None:
            x["iv_cons"] = iv_c
        notes.append("保險：IV_cons 依 VAL-081 可計算版")
    iv_cons = _val(x, "iv_cons", pv(oe, g0c, r_d, g_term_c) if g0c is not None else None)
    iv_base = _val(x, "iv_base", pv(oe, g0b, r_d, 0.03) if g0b is not None else None)
    iv_opt = _val(x, "iv_opt", pv(oe, g0o, r_d, 0.03) if g0o is not None else None)
    out = {"r_d": r_d, "r_stress": r_stress, "low_rate_start": low, "g0": {"cons": g0c, "base": g0b, "opt": g0o},
           "IV_cons": iv_cons, "IV_base": iv_base, "IV_opt": iv_opt, "IV0": iv0}
    if iv_cons is None or iv_cons <= 0:
        out.update(cap=None, conclusion_hint="MISSING_DATA 或無法估值（IV_cons 缺或 ≤0）", notes=notes)
        return out
    width = iv_opt / iv_cons if iv_opt is not None else None
    out["width_opt_over_cons"] = width
    no_cap = None
    if width and width > 4:
        no_cap = "P-21 區間 >4：不做（不設上限）"
    if width and width > 3:
        grade = ORDER[min(3, ORDER.index(grade) + 1)]
        notes.append(f"P-21 區間 >3：等級降一級 → {grade}")
    out["grade_used"] = grade
    if grade == "D":
        no_cap = no_cap or "D 級：不做（不設上限）"
    k = GRADE_CAP.get(grade, GRADE_CAP["C"])  # D 級只為輸出報告欄而用 C 係數，不設上限
    # ---- P1／P3 ----
    def ivc_at(r, oe_=oe):
        # 覆寫值與保險式不含 E：結論測試的 E×0.9 以價值等比例縮放近似（推論；否則該半邊測試永遠不起作用）
        scale = oe_ / oe if oe else 1.0
        if x.get("iv_cons") is not None and not x.get("insurance"):
            return x["iv_cons"] * (r_d / r) * scale  # 覆寫值：以零成長近似換算利率（推論）
        if x.get("insurance"):
            ins = x["insurance"]
            return (ins["tbv_ps"] + max(0.0, ins["float_ps"] * (L - ins["float_cost"] - 0.01) / r)) * scale
        return pv(oe_, g0c, r, g_term_c)
    P1 = k * iv_cons
    P3 = k * ivc_at(r_stress)
    # ---- P2′：使 g_impl ≤10% 的最高價（以正常化盈餘 oe、r_d；永續 g_term＝min(3%, 30 年期 −1pp)）----
    g_term_i = min(0.03, max(0.0, L - 0.01))
    out["g_term_impl"] = g_term_i
    g_impl = None if fin else solve_g(oe, price, r_d, g_term_i)
    out["g_impl_gt_cap"] = (not fin) and g_impl is None
    P2 = None
    if not fin:
        P2 = pv(oe, G_IMPL_MAX, r_d, g_term_i)
        if hist and min(hist) <= 0:
            notes.append("歷史成長 ≤0：反方必答（b）（c），答不出 → 觀察（references/05 §4）")
        out["g_cap_for_P2"] = G_IMPL_MAX
    # SKILL §3：歷史成長 ≤0 → 上限只能用 IV0（E ÷ r_d）× 等級係數
    iv0_cap = k * iv0 if hist and min(hist) <= 0 else None
    # ---- P4′ ----
    P4 = None
    pem = x.get("largecap_pe_median")
    if pem:
        if fin:
            fpm = x.get("fin_pe_median")
            P4 = min(pem * 1.0, fpm * 1.1) * eps if fpm else pem * eps
        else:
            k4 = 1.2 if grade == "A" else 1.0
            P4 = k4 * pem * eps
            out["P4_multiplier"] = k4
    else:
        notes.append("缺大型股 P/E 中位：P4′ 不適用，W-PE 以絕對 20／25 記")
    comps = {"P1_report_only": P1, "P2_prime": P2, "P3_report_only": P3, "P4p": P4}
    # P4′ 為軟上限（規則書 P-19）——不進硬上限；超過時註記，須在 G9 寫理由
    if P4 is not None and price > P4:
        notes.append("現價 > P4′（軟上限）：G9 反方必寫理由")
    # 金融類沒有 P2′：硬上限＝min(P1, P4′)（P1＝等級係數×IV_cons 保留安全邊際；P4′ 是軟上限，不可單獨當硬上限；推論）
    capc = [P2] if P2 is not None else [v for v in (P1, P4) if v is not None]
    cap = min(capc)
    if grade == "C":
        notes.append("C 級：最多觀察，不得買")
    # 自動買進上限（合併版一致性條款，推論）：不得高於基準情境價值 IV_base——
    # 賣出檢視線 sell_review_above＝IV_base，買在其上等於一買進就觸發賣出檢視。
    if iv0_cap is not None and cap > iv0_cap:
        cap = iv0_cap
        notes.append("歷史成長 ≤0：上限改用 IV0 × 等級係數（SKILL §3）")
    if no_cap:
        cap = None
    abc = (min(cap, iv_base) if iv_base is not None else cap) if cap is not None else None
    if iv_base is not None and cap is not None and iv_base < cap:
        notes.append("IV_base < P2′：自動買進上限取 IV_base（一致性條款）")
    comps["IV0_cap"] = iv0_cap
    out.update(components=comps, cap=cap, auto_buy_cap=abc, g_impl=g_impl, price=price,
               price_to_cons=price / iv_cons, price_to_cap=price / cap if cap else None,
               price_to_auto_buy_cap=price / abc if abc else None)
    # ---- 結論測試（距上限 <5%）----
    gap = (abc - price) / abc if abc else None
    if gap is not None and 0 <= gap < 0.05:
        p2_oe = pv(oe * 0.9, G_IMPL_MAX, r_d, g_term_i) if not fin else k * ivc_at(r_d, oe * 0.9)
        p2_sens = pv(oe, G_IMPL_MAX, r_sens, g_term_i) if not fin else k * ivc_at(r_sens)
        flip = min(p2_oe, p2_sens) < price
        out["conclusion_test"] = {"P2_prime_with_E_x0.9": p2_oe, "P2_prime_rate_sensitivity": p2_sens, "flips": flip}
        if flip:
            notes.append("結論測試翻轉 → WATCH（VAL-045）")
    out["sensitivity_cap_P3_plus4pp"] = k * ivc_at(r_sens)
    pe = price / eps if eps > 0 else None
    th = 25 if ((x.get("roe") or 0) >= 0.18 and (x.get("eps_cagr5") or 0) >= 0.10) else 20
    out["W_PE"] = bool(pe and pe > th)
    out["norm_pe"] = pe
    if g_impl is not None and hist:
        g5 = x.get("g5") if x.get("g5") is not None else min(hist)
        out["g_impl_checks"] = {"a_le_10pct_gate": g_impl <= G_IMPL_MAX,
                                "b_le_hist5y" + ("_half" if grade == "A" else ""): g_impl <= g5 * (0.5 if grade == "A" else 1.0),
                                "c_le_cons_g0": g_impl <= g0c}
    ok = abc is not None and price < abc and grade in ("A", "B") and not out.get("conclusion_test", {}).get("flips")
    out["conclusion_hint"] = (no_cap if no_cap else "價格 < 自動買進上限（仍須 G8、G9–G11 與三閘門）" if ok
                              else "價格 ≥ 自動買進上限、C 級、隱含成長超出可解範圍或結論測試翻轉 → WATCH")
    out["notes"] = notes + ["保守情境 1–10 年遞減路徑採 g_impl 同式（推論）"]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--out")
    a = ap.parse_args()
    res = calc(json.load(open(a.input, encoding="utf-8")))

    def rnd(v):
        if isinstance(v, float):
            return round(v, 4)
        if isinstance(v, dict):
            return {k: rnd(w) for k, w in v.items()}
        if isinstance(v, list):
            return [rnd(w) for w in v]
        return v
    s = json.dumps(rnd(res), ensure_ascii=False, indent=1)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(s)
    print(s)


if __name__ == "__main__":
    main()
