#!/usr/bin/env python3
"""估值三情境與買價上限計算器（對應 規則書 第 6 節、P-14～P-22、VAL-046、VAL-050、VAL-051、VAL-081）。

  python cap_calc.py input.json [--out result.json]

輸入 JSON（每股數字；比率用小數；數字欄缺、null、NaN／無窮大一律視為「缺」）：
  oe                 每股業主盈餘 E（SKILL §3：經常性稅後淨利＋折舊攤銷−維持性資本支出；GAAP 淨利已含股權薪酬費用，不加回也不再扣）   必要
  g10, g5            10 年、5 年內生業主盈餘（或營業盈餘）年複合；缺填 null                    至少一個
  grade              確定性等級 A/B/C/D（不得先含 P-21 降級：非金融 P-21 區間 >3 由本腳本自動再降一級、>4 不做，
                     金融類不適用 P-21；procedure/06 §1、設計決策 D22）   必要
  long_rate          最近交易日 30 年期公債殖利率                                              必要
  long_rate_20y_med  過去 20 年 30 年期中位（判斷低利率起點；缺 → 視為非低利率並註記）
  price              現價                                                                        必要
  norm_eps           正常化營業 EPS（P4′ 與 W-PE 用；缺 → 用 oe 並註記；框架的荒謬級線 50×E 也用它）
  largecap_pe_median 大型股 P/E 中位（缺 → P4′ 不適用）
  fin_pe_median      金融同業 P/E 中位（金融類 P4′）
  financial          true/false（JSON 布林；字串 "true"/"false" 會轉換，其他值報錯）。金融類不做 P2；
                     iv_cons/iv_base/iv_opt 覆寫與 insurance 只在 financial=true 時採用（自動流程的覆寫由框架清為 null：
                     金融 IV_base 未定義 → auto_buy_cap null、最多 WATCH，D20）
  insurance          {"tbv_ps":..,"float_ps":..,"float_cost":..}（VAL-081 可計算版；有 iv_cons 覆寫時以覆寫為準；
                     {} 或三欄皆 null 視同缺；只在 financial=true 時驗證，部分填寫 → 報錯）
  base_g0, opt_g0    基準／樂觀起始成長（缺：基準＝min(已證明成長,10%)；樂觀＝min(15%, 較高歷史成長)；已證明成長＝10 年與 5 年較高者）；
                     自動流程由框架清為 null，一律走預設（看過價格的 FULL_B 不得自填）
  moat_widening      true/false → true 時保守終值成長可 3%（護城河三測全強且變寬），預設 0%
  incr_roic_gt_rd    true | false | null（三值，D97）：true＝近 5 年增量資本報酬確定 > r_d（A 級 P4′ 可 1.2 倍）；
                     false＝確定 ≤ r_d → 成長不加價值：硬上限不高於 IV0×等級係數（同近零成長），並記註；
                     null／缺＝資料不足、未判定 → 不改上限，記註（G9 反方作答）。P4′ 1.2 倍只在 true 時
  w_rev              true/false（P-05 營收衰退警示 W-REV 成立 → 估值不給成長，上限只用 IV0×等級係數）；自動流程中非金融
                     由框架依資料包營收 3 年或 5 年年複合 ≤0 設 true，模型只能打開、不能關
  roe, eps_cagr5     W-PE 門檻放寬條件（ROE ≥18% 且 5 年 EPS 年複合 ≥10% → 25 倍）
  iv_cons, iv_base, iv_opt  直接覆寫（只限金融類：銀行等以 VAL-080 另算者；只供人工單跑，自動流程由框架清為 null）
  cap_discount_k     自動買進上限的確定性折扣（框架設定注入，模型不填）：數字（各級同值）或 {"A":..,"B":..}；
                     缺 → A 0.95／B 0.85（推論，ASK-1＝B）
路徑（推論，與 規則書 g_impl 同式）：第 t 年成長＝g＋(min(g,4%)−g)×(t−1)/9，t=1..10；保守終值 0%、基準與樂觀 3%
（歷史成長 ≤0 → 基準終值 0%）。保守起始成長：正成長 × 0.75（只有一期 × 0.6），≤0 不打折。
輸出：三情境、IV0、r_d、r_stress、g_impl、P1、P1～P4′、硬上限 cap＝P2′（歷史成長 ≤1%「近零」或 W-REV → 不高於 IV0×等級係數；
      incr_roic_gt_rd＝false 同樣不高於 IV0×等級係數；P4′ 軟上限、P1、P3 為報告欄；金融類無 P2′ → min(P1, P4′)）、
      自動買進上限 auto_buy_cap＝CONCLUSION_MARGIN（0.95）× auto_buy_cap_raw，auto_buy_cap_raw＝min(cap, k×IV_base)
      （IV_base 缺 → null；D90：VAL-045 結論測試的 E×0.9 腿在本模型下恆等於 0.9×上限〔各項對 E 線性〕，
      等同「現價須低於上限的 95%」，故明文寫進 auto_buy_cap〔abc_effective〕，不再另做翻轉測試、不輸出 conclusion_test）、
      price_to_cons（現價÷IV_cons）、敏感度（sensitivity_cap_P3_plus4pp：r_sens 下的 P3，只作報告）、W-PE、p21_applicable、g9_reason_required（兩期成長差 >5pp → G9 須寫理由）、watch_reasons、
      no_value_reason、註記。IV_cons／IV_base／IV_opt／IV0 ≤0 一律輸出 null。業主盈餘 oe ≤0 → 無法估值
      （no_value_reason＝iv_cons_le_0 或 oe_le_0；成長缺時也是 oe_le_0，不是 missing_growth），IV_base／IV_opt
      （含金融覆寫）輸出 null。
"""
import argparse
import json
import math

