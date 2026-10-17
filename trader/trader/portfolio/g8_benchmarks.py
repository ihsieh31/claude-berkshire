"""G8 比較表的國庫券與大盤兩列（procedure/06 §2）。框架計算，模型照抄。

- 短期國庫券：FRED DGS3MO（3 個月，決策日前最後一筆），確定性係數 1.0。
- 大盤：P-27 分區預期報酬（依 P-66 本資料庫衍生數列「非金融公司權益 ÷ GDP」分區），扣摩擦後 × 0.8。
  分區（rulebook P-66 (b)）：≤60% 低、60–120% 中、120–150% 高、>150% 極高；
  主判準為自身歷史擴張視窗分位（≥75 高、≥90 極高），與絕對切點衝突取較高一區。
  P-27：低 10–12%、中 5–8%（隨區內位置遞減）、高 3–5%、極高 ≤3%；扣摩擦約 0.5–2pp。
  區內取值與摩擦（推論，未回測）：低 11%；中 8%→5%、高 5%→3% 依比值線性；極高 3%；
  分位把區別往上推時（比值不在該區絕對範圍）取該區上緣；摩擦取 1pp；下限 0。
MKT-010：這個數字只用在組合層比較（G8、排序），不得代入個股估值改買價。
"""
from __future__ import annotations

from typing import Any

TREASURY_SERIES = "DGS3MO"
MARKET_SERIES = "非金融公司權益 ÷ GDP（FRED NCBEILQ027S ÷ GDP；P-66 衍生數列）"
TREASURY_COEF = 1.0
MARKET_COEF = 0.8
FRICTION = 0.01                      # 推論：P-27「扣摩擦約 0.5–2pp」取 1pp
ZONES = ("低", "中", "高", "極高")
CUTS = (0.60, 1.20, 1.50)            # P-66 (b) 絕對切點
PCT_HIGH, PCT_EXTREME = 75.0, 90.0   # 主判準：擴張視窗分位


def _abs_zone(r: float) -> str:
    if r <= CUTS[0]:
        return "低"
    if r <= CUTS[1]:
        return "中"
    if r <= CUTS[2]:
        return "高"
    return "極高"


def _lerp(r: float, lo: float, hi: float, v_lo: float, v_hi: float) -> float:
    t = min(max((r - lo) / (hi - lo), 0.0), 1.0)
    return v_lo + (v_hi - v_lo) * t


def p27_nominal(ratio: float, zone: str) -> float:
    """分區對應的名目 10 年預期報酬（小數）。"""
    if zone == "低":
        return 0.11
    if zone == "極高":
        return 0.03
    if zone == "中":
        return _lerp(ratio, CUTS[0], CUTS[1], 0.08, 0.05) if _abs_zone(ratio) == "中" else 0.05
    return _lerp(ratio, CUTS[1], CUTS[2], 0.05, 0.03) if _abs_zone(ratio) == "高" else 0.05  # 高


def market_row(series) -> dict[str, Any]:
    """series：equity_gdp 的季序列（index 日期字串升冪，值為小數）。"""
    r = float(series.iloc[-1])
    pct = round(float((series <= r).mean() * 100), 1)  # 擴張視窗：全部可得歷史（含本季）
    az = _abs_zone(r)
    pz = "極高" if pct >= PCT_EXTREME else "高" if pct >= PCT_HIGH else None
    zone = max([az] + ([pz] if pz else []), key=ZONES.index)
    nominal = round(p27_nominal(r, zone), 4)
    after = round(max(nominal - FRICTION, 0.0), 4)
    return {"series": MARKET_SERIES, "quarter": str(series.index[-1]), "ratio": round(r, 4),
            "history_start": str(series.index[0]), "percentile": pct, "zone_absolute": az, "zone_percentile": pz,
            "zone": zone, "p27_nominal": nominal, "friction": FRICTION, "expected_return": after,
            "certainty_coef": MARKET_COEF, "adjusted": round(after * MARKET_COEF, 4)}


def benchmarks(fred, asof: str) -> dict[str, Any]:
    """{treasury, market}；取不到的一列給 {"error": …}，模型照填 null。"""
    out: dict[str, Any] = {}
    if fred is None:
        err = {"error": "未設定 FRED 用戶端"}
        return {"treasury": dict(err), "market": dict(err)}
    try:
        v, d = fred.latest(TREASURY_SERIES, asof)
        out["treasury"] = {"series": TREASURY_SERIES, "date": d, "expected_return": round(v / 100, 4),
                           "certainty_coef": TREASURY_COEF, "adjusted": round(v / 100 * TREASURY_COEF, 4)}
    except Exception as e:  # noqa: BLE001 - 缺資料只標註
        out["treasury"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["market"] = market_row(fred.equity_gdp(asof))
    except Exception as e:  # noqa: BLE001
        out["market"] = {"error": f"{type(e).__name__}: {e}"}
    return out
