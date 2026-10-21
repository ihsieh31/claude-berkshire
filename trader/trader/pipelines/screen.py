"""季度初選（純程式，不呼叫模型）。

流程：抓整個股票池的輸入（凍結當月 P/E 中位）→ 由登錄表與使用者檔產生 exclusions.csv →
以子程序執行 skill 的 scripts/screen.py（規則以 skill 為準，不複製）→ 讀交接檔 →
比對登錄表決定哪些代號要做完整分析 → 寫出去價格版交接檔 → 記錄 run。

輸出目錄：state/screens/{asof}/（inputs/、exclusions.csv、screen_overrides.csv、screen.csv／.md／_handoff.json、
handoff_noprice.json、decisions.json、decisions_price.json（只給價格檔）、summary.json）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .. import db
from ..config import Config
from ..data.inputs import MAX_SEC_STALE_RATIO, MIN_COVERAGE, UNIVERSE_FILE, _read_universe_file, coverage, fetch_inputs
from ..data.universe import normalize_ticker
from ..jobs.sandbox import _has_price_key, scrub_prices
from ..paths import Paths

EXCL_COLS = ["excl_asset_class", "excl_competence", "excl_unvaluable", "excl_fraud", "excl_recent_reject"]
USER_EXCLUSIONS = "exclusions.csv"     # state/exclusions.csv：使用者或深度分析回寫的 X1–X5
USER_OVERRIDES = "screen_overrides.csv"  # state/screen_overrides.csv：ticker,date,reason；人工撤銷結構性刪除（D62）
REJECT_STATUSES = {"TOO_HARD", "REJECT"}
FULL_REVIEW_MAX_DAYS = 365             # 12 個月內做過完整分析者不重做（除非已到 next_full_review_due）
DECISIONS = "decisions.json"
DECISIONS_PRICE = "decisions_price.json"   # 全池 ticker → T3／T5 價格類困境碼；只給價格檔（D71、M1-R6-02）
NOTE_COLS = ("data_notes", "unrated_fields", "dq_note",   # 與短名單交接同樣帶給手動／觸發代號（M1-R5-04）
             "fy_last_filed", "max_filed_used", "mcap_shares_source")   # 非價格的資料時點與股數來源（A3-M1C-11）
UNSCREENED = "未篩"
PRICE_DISTRESS = ("T3_", "T5_")        # 價格類困境碼（D71）：不進前半，不影響 screen_decision
X5_CODE = "X5_"                        # 近期已淘汰：被觸發或手動重評本身即例外，不算閘門 1 的刪除（P39R）


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


X5_EXEMPT_ORIGINS = {"hark_b"}  # 登錄表 data.origin：B 機（Hark）結果，不納入 X5
_TRUE = ("1", "1.0", "true", "yes", "y")


def _x5(d: dict, asof: str) -> bool:
    age = _days_before(d.get("last_full_review_date"), asof)
    return (d.get("status") in REJECT_STATUSES and age is not None and 0 < age < FULL_REVIEW_MAX_DAYS
            and d.get("origin") not in X5_EXEMPT_ORIGINS)


def _integrity(d: dict) -> bool:
    """誠信淘汰（NEVER-04(a)，登錄表 integrity_reject）：現任決策者在位期間不重評，不設 12 個月年限（D27、A3-M1B-01）；
    他機（hark_b）結論不鎖死（ASK-2）。"""
    return bool(d.get("integrity_reject")) and d.get("origin") not in X5_EXEMPT_ORIGINS


NOT_THIS_SKILL = "NOT_THIS_SKILL"      # 深度分析停止碼：不屬本 skill 的資產類別 → X1（M1-D2-12）


def _x1(d: dict, asof: str) -> bool:
    """登錄表推得的 X1：本機 12 個月內深度分析判 NOT_THIS_SKILL；他機（hark_b）結論不鎖死（ASK-2＝A），
    滿 12 個月失效、再被選入就重新完整分析（M1-RS2-01）。"""
    age = _days_before(d.get("last_full_review_date"), asof)
    return (d.get("last_reason_code") == NOT_THIS_SKILL and age is not None and 0 < age < FULL_REVIEW_MAX_DAYS
            and d.get("origin") not in X5_EXEMPT_ORIGINS)


def build_exclusions(paths: Paths, registry: dict[str, dict], asof: str) -> pd.DataFrame:
    """合併使用者 exclusions.csv 與登錄表推得的 X5（12 個月內完整分析判 TOO_HARD／REJECT）與 X1
    （登錄表 last_reason_code＝NOT_THIS_SKILL：深度分析判定不屬本 skill 的資產類別，初篩即刪、短名單遞補；
    比照 X5：分析日須早於 asof、12 個月內有效、origin 在 X5_EXEMPT_ORIGINS 者不設，M1-D2-12／M1-RS2-01）。

    「無重大新事實」由觸發檢查判斷：兩週觀察名單排程也帶入 TOO_HARD／REJECT（review.WATCH_RUN_EXTRA），重大 8-K
    （P-62／D27；新年報不算）→ FULL 重新分析並改寫登錄表狀態，X5 隨之解除（M1-D2-04）。同一代號多列時旗標取 OR（M1-D7-1）。
    分析日不早於 asof（age<=0：同日或之後才做的分析）不是 asof 當時已知的資訊，不設 X5（M1-D6-02）。
    登錄表 data 的來源標記 origin 在 X5_EXEMPT_ORIGINS 者（他機產生、搬來當初始資料）不進 X5
    （使用者 2026-10-18 ASK-2）；本機的完整結果與停止結果（含持股停止分支）重建登錄表時標記消失；
    舊資料日分支（保留較新的他機結論）、非分析性停止（D87）與 touch_review 保留標記（A3-M8B-07）。
    """
    rows: dict[str, dict] = {}
    f = paths.state / USER_EXCLUSIONS
    if f.exists():
        user = pd.read_csv(f, dtype={"ticker": str})
        if "ticker" not in user:
            raise ScreenError(f"{f} 缺 ticker 欄")
        for _, r in user.iterrows():
            t = normalize_ticker(str(r["ticker"]))
            old = rows.get(t, {})
            rows[t] = {c: old.get(c, False) or str(r.get(c, "")).strip().lower() in _TRUE for c in EXCL_COLS}
    for t, d in registry.items():
        if _x5(d, asof):
            rows.setdefault(t, {c: False for c in EXCL_COLS})["excl_recent_reject"] = True
        if _x1(d, asof):
            rows.setdefault(t, {c: False for c in EXCL_COLS})["excl_asset_class"] = True
        if _integrity(d):   # X4 誠信紅旗：不看年限（A3-M1B-01）
            rows.setdefault(t, {c: False for c in EXCL_COLS})["excl_fraud"] = True
    df = pd.DataFrame([{"ticker": t, **v} for t, v in sorted(rows.items())], columns=["ticker"] + EXCL_COLS)
    return df


def needs_full(d: dict | None, asof: str) -> tuple[bool, str]:
    """是否需要完整分析；回傳 (是否, 理由)。

    他機（origin 在 X5_EXEMPT_ORIGINS）的 REJECT／TOO_HARD／NOT_THIS_SKILL 不鎖死：再被初選選入就在本機重新完整分析
    （使用者 2026-10-18 ASK-2；M1-RS2-01）；他機的其他狀態照一般規則，直接當初始資料。"""
    if not d:
        return True, "新進（登錄表無紀錄）"
    if _integrity(d):   # A3-M1B-01：不因滿 12 個月或 MISSING_DATA 重做 FULL；重大 8-K 5.02 由兩週排程處理
        return False, "誠信淘汰（NEVER-04(a)），現任決策者在位不重評"
    if d.get("status") == "MISSING_DATA":
        return True, "上次缺資料（routing skip_rule：MISSING_DATA 不跳過）"
    if d.get("origin") in X5_EXEMPT_ORIGINS and (d.get("status") in REJECT_STATUSES
                                                 or d.get("last_reason_code") == NOT_THIS_SKILL):
        return True, "他機淘汰結論，本機重新完整分析"
    age = _days_before(d.get("last_full_review_date"), asof)
    if age is None:
        return True, "從未完整分析"
    if age <= 0:  # 同日或晚於 asof 的分析不是 asof 當時已知的資訊（M1-D6-02）
        return True, "登錄表紀錄不早於 asof，非時點"
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


def merge_share_class_exclusions(excl: pd.DataFrame, dropped: list[dict]) -> pd.DataFrame:
    """被 dedupe 去掉的類別（GOOG）的 X1–X5 旗標 OR 到留下的代號（GOOGL），否則寫在另一類別的誠信紅旗會失效（M1-D7-1）。"""
    kept = {normalize_ticker(d["ticker"]): normalize_ticker(d["kept"]) for d in dropped}
    if excl.empty or not kept:
        return excl
    e = excl.assign(ticker=excl["ticker"].map(lambda t: kept.get(t, t)))
    return e.groupby("ticker", as_index=False)[EXCL_COLS].any()


def load_overrides(paths: Paths, asof: str, dropped: list[dict], ignored: list[dict] | None = None) -> pd.DataFrame:
    """state/screen_overrides.csv（ticker,date,reason）：date ≤ asof 者生效（時點），被去掉的類別對映到留下的代號（D62）。
    同代號多列取 date 最新者（與檔案列順序無關）；date 空白、格式錯或晚於 asof 的列不傳給 skill，附理由加進 ignored
    （呼叫端寫 summary.overrides_ignored；A3-M1B-02）。"""
    f = paths.state / USER_OVERRIDES
    cols = ["ticker", "date", "reason"]
    if not f.exists():
        return pd.DataFrame(columns=cols)
    o = pd.read_csv(f, dtype=str).fillna("")
    if not set(cols) <= set(o.columns):
        raise ScreenError(f"{f} 需有 {cols} 欄")
    kept = {normalize_ticker(d["ticker"]): d["kept"] for d in dropped}
    o["ticker"] = o["ticker"].map(lambda t: kept.get(normalize_ticker(t), normalize_ticker(t)))
    age = o["date"].map(lambda d: _days_before(d, asof))
    ok = age.map(lambda a: not pd.isna(a) and a >= 0)
    if ignored is not None:
        for (_, r), a in zip(o.loc[~ok].iterrows(), age[~ok]):
            why = ("date 空白（宿主要求 date）" if not str(r["date"]).strip() else
                   "date 格式錯" if pd.isna(a) else "date 晚於 asof（時點）")
            ignored.append({"ticker": r["ticker"], "date": r["date"], "reason": r["reason"], "ignored": why})
    o = o.loc[ok, cols].assign(_d=o.loc[ok, "date"].map(lambda d: str(d)[:10]))
    return o.sort_values("_d", kind="stable").drop_duplicates("ticker", keep="last")[cols].sort_index()


def _decision(delete_codes: str, distress_codes: str) -> str:
    """閘門 1 用的 screen_decision。X5 只擋初選再選入；已被觸發或手動要求重評的代號不因 X5 判刪除（delete_codes 仍保留 X5）。"""
    gate = ";".join(c for c in delete_codes.split(";") if c and not c.startswith(X5_CODE))
    if gate:
        return f"DELETE（{gate}）"
    return "PASS-困境" if distress_codes else "PASS"


def write_decisions(outdir: Path, csv: Path, dropped: list[dict]) -> dict:
    """全池 ticker → 非價格判定（delete／T1·T2·T4 困境／旗標／資料註記），給非短名單代號的完整分析補 screen_decision
    與資料註記（M1-D7-4、M1-R5-04）。T3／T5 另寫 decisions_price.json，只給價格檔（D71、M1-R6-02）。"""
    df = pd.read_csv(csv, dtype={"ticker": str})
    df = df.reindex(columns=list(dict.fromkeys(list(df.columns) + ["delete_codes", "distress_codes", "flag_codes",
                                                                   *NOTE_COLS]))).fillna("")
    out, price = {}, {}
    for r in df.itertuples():
        codes = [c for c in str(r.distress_codes).split(";") if c]
        dist = ";".join(c for c in codes if not c.startswith(PRICE_DISTRESS))
        t = normalize_ticker(r.ticker)
        out[t] = {"screen_decision": _decision(str(r.delete_codes), dist),
                  "delete_codes": str(r.delete_codes), "distress_codes": dist, "flag_codes": str(r.flag_codes),
                  **{k: str(getattr(r, k)) for k in NOTE_COLS}}
        if pc := ";".join(c for c in codes if c.startswith(PRICE_DISTRESS)):
            price[t] = pc
    for d in dropped:   # 同公司另一類別沿用留下代號的判定
        k, t = normalize_ticker(d["kept"]), normalize_ticker(d["ticker"])
        if k in out:
            out[t] = {**out[k], "share_class_of": k}
        if k in price:
            price[t] = price[k]
    (outdir / DECISIONS).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / DECISIONS_PRICE).write_text(json.dumps(price, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def screen_context_for(paths: Paths, asof: str, ticker: str, note: str) -> dict:
    """非短名單代號的 screen_context：12 個月內最近一次（asof−365 天 < 初篩日 ≤ asof、成功完成）且股票池含該代號的
    初篩全池判定；較新一次沒有該代號就往較早的找，note 寫來源日（A3-M1A-03）。都查不到 → 未篩（M1-D7-4；D17：閘門 1 通過但記警示）。"""
    t = normalize_ticker(ticker)
    root = paths.state / "screens"
    days = sorted((d.name for d in root.iterdir() if d.is_dir() and len(d.name) == 10 and d.name <= asof[:10]
                   and (_days_before(d.name, asof) or 0) < FULL_REVIEW_MAX_DAYS),
                  reverse=True) if root.is_dir() else []
    missing: list[str] = []
    for day in days:
        f, sm = root / day / DECISIONS, root / day / "summary.json"
        if not (f.is_file() and sm.is_file()) or "error" in json.loads(sm.read_text(encoding="utf-8")):
            continue
        d = json.loads(f.read_text(encoding="utf-8")).get(t)
        if d is None:   # 該次股票池無此代號：往較早（仍在 12 個月內）的初篩找（D17、A3-M1A-03）
            missing.append(day)
            continue
        extra = f"（較新的 {'、'.join(missing)} 初篩股票池無此代號）" if missing else ""
        return {"ticker": t, **d, "screen_asof": day, "note": f"{note}；初篩判定取自 {day} 全池{extra}"}
    if missing:
        return {"ticker": t, "screen_decision": UNSCREENED,
                "note": f"{note}；12 個月內的初篩（{'、'.join(missing)}）股票池皆無此代號"}
    return {"ticker": t, "screen_decision": UNSCREENED, "note": f"{note}；12 個月內無可用的初篩紀錄"}


def screen_price_context_for(paths: Paths, ctx: dict) -> dict | None:
    """screen_context_for 所用那次初篩中該代號的 T3／T5（decisions_price.json）；有才回傳，呼叫端放進
    params['price_meta']['screen_price_context']，只進價格檔、不進 screen_context（D71、M1-R6-02）。"""
    day = ctx.get("screen_asof")
    f = paths.state / "screens" / str(day) / DECISIONS_PRICE if day else None
    if f is None or not f.is_file():
        return None
    codes = json.loads(f.read_text(encoding="utf-8")).get(normalize_ticker(ctx.get("ticker") or ""))
    return {"price_distress_codes": codes} if codes else None


def run_skill_screen(paths: Paths, features: Path, prices: Path, exclusions: Path | None, asof: str,
                     out_prefix: Path, top: int, special_max: int, overrides: Path | None = None,
                     universe_note: str | None = None) -> None:
    script = paths.skill_screen / "scripts" / "screen.py"
    cmd = [sys.executable, str(script), "--features", str(features), "--prices", str(prices), "--asof", asof,
           "--top", str(top), "--special-max", str(special_max), "--out", str(out_prefix)]
    if exclusions is not None:
        cmd += ["--exclusions", str(exclusions)]
    if overrides is not None:
        cmd += ["--overrides", str(overrides)]
    if universe_note:
        cmd += ["--universe-note", universe_note]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise ScreenError(f"skill screen.py 失敗（{p.returncode}）：{(p.stderr or p.stdout)[-1500:]}")


def run_screen(cfg: Config, paths: Paths, conn, asof: str, top: int = 15, special_max: int = 0,
               inputs_dir: Path | None = None, kind: str = "quarterly_screen",
               clients: dict[str, Any] | None = None, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """執行初選並回傳摘要。inputs_dir 給定時沿用該目錄的 features.csv／prices.csv（不重抓）。

    同 asof 已完成（runs done 且有 summary.json）→ ScreenError，force=True 才重跑（M1-D9-01）。
    重跑或前次失敗／中斷留下的 screens/{asof}/ 先整個改名為 {asof}.bak-{時間} 保留，不原地覆寫（M1-D9-05）。"""
    run_id = f"screen-{asof}"
    outdir = paths.state / "screens" / asof
    row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    analyze_run_id = f"analyze-{asof}"   # 本次初選對應的分析 run（A3-M1E-03；bootstrap 決定後回填）
    pin_dropped = None
    if outdir.is_dir() and any(outdir.iterdir()):
        prev_done = row is not None and row["status"] == "done" and (outdir / "summary.json").is_file()
        if prev_done and _prev_dry_run(outdir / "summary.json"):
            prev_done = False   # 前次是 dry-run：視同未完成，改名後重跑（A3-M1E-02）
        if prev_done and not force:
            raise ScreenError(f"{asof} 的初選已完成（{outdir}）；重跑會換掉交接與 to_analyze，確定要重跑請加 --force"
                              "（舊目錄會改名保留）")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        bak, n = outdir.with_name(f"{asof}.bak-{stamp}"), 1
        while bak.exists():
            bak, n = outdir.with_name(f"{asof}.bak-{stamp}-{n}"), n + 1
        rel = None
        if inputs_dir is not None and Path(inputs_dir).resolve().is_relative_to(outdir.resolve()):
            rel = Path(inputs_dir).resolve().relative_to(outdir.resolve())
        outdir.rename(bak)
        if prev_done:   # --force 重跑已完成的初選：分析也要新 run，不沿用 analyze-{asof} 已完成的工作（M1-RS8-01）
            analyze_run_id = f"analyze-{asof}-r{stamp}"
        if rel is not None:  # 沿用的輸入在舊目錄內：複製回新目錄（analyze 讀 screens/{asof}/inputs）
            shutil.copytree(bak / rel, outdir / rel)
        elif inputs_dir is None and (bak / "inputs" / UNIVERSE_FILE).is_file():  # 重抓也沿用上次的股票池（M1-D9-04）
            old = _read_universe_file(bak / "inputs" / UNIVERSE_FILE)
            if old is not None and "override" in old[1]:   # 測試／人工名單不釘選，重新載入股票池（A3-M1E-04）
                pin_dropped = old[1]
            else:
                (outdir / "inputs").mkdir(parents=True)
                shutil.copy2(bak / "inputs" / UNIVERSE_FILE, outdir / "inputs" / UNIVERSE_FILE)
    outdir.mkdir(parents=True, exist_ok=True)
    if row is None:
        db.insert(conn, "runs", {"run_id": run_id, "kind": kind, "asof": asof, "status": "running",
                                 "started_at": db.now_iso()})
    else:
        conn.execute("UPDATE runs SET status='running', started_at=?, finished_at=NULL WHERE run_id=?",
                     (db.now_iso(), run_id))
        conn.commit()
    summary: dict[str, Any] = {"run_id": run_id, "asof": asof, "outdir": str(outdir),
                               "dry_run": bool(dry_run),   # 排程 _last_screen 只認非 dry_run（M8-R3-02）
                               "analyze_run_id": analyze_run_id}
    if pin_dropped:
        summary["universe_pin_dropped"] = f"上次股票池來源 {pin_dropped}（override 名單），本次不沿用、重新載入"
    try:
        if inputs_dir is None:
            inputs_dir = outdir / "inputs"
            summary["fetch"] = fetch_inputs(cfg, paths, conn, asof, inputs_dir, tickers=None, freeze_pe=True,
                                            clients=clients)
            stale_ratio = summary["fetch"].get("sec_stale_ratio") or 0
            if stale_ratio > MAX_SEC_STALE_RATIO:   # SEC 失敗／封鎖、大量用舊快取：不產出名單，run failed 讓排程重試（A3-M1E-01）
                raise ScreenError(f"SEC 舊快取比例 {stale_ratio:.1%} > {MAX_SEC_STALE_RATIO:.0%}（SEC 抓取失敗或封鎖），不產出名單")
            if summary["fetch"].get("sec_blocked"):   # 封鎖中但比例小（封鎖在抓取尾段）：P/E 已不凍結，同樣失敗重試
                raise ScreenError(f"SEC 封鎖中（舊快取比例 {stale_ratio:.1%}），不產出名單")
        features, prices = inputs_dir / "features.csv", inputs_dir / "prices.csv"
        missing = [p.name for p in (features, prices) if not p.exists()]
        if missing:
            raise ScreenError(f"缺輸入 {missing}（需要 SEC 與 Alpaca 兩者）")
        fdf = pd.read_csv(features, dtype={"ticker": str})
        pdf = pd.read_csv(prices, dtype={"ticker": str})
        cov = coverage(fdf, pdf, len(set(fdf["ticker"]) | set(pdf["ticker"])))
        summary["coverage"] = round(cov, 4)
        if cov < MIN_COVERAGE:  # SEC 或價格大量失敗：不產出殘缺名單，run 標 failed 讓排程重試（M1-D7-2）
            raise ScreenError(f"輸入覆蓋率 {cov:.1%} < {MIN_COVERAGE:.0%}（有價格且有財報的代號太少），不產出名單")

        src = (summary.get("fetch") or {}).get("universe_source")
        uni = _read_universe_file(inputs_dir / UNIVERSE_FILE)
        summary.update({"universe_source": src or (uni[1] if uni else None),   # M1-D5-06（--inputs 時讀 universe.txt）
                        "n_universe": len(uni[0]) if uni else len(set(fdf["ticker"]) | set(pdf["ticker"])),
                        "top": top, "special_max": special_max})
        from ..analysis.writeback import skill_hashes_file   # 延遲匯入
        summary["skill_hashes"] = skill_hashes_file(paths)

        registry = _registry(conn)
        features, dup = dedupe_share_classes(features, outdir, registry)
        if dup:
            summary["share_class_dedup"] = dup
        excl = merge_share_class_exclusions(build_exclusions(paths, registry, asof), dup)
        excl_path = outdir / "exclusions.csv"
        if len(excl):
            excl.to_csv(excl_path, index=False)
        else:
            excl_path.unlink(missing_ok=True)   # 不留上次的舊檔誤導人工（M1-D5-08）
            excl_path = None
        summary["exclusions"] = {c: int(excl[c].sum()) for c in EXCL_COLS if len(excl)}
        summary["x5"] = [{"ticker": t, "status": d.get("status"), "last_full_review_date": d.get("last_full_review_date"),
                          "expires": (date.fromisoformat(str(d["last_full_review_date"])[:10])
                                      + timedelta(days=FULL_REVIEW_MAX_DAYS)).isoformat()}
                         for t, d in sorted(registry.items()) if _x5(d, asof)]
        summary["x1_registry"] = [   # 登錄表推得的 X1（NOT_THIS_SKILL）與到期日（A3-M1C-03）
            {"ticker": t, "last_reason_code": d.get("last_reason_code"),
             "last_full_review_date": d.get("last_full_review_date"),
             "expires": (date.fromisoformat(str(d["last_full_review_date"])[:10])
                         + timedelta(days=FULL_REVIEW_MAX_DAYS)).isoformat()}
            for t, d in sorted(registry.items()) if _x1(d, asof)]
        summary["x4_integrity"] = [   # 登錄表 integrity_reject → X4，不設年限（A3-M1B-01）
            {"ticker": t, "status": d.get("status"), "last_full_review_date": d.get("last_full_review_date")}
            for t, d in sorted(registry.items()) if _integrity(d)]
        ignored: list[dict] = []
        ov = load_overrides(paths, asof, dup, ignored)
        if ignored:
            summary["overrides_ignored"] = ignored
        ov_path = None
        if len(ov):
            ov_path = outdir / USER_OVERRIDES
            ov.to_csv(ov_path, index=False)

        prefix = outdir / "screen"
        note = f"股票池：{summary['universe_source'] or '未知'}，{summary['n_universe']} 檔；決策日 {asof}"
        run_skill_screen(paths, features, prices, excl_path, asof, prefix, top, special_max, ov_path, note)
        hand = json.loads((outdir / "screen_handoff.json").read_text(encoding="utf-8"))
        if hand.get("asof") != asof or not isinstance(hand.get("core_shortlist"), list):
            raise ScreenError("交接檔格式不符或決策日不一致")

        noprice = scrub_prices(hand)
        noprice.pop("special_watch", None)   # 只列觀察、含道名與統計排除理由（價格衍生），不給下游
        if _has_price_key(noprice):  # 防禦：scrub 後不得殘留
            raise ScreenError("去價格交接檔仍含價格鍵")
        leak = [r["ticker"] for k in ("core_shortlist", "special_handoff") for r in noprice.get(k) or []
                if any(c.startswith(PRICE_DISTRESS) for c in str(r.get("distress_codes") or "").split(";"))
                or "lane" in r or "exclude_reasons" in r]
        if leak:  # D71：價格類困境碼與道名只能在 price_context
            raise ScreenError(f"去價格交接檔仍含價格類困境碼或道名：{leak}")
        (outdir / "handoff_noprice.json").write_text(json.dumps(noprice, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")

        write_decisions(outdir, outdir / "screen.csv", dup)
        csv = pd.read_csv(outdir / "screen.csv", dtype={"ticker": str})
        dl = csv[csv["delete_codes"].fillna("") != ""]
        review = []   # D62：值得人工複核的刪除（金融股、歷史經濟力佳、有資料品質註記）
        for r in dl.itertuples():
            if not any(c.startswith("J") for c in str(r.delete_codes).split(";")):
                continue   # screen_overrides 只撤銷 J*，只有 X1–X5 的刪除列了也撤不掉（M1-RS2-03）
            why = [w for w, hit in (("金融", getattr(r, "fin_like", 0) == 1),
                                    ("歷史經濟力佳", bool(getattr(r, "strong_history", False))),
                                    ("資料品質註記", isinstance(getattr(r, "dq_note", None), str) and r.dq_note))
                   if hit]
            if why:
                review.append({"ticker": r.ticker, "codes": r.delete_codes, "why": why})
        picks = [(r["ticker"], "core") for r in hand["core_shortlist"]] + \
                [(r["ticker"], "special") for r in hand.get("special_handoff", [])]
        to_analyze, skipped = [], []
        for t, lane in picks:
            need, why = needs_full(registry.get(normalize_ticker(t)), asof)
            (to_analyze if need else skipped).append({"ticker": t, "lane": lane, "reason": why})
        summary.update({
            "counts": hand.get("counts", {}),
            "formula_version": hand.get("formula_version"),   # D66：記錄排名公式版本
            "deleted": hand.get("deleted", []),               # M1-D5-05：[{ticker, codes}]
            "lacking": [{k: r.get(k) for k in ("ticker", "missing", "data_notes", "dq_note")}   # M1-R5-02
                        for r in hand.get("lacking", [])],
            "deletions_to_review": review,
            "overrides": hand.get("overrides", []),
            "overrides_expired": hand.get("overrides_expired", []),   # 滿 12 個月不再撤銷（M1-R4-03）
            "shortlist": [r["ticker"] for r in hand["core_shortlist"]],
            "special_handoff": [r["ticker"] for r in hand.get("special_handoff", [])],
            "to_analyze": to_analyze,
            "skipped_recent": skipped,
        })
        warns = []
        if len(hand["core_shortlist"]) < top:
            warns.append(f"短名單只有 {len(hand['core_shortlist'])} 檔（< {top}）")
        if skipped:   # 12 個月內已完整分析者占名額而不遞補（M1-D2-12）
            warns.append(f"短名單 {len(skipped)} 檔 12 個月內已完整分析、本次不重做（未遞補）："
                         + "、".join(x["ticker"] for x in skipped))
        ana = {normalize_ticker(x["ticker"]) for x in to_analyze}
        if miss := [x.get("ticker") for x in summary["overrides"] if normalize_ticker(str(x.get("ticker"))) not in ana]:
            warns.append("撤銷不保證進短名單，要分析請手動 trader analyze --tickers：" + "、".join(map(str, miss)) + "（D95）")
        src = str(summary.get("universe_source") or "")
        if "override" in src:   # 測試／人工名單（A3-M1E-04）：非完整股票池、P/E 中位不凍結
            warns.append(f"股票池為 override 名單（{src}）：非完整股票池，P/E 中位未凍結"
                         + ("；pinned 表示沿用輸入目錄裡的 universe.txt，要換名單請刪除它" if src.startswith("pinned:") else ""))
        if src.endswith("_not_pit"):  # 補跑日沒有更早的 universe_versions，用的是目前名單（M1-D6-04）
            warns.append(f"股票池非時點（{src}）：可能含 asof 之後才納入的成分股")
        elif not src:   # --inputs 目錄無 universe.txt（M1-D9-04 之前的輸入；M1-R6-04）
            warns.append("股票池來源未知（輸入目錄無 universe.txt），無法確認時點")
        if warns:
            summary["warning"] = "；".join(warns)
        status = "done"
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        status = "failed"
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")
    conn.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
                 (status, db.now_iso(), json.dumps(summary, ensure_ascii=False, default=str), run_id))
    conn.commit()
    if status == "done" and not dry_run:
        _record_review_anomaly(conn, summary, outdir)
    return summary


REVIEW_KIND = "screen_review"
REVIEW_KEYS = ("deletions_to_review", "overrides_expired", "overrides_ignored", "lacking")


def _record_review_anomaly(conn, summary: dict, outdir: Path) -> None:
    """初篩待人工看的項目（待複核刪除、過期／被忽略的撤銷、資料不足、警告）只寫 summary.json 沒人讀 → 記一筆異常
    kind=screen_review（同 asof 已有未結者改寫內容、不重複新增；A3-M1C-06）。"""
    counts = {k: len(summary.get(k) or []) for k in REVIEW_KEYS if summary.get(k)}
    if not counts and not summary.get("warning"):
        return
    detail = {"asof": summary.get("asof"), "counts": counts, "warning": summary.get("warning"),
              "summary": str(outdir / "summary.json"),
              "action": "看 summary.json 的各清單；需撤銷刪除寫 state/screen_overrides.csv，看完後結案"}
    for r in conn.execute("SELECT anomaly_id, detail FROM anomalies WHERE kind=? AND status='open'", (REVIEW_KIND,)):
        try:
            same = json.loads(r["detail"] or "{}").get("asof") == summary.get("asof")
        except ValueError:
            same = False
        if same:
            conn.execute("UPDATE anomalies SET detail=? WHERE anomaly_id=?",
                         (json.dumps(detail, ensure_ascii=False), r["anomaly_id"]))
            conn.commit()
            return
    db.insert(conn, "anomalies", {"ticker": None, "kind": REVIEW_KIND, "pause_trading": 0, "created_at": db.now_iso(),
                                  "detail": json.dumps(detail, ensure_ascii=False)})


def _prev_dry_run(summary_file: Path) -> bool:
    try:
        return bool(json.loads(summary_file.read_text(encoding="utf-8")).get("dry_run"))
    except (OSError, ValueError):
        return False
