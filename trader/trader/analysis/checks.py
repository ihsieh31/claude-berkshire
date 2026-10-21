"""深度分析的程式檢查：閘門 3、買價上限計算、訊號卡與計算結果一致性、閘門降級。

規則來源：`berkshire-deep-merged/SKILL.md` §0A、§3、§4。
計算一律匯入 skill 腳本（`scripts/cap_calc.py`、`scripts/gate3_check.py`），不在此複製公式。
"""
from __future__ import annotations

import copy
import importlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from trader.paths import get_paths


class CheckError(Exception):
    """輸入檔壞或計算失敗（呼叫端應記錄，不得把該檔當成通過）。"""


_MODULES: dict[str, Any] = {}


def _skill_module(name: str) -> Any:
    """匯入 skill 的 scripts 目錄中的模組（只匯入，不執行 main）。"""
    scripts = get_paths().skill_deep / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    if name not in _MODULES:
        _MODULES[name] = importlib.import_module(name)
    return _MODULES[name]


def _round(v: Any) -> Any:
    """與 cap_calc.py 的 main() 相同的四捨五入方式（浮點數取 4 位）。"""
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, dict):
        return {k: _round(w) for k, w in v.items()}
    if isinstance(v, list):
        return [_round(w) for w in v]
    return v


# ---------------------------------------------------------------- 閘門 3

def _gate3_fail(msg: str, path: Path) -> dict:
    return {
        "gate3_pass": False,
        "failures": [msg],
        "strong_count": 0,
        "unknown_count": 0,
        "max_grade_from_C1": None,
        "questionnaire_version": None,
        "answers_file": str(path),
    }