GRADE_CAP = {"A": 0.85, "B": 0.75, "C": 0.40}
ORDER = ["A", "B", "C", "D"]
CAP_DISCOUNT_K = {"A": 0.95, "B": 0.85}  # ASK-1＝B（推論，無回測）：auto_buy_cap＝min(P2′, k×IV_base)


G_IMPL_MAX = 0.10  # 見 ../references/procedure/05_valuation_caps.md §4
NEAR_ZERO_G = 0.01  # D21：歷史成長 ≤1% 視為「近零」（推論）
G9_SPREAD = 0.05    # ASK-1＝B：10 年與 5 年成長差 >5pp → G9 必寫理由
# D90（A3-M2B-01）：VAL-045 結論測試「距上限 <5% → 以 E×0.9 重算；低於現價 → WATCH」在本模型下退化（上限對 E 線性，
# E×0.9 的上限恆＝0.9×上限，距上限 <5% 時必翻轉），實際買點＝0.95×上限。行為不變的明文化：
# auto_buy_cap＝CONCLUSION_MARGIN × min(P2′, k×IV_base)，不另做翻轉測試（不放寬買點）
CONCLUSION_MARGIN = 0.95
NUM_KEYS = ("oe", "g10", "g5", "long_rate", "long_rate_20y_med", "price", "norm_eps", "largecap_pe_median",
            "fin_pe_median", "base_g0", "opt_g0", "roe", "eps_cagr5", "iv_cons", "iv_base", "iv_opt")
BOOL_KEYS = ("financial", "moat_widening", "incr_roic_gt_rd", "w_rev")
OVERRIDE_KEYS = ("iv_cons", "iv_base", "iv_opt")
INS_KEYS = ("tbv_ps", "float_ps", "float_cost")


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


def _num(v, key, notes):
    """數字欄：null／NaN／無窮大 → None（缺）；布林或非數字 → ValueError。"""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{key}={v!r} 不是數字")
    if not math.isfinite(v):
        notes.append(f"{key}={v!r} 非有限數字：視為缺")
        return None
    return v


def _bool(v, key):
    """布林欄：只接受 JSON 布林、null 或字串 "true"/"false"；其他 → ValueError（不得把 "false" 當真）。"""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    raise ValueError(f"{key}={v!r} 不是布林")


def _discount_k(v):
    """cap_discount_k → {等級: k}；缺 → CAP_DISCOUNT_K。k 須在 (0, 1]。"""
    ks = dict(CAP_DISCOUNT_K)
    if v is None:
        return ks
    if isinstance(v, dict):
        items = v.items()
    else:
        items = [(g, v) for g in ORDER]
    for g, k in items:
        if g not in ORDER:
            raise ValueError(f"cap_discount_k 等級 {g!r} 不是 A/B/C/D")
        if isinstance(k, bool) or not isinstance(k, (int, float)) or not math.isfinite(k) or not 0 < k <= 1:
            raise ValueError(f"cap_discount_k[{g}]={k!r} 須在 (0, 1]")
        ks[g] = float(k)
    return ks


