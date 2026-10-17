"""匯出（M6e）：CSV、JSON、XLSX。資料集與儀表板同源（metrics），不另算。"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from . import metrics as M

DATASETS = ("holdings", "orders", "fills", "cash", "corporate_actions", "analyses", "nav", "usage", "approvals",
            "anomalies")
FORMATS = ("csv", "json", "xlsx")


def _flat(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v


def dataset(conn, name: str) -> list[dict]:
    if name not in DATASETS:
        raise ValueError(f"資料集須為 {'、'.join(DATASETS)}")
    if name == "holdings":
        return M.holdings(conn)
    if name in ("orders", "fills", "cash", "corporate_actions"):
        return M.trades(conn)[name]
    if name == "analyses":
        return M.analyses(conn, limit=100000)
    if name == "nav":
        rows = {r["date"]: dict(r) for r in conn.execute(
            "SELECT date, nav, cash, positions_value, n_positions, benchmark_close FROM nav_daily")}
        return [{**rows[x["date"]], "account_index": x["account_index"], "spy_index": x["spy_index"]}
                for x in M.series(conn)]
    if name == "usage":
        return M.usage(conn)
    if name == "approvals":
        return [dict(r) for r in conn.execute("SELECT * FROM approvals ORDER BY approval_id")]
    return M.anomalies(conn)["open"]


def _columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    cols = _columns(rows)
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: _flat(r.get(k)) for k in cols})
    return "﻿" + buf.getvalue()  # BOM：Excel 開中文不亂碼


def to_xlsx(data: dict[str, list[dict]]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in data.items():
        ws = wb.create_sheet(name[:31])
        cols = _columns(rows)
        ws.append(cols)
        for r in rows:
            ws.append([_flat(r.get(k)) for k in cols])
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_json(data: dict[str, list[dict]]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def export(conn, names: list[str], fmt: str, out_dir: Path, stamp: str) -> list[Path]:
    """寫檔：csv 每個資料集一檔；json、xlsx 合成一檔。回傳寫出的路徑。"""
    if fmt not in FORMATS:
        raise ValueError(f"格式須為 {'、'.join(FORMATS)}")
    data = {n: dataset(conn, n) for n in names}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        paths = []
        for n, rows in data.items():
            p = out_dir / f"{n}_{stamp}.csv"
            p.write_text(to_csv(rows), encoding="utf-8")
            paths.append(p)
        return paths
    p = out_dir / f"berkshire_{stamp}.{fmt}"
    if fmt == "json":
        p.write_text(to_json(data), encoding="utf-8")
    else:
        p.write_bytes(to_xlsx(data))
    return [p]