def run_gate3(answers_path: Path, asof: str | None = None) -> dict:
    """驗證閘門 3 作答檔。檔案不存在、JSON 壞或內容無法判讀 → 回傳未通過（不拋例外）。
    asof（工作的決策日）給定時以它為決策日，作答檔 decision_date 不同即不通過（M2_D6_05）。"""
    path = Path(answers_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _gate3_fail(f"閘門 3 作答檔不存在：{path}", path)
    except OSError as e:
        return _gate3_fail(f"閘門 3 作答檔讀取失敗：{e}", path)
    try:
        ans = json.loads(text)
    except ValueError as e:
        return _gate3_fail(f"閘門 3 作答檔 JSON 解析失敗：{e}", path)
    if not isinstance(ans, dict):
        return _gate3_fail("閘門 3 作答檔頂層不是 JSON 物件", path)

    gate3 = _skill_module("gate3_check")
    try:
        return gate3.evaluate(ans, asof=asof)
    except Exception as e:  # 失敗關閉：內容壞就不通過，不讓它變成通過
        return _gate3_fail(f"閘門 3 作答檔內容無法判讀：{type(e).__name__}: {e}", path)


# ---------------------------------------------------------------- 買價上限

ABSURD_PE = 50          # 荒謬級線 ＝ 50 × 正常化每股盈餘 E（SKILL §4；cap_calc 不輸出此值）
GRADE_ORDER = ("A", "B", "C", "D")


def absurd_line(cap_input: dict) -> float | None:
    """荒謬級線 ＝ 50 × E（E＝norm_eps，缺用 oe）；E ≤0 或缺 → None（無荒謬級線，不得變成 0 或負數而誤觸）。"""
    e = cap_input.get("norm_eps") or cap_input.get("oe")
    if not _finite_num(e) or e <= 0:   # NaN／無窮大也不得變成價格線（M6-D8-02）
        return None
    return round(ABSURD_PE * float(e), 4)


def worse_grade(*grades: Any) -> str | None:
    """較保守（較低）的等級；不認得的值略過，全不認得回 None。"""
    known = [g for g in grades if g in GRADE_ORDER]
    return max(known, key=GRADE_ORDER.index) if known else None


# cap_input 合理範圍（推論：只擋單位或小數點錯誤，例如 g 填 8 而非 0.08、oe 以百萬元計；不做業務判斷）
_RATE_FIELDS = {"g10": (-1, 1), "g5": (-1, 1), "long_rate": (0, 0.2), "roe": (-1, 1), "eps_cagr5": (-1, 1)}
PRICE_TO_OE_RANGE = (1, 500)
NORM_EPS_TO_OE_MIN = 0.2   # 推論（M6-D8-02）：norm_eps 遠小於 oe → 荒謬級線（50×E）低到立刻觸發。只設下限：
                           # 重資產公司 OE＝淨利−(K−1)×折舊可遠低於 EPS，上限會誤擋；向上的單位錯由 price/norm_eps 範圍擋


_NUM_FIELDS = ("oe", "g10", "g5", "long_rate", "long_rate_20y_med", "price", "norm_eps", "largecap_pe_median",
               "fin_pe_median", "base_g0", "opt_g0", "roe", "eps_cagr5", "iv_cons", "iv_base", "iv_opt")
_BOOL_FIELDS = ("financial", "moat_widening", "incr_roic_gt_rd", "w_rev")


def _finite_num(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def cap_input_problems(x: dict) -> list[str]:
    """cap_input 欄位超出合理範圍的清單（空＝通過）。oe ≤ 0 交給 cap_calc（無法估值），不在此擋。

    另擋：數字欄為 NaN／無窮大或非數字、布林欄不是布林（字串 "true"/"false" 容許，cap_calc 會轉換）、
    cap_discount_k 不在 (0, 1]（M2_D9_04；null 照舊視為缺）。"""
    out = []
    for k in _NUM_FIELDS:
        v = x.get(k)
        if v is not None and not _finite_num(v):
            out.append(f"{k}={v!r} 不是有限數字")
    for k in _BOOL_FIELDS:
        v = x.get(k)
        if v is not None and not isinstance(v, bool) and not (
                isinstance(v, str) and v.strip().lower() in ("true", "false")):
            out.append(f"{k}={v!r} 不是布林（true/false）")
    kd = x.get("cap_discount_k")
    if kd is not None:
        vals = list(kd.values()) if isinstance(kd, dict) else [kd]
        if not vals or not all(_finite_num(v) and 0 < v <= 1 for v in vals) or (
                isinstance(kd, dict) and not set(kd) <= set(GRADE_ORDER)):
            out.append(f"cap_discount_k={kd!r} 須為 (0, 1] 的數字或 {{等級: 數字}}")
    for k, (lo, hi) in _RATE_FIELDS.items():
        v = x.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi):
            out.append(f"{k}={v!r} 不在 [{lo}, {hi}]（比率請用小數，例如 0.08）")
    if x.get("grade") not in GRADE_ORDER:
        out.append(f"grade={x.get('grade')!r} 不是 A/B/C/D")
    px, oe = x.get("price"), x.get("oe")
    oe_ok = _finite_num(oe) and oe > 0
    lo, hi = PRICE_TO_OE_RANGE
    if not _finite_num(px) or px <= 0:
        out.append(f"price={px!r} 必須是正數")
    elif oe_ok and not lo <= px / oe <= hi:
        out.append(f"price/oe={px / oe:.4g} 不在 [{lo}, {hi}]（oe 應為每股金額）")
    # norm_eps（P4′、W-PE、荒謬級線用）：單位錯會讓荒謬級線立刻觸發或失效（M6-D8-02、M2-D8-01 (c)；範圍為推論，只擋單位錯）
    ne = x.get("norm_eps")
    if _finite_num(ne):
        if ne <= 0:
            out.append(f"norm_eps={ne!r} 必須是正數（無正常化盈餘請填 null）")
        else:
            if oe_ok and ne / oe < NORM_EPS_TO_OE_MIN:
                out.append(f"norm_eps/oe={ne / oe:.4g} < {NORM_EPS_TO_OE_MIN}（norm_eps 應為每股金額）")
            if _finite_num(px) and px > 0 and not lo <= px / ne <= hi:
                out.append(f"price/norm_eps={px / ne:.4g} 不在 [{lo}, {hi}]（norm_eps 應為每股金額）")
    # iv_* 覆寫（金融類 VAL-080）：須為正、≤ 50×oe（荒謬級倍數；推論）、iv_cons ≤ iv_base ≤ iv_opt（M2-D8-01 (c)、M6-D8-01）
    ivs = [(k, x.get(k)) for k in ("iv_cons", "iv_base", "iv_opt") if _finite_num(x.get(k))]
    for k, v in ivs:
        if v <= 0:
            out.append(f"{k}={v!r} 必須是正數")
        elif oe_ok and v > ABSURD_PE * oe:
            out.append(f"{k}={v!r} 高於 {ABSURD_PE}×oe（{ABSURD_PE * oe:.4g}）")
    vals = [v for _, v in ivs]
    if vals != sorted(vals):
        out.append("iv_cons ≤ iv_base ≤ iv_opt 不成立：" + "、".join(f"{k}={v!r}" for k, v in ivs))
    return out


def run_cap_calc(cap_input_path: Path) -> dict:
    """以 cap_calc.calc 計算三情境、P1–P4′、自動買進上限。數值與 cap_calc.py 命令列輸出相同（四位小數）。"""
    path = Path(cap_input_path)
    try:
        x = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CheckError(f"cap_calc 輸入檔讀取或解析失敗：{path}：{e}") from e
    if not isinstance(x, dict):
        raise CheckError(f"cap_calc 輸入檔頂層不是 JSON 物件：{path}")
    bad = cap_input_problems(x)
    if bad:
        raise CheckError(f"cap_calc 輸入超出合理範圍：{path}：" + "；".join(bad))

    cap_calc = _skill_module("cap_calc")
    try:
        res = cap_calc.calc(x)
    except Exception as e:  # 缺必要欄位（KeyError）、等級不合法（ValueError）等皆屬輸入壞
        raise CheckError(f"cap_calc 計算失敗（輸入不完整或不合法）：{path}：{type(e).__name__}: {e}") from e
    return _round(res)


# ---------------------------------------------------------------- D26：機械版業主盈餘比對

OE_CHECK_TOL = 0.10       # D26：E、K、g 與機械版相對差 >10% → 退回一次（推論門檻，使用者 D26 決定）
OE_CHECK_G_FLOOR = 0.01   # 推論：成長率另需絕對差 >1pp 才算不同（小成長率的 10% 只是雜訊）


def _fy(v: Any) -> int | None:
    """財年正規化（A3-M3C-06）：整數（或整數值浮點）與 'FY2020'／'2020' 字串 → 2020；其他 → None。"""
    if _finite_num(v):
        return int(v) if float(v).is_integer() else None
    if isinstance(v, str):
        m = re.fullmatch(r"\s*(?:FY)?\s*(\d{4})\s*", v, re.I)
        return int(m.group(1)) if m else None
    return None


def oe_check(md_text: str, peer_shock_years: list | None, model: dict | None,
             distress_pass: bool = False) -> dict | None:
    """D26：以資料包 10 年財報表算機械版 E、K、g10／g5（deep_quant.mechanical_oe），與模型的 normalized_inputs 比對。
    peer_shock_years：框架同業段（_peers.json）的共同衝擊年；None＝缺同業資料（不比剔除年）。
    distress_pass：4A 困境三問全過 → 機械版非衝擊年不足 3 年時取窗口中位（A3-M2C-06；預設 False＝原行為）。
    模型 normalized_inputs.g_adjustment_reason 非空（VAL-013 基期失真／收購剔除等改算 g）→ g10／g5 差異只記 g_notes，
    不進 diffs（D93）；E、K、剔除年照核。財年接受整數與 'FY2020'（_fy）。
    回傳 {"mechanical", "model", "diffs"}（diffs 空＝一致）；資料包缺表或資料不足 → None（不比對）。"""
    dq = _skill_module("deep_quant")
    shocks = [y for y in (_fy(v) for v in peer_shock_years or []) if y is not None]
    mech = dq.mechanical_oe(dq.dossier_table(md_text or ""), shocks, distress_pass=distress_pass)
    if mech is None:
        return None
    model = model or {}
    # D73b：模型逐項列出的一次性調整（fy、amount 稅後、source 必填）加回機械版後再比 E；調整不影響 K 與剔除年比對
    adjs: dict[int, float] = {}
    for a in model.get("one_time_adjustments") or []:
        fy = _fy(a.get("fy")) if isinstance(a, dict) else None
        if fy is not None and _finite_num(a.get("amount")) and str(a.get("source") or "").strip():
            adjs[fy] = adjs.get(fy, 0.0) + float(a["amount"])
    mech_adj = dq.mechanical_oe(dq.dossier_table(md_text or ""), shocks, adjs,
                                distress_pass=distress_pass) if adjs else None
    g_reason = str(model.get("g_adjustment_reason") or "").strip()
    diffs: list[str] = []
    g_notes: list[str] = []
    for key, mkey, is_g in (("E", "oe_per_share", False), ("K", "K", False), ("g10", "g10", True), ("g5", "g5", True)):
        a, b = model.get(mkey), (mech_adj or mech).get(key) if key in ("E", "g10", "g5") else mech.get(key)
        if not _finite_num(a) or not _finite_num(b):
            continue
        d = abs(a - b)
        if d > OE_CHECK_TOL * max(abs(a), abs(b)) and (not is_g or d > OE_CHECK_G_FLOOR):
            msg = f"{key}：模型 {a:.4g}，機械版{'（含一次性調整）' if mech_adj and key != 'K' else ''} {b:.4g}"
            if is_g and g_reason:   # D93：模型寫明改算 g 的理由 → 只記註、不退回
                g_notes.append(f"{msg}（模型註明改算理由：{g_reason[:200]}；只記註不退回，D93）")
            else:
                diffs.append(msg)
    if peer_shock_years is not None:
        lo, hi = mech["fy_last"] - 9, mech["fy_last"]
        mine = sorted({y for y in (_fy(v) for v in model.get("excluded_shock_years") or [])
                       if y is not None and lo <= y <= hi})
        peer = sorted({y for y in shocks if lo <= y <= hi})
        if mine != peer:
            diffs.append(f"剔除年：模型 {mine}，同業共同衝擊年 {peer}")
    m = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in mech.items() if k != "oe_by_year"}
    out = {"mechanical": m, "model": {k: model.get(k) for k in ("oe_per_share", "K", "g10", "g5",
                                                                 "excluded_shock_years", "one_time_adjustments",
                                                                 "g_adjustment_reason")},
           "diffs": diffs}
    if g_notes:
        out["g_notes"] = g_notes
    if mech_adj:
        out["mechanical_adjusted"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in mech_adj.items()
                                      if k in ("E", "g10", "g5", "basis")}
        out["adjustments_by_fy"] = adjs
    return out