def normalize(x):
    """輸入正規化（M2_D9_04）：數字欄非有限 → 缺；布林欄只收布林；必要欄缺 → ValueError。回傳 (x, notes)。"""
    x = dict(x)  # 不改呼叫端的 dict
    notes = []
    for k in NUM_KEYS:
        x[k] = _num(x.get(k), k, notes)
    for k in BOOL_KEYS:
        x[k] = _bool(x.get(k), k)
    for k in ("oe", "price", "long_rate"):
        if x[k] is None:
            raise ValueError(f"缺必要欄位 {k}（或非有限數字）")
    if x["price"] <= 0:
        raise ValueError(f"price={x['price']!r} 必須是正數")
    if x.get("grade") not in ORDER:
        raise ValueError(f"grade={x.get('grade')!r} 不是 A/B/C/D")
    ins = x.get("insurance")
    if isinstance(ins, dict) and all(ins.get(k) is None for k in INS_KEYS):
        ins = None  # {} 或三欄皆 null → 視同缺（舊版行為）
    x["insurance"] = ins
    if ins is not None and x["financial"]:  # 非金融不驗證：calc 反正忽略
        if not isinstance(ins, dict):
            raise ValueError("insurance 必須是物件")
        ins = {k: _num(ins.get(k), f"insurance.{k}", notes) for k in INS_KEYS}
        n_missing = sum(v is None for v in ins.values())
        if 0 < n_missing < len(INS_KEYS):
            raise ValueError("insurance 缺 tbv_ps／float_ps／float_cost")
        x["insurance"] = ins if n_missing == 0 else None
    x["cap_discount_k"] = _discount_k(x.get("cap_discount_k"))
    return x, notes


def _pos(v):
    """價值 ≤0 不得當價格線（M2_D9_02）：→ None。"""
    return v if v is not None and v > 0 else None


