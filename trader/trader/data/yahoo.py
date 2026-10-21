"""Yahoo Finance 日線（免費、非官方）→ 與 AlpacaClient 相同介面的 bars()／corporate_actions()。

用途：研究用價格來源（skill 原本的 fetch_prices.py 即用 Yahoo）。只供初選與深度分析取價；
每日紙上撮合、限價、ADV 一律用 Alpaca（daily 遇到本客戶端直接拒絕；calendar() 拋 DataError 讓日曆退回快取／規則）。
啟用：設定檔 data.research_price_source: yahoo（make_clients 的 "prices" 客戶端；不會放進 "alpaca"，D34／M4-D7-1）。

口徑（與 Alpaca adjustment 對應）：
  split ＝ Yahoo close（已依抓取當下為止的全部拆股調整，未含股利）
  all   ＝ Yahoo adjclose（拆股＋股利）
  raw   ＝ split × 該日之後所有拆股比例的乘積（還原為當日實際成交價）
時點：只回傳 [start, end] 內的日線；拆股與股利只回傳 ex_date 在 [start, end] 內者。
快取：20 小時內有效，但抓取後若已過紐約 16:15（收盤）就重抓，盤中抓到的半根日線不會被當收盤沿用（M4-D7-4）。
股利：Yahoo dividends.amount 已依其後的拆股（含分拆係數）調整；cash_per_share 乘回 ex_date 之後的係數乘積，還原實付金額（M4-D1-17）。
分拆（spin-off）：Yahoo 把分拆的價格調整係數也放在 events.splits（FDX 1241:1000、SPGI 1.057…），close 也依它調整。
  corporate_actions 只把整數或簡單分數比例（skill screen.is_split_ratio）標為拆股；其他標 type="spin_off"
  （ratio 空、raw.yahoo_adj_factor 記係數），與 Alpaca 口徑一致，不進拆股字串、不進帳本自動換算（M1-D1-05／M4-D2-13）。
  純分拆的係數一定 >1；非簡單比例而 <1（HON 0.9535＝1:2 合股×分拆）表示混了合股，無法自動拆開 → raw.needs_human 標需人工。
  raw 日線仍乘回全部係數（還原當日實際成交價）。注意：split 口徑日線（daily_closes 用）在 Yahoo 含分拆調整
  （分拆日無價格斷層），在 Alpaca 不含。
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

from .http import DataError, atomic_write_text
from .prices_adapter import last_close_ny
from .universe import normalize_ticker

UA = "Mozilla/5.0 (berkshire-trader research)"
BAR_COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume", "vwap"]
CACHE_TTL_S = 20 * 3600


class YahooClient:
    feed = "yahoo"

    def __init__(self, cache_dir: Path | None = None, min_interval: float = 0.4):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.min_interval = min_interval
        self._last = 0.0
        self._mem: dict[str, dict] = {}

    # ---- 原始資料（含快取） ----
    def _chart(self, tk: str) -> dict | None:
        if tk in self._mem:
            return self._mem[tk]
        f = self.cache_dir / f"{tk}.json" if self.cache_dir else None
        if (f is not None and f.is_file() and time.time() - f.stat().st_mtime < CACHE_TTL_S
                and f.stat().st_mtime >= last_close_ny().timestamp()):   # 收盤前抓的快取在收盤後不沿用（M4-D7-4）
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:  # 半截快取（寫入中途被殺）→ 視同沒有快取，重抓
                d = None
            if d is not None:
                self._mem[tk] = d
                return d
        sym = tk.replace(".", "-")
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               "?range=10y&interval=1d&includeAdjustedClose=true&events=div%2Csplit")
        d = None
        for i in range(3):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = json.loads(r.read().decode("utf-8"))
                res = (body.get("chart") or {}).get("result")
                d = res[0] if res else None
                break
            except Exception:  # noqa: BLE001 - 重試後仍失敗 → 該檔無資料
                time.sleep(2 * (i + 1))
        if d is None:
            return None
        if f is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(f, json.dumps(d))
        self._mem[tk] = d
        return d

    @staticmethod
    def _splits(d: dict) -> list[tuple[str, float]]:
        out = []
        for v in ((d.get("events") or {}).get("splits") or {}).values():
            if v.get("denominator"):
                ex = str(pd.Timestamp(int(v["date"]), unit="s", tz="UTC").tz_convert("America/New_York").date())
                out.append((ex, float(v["numerator"]) / float(v["denominator"])))
        return sorted(out)

    @staticmethod
    def _frame(d: dict) -> pd.DataFrame:
        ts = d.get("timestamp") or []
        if not ts:
            return pd.DataFrame()
        q = d["indicators"]["quote"][0]
        adj = (d["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
        dates = pd.to_datetime(ts, unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
        return pd.DataFrame({"date": dates, "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                             "close": q.get("close"), "volume": q.get("volume"), "adj": adj}).dropna(subset=["close"])

    # ---- 與 AlpacaClient 相同的介面 ----
    def bars(self, symbols: list[str], start: str, end: str, adjustment: str = "raw",
             timeframe: str = "1Day") -> pd.DataFrame:
        rows: list[pd.DataFrame] = []
        for s in symbols:
            tk = normalize_ticker(s)
            if not tk:
                continue
            d = self._chart(tk)
            if not d:
                continue
            df = self._frame(d)
            if df.empty:
                continue
            df = df[(df["date"] >= start[:10]) & (df["date"] <= end[:10])].copy()
            if df.empty:
                continue
            if adjustment == "all":
                f = df["adj"] / df["close"]
                for c in ("open", "high", "low", "close"):
                    df[c] = df[c] * f
            elif adjustment == "raw":
                sp = self._splits(d)
                mult = df["date"].map(lambda x: float(pd.Series([r for e, r in sp if e > x]).prod()) if sp else 1.0)
                for c in ("open", "high", "low", "close"):
                    df[c] = df[c] * mult
                df["volume"] = df["volume"] / mult
            df["ticker"] = tk
            df["vwap"] = None
            rows.append(df[BAR_COLUMNS])
        if not rows:
            return pd.DataFrame(columns=BAR_COLUMNS)
        return pd.concat(rows, ignore_index=True).sort_values(["ticker", "date"]).reset_index(drop=True)

    def corporate_actions(self, symbols: list[str], start: str, end: str,
                          types: list[str] | None = None) -> list[dict]:
        from .prices_adapter import _skill   # 拆股判定只有一份（skill screen.py）
        is_split = _skill("screen").is_split_ratio
        out: list[dict] = []
        for s in symbols:
            tk = normalize_ticker(s)
            d = self._chart(tk) if tk else None
            if not d:
                continue
            for ex, ratio in self._splits(d):
                if start[:10] <= ex <= end[:10]:
                    split = is_split(ratio)
                    typ = ("forward_split" if ratio >= 1 else "reverse_split") if split else "spin_off"
                    out.append({"id": f"yahoo:{tk}:{'split' if split else 'spin_off'}:{ex}", "ticker": tk, "type": typ,
                                "ex_date": ex, "process_date": ex, "record_date": None, "payable_date": None,
                                "ratio_new_per_old": ratio if split else None, "cash_per_share": None, "new_ticker": None,
                                "raw": {"source": "yahoo"} if split else {"source": "yahoo", "yahoo_adj_factor": ratio,
                                        **({"needs_human": "係數 <1 且非簡單比例：可能同時有合股，股數需人工核對"}
                                           if ratio < 1 else
                                           {"needs_human": "係數介於 1 與 1.1 且非簡單比例：可能是小比例股票股利"
                                                           "（如 5%），不乘股數，需人工核對"}
                                           if ratio < 1.1 else {})}})   # A3-M4A-07
            sp = self._splits(d)
            for v in ((d.get("events") or {}).get("dividends") or {}).values():
                ex = str(pd.Timestamp(int(v["date"]), unit="s", tz="UTC").tz_convert("America/New_York").date())
                if start[:10] <= ex <= end[:10]:
                    mult = 1.0
                    for e, r in sp:   # 與 bars(raw) 相同：乘回 ex_date 之後的全部係數（M4-D1-17）
                        if e > ex:
                            mult *= r
                    out.append({"id": f"yahoo:{tk}:div:{ex}", "ticker": tk, "type": "cash_dividend",
                                "ex_date": ex, "process_date": ex, "record_date": None, "payable_date": None,
                                "ratio_new_per_old": None, "cash_per_share": float(v.get("amount") or 0) * mult,
                                "new_ticker": None, "raw": {"source": "yahoo"}})
        if types:
            out = [a for a in out if a["type"] in types]
        return sorted(out, key=lambda a: (a["ticker"], a["ex_date"]))

    def news(self, symbols: list[str], start: str, end: str) -> list[dict]:
        return []

    def calendar(self, start: str, end: str) -> list[dict]:
        raise DataError("Yahoo 客戶端不提供交易日曆（daily 流程請用 Alpaca）")   # TradingCalendar 退回快取／規則（M4-D7-1）