# 訊號卡 value_per_share 鍵 ↔ cap_calc 輸出鍵（SKILL §3 三情境＋IV0）
_IV_MAP = (
    ("conservative", "IV_cons"),
    ("base", "IV_base"),
    ("optimistic", "IV_opt"),
    ("zero_growth_IV0", "IV0"),
)


def _as_num(v: Any) -> float | None:
    """None → None；int／float → float；其他型別（含 bool、字串）拋 TypeError。"""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"非數字：{v!r}")
    return float(v)


def _near(a: float | None, b: float | None, rel_tol: float) -> tuple[bool, str]:
    """相等判斷。兩邊皆 None 一致；一邊 None 不一致；否則以兩者較大絕對值為基準的相對差與 rel_tol 比較。"""
    if a is None and b is None:
        return True, ""
    if a is None or b is None:
        return False, "一邊為 None"
    ref = max(abs(a), abs(b))
    if ref == 0.0:
        return True, ""
    rel = abs(a - b) / ref
    return rel <= rel_tol, f"相對差 {rel:.4%}"


def compare_signal_with_cap(signal_card: dict, cap: dict, rel_tol: float = 0.005) -> list[str]:
    """比對訊號卡與 cap_calc 結果：auto_buy_cap 與三情境＋IV0。回傳不一致說明（空清單＝一致）。"""
    mismatches: list[str] = []

    def check(label: str, card_val: Any, cap_val: Any) -> None:
        try:
            a = _as_num(card_val)
            b = _as_num(cap_val)
        except TypeError as e:
            mismatches.append(f"{label}：{e}（訊號卡 {card_val!r}，cap_calc {cap_val!r}）")
            return
        ok, why = _near(a, b, rel_tol)
        if not ok:
            mismatches.append(f"{label}：訊號卡 {card_val!r}，cap_calc {cap_val!r}（{why}）")

    check("auto_buy_cap", signal_card.get("auto_buy_cap"), cap.get("auto_buy_cap"))
    vps = signal_card.get("value_per_share") or {}
    for card_key, cap_key in _IV_MAP:
        check(f"value_per_share.{card_key}", vps.get(card_key), cap.get(cap_key))
    return mismatches


