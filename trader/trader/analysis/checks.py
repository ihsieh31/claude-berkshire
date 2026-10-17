"""深度分析的程式檢查：閘門 3、買價上限計算、訊號卡與計算結果一致性、閘門降級。

規則來源：`berkshire-deep-merged/SKILL.md` §0A、§3、§4。
計算一律匯入 skill 腳本（`scripts/cap_calc.py`、`scripts/gate3_check.py`），不在此複製公式。
"""
from __future__ import annotations

import copy
import importlib
import json
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


def run_gate3(answers_path: Path) -> dict:
    """驗證閘門 3 作答檔。檔案不存在、JSON 壞或內容無法判讀 → 回傳未通過（不拋例外）。"""
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
        return gate3.evaluate(ans)
    except Exception as e:  # 失敗關閉：內容壞就不通過，不讓它變成通過
        return _gate3_fail(f"閘門 3 作答檔內容無法判讀：{type(e).__name__}: {e}", path)


# ---------------------------------------------------------------- 買價上限

ABSURD_PE = 50          # 荒謬級線 ＝ 50 × 正常化每股盈餘 E（SKILL §4；cap_calc 不輸出此值）
GRADE_ORDER = ("A", "B", "C", "D")


def absurd_line(cap_input: dict) -> float | None:
    """荒謬級線 ＝ 50 × E（E＝norm_eps，缺用 oe）；E ≤0 或缺 → None（無荒謬級線，不得變成 0 或負數而誤觸）。"""
    e = cap_input.get("norm_eps") or cap_input.get("oe")
    if isinstance(e, bool) or not isinstance(e, (int, float)) or e <= 0:
        return None
    return round(ABSURD_PE * float(e), 4)


def worse_grade(*grades: Any) -> str | None:
    """較保守（較低）的等級；不認得的值略過，全不認得回 None。"""
    known = [g for g in grades if g in GRADE_ORDER]
    return max(known, key=GRADE_ORDER.index) if known else None


# cap_input 合理範圍（推論：只擋單位或小數點錯誤，例如 g 填 8 而非 0.08、oe 以百萬元計；不做業務判斷）
_RATE_FIELDS = {"g10": (-1, 1), "g5": (-1, 1), "long_rate": (0, 0.2), "roe": (-1, 1), "eps_cagr5": (-1, 1)}
PRICE_TO_OE_RANGE = (1, 500)


def cap_input_problems(x: dict) -> list[str]:
    """cap_input 欄位超出合理範圍的清單（空＝通過）。oe ≤ 0 交給 cap_calc（無法估值），不在此擋。"""
    out = []
    for k, (lo, hi) in _RATE_FIELDS.items():
        v = x.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi):
            out.append(f"{k}={v!r} 不在 [{lo}, {hi}]（比率請用小數，例如 0.08）")
    if x.get("grade") not in GRADE_ORDER:
        out.append(f"grade={x.get('grade')!r} 不是 A/B/C/D")
    px, oe = x.get("price"), x.get("oe")
    if not isinstance(px, (int, float)) or isinstance(px, bool) or px <= 0:
        out.append(f"price={px!r} 必須是正數")
    elif isinstance(oe, (int, float)) and not isinstance(oe, bool) and oe > 0:
        lo, hi = PRICE_TO_OE_RANGE
        if not lo <= px / oe <= hi:
            out.append(f"price/oe={px / oe:.4g} 不在 [{lo}, {hi}]（oe 應為每股金額）")
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


def apply_cap_override(signal_card: dict, cap: dict) -> dict:
    """回傳新的訊號卡：以 cap_calc 的數字覆寫上限與 IV 欄位，並在 framework_notes 記錄覆寫前後。

    不修改傳入的 signal_card。cap 中缺的鍵以 None 覆寫（計算結果為準），並記入 framework_notes。
    """
    out = copy.deepcopy(signal_card)
    notes: list[str] = list(out.get("framework_notes") or [])

    def set_field(path: str, parent: dict, key: str, new: Any) -> None:
        old = parent.get(key)
        parent[key] = new
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
    set_field("price_to_conservative", out, "price_to_conservative", cap.get("price_to_cons"))
    set_field("price_to_cap", out, "price_to_cap", cap.get("price_to_cap"))
    set_field("implied_growth", out, "implied_growth", cap.get("g_impl"))
    set_field("g_impl_gt_cap", out, "g_impl_gt_cap", cap.get("g_impl_gt_cap"))
    warnings = out.get("warnings")
    if not isinstance(warnings, dict):
        warnings = {}
        out["warnings"] = warnings
    set_field("warnings.W_PE", warnings, "W_PE", cap.get("W_PE"))

    out["framework_notes"] = notes
    return out


# ---------------------------------------------------------------- 閘門降級（SKILL §0A）

def _is_delete(screen_decision: str | None) -> bool:
    if screen_decision is None:
        return False
    text = str(screen_decision).strip()
    return text.startswith("刪除") or "DELETE" in text.upper()


def gate2_failures(cap: dict) -> list[str]:
    """閘門 2（SKILL §0A）：現價 < 自動買進上限、等級 A／B、結論測試不翻轉。回傳不通過理由（空＝通過）。"""
    out = []
    abc, px = cap.get("auto_buy_cap"), cap.get("price")
    if abc is None:
        out.append("無自動買進上限")
    elif not isinstance(px, (int, float)) or not px < abc:
        out.append(f"現價 {px} 未低於自動買進上限 {abc}")
    if cap.get("grade_used") not in ("A", "B"):
        out.append(f"等級 {cap.get('grade_used')} 不是 A／B")
    if (cap.get("conclusion_test") or {}).get("flips"):
        out.append("結論測試翻轉")
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
