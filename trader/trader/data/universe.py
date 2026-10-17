"""S&P 500 股票池：override 優先、快取、下載、過期重抓與舊快取備援；名單變動才寫入 universe_versions。

代號標準形：大寫、點號（`BRK.B`）。下載結果少於 MIN_UNIVERSE 檔視為來源異常，不寫快取。
"""
from __future__ import annotations

import csv
import json
import hashlib
import io
import time
from datetime import datetime, timezone
from pathlib import Path

from trader import db
from trader.data.http import DataError, HttpClient
from trader.paths import Paths

SP500_SOURCES = ["https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"]
MIN_UNIVERSE = 450  # 少於此檔數視為來源異常
OVERRIDE_NAME = "universe_override.txt"
CACHE_NAME = "sp500.csv"


def normalize_ticker(s: str) -> str:
    """去頭尾空白、大寫，並把 `-`、`/` 換成 `.`（`BRK-B`→`BRK.B`）。"""
    return str(s).strip().upper().replace("-", ".").replace("/", ".")


def _parse_override(text: str) -> list[str]:
    """每行一檔；`#` 之後為註解，空行略過。"""
    out = []
    for line in text.splitlines():
        code = line.split("#", 1)[0].strip()
        if code:
            out.append(normalize_ticker(code))
    return sorted(set(out))


def _parse_csv(text: str) -> list[str]:
    """讀取 CSV 的 Symbol 欄位，回傳標準化、排序去重後的代號。"""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Symbol" not in reader.fieldnames:
        raise DataError("股票池 CSV 缺少 Symbol 欄位")
    out = [normalize_ticker(r["Symbol"]) for r in reader if (r.get("Symbol") or "").strip()]
    return sorted(set(out))


def _age_seconds(path: Path) -> float:
    return time.time() - path.stat().st_mtime


def _record_version(conn, tickers: list[str], source: str) -> None:
    """名單（排序後以換行串接）的 sha256 與最後一列不同才新增一列。"""
    digest = hashlib.sha256("\n".join(tickers).encode("utf-8")).hexdigest()
    row = conn.execute("SELECT sha256 FROM universe_versions ORDER BY version_id DESC LIMIT 1").fetchone()
    if row is not None and row["sha256"] == digest:
        return
    db.insert(conn, "universe_versions", {
        "asof": datetime.now(timezone.utc).date().isoformat(),
        "source": source,
        "tickers": tickers,
        "sha256": digest,
        "created_at": db.now_iso(),
    })


def _download(http: HttpClient) -> list[str]:
    """依序試各來源；每個來源都失敗或名單過短則丟出 DataError。"""
    errors = []
    for url in SP500_SOURCES:
        try:
            text = http.get(url).text
            tickers = _parse_csv(text)
            if len(tickers) < MIN_UNIVERSE:
                raise DataError(f"股票池來源異常：只有 {len(tickers)} 檔（< {MIN_UNIVERSE}）")
            return tickers, text
        except DataError as e:
            errors.append(str(e))
    raise DataError("股票池下載全部失敗：" + "；".join(errors))


def load_universe(paths: Paths, conn, http: HttpClient | None = None, refresh: bool = False,
                  max_age_days: float = 7, asof: str | None = None) -> tuple[list[str], str]:
    """回傳 (代號清單，來源)；來源為 override | download | cache | cache_stale | version:{日期} | 前述＋"_not_pit"。

    asof 早於今天（補跑過去日期）：改用 universe_versions 中 asof ≤ 該日的最後一版（時點正確）；
    沒有那麼早的版本 → 仍用目前名單，來源加註 "_not_pit"（成分股可能含之後才納入者）。
    """
    override = paths.state / OVERRIDE_NAME
    if override.exists():
        tickers = _parse_override(override.read_text(encoding="utf-8"))
        if not tickers:
            raise DataError(f"{OVERRIDE_NAME} 沒有有效代號")
        _record_version(conn, tickers, "override")
        return tickers, "override"

    today = datetime.now(timezone.utc).date().isoformat()
    if asof is not None and asof < today:
        row = conn.execute("SELECT asof, tickers FROM universe_versions WHERE asof <= ? "
                           "ORDER BY asof DESC, version_id DESC LIMIT 1", (asof,)).fetchone()
        if row is not None:
            return list(json.loads(row["tickers"])), f"version:{row['asof']}"
        tickers, src = load_universe(paths, conn, http, refresh, max_age_days)
        return tickers, src + "_not_pit"

    cache = paths.cache / "universe" / CACHE_NAME
    if cache.exists() and not refresh and _age_seconds(cache) < max_age_days * 86400:
        tickers = _parse_csv(cache.read_text(encoding="utf-8"))
        _record_version(conn, tickers, "cache")
        return tickers, "cache"

    own_http = http is None
    client = http or HttpClient(min_interval=0.0, retries=2, base_delay=1.0)
    try:
        try:
            tickers, text = _download(client)
        except DataError:
            # 下載失敗：有舊快取就用舊快取（來源標 cache_stale），否則把錯誤往上丟
            if not cache.exists():
                raise
            tickers = _parse_csv(cache.read_text(encoding="utf-8"))
            _record_version(conn, tickers, "cache_stale")
            return tickers, "cache_stale"
    finally:
        if own_http:
            client.close()

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    _record_version(conn, tickers, "download")
    return tickers, "download"