# 訊號卡 cap_components 鍵 ↔ cap_calc components 鍵（報告欄；ASK-1：P1 另列）
_COMP_MAP = (
    ("P1_report_only", "P1_report_only"),
    ("P2_prime_g_impl_10", "P2_prime"),
    ("P3_report_only", "P3_report_only"),
    ("P4_prime", "P4p"),
    ("IV0_cap", "IV0_cap"),   # 近零成長／W-REV 時的 IV0×等級係數上限（M2_D9_06）
)


def set_cap_report_fields(signal_card: dict, cap: dict) -> dict:
    """回傳新的訊號卡：報告欄 cap_components（P1、P2′、P3、P4′）與 price_to_conservative（現價÷IV_cons）
    一律取 cap_calc 結果（不影響上限判定，不需退回模型重做）；有變動者記入 framework_notes。"""
    out = copy.deepcopy(signal_card)
    notes: list[str] = list(out.get("framework_notes") or [])
    comps = cap.get("components") or {}
    cc = out.get("cap_components")
    if not isinstance(cc, dict):
        cc = {}
        out["cap_components"] = cc
    pairs = [(cc, card_key, f"cap_components.{card_key}", comps.get(cap_key)) for card_key, cap_key in _COMP_MAP]
    pairs.append((out, "price_to_conservative", "price_to_conservative", cap.get("price_to_cons")))
    for parent, key, label, new in pairs:
        old = parent.get(key)
        parent[key] = new
        if old != new:
            notes.append(f"cap_calc 覆寫 {label}：{old!r} → {new!r}")
    out["framework_notes"] = notes
    return out


