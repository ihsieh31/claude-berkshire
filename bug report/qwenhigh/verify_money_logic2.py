"""第二批佐證（唯讀；只 import 專案原始碼，不寫入專案任何檔案、不碰 state/ 資料庫）。

V1 閘門欄位型別分裂：write_price_result 存「通過」字串，report/validation 的 _truthy 只認 True/"true"
   → 每一筆合法 PRICE 買進都被檢討報告標成流程違規；portfolio/sizing 卻認得「通過」。
V2 金融股硬上限其實是軟上限 P4′：checks.py:177 註解宣稱「P2′ 硬上限」，但 financial=true 時 cap=P4′。
V3 自動買進上限對「最樂觀的歷史成長」1:1 線性：max(hist) 提高多少，上限就提高多少 → 沒有安全邊際。
V4 `norm_eps or oe`：norm_eps 恰為 0 時無聲退回 oe（falsy 0），荒謬級線因此跟着錯。
V5 g_impl_checks（隱含成長 b／c 兩題）由 cap_calc 算出後，框架完全沒有.reader → 閘門 2 只看 a 題。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path("/Users/zongen/Downloads/claude code/berkshire")
PKG = ROOT / "trader"
SKILL = ROOT / "berkshire-deep-merged"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


sys.path.insert(0, str(PKG))
from trader.portfolio import sizing as S            # noqa: E402
from trader.report import validation as V           # noqa: E402
from trader.analysis import checks as C             # noqa: E402


def cap_calc():
    sys.path.insert(0, str(SKILL / "scripts"))
    return importlib.import_module("cap_calc")


def hr(t: str) -> None:
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


# ---------------------------------------------------------------- V1
hr("V1 同一欄位三種真值標準（decisions.gate2_price_below_cap）")
# write_price_result() 的寫入形狀（writeback.py:545-546，gate2 為字串、gate3 為 bool）
for gate2 in ("通過", "不通過", "無上限"):
    log = {"action": "BUY", "gate1_not_deleted": True, "gate2_price_below_cap": gate2,
           "gate3_pass": True, "review_type": "PRICE"}
    report_violation = [k for k in V.GATE_KEYS if not V._truthy(log.get(k))]      # validation.py:64
    order_blocked = S.gate_failures(log, None)                                     # sizing.py:52
    print(f"  gate2={gate2:<6} 檢討報告判違規={report_violation or '無'}"
          f"｜下單端 gate_failures={order_blocked or '全部通過（可下單）'}")
print("  → 「通過」在下單端是真、在檢討報告端是假：合規的買進必然被報成違規，報告 §1 永遠不乾淨。")

# ---------------------------------------------------------------- V2
hr("V2 金融股：cap 落在被規則書宣稱『不進硬上限』的 P4′")
cc = cap_calc()
base = {"long_rate": 0.045, "oe": 10.0, "norm_eps": 10.0, "price": 100.0, "grade": "A",
        "g10": 0.06, "g5": 0.05, "largecap_pe_median": 21.0, "fin_pe_median": 15.0,
        "financial": True, "roe": 0.2, "eps_cagr5": 0.05}
r = cc.calc(dict(base))
print(f"  financial=true：cap={r['cap']}  auto_buy_cap={r['auto_buy_cap']}  "
      f"P2′={r['components']['P2_prime']}  P4′={r['components']['P4p']}  P1(報告欄)={r['components']['P1_report_only']}")
print(f"  cap_calc.py:150 註解＝『P4′ 為軟上限（規則書 P-19）——不進硬上限』；"
      f"checks.py:177 卻把這個值寫成 buy_price_cap（註解『P2′ 硬上限』）")
print(f"  sell_review_above 覆寫值＝IV_base={r['IV_base']}")

base2 = dict(base, financial=True, largecap_pe_median=None, fin_pe_median=None)  # 金融＋完全沒有本益比中位
r2 = cc.calc(base2)
print(f"  金融且無任何 P/E 中位：P2′={r2['components']['P2_prime']} P4′={r2['components']['P4p']} "
      f"cap={r2['cap']}（＝P1 報告欄 {r2['components']['P1_report_only']}）"
      f" → cap_calc.py:153 fallback 把『只供報告』的 P1 当成硬上限")

# ---------------------------------------------------------------- V3
hr("V3 上限對『最樂觀歷史成長』的線性依賴（非金融，A 級，r_d=10%）")
iv0 = base["oe"] / max(0.10, base["long_rate"] + 0.03)
print(f"  IV0（零成長價值）= {iv0:.2f}；規則書要求歷史成長≤0 時上限只能用 IV0×等級上限 = {0.85 * iv0:.2f}")
rows = []
for g10, g5 in [(0.02, 0.02), (0.04, 0.04), (0.06, 0.06), (0.08, 0.08), (0.10, 0.10), (0.12, 0.12),
                (-0.02, 0.01), (-0.02, -0.05)]:
    x = cc.calc(dict(base, financial=False, g10=g10, g5=g5, largecap_pe_median=21.0))
    rows.append((g10, g5, x["g0"]["base"], x["IV_base"], x["auto_buy_cap"], 0.85 * iv0))
print("   g10     g5    基準g0   IV_base  auto_buy_cap  規則書應有上限(0.85×IV0)")
for g10, g5, gb, ivb, abc, should in rows:
    print(f"  {g10:+.2f}  {g5:+.2f}   {gb:+.3f}  {ivb:8.2f}  {abc:9.2f}   {should:8.2f}   "
          f"高出 {abc / should - 1:+.0%}")
print("  → 基準情境 g0＝min(max(hist),10%)（cap_calc.py:87）：歷史最好的那個數字直接決定買進上限，"
      "且 IV_base 就是上限本身（一致性條款收緊後），買在上限＝已為未來 10 年最佳歷史成長付-full 價。")

# ---------------------------------------------------------------- V4
hr("V4 `norm_eps or oe` 的 falsy 0")
for ne in (None, 0.0, 0.05):
    e = (ne if ne else base["oe"])          # review_jobs.py:235 與 cap_calc.py:75 的寫法
    print(f"  norm_eps={ne!r:<6} → 實際使用的 E = {e}，荒謬級線 50×E = {50 * e}")
print("  → 正常化每股盈餘 legitimately 為 0 的公司，50×E 沿用未正常化的 oe；oe≤0 時荒謬級線 ≤0，"
      "任何正股價都 > 該線 → SELL_HALF（review_jobs.py:_map_action）。")

# ---------------------------------------------------------------- V5
hr("V5 g_impl_checks 無讀者：閘門 2 只檢查 a 題")
x = cc.calc(dict(base, financial=False, g10=-0.02, g5=0.01, price=118.0))
print(f"  輸入：g10=-2%／g5=+1%（保守 g0＝{x['g0']['cons']:+.3%}）｜現價 118｜auto_buy_cap={x['auto_buy_cap']:.2f}")
print("  cap_calc 輸出：", json.dumps({"price": x["price"], "auto_buy_cap": x["auto_buy_cap"],
                                      "g_impl": x["g_impl"], "g_impl_checks": x.get("g_impl_checks"),
                                      "grade_used": x["grade_used"],
                                      "flips": x.get("conclusion_test", {}).get("flips")},
                                     ensure_ascii=False))
# review_jobs.PriceHandler.post 的閘門 2 條件（原始碼逐字重現）
cap, price = x, x["price"]
gate2 = "通過" if (price < cap["auto_buy_cap"] and cap["grade_used"] in ("A", "B")
                   and not (cap.get("conclusion_test") or {}).get("flips")) else "不通過"
ch = cap.get("g_impl_checks") or {}
print(f"  框架 gate2 = {gate2}｜但 b／c 題結果 = {ch}")
print("  → trader/ 內 g_impl_checks 零個參考點（writeback.py:531-535 未持久化，"
      "review_jobs.py:352-360 未使用）：『隱含成長不得高於歷史／不得高於保守 g0』兩題完全靠模型自律。")

# apply_cap_override 会不会把它補回來？
card = {"auto_buy_cap": 50.0, "buy_price_cap": 50.0, "sell_review_above": 50.0, "value_per_share": {},
        "implied_growth": 0.02, "warnings": {}}
ov = C.apply_cap_override(card, cap)
print("  apply_cap_override 覆寫後的鍵：", sorted(k for k in ov if "g_impl" in k or "cap" in k))
print("  → 覆寫把模型的數字換成 cap_calc 的數字（含 V2 的軟上限），但也不會補上 b／c 題的把關。")