def calc(x):
    x, notes = normalize(x)
    L = x["long_rate"]
    low = x["long_rate_20y_med"] is not None and L < x["long_rate_20y_med"]
    if x["long_rate_20y_med"] is None:
        notes.append("缺 20 年長債中位：視為非低利率起點")
    r_d = max(0.10, L + 0.03)
    r_stress = max(0.10, L + 0.06 + (0.01 if low else 0.0))
    r_sens = max(0.10, L + 0.07 + (0.01 if low else 0.0))
    oe, price = x["oe"], x["price"]
    eps = x["norm_eps"] or oe
    if not x["norm_eps"]:
        notes.append("缺 norm_eps：P4′／W-PE 以 oe（業主盈餘 E）代替正常化 EPS")
    hist = [g for g in (x["g10"], x["g5"]) if g is not None]
    grade = x["grade"]
    fin = bool(x["financial"])
    p21 = not fin  # D22／D20：P-21 只適用非金融
    # 覆寫值與保險式只限金融類（M2-D8-01 (b)）
    if not fin:
        ign = [k for k in OVERRIDE_KEYS + ("insurance",) if x.get(k) is not None]
        if ign:
            notes.append("非金融：忽略 " + "／".join(ign) + "（覆寫只在 financial=true 時採用）")
        for k in OVERRIDE_KEYS + ("insurance",):
            x[k] = None
    ins = x.get("insurance")
    had_override = x["iv_cons"] is not None  # M2_D1_06：有覆寫就一律用覆寫值（保險式只在無覆寫時用）
    near_zero = bool(hist) and min(hist) <= NEAR_ZERO_G
    w_rev = x["w_rev"] is True
    roic_le_rd = x["incr_roic_gt_rd"] is False   # D97：false＝確定增量資本報酬 ≤ r_d；null＝未判定（不改上限）
    # ---- 三情境 ----
    if hist:
        gmin = min(hist)
        if gmin <= 0:
            g0c = gmin  # D21：負成長不打折（否則越差越樂觀）
        else:
            g0c = min(0.08, gmin * (0.75 if len(hist) == 2 else 0.6))
            if len(hist) == 1:
                notes.append("只有一個期間的歷史成長：保守 g0 ×0.6")
    else:
        g0c = None
    g_term_c = 0.03 if x["moat_widening"] else 0.0
    g_term_b = 0.0 if hist and min(hist) <= 0 else 0.03  # D21：歷史成長 ≤0 → 基準終值 0%
    if g_term_b == 0.0:
        notes.append("歷史成長 ≤0：IV_base 永續成長取 0%")
    g0b = _val(x, "base_g0", min(max(hist), 0.10) if hist else None)
    g0o = _val(x, "opt_g0", min(0.15, max(hist)) if hist else None)
    iv0 = oe / r_d
    if ins and not had_override:
        notes.append("保險：IV_cons 依 VAL-081 可計算版")
    elif ins:
        notes.append("保險：有 iv_cons 覆寫，IV_cons／P1／P3 以覆寫值為準")

    def ivc_at(r, oe_=oe):
        # 覆寫值與保險式不含 E：oe_≠oe 時以價值等比例縮放近似（推論；D90 起結論測試不再以 E×0.9 呼叫，保留參數）
        scale = oe_ / oe if oe else 1.0
        if had_override:
            return x["iv_cons"] * (r_d / r) * scale  # 覆寫值：以零成長近似換算利率（推論）
        if ins:
            return (ins["tbv_ps"] + max(0.0, ins["float_ps"] * (L - ins["float_cost"] - 0.01) / r)) * scale
        return pv(oe_, g0c, r, g_term_c) if g0c is not None else None

    def ivb_at(r, oe_=oe):
        # 金融類：IV_base 只取覆寫（D20：不借用一般公司的 DCF）；非金融：基準情境 DCF
        if fin:
            v = x["iv_base"]
            return _pos(v * (r_d / r) * (oe_ / oe if oe else 1.0)) if v is not None else None   # oe＝0 不除零（M2-RS8-02）
        return _pos(pv(oe_, g0b, r, g_term_b)) if g0b is not None else None

    iv_cons = ivc_at(r_d)
    iv_base = ivb_at(r_d)
    if fin:
        iv_opt = _pos(x["iv_opt"])
        if hist and (x["iv_base"] is None or x["iv_opt"] is None):
            notes.append("金融類：IV_base／IV_opt 只取覆寫，不借用一般公司 DCF（D20）")
    else:
        iv_opt = _pos(pv(oe, g0o, r_d, 0.03)) if g0o is not None else None
    g9 = len(hist) == 2 and abs(hist[0] - hist[1]) > G9_SPREAD
    if g9:
        notes.append("10 年與 5 年成長差 >5pp：已證明成長取較高者，G9 反方須寫理由")
    out = {"r_d": r_d, "r_stress": r_stress, "low_rate_start": low, "g0": {"cons": g0c, "base": g0b, "opt": g0o},
           "IV_cons": _pos(iv_cons), "IV_base": iv_base, "IV_opt": iv_opt, "IV0": _pos(iv0),
           "p21_applicable": p21, "g9_reason_required": g9, "near_zero_growth": near_zero, "w_rev": w_rev,
           "incr_roic_gt_rd": x["incr_roic_gt_rd"]}
    if roic_le_rd:
        notes.append("增量資本報酬 ≤ r_d（incr_roic_gt_rd＝false）：成長不加價值，硬上限不高於 IV0 × 等級係數（D97）")
    elif x["incr_roic_gt_rd"] is None:
        notes.append("增量資本報酬是否 > r_d 未判定（incr_roic_gt_rd＝null）：不改上限，G9 反方作答（D97）")
    no_value = None
    if oe <= 0 and iv_cons is None:
        no_value = "oe_le_0"   # 業主盈餘 ≤0：補成長也估不出價值，不是 MISSING_DATA（M2-R5-02）
    elif iv_cons is None:
        no_value = "missing_growth"
    elif iv_cons <= 0:
        no_value = "iv_cons_le_0"
    elif oe <= 0:
        no_value = "oe_le_0"
    if no_value:
        hint = ("MISSING_DATA（缺歷史成長，IV_cons 無法計算）" if no_value == "missing_growth"
                else "無法估值（IV_cons ≤0 或業主盈餘 ≤0）")
        out.update(cap=None, auto_buy_cap=None, grade_used=grade, price=price, g_impl=None, g_impl_gt_cap=None,
                   P1=None, price_to_cons=None, components={}, no_value_reason=no_value,
                   watch_reasons=["no_auto_buy_cap"], conclusion_hint=hint, notes=notes)
        if oe <= 0:   # 無法估值就沒有賣出檢視線：不輸出（金融覆寫）IV_base／IV_opt（M2-RS8-02）
            out.update(IV_base=None, IV_opt=None)
        return out
    width = iv_opt / iv_cons if iv_opt is not None else None
    out["width_opt_over_cons"] = width
    no_cap = None
    if p21 and width and width > 4:
        no_cap = "P-21 區間 >4：不做（不設上限）"
    if p21 and width and width > 3:
        grade = ORDER[min(3, ORDER.index(grade) + 1)]
        notes.append(f"P-21 區間 >3：等級降一級 → {grade}")
    out["grade_used"] = grade
    if grade == "D":
        no_cap = no_cap or "D 級：不做（不設上限）"
    k = GRADE_CAP.get(grade, GRADE_CAP["C"])  # D 級只為輸出報告欄而用 C 係數，不設上限
    kd = x["cap_discount_k"].get(grade, min(x["cap_discount_k"].values()))
    out["cap_discount_k"] = kd
    g_term_i = min(0.03, max(0.0, L - 0.01))  # P2′ 永續 g_term＝min(3%, 30 年期 −1pp)
    out["g_term_impl"] = g_term_i
    # ---- P4′ ----
    P4 = None
    pem = x["largecap_pe_median"]
    if pem and eps > 0:
        if fin:
            fpm = x["fin_pe_median"]
            P4 = min(pem * 1.0, fpm * 1.1) * eps if fpm else pem * eps
        else:
            k4 = 1.2 if grade == "A" and x["incr_roic_gt_rd"] is True else 1.0
            P4 = k4 * pem * eps
            out["P4_multiplier"] = k4
            if grade == "A":
                notes.append(f"P4′ 倍數 {k4}（A 級且增量 ROIC > r_d {'成立' if k4 > 1 else '未成立或缺'}）")
    elif not pem:
        notes.append("缺大型股 P/E 中位：P4′ 不適用，W-PE 以絕對 20／25 記")

    def parts(oe_, r):
        """在盈餘 oe_、折現率 r 下重算 P1、P2′、IV0 上限、硬上限與實際綁定的自動買進上限（結論測試共用）。"""
        scale = oe_ / oe
        P1_ = k * ivc_at(r, oe_)
        if fin:
            P2_ = None
            P4_ = P4 * scale if P4 is not None else None
            # 金融類沒有 P2′：硬上限＝min(P1, P4′)（P1＝等級係數×IV_cons 保留安全邊際；P4′ 不可單獨當硬上限；推論）
            cap_ = min(v for v in (P1_, P4_) if v is not None)
        else:
            P2_ = pv(oe_, G_IMPL_MAX, r, g_term_i)
            cap_ = P2_
        # SKILL §3：歷史成長為負或近零（≤1%，D21）、W-REV 或增量資本報酬確定 ≤ r_d（D97）→ 上限只能用 IV0（E ÷ r_d）× 等級係數
        iv0c = k * oe_ / r if (near_zero or w_rev or roic_le_rd) else None
        if iv0c is not None:
            cap_ = min(cap_, iv0c)
        ivb = ivb_at(r, oe_)
        # ASK-1＝B：自動買進上限（原始）＝min(硬上限, k×IV_base)；IV_base 缺 → 不設（失敗關閉；D20 金融類）
        abc_ = None if (no_cap or ivb is None) else min(cap_, kd * ivb)
        return {"P1": P1_, "P2": P2_, "iv0_cap": iv0c, "cap": None if no_cap else cap_, "ivb": ivb, "abc": abc_}

    m = parts(oe, r_d)
    P1, P2, iv0_cap, cap, abc_raw = m["P1"], m["P2"], m["iv0_cap"], m["cap"], m["abc"]
    abc = CONCLUSION_MARGIN * abc_raw if abc_raw is not None else None   # D90：abc_effective
    P3 = k * ivc_at(r_stress)
    g_impl = None if fin else solve_g(oe, price, r_d, g_term_i)
    out["g_impl_gt_cap"] = (not fin) and g_impl is None
    if not fin:
        if hist and min(hist) <= 0:
            notes.append("歷史成長 ≤0：反方必答（b）（c），答不出 → 觀察（references/procedure/05_valuation_caps.md §4）")
        out["g_cap_for_P2"] = G_IMPL_MAX
    comps = {"P1_report_only": P1, "P2_prime": P2, "P3_report_only": P3, "P4p": P4, "IV0_cap": iv0_cap,
             "IV_base_x_k": kd * iv_base if iv_base is not None else None}
    # P4′ 為軟上限（規則書 P-19）——不進硬上限；超過時註記，須在 G9 寫理由（金融類 P4′ 已進硬上限）
    if P4 is not None and price > P4:
        notes.append("現價 > P4′（軟上限）：G9 反方必寫理由")
    if grade == "C":
        notes.append("C 級：最多觀察，不得買")
    if iv0_cap is not None and cap is not None and cap == iv0_cap:
        notes.append("歷史成長 ≤1%（近零）、W-REV 或增量資本報酬 ≤ r_d：上限改用 IV0 × 等級係數（SKILL §3）")
    if not no_cap and iv_base is None:
        notes.append("IV_base 缺：不設自動買進上限（最多 WATCH）")
    if abc_raw is not None and cap is not None and abc_raw < cap:
        notes.append(f"k×IV_base（k={kd}）< 硬上限：自動買進上限取 k×IV_base（ASK-1＝B）")
    if abc is not None:
        notes.append(f"自動買進上限＝{CONCLUSION_MARGIN} × min(硬上限, k×IV_base)（VAL-045 結論測試 E×0.9 腿明文化，D90）")
        if abc <= price < abc_raw:
            notes.append("現價距原始上限 <5%（VAL-045 結論測試區）：已在自動買進上限之上 → WATCH（D90）")
    out.update(components=comps, cap=cap, auto_buy_cap=abc, auto_buy_cap_raw=abc_raw, abc_effective=abc,
               conclusion_margin=CONCLUSION_MARGIN, g_impl=g_impl, price=price, P1=P1,
               price_to_cons=price / iv_cons, price_to_cap=price / cap if cap else None,
               price_to_auto_buy_cap=price / abc if abc else None)
    # 結論測試（VAL-045）不再另做（D90）：E×0.9 腿已併入 auto_buy_cap；r_sens 只留報告欄（下行）
    out["sensitivity_cap_P3_plus4pp"] = k * ivc_at(r_sens)
    pe = price / eps if eps > 0 else None
    th = 25 if ((x["roe"] or 0) >= 0.18 and (x["eps_cagr5"] or 0) >= 0.10) else 20
    out["W_PE"] = bool(pe and pe > th)
    out["norm_pe"] = pe
    if g_impl is not None and hist:
        g5 = x["g5"] if x["g5"] is not None else min(hist)
        out["g_impl_checks"] = {"a_le_10pct_gate": g_impl <= G_IMPL_MAX,
                                # A 級門檻＝5 年成長的一半，只在 g5 > 0 時（g5 ≤0 時一半反而較寬；A3-M2A-03）
                                "b_le_hist5y" + ("_half" if grade == "A" else ""):
                                    g_impl <= g5 * (0.5 if grade == "A" and g5 > 0 else 1.0),
                                "c_le_cons_g0": g_impl <= g0c}
    reasons = []
    if abc is None:
        reasons.append("no_auto_buy_cap")
    elif not price < abc:
        reasons.append("price_ge_cap")
    if grade not in ("A", "B"):
        reasons.append("grade_not_AB")
    if out["g_impl_gt_cap"]:
        reasons.append("g_impl_unsolvable")
    out["watch_reasons"] = reasons
    out["no_value_reason"] = None
    out["conclusion_hint"] = (no_cap if no_cap else "價格 < 自動買進上限（仍須 G8、G9–G11 與三閘門）" if not reasons
                              else "WATCH：" + "、".join(reasons))
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