def cap_inferred_keys(cap: dict) -> list[str]:
    """cap_calc 推導寫上訊號卡的數字時套用的推論門檻鍵（references/inferred_thresholds.md 的鍵名）。
    只依 cap_calc 結果判斷實際走到的分支；金融類的 IV_cons 可能來自覆寫或保險式，DCF 類鍵不代列（由模型列）。"""
    rd = cap.get("r_d")
    keys = [] if not isinstance(rd, (int, float)) else ["rd_floor"] if rd <= 0.10 + 1e-9 else ["rd_spread"]
    if cap.get("no_value_reason") is not None or not cap.get("components"):
        return keys   # 無法估值：只算了 r_d
    comps, fin = cap["components"], cap.get("p21_applicable") is False
    keys.append("grade_caps")   # P1、P3、IV0 上限、金融硬上限的等級係數
    keys += ["fin_hard_cap"] if fin else ["gimpl_cap", "g_term_impl", "terminal_cons", "cons_path_decay"]
    if not fin and ((cap.get("g0") or {}).get("cons") or 0) > 0:
        keys.append("g0_haircut")
    if not fin and cap.get("width_opt_over_cons") is not None:
        keys.append("range_width")
    if comps.get("P3_report_only") is not None:
        keys.append("stress_add")
    if comps.get("P4p") is not None:
        keys.append("p4_fin" if fin else "p4_soft_A" if cap.get("P4_multiplier") == 1.2 else None)   # A 級 ×1.2 實際套用才列
    keys.append("wpe_lines")
    if cap.get("near_zero_growth"):
        keys.append("near_zero_g")
    if cap.get("g9_reason_required"):
        keys.append("g9_growth_spread")
    if cap.get("auto_buy_cap") is not None:
        keys += ["cap_discount_k", "abc_consistency"]
    if cap.get("auto_buy_cap") is not None:
        keys.append("near_cap")       # D90：auto_buy_cap＝0.95×原始上限（VAL-045「距上限 <5%」明文化）實際套用
    if cap.get("sensitivity_cap_P3_plus4pp") is not None:
        keys.append("stress_sens")    # 敏感度利率（只作報告欄）；D90 起不再有翻轉測試
    return [k for k in keys if k]


