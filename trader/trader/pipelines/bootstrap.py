"""初選＋深度分析（M7）：bootstrap 與每季初選共用。

screen（整個股票池 → 交接檔 → 依 skip_rule 決定 to_analyze）→ analyze（每檔 DOSSIER → FULL_A → …）。
FULL_A／RED_TEAM 照同意閘門停在 awaiting_approval，由儀表板或 `trader approvals` 放行。
dry_run：只跑初選並列出將分析的代號（不建分析工作、不呼叫模型、不寫帳本）。
"""
from __future__ import annotations

from typing import Any

from ..config import Config
from ..paths import Paths
from .analyze import run_analyze
from .screen import run_screen


def run_bootstrap(cfg: Config, paths: Paths, conn, asof: str, tickers: list[str] | None = None,
                  top: int = 15, dry_run: bool = False, kind: str = "bootstrap",
                  clients: dict[str, Any] | None = None, router=None) -> dict[str, Any]:
    out: dict[str, Any] = {"asof": asof, "dry_run": dry_run}
    if tickers is None:
        scr = run_screen(cfg, paths, conn, asof, top=top, kind=kind, clients=clients)
        out["screen"] = {k: scr.get(k) for k in ("run_id", "shortlist", "to_analyze", "skipped_recent", "warning", "error")
                         if scr.get(k) is not None}
        if "error" in scr:
            out["error"] = f"初選失敗：{scr['error']}"
            return out
        tickers = [x["ticker"] for x in scr.get("to_analyze", [])]
    out["tickers"] = tickers
    if dry_run:
        out["note"] = "dry-run：只跑初選，未建分析工作"
        return out
    if not tickers:
        out["note"] = "沒有需要完整分析的代號（皆在 12 個月內分析過）"
        return out
    out["analyze"] = run_analyze(cfg, paths, conn, asof, tickers=tickers, screen_asof=asof,
                                 clients=clients, router=router)
    return out
