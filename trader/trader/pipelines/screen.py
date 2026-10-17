"""季度初選（純程式，不呼叫模型）。

流程：抓整個股票池的輸入（凍結當月 P/E 中位）→ 由登錄表與使用者檔產生 exclusions.csv →
以子程序執行 skill 的 scripts/screen.py（規則以 skill 為準，不複製）→ 讀交接檔 →
比對登錄表決定哪些代號要做完整分析 → 寫出去價格版交接檔 → 記錄 run。

輸出目錄：state/screens/{asof}/（inputs/、exclusions.csv、screen.csv／.md／_handoff.json、
handoff_noprice.json、summary.json）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .. import db
from ..config import Config
from ..data.inputs import fetch_inputs
from ..data.universe import normalize_ticker
from ..jobs.sandbox import _has_price_key, scrub_prices
from ..paths import Paths

EXCL_COLS = ["excl_asset_class", "excl_competence", "excl_unvaluable", "excl_fraud", "excl_recent_reject"]
USER_EXCLUSIONS = "exclusions.csv"     # state/exclusions.csv：使用者或深度分析回寫的 X1–X5
REJECT_STATUSES = {"TOO_HARD", "REJECT"}
FULL_REVIEW_MAX_DAYS = 365             # 12 個月內做過完整分析者不重做（除非已到 next_full_review_due）


class ScreenError(Exception):
    """初選無法完成（輸入缺、skill 腳本失敗、交接檔格式不符）。"""


def _registry(conn) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT ticker, status, data FROM registry"):
        d = json.loads(r["data"]) if r["data"] else {}
        d.setdefault("status", r["status"])
        out[normalize_ticker(r["ticker"])] = d
    return out


def _days_before(d: str | None, asof: str) -> int | None:
    if not d:
        return None
    try:
        return (date.fromisoformat(asof) - date.fromisoformat(str(d)[:10])).days
    except ValueError:
        return None


def build_exclusions(paths: Paths, registry: dict[str, dict], asof: str) -> pd.DataFrame:
    """合併使用者 exclusions.csv 與登錄表推得的 X5（12 個月內完整分析判 TOO_HARD／REJECT）。

    「無重大新事實」由觸發檢查（M4）判斷：有新事實會重新分析並改寫登錄表狀態，X5 隨之解除。
    """
    rows: dict[str, dict] = {}
    f = paths.state / USER_EXCLUSIONS
    if f.exists():
        user = pd.read_csv(f, dtype={"ticker": str})
        if "ticker" not in user:
            raise ScreenError(f"{f} 缺 ticker 欄")
        for _, r in user.iterrows():
            t = normalize_ticker(str(r["ticker"]))
            rows[t] = {c: str(r.get(c, "")).strip().lower() in ("1", "1.0", "true", "yes", "y") for c in EXCL_COLS}
    for t, d in registry.items():
        age = _days_before(d.get("last_full_review_date"), asof)
        if d.get("status") in REJECT_STATUSES and age is not None and 0 <= age < FULL_REVIEW_MAX_DAYS:
            rows.setdefault(t, {c: False for c in EXCL_COLS})["excl_recent_reject"] = True
    df = pd.DataFrame([{"ticker": t, **v} for t, v in sorted(rows.items())], columns=["ticker"] + EXCL_COLS)
    return df


def needs_full(d: dict | None, asof: str) -> tuple[bool, str]:
    """是否需要完整分析；回傳 (是否, 理由)。"""
    if not d:
        return True, "新進（登錄表無紀錄）"
    if d.get("status") == "MISSING_DATA":
        return True, "上次缺資料（routing skip_rule：MISSING_DATA 不跳過）"
    age = _days_before(d.get("last_full_review_date"), asof)
    if age is None:
        return True, "從未完整分析"
    if age >= FULL_REVIEW_MAX_DAYS:
        return True, f"上次完整分析已 {age} 天"
    due = d.get("next_full_review_due")
    if due and str(due)[:10] <= asof:
        return True, f"已到下次完整分析日 {str(due)[:10]}"
    return False, f"{age} 天前已完整分析"


def dedupe_share_classes(features: Path, outdir: Path, registry: dict[str, dict]) -> tuple[Path, list[dict]]:
    """同一 CIK 多個股票類別（GOOG／GOOGL、FOX／FOXA、NWS／NWSA）只留一列，避免同一家公司占兩個名額。
    保留：已在登錄表者；否則代號字母序最後者（推論：多為流動性較高的 A 類或無投票權類）。
    沒有重複 → 回傳原檔。"""
    df = pd.read_csv(features, dtype={"cik": str, "ticker": str})
    if "cik" not in df or not df["cik"].notna().any():
        return features, []
    dropped: list[dict] = []
    keep_idx = []
    for cik, g in df.groupby(df["cik"].fillna(df["ticker"]), sort=False):
        if len(g) == 1:
            keep_idx.append(g.index[0])
            continue
        ts = list(g["ticker"])
        known = [t for t in ts if normalize_ticker(t) in registry]
        pick = known[0] if known else sorted(ts)[-1]
        keep_idx.append(g.index[ts.index(pick)])
        dropped += [{"ticker": t, "kept": pick, "cik": cik} for t in ts if t != pick]
    if not dropped:
        return features, []
    out = outdir / "features_dedup.csv"
    df.loc[sorted(keep_idx)].to_csv(out, index=False)
    return out, dropped


def run_skill_screen(paths: Paths, features: Path, prices: Path, exclusions: Path | None, asof: str,
                     out_prefix: Path, top: int, special_max: int) -> None:
    script = paths.skill_screen / "scripts" / "screen.py"
    cmd = [sys.executable, str(script), "--features", str(features), "--prices", str(prices), "--asof", asof,
           "--top", str(top), "--special-max", str(special_max), "--out", str(out_prefix)]
    if exclusions is not None:
        cmd += ["--exclusions", str(exclusions)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise ScreenError(f"skill screen.py 失敗（{p.returncode}）：{(p.stderr or p.stdout)[-1500:]}")


def run_screen(cfg: Config, paths: Paths, conn, asof: str, top: int = 15, special_max: int = 0,
               inputs_dir: Path | None = None, kind: str = "quarterly_screen",
               clients: dict[str, Any] | None = None) -> dict[str, Any]:
    """執行初選並回傳摘要。inputs_dir 給定時沿用該目錄的 features.csv／prices.csv（不重抓）。"""
    run_id = f"screen-{asof}"
    outdir = paths.state / "screens" / asof
    outdir.mkdir(parents=True, exist_ok=True)
    if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        db.insert(conn, "runs", {"run_id": run_id, "kind": kind, "asof": asof, "status": "running",
                                 "started_at": db.now_iso()})
    else:
        conn.execute("UPDATE runs SET status='running', started_at=?, finished_at=NULL WHERE run_id=?",
                     (db.now_iso(), run_id))
        conn.commit()
    summary: dict[str, Any] = {"run_id": run_id, "asof": asof, "outdir": str(outdir)}
    try:
        if inputs_dir is None:
            inputs_dir = outdir / "inputs"
            summary["fetch"] = fetch_inputs(cfg, paths, conn, asof, inputs_dir, tickers=None, freeze_pe=True,
                                            clients=clients)
        features, prices = inputs_dir / "features.csv", inputs_dir / "prices.csv"
        missing = [p.name for p in (features, prices) if not p.exists()]
        if missing:
            raise ScreenError(f"缺輸入 {missing}（需要 SEC 與 Alpaca 兩者）")

        registry = _registry(conn)
        features, dup = dedupe_share_classes(features, outdir, registry)
        if dup:
            summary["share_class_dedup"] = dup
        excl = build_exclusions(paths, registry, asof)
        excl_path = None
        if len(excl):
            excl_path = outdir / "exclusions.csv"
            excl.to_csv(excl_path, index=False)
        summary["exclusions"] = {c: int(excl[c].sum()) for c in EXCL_COLS if len(excl)}

        prefix = outdir / "screen"
        run_skill_screen(paths, features, prices, excl_path, asof, prefix, top, special_max)
        hand = json.loads((outdir / "screen_handoff.json").read_text(encoding="utf-8"))
        if hand.get("asof") != asof or not isinstance(hand.get("core_shortlist"), list):
            raise ScreenError("交接檔格式不符或決策日不一致")

        noprice = scrub_prices(hand)
        if _has_price_key(noprice):  # 防禦：scrub 後不得殘留
            raise ScreenError("去價格交接檔仍含價格鍵")
        (outdir / "handoff_noprice.json").write_text(json.dumps(noprice, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")

        picks = [(r["ticker"], "core") for r in hand["core_shortlist"]] + \
                [(r["ticker"], "special") for r in hand.get("special_handoff", [])]
        to_analyze, skipped = [], []
        for t, lane in picks:
            need, why = needs_full(registry.get(normalize_ticker(t)), asof)
            (to_analyze if need else skipped).append({"ticker": t, "lane": lane, "reason": why})
        summary.update({
            "counts": hand.get("counts", {}),
            "shortlist": [r["ticker"] for r in hand["core_shortlist"]],
            "special_handoff": [r["ticker"] for r in hand.get("special_handoff", [])],
            "to_analyze": to_analyze,
            "skipped_recent": skipped,
        })
        if len(hand["core_shortlist"]) < top:
            summary["warning"] = f"短名單只有 {len(hand['core_shortlist'])} 檔（< {top}）"
        status = "done"
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        status = "failed"
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")
    conn.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
                 (status, db.now_iso(), json.dumps(summary, ensure_ascii=False, default=str), run_id))
    conn.commit()
    return summary