def add_cap_inferred_keys(signal_card: dict, cap: dict, notes: list[str] | None = None) -> list[str]:
    """把 cap_inferred_keys 併入訊號卡 inferred_thresholds_used（就地；模型已列者保留在前、去重）。回傳新增的鍵。
    模型列的非字串元素：dict 有字串 "key" 取之，其餘略過（記入 notes），避免去重拋 TypeError 讓 FULL_B 失敗。"""
    cur = signal_card.get("inferred_thresholds_used")
    cur = [] if cur is None else list(cur) if isinstance(cur, list) else [cur]
    keep, dropped = [], []
    for k in cur:
        if isinstance(k, dict) and isinstance(k.get("key"), str):
            k = k["key"]
        (keep if isinstance(k, str) else dropped).append(k)
    cur = keep
    if dropped and notes is not None:
        notes.append(f"inferred_thresholds_used 略過非字串項目：{dropped!r}")
    added = [k for k in dict.fromkeys(cap_inferred_keys(cap)) if k not in cur]
    signal_card["inferred_thresholds_used"] = list(dict.fromkeys(cur)) + added
    return added


def apply_cap_override(signal_card: dict, cap: dict) -> dict:
    """回傳新的訊號卡：以 cap_calc 的數字覆寫上限、IV 與所有腳本欄位，並在 framework_notes 記錄有變動者的前後。

    FULL_B 每次都呼叫（M2_D9_06：上限與 IV 一致時，其餘腳本欄也不得留模型值）。
    不修改傳入的 signal_card。cap 中缺的鍵以 None 覆寫（計算結果為準）。
    inferred_thresholds_used 另補 cap_calc 套用的推論門檻鍵（cap_inferred_keys；模型已列者不動）。
    """
    out = set_cap_report_fields(signal_card, cap)
    notes: list[str] = list(out.get("framework_notes") or [])

    def set_field(path: str, parent: dict, key: str, new: Any) -> None:
        old = parent.get(key)
        parent[key] = new
        if old != new:   # FULL_B 每次都覆寫（M2_D9_06）：只記有變動者
            notes.append(f"cap_calc 覆寫 {path}：{old!r} → {new!r}")

    vps = out.get("value_per_share")
    if not isinstance(vps, dict):
        vps = {}
        out["value_per_share"] = vps
    for card_key, cap_key in _IV_MAP:
        set_field(f"value_per_share.{card_key}", vps, card_key, cap.get(cap_key))

    set_field("auto_buy_cap", out, "auto_buy_cap", cap.get("auto_buy_cap"))
    set_field("buy_price_cap", out, "buy_price_cap", cap.get("cap"))  # P2′ 硬上限
    set_field("sell_review_above", out, "sell_review_above", cap.get("IV_base"))  # SKILL §4：＝IV_base
    set_field("r_d", out, "r_d", cap.get("r_d"))
    set_field("price_to_cap", out, "price_to_cap", cap.get("price_to_auto_buy_cap"))  # 現價 ÷ auto_buy_cap（閘門 2 的比值）
    set_field("grade_used", out, "grade_used", cap.get("grade_used"))
    set_field("conclusion_test_flips", out, "conclusion_test_flips", False)   # D90：不再做翻轉測試，恆 false（欄位保留相容）
    set_field("implied_growth", out, "implied_growth", cap.get("g_impl"))
    set_field("g_impl_gt_cap", out, "g_impl_gt_cap", cap.get("g_impl_gt_cap"))
    warnings = out.get("warnings")
    if not isinstance(warnings, dict):
        warnings = {}
        out["warnings"] = warnings
    set_field("warnings.W_PE", warnings, "W_PE", cap.get("W_PE"))
    added = add_cap_inferred_keys(out, cap, notes)   # M2-D5-04：cap_calc 套用的推論門檻由框架代列
    if added:
        notes.append("框架補列 inferred_thresholds_used（cap_calc 套用）：" + "、".join(added))

    out["framework_notes"] = notes
    return out


