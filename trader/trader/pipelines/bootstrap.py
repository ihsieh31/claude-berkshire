"""初選＋深度分析（M7）：bootstrap 與每季初選共用。

screen（整個股票池 → 交接檔 → 依 skip_rule 決定 to_analyze）→ analyze（每檔 DOSSIER → FULL_A → …）。
FULL_A／RED_TEAM 照同意閘門停在 awaiting_approval，由儀表板或 `trader approvals` 放行。
dry_run：只跑初選並列出將分析的代號（不建分析工作、不呼叫模型、不寫帳本）。
同 asof 初選已完成時不重跑（回 error），force=True 才重跑（舊目錄改名保留）；重跑後的分析改用新的
run_id（analyze-{asof}-r時間），否則同 run 已完成的工作不會重加、會靜默回傳上一輪決策（M1-RS8-01）。
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..config import Config
from ..paths import Paths
from .analyze import run_analyze
from .screen import ScreenError, run_screen


def _backfill_analyze_run_id(paths: Paths, conn, asof: str, run_id: str) -> None:
    f = paths.state / "screens" / asof / "summary.json" if paths is not None else None
    if f is None or not f.is_file():
        return
    s = json.loads(f.read_text(encoding="utf-8"))
    if s.get("analyze_run_id") == run_id:
        return
    s["analyze_run_id"] = run_id
    f.write_text(json.dumps(s, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if conn is not None:
        conn.execute("UPDATE runs SET summary=? WHERE run_id=?",
                     (json.dumps(s, ensure_ascii=False, default=str), f"screen-{asof}"))
        conn.commit()


def run_bootstrap(cfg: Config, paths: Paths, conn, asof: str, tickers: list[str] | None = None,
                  top: int = 15, dry_run: bool = False, kind: str = "bootstrap",
                  clients: dict[str, Any] | None = None, router=None, force: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"asof": asof, "dry_run": dry_run}
    run_id = None
    if tickers is None:
        try:
            scr = run_screen(cfg, paths, conn, asof, top=top, kind=kind, clients=clients, dry_run=dry_run,
                             **({"force": True} if force else {}))
        except ScreenError as e:   # 同 asof 已完成：不重跑（M1-D9-01）
            out["error"] = f"初選未執行：{e}"
            return out
        out["screen"] = {k: scr.get(k) for k in ("run_id", "shortlist", "to_analyze", "skipped_recent", "warning", "error")
                         if scr.get(k) is not None}
        if "error" in scr:
            out["error"] = f"初選失敗：{scr['error']}"
            return out
        tickers = [x["ticker"] for x in scr.get("to_analyze", [])]
        if force:   # 初選已重跑：分析也要重做，不得沿用 analyze-{asof} 已完成的工作（M1-RS8-01）
            run_id = f"analyze-{asof}-r{time.strftime('%Y%m%dT%H%M%S')}"
        if not dry_run:   # 回填本次分析 run_id，中斷後 `trader analyze --asof` 才接得上（A3-M1E-03）
            _backfill_analyze_run_id(paths, conn, asof, run_id or f"analyze-{asof}")
    out["tickers"] = tickers
    if dry_run:
        out["note"] = "dry-run：只跑初選，未建分析工作"
        return out
    if not tickers:
        out["note"] = "沒有需要完整分析的代號（皆在 12 個月內分析過）"
        return out
    out["analyze"] = run_analyze(cfg, paths, conn, asof, tickers=tickers, screen_asof=asof,
                                 clients=clients, router=router, **({"run_id": run_id} if run_id else {}))
    return out
