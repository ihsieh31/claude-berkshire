"""FRED 資料來源：30 年期公債殖利率（DGS30）；G8 用的 3 個月國庫券（DGS3MO）與非金融公司權益 ÷ GDP（NCBEILQ027S、GDP）。

有 API 金鑰走 JSON API；無金鑰走公開 CSV。快取過期重抓；抓失敗而有快取時用快取。
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from trader.data.http import DataError, HttpClient

API_URL = "https://api.stlouisfed.org/fred/series/observations"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
OBS_START = "1977-01-01"
LONG_START = {"NCBEILQ027S": "1945-01-01", "GDP": "1945-01-01"}   # 權益 ÷ GDP 分位要用全部歷史
Z1_LAG_DAYS = 170          # 推論：季初日期 + 170 天 ≈ 季末後 10 週 Z.1 公布


def _num(s: str | None) -> float | None:
    """FRED 缺值以 "." 表示；空字串與無法解析者同樣視為缺值。"""
    if s is None:
        return None
    s = str(s).strip()
    if s in ("", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _years_before(asof: str, n: int) -> str:
    """asof 往前 n 年的日期字串（2/29 退為 2/28）。"""
    d = date.fromisoformat(asof)
    try:
        return d.replace(year=d.year - n).isoformat()
    except ValueError:
        return d.replace(year=d.year - n, day=28).isoformat()


class FredClient:
    def __init__(self, api_key: str | None, cache_dir: Path, http: HttpClient | None = None,
                 max_age_hours: float = 20):
        self.api_key = api_key or None  # 空字串視同沒有金鑰
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_age_s = max_age_hours * 3600
        # 金鑰只放在 params；錯誤訊息經 secrets 遮罩
        self.http = http or HttpClient(min_interval=0.2, secrets=[self.api_key])

    # ---- 抓取 ----
    def _fetch_api(self, series_id: str) -> list[list]:
        data = self.http.get_json(API_URL, params={
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": LONG_START.get(series_id, OBS_START),
        })
        if not isinstance(data, dict) or "observations" not in data:
            raise DataError(f"FRED 回應缺少 observations：{series_id}")
        obs = []
        for x in data["observations"]:
            v = _num(x.get("value"))
            if v is not None:
                obs.append([str(x["date"]), v])
        return obs

    def _fetch_csv(self, series_id: str) -> list[list]:
        text = self.http.get(CSV_URL, params={"id": series_id}).text
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or len(header) < 2:
            raise DataError(f"FRED CSV 欄位不符：{series_id}")
        # 日期欄為第一欄（observation_date 或 DATE）；數值欄以序列代號定位，找不到則取第二欄
        val_idx = header.index(series_id) if series_id in header else 1
        obs = []
        for row in reader:
            if len(row) <= val_idx:
                continue
            v = _num(row[val_idx])
            if v is not None:
                obs.append([row[0], v])
        return obs

    # ---- 快取與序列 ----
    @staticmethod
    def _to_series(obs: list[list]) -> pd.Series:
        if not obs:
            return pd.Series(dtype=float)
        s = pd.Series([v for _, v in obs], index=[d for d, _ in obs], dtype=float)
        return s.sort_index()

    def series(self, series_id: str = "DGS30") -> pd.Series:
        """回傳 float Series：index 為 YYYY-MM-DD 字串（升冪），值為原始百分比（如 4.75）。"""
        path = self.cache_dir / f"{series_id}.json"
        if path.exists() and (time.time() - path.stat().st_mtime) < self.max_age_s:
            return self._to_series(json.loads(path.read_text(encoding="utf-8")))
        try:
            obs = self._fetch_api(series_id) if self.api_key else self._fetch_csv(series_id)
            if not obs:
                raise DataError(f"FRED 無有效觀測值：{series_id}")
        except Exception:
            # 抓取失敗：有舊快取就用舊快取，否則把錯誤往上拋
            if path.exists():
                return self._to_series(json.loads(path.read_text(encoding="utf-8")))
            raise
        path.write_text(json.dumps(obs), encoding="utf-8")
        return self._to_series(obs)

    # ---- 時點查詢（只用 asof 以前的資料，嚴格小於） ----
    def t30(self, asof: str) -> tuple[float, str]:
        """最後一個日期 < asof 的 30 年期殖利率，回傳 (小數, 日期)；例如 4.75% → 0.0475。"""
        s = self.series("DGS30")
        s = s[s.index < asof]
        if s.empty:
            raise DataError(f"DGS30 在 {asof} 前無資料")
        return float(s.iloc[-1]) / 100, str(s.index[-1])

    def t30_median_20y(self, asof: str) -> float:
        """[asof 前 20 年, asof) 區間日值的中位數，小數。"""
        s = self.series("DGS30")
        start = _years_before(asof, 20)
        w = s[(s.index >= start) & (s.index < asof)]
        if w.empty:
            raise DataError(f"DGS30 在 {start} 至 {asof} 間無資料")
        return float(w.median()) / 100


    def latest(self, series_id: str, asof: str) -> tuple[float, str]:
        """最後一個日期 < asof 的原始值與日期（不換算單位）。"""
        s = self.series(series_id)
        s = s[s.index < asof]
        if s.empty:
            raise DataError(f"{series_id} 在 {asof} 前無資料")
        return float(s.iloc[-1]), str(s.index[-1])

    def equity_gdp(self, asof: str, lag_days: int = Z1_LAG_DAYS) -> pd.Series:
        """非金融公司權益（NCBEILQ027S，百萬美元）÷ 名目 GDP（十億美元）的季序列，小數（2.55 = 255%）。

        只留「季初日期 + lag_days < asof」的季度：FRED 以季初標日期，Z.1 約在季末後 10 週公布（推論：取 170 天）。
        注意 FRED 給的是修正後數值，不是當時版本；只供當期使用，回測不可用。"""
        eq, gdp = self.series("NCBEILQ027S"), self.series("GDP")
        df = pd.concat({"eq": eq, "gdp": gdp}, axis=1).dropna()
        cutoff = (date.fromisoformat(asof) - timedelta(days=lag_days)).isoformat()
        df = df[df.index < cutoff]
        if df.empty:
            raise DataError(f"NCBEILQ027S／GDP 在 {cutoff} 前無資料")
        return (df["eq"] / 1000.0) / df["gdp"]