# ---------------------------------------------------------------- 閘門降級（SKILL §0A）

def _is_delete(screen_decision: str | None) -> bool:
    if screen_decision is None:
        return False
    text = str(screen_decision).strip()
    return text.startswith("刪除") or "DELETE" in text.upper()


def gate2_failures(cap: dict) -> list[str]:
    """閘門 2（SKILL §0A）：現價 < 自動買進上限、等級 A／B。回傳不通過理由（空＝通過）。
    D90：結論測試已併入 auto_buy_cap（＝0.95×原始上限），不再另查 conclusion_test 翻轉。"""
    out = []
    abc, px = cap.get("auto_buy_cap"), cap.get("price")
    if abc is None:
        out.append("無自動買進上限")
    elif not isinstance(px, (int, float)) or not px < abc:
        out.append(f"現價 {px} 未低於自動買進上限 {abc}")
    if cap.get("grade_used") not in ("A", "B"):
        out.append(f"等級 {cap.get('grade_used')} 不是 A／B")
    # 不變式（A3-M2B-09）：荒謬級線（50×E＝50×price÷norm_pe）須高於自動買進上限與 IV_base；
    # 否則買進上限高於「至少賣一半」線＝norm_eps 與 oe 嚴重不一致 → 不通過（BUY 最多 WATCH）
    pe = cap.get("norm_pe")
    if _finite_num(px) and px > 0 and _finite_num(pe) and pe > 0:
        line = ABSURD_PE * px / pe
        low = [f"{n} {v:.4g}" for n, v in (("自動買進上限", abc), ("IV_base", cap.get("IV_base")))
               if _finite_num(v) and line <= v]
        if low:
            out.append(f"荒謬級線 {line:.4g}（{ABSURD_PE}×E）不高於" + "、".join(low)
                       + "：norm_eps 與 oe 嚴重不一致")
    return out


def cap_signal_by_gates(signal: str, gate3: dict | None, screen_decision: str | None,
                        cap: dict | None = None) -> tuple[str, list[str]]:
    """程式可判的自動買進閘門降級：閘門 3 未過或缺 → BUY 降 WATCH；初篩為刪除類 → BUY 降 WATCH；
    給 cap（cap_calc 結果）時另核閘門 2，不過 → BUY 降 WATCH。

    非 BUY 原樣回傳（WATCH 不變、NO_ACTION 不變）。回傳 (新訊號, 理由清單)。
    """
    if signal != "BUY":
        return signal, []
    reasons: list[str] = []
    if gate3 is None or gate3.get("gate3_pass") is not True:
        reasons.append("閘門 3 未過或缺：BUY 降為 WATCH（SKILL §0A）")
    if _is_delete(screen_decision):
        reasons.append(f"初篩為刪除類（{screen_decision}）：BUY 降為 WATCH（SKILL §0A 閘門 1）")
    if cap is not None:
        g2 = gate2_failures(cap)
        if g2:
            reasons.append("閘門 2 未過（" + "、".join(g2) + "）：BUY 降為 WATCH（SKILL §0A）")
    if reasons:
        return "WATCH", reasons
    return "BUY", []
