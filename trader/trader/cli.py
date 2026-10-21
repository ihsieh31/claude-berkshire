"""命令列介面：trader init／doctor／run-job（M0），其餘子命令先註冊、執行時回 2。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Config, ConfigError, load_config, resolve_config_path
from .db import connect, migrate
from .jobs import isolation
from .jobs.isolation import IsolationError
from .llm.base import call_with_retry, extract_json
from .models import AGENTIC_BACKENDS, JOB_TYPES, JobSpec
from .paths import get_paths

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
ALPACA_URL = "https://paper-api.alpaca.markets/v2/clock"
FRED_URL = "https://api.stlouisfed.org/fred/series"

def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .data.inputs import fetch_inputs  # 延遲匯入：資料層較重
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    if args.refreeze:   # M1-R4-01：重凍不得晚於原凍結日（抓資料前先擋，freeze_monthly 另有同一檢查）
        from .data.pe_median import _read_frozen, freeze_path
        old = _read_frozen(freeze_path(paths, args.asof[:7]))
        if old is not None and str(old.get("asof") or "") < args.asof:
            print(f"[FAIL] 未重凍：--asof {args.asof} 晚於原凍結日 {old.get('asof')}，請用 --asof {old.get('asof')} 重凍")
            return 1
    conn = connect(paths.db)
    try:
        migrate(conn)
        tickers = [t for t in args.tickers.split(",")] if args.tickers else None
        out = Path(args.out) if args.out else paths.work / f"inputs-{args.asof}"
        summary = fetch_inputs(cfg, paths, conn, args.asof, out, tickers=tickers, freeze_pe=args.freeze_pe,
                               refreeze=args.refreeze)
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    if args.refreeze and not isinstance(summary.get("pe_median"), dict):
        print(f"[FAIL] 未重凍：{summary.get('pe_median')}")
        return 1
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .pipelines.screen import ScreenError, run_screen
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            summary = run_screen(cfg, paths, conn, args.asof, top=args.top, special_max=args.special_max,
                                 inputs_dir=Path(args.inputs) if args.inputs else None, force=args.force)
        except ScreenError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return 1 if "error" in summary else 0


def cmd_analyze(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .pipelines.analyze import AnalyzeError, run_analyze
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        tickers = args.tickers.split(",") if args.tickers else None
        try:
            summary = run_analyze(cfg, paths, conn, args.asof, tickers=tickers, screen_asof=args.screen_date,
                                  run_id=args.run_id)
        except AnalyzeError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    if summary.get("not_rerun"):   # 同 run 已完成的分析不會重做（A3-M1E-03，比照 cmd_bootstrap）
        print(f"[WARN] {summary['run_id']} 已分析過、本次未重做：{','.join(summary['not_rerun'])}；"
              "重做請用 `trader analyze --asof 日期 --tickers … --run-id 新的run_id`")
    _awaiting_hint(summary["engine"], summary.get("run_id"))
    return 0 if summary["engine"].get("failed", 0) == 0 else 1


def cmd_review(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .pipelines.review import ReviewError, run_review
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            summary = run_review(cfg, paths, conn, args.asof, scope=args.scope, run_id=args.run_id,
                                 offline_filings=Path(args.offline_filings) if args.offline_filings else None,
                                 due_only=args.due_only)
        except ReviewError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    _awaiting_hint(summary.get("engine"), summary.get("run_id"))
    if "error" in summary:
        return 1
    return 0 if (summary.get("engine") or {}).get("failed", 0) == 0 else 1


def _awaiting_hint(engine_result: dict | None, run_id: str | None = None) -> None:
    n = (engine_result or {}).get("awaiting_approval", 0)
    if n:
        print(f"[WAIT] {n} 個強模型工作等待同意：trader approvals list → trader approvals approve … → trader run-approved")
    if (engine_result or {}).get("status") == "paused_budget":   # M8-D5-03
        r = run_id or "<run>"
        print(f"[WAIT] 預算用完（不會自動續跑，D44）：`trader jobs resume --run-id {r}` 續跑（或儀表板待同意頁的「續跑」），"
              f"不要了用 `trader jobs abandon --run-id {r}`；可先調高 config 的 budget；`trader jobs list --run-id {r}` 看未完成工作")


def _http_get(url: str, headers: dict | None = None, timeout: float = 10.0) -> Any:
    """網路請求的進入點；測試以 monkeypatch 替換，避免真的連網。不跟隨轉址：金鑰標頭不得送到別的主機
    （與 data/http.py 同政策；3xx 由 _probe 報 FAIL，A3-M8E-01）。"""
    return httpx.get(url, headers=headers or {}, timeout=timeout, follow_redirects=False)


def _today() -> str:
    """人工指令（ledger、trade、registry retire、export）的預設日＝紐約今日：與 daily 的決策日／撮合日同一日界。
    台北上午（紐約前一晚）用本機日會晚一個交易日撮合（A3-M8A-01）；入金另由 book.adjust 守門（A3-M8C2-01）。"""
    from .data.http import ny_today
    return ny_today()


def _redact(text: str, secrets: list[str | None]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _load_or_report(config_arg: str | None) -> Config | None:
    try:
        return load_config(config_arg)
    except ConfigError as e:
        print(f"[FAIL] 設定檔有誤：\n{e}")
        return None


# ---------------------------------------------------------------- init
def cmd_init(args: argparse.Namespace) -> int:
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        version = migrate(conn)
    finally:
        conn.close()
    print(f"[OK] 狀態目錄 {paths.state}；資料庫 {paths.db}（schema 版本 {version}）")

    cfg_path = resolve_config_path(getattr(args, "config", None))
    if not cfg_path.is_file():
        print(f"[FAIL] 找不到設定檔 {cfg_path}；請執行 cp config.example.yaml config.yaml 後再修改")
        return 1
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    print(f"[OK] 設定檔 {cfg.source_path}")

    warnings = 0
    env_names: list[tuple[str, str | None]] = [
        ("SEC", cfg.data.sec_user_agent_env),
        ("Alpaca key", cfg.data.alpaca_key_env),
        ("Alpaca secret", cfg.data.alpaca_secret_env),
        ("FRED", cfg.data.fred_key_env),
    ]
    for tier_name, tier in cfg.models.items():
        if tier.api_key_env:
            env_names.append((f"模型 {tier_name}", tier.api_key_env))
    for label, env in env_names:
        if env and cfg.secret(env) is None:
            print(f"[WARN] {label}：環境變數 {env} 未設定")
            warnings += 1

    sec_ua = cfg.secret(cfg.data.sec_user_agent_env)
    if sec_ua and "@" not in sec_ua:
        print(f"[WARN] {cfg.data.sec_user_agent_env} 格式應為「名稱 你的email」")
        warnings += 1

    for tier_name, tier in cfg.models.items():
        if tier.backend == "claude_code" and shutil.which(tier.claude_bin) is None:
            print(f"[WARN] 模型 {tier_name}：找不到 claude CLI（{tier.claude_bin}）")
            warnings += 1

    print(f"[OK] 初始化完成（警告 {warnings} 項）")
    return 0


# ---------------------------------------------------------------- doctor
def _report(results: list[tuple[str, str, str]], tag: str, name: str, msg: str) -> None:
    results.append((tag, name, msg))
    print(f"[{tag}] {name}  {msg}")


def _check_network(cfg: Config, timeout: float, results: list) -> None:
    secrets = [cfg.secret(cfg.data.alpaca_key_env), cfg.secret(cfg.data.alpaca_secret_env),
               cfg.secret(cfg.data.fred_key_env)]

    # SEC
    sec_ua = cfg.secret(cfg.data.sec_user_agent_env)
    if not sec_ua:
        _report(results, "SKIP", "SEC", f"未設定環境變數 {cfg.data.sec_user_agent_env}")
    else:
        _probe("SEC", SEC_URL, {"User-Agent": sec_ua}, timeout, secrets, results)

    # Alpaca
    key = cfg.secret(cfg.data.alpaca_key_env)
    secret = cfg.secret(cfg.data.alpaca_secret_env)
    if not key or not secret:
        _report(results, "SKIP", "Alpaca", f"未設定 {cfg.data.alpaca_key_env} 或 {cfg.data.alpaca_secret_env}")
    else:
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        _probe("Alpaca", ALPACA_URL, headers, timeout, secrets, results)

    # FRED
    fred_key = cfg.secret(cfg.data.fred_key_env)
    if not fred_key:
        _report(results, "SKIP", "FRED", f"未設定環境變數 {cfg.data.fred_key_env}")
    else:
        url = f"{FRED_URL}?series_id=DGS30&api_key={fred_key}&file_type=json"
        _probe("FRED", url, None, timeout, secrets, results)


def _probe(name: str, url: str, headers: dict | None, timeout: float, secrets: list, results: list) -> None:
    try:
        resp = _http_get(url, headers=headers, timeout=timeout)
    except Exception as e:  # 網路錯誤；訊息可能含 URL 或金鑰，需遮罩
        _report(results, "FAIL", name, _redact(f"連線失敗（{type(e).__name__}）：{e}", secrets))
        return
    if resp.status_code == 200:
        _report(results, "OK", name, "HTTP 200")
    else:
        _report(results, "FAIL", name, f"HTTP {resp.status_code}")


def _doctor_sandbox_root(cfg: Config, paths) -> Path:
    """與 Engine._sandbox_root 相同的隔離沙箱根（家目錄與暫存目錄之外）。"""
    sb = cfg.sandbox
    root = Path(sb.root).expanduser() if sb.root else isolation.default_sandbox_root()
    return isolation.prepare_root(root, isolation.deny_roots(sb.extra_deny, paths.state))


def _check_models(cfg: Config, results: list, paths) -> None:
    try:
        from trader.llm.router import build_runner  # 延遲匯入：router 由其他模組實作
    except ImportError as e:
        _report(results, "FAIL", "models", f"無法匯入 trader.llm.router：{e}")
        return

    secrets = [cfg.secret(t.api_key_env) for t in cfg.models.values()]
    for name in ("strong", "medium", "weak"):
        tier = cfg.models.get(name)
        if tier is None:
            continue
        label = f"模型 {name}（{tier.backend}:{tier.model}）"
        if tier.backend == "fake":
            _report(results, "SKIP", label, "fake 後端不做連線測試")
            continue
        if tier.backend == "openai_compat" and cfg.secret(tier.api_key_env) is None:
            _report(results, "SKIP", label, f"未設定環境變數 {tier.api_key_env}")
            continue
        try:   # 代理式後端的沙箱必須在隔離根下（系統暫存目錄在封鎖清單內，M8-D2-01）
            root = _doctor_sandbox_root(cfg, paths) if tier.backend in AGENTIC_BACKENDS else None
        except IsolationError as e:
            _report(results, "FAIL", label, str(e))
            continue
        with tempfile.TemporaryDirectory(prefix="doctor-", dir=root) as sandbox:
            job = JobSpec(
                job_id="doctor:DOCTOR:DOSSIER:1",
                run_id="doctor",
                job_type="DOSSIER",
                ticker="DOCTOR",
                asof="1970-01-01",
                prompt_text='只回覆 JSON：{"ok": true}',
                sandbox=Path(sandbox),
            )
            try:
                runner = build_runner(tier, cfg.sandbox.extra_deny, paths.state)
                res = call_with_retry(runner, job, retries=0)
                parsed = res.parsed_json or extract_json(res.text)
                if isinstance(parsed, dict) and parsed.get("ok") is True:
                    _report(results, "OK", label, f"回應正確（{res.latency_s:.1f}s）")
                else:
                    _report(results, "FAIL", label, "回應不是 {\"ok\": true}")
            except Exception as e:  # noqa: BLE001 - 任何錯誤都要顯示為 FAIL
                _report(results, "FAIL", label, _redact(f"{type(e).__name__}: {e}", secrets))


_STATE_COUNTS = [   # (名稱, SQL, 等級)：count > 0 即該等級（換機程序 §2.2）
    ("待執行工作", "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running','awaiting_approval')", "FAIL"),
    ("待同意／已同意未執行", "SELECT COUNT(*) FROM approvals WHERE status IN ('pending','deferred','approved')", "FAIL"),
    ("failed 分析工作未寫回", "SELECT COUNT(*) FROM jobs j WHERE status='failed' AND job_type<>'CORP_ACTION' AND NOT EXISTS "
     "(SELECT 1 FROM decisions d WHERE d.run_id=j.run_id AND d.ticker=j.ticker)", "FAIL"),
    ("failed 工作（CORP_ACTION 或已補寫回的歷史列）", "SELECT COUNT(*) FROM jobs j WHERE status='failed' AND (job_type='CORP_ACTION' "
     "OR EXISTS (SELECT 1 FROM decisions d WHERE d.run_id=j.run_id AND d.ticker=j.ticker))", "WARN"),
    ("完整分析決策缺訊號卡", "SELECT COUNT(*) FROM decisions WHERE json_extract(data,'$.review_type') IS NULL AND job_id IS NULL "
     "AND decision_id NOT IN (SELECT decision_id FROM signals)", "FAIL"),
    ("訊號卡缺決策", "SELECT COUNT(*) FROM signals WHERE decision_id NOT IN (SELECT decision_id FROM decisions)", "FAIL"),
    ("理由被丟掉的決策", "SELECT COUNT(*) FROM decisions WHERE json_extract(data,'$.reason_one_liner') LIKE '停止於 %，停止碼 %'",
     "FAIL"),
]


def _check_state(paths, results: list) -> None:
    """本機 state 檢查（M8-D4-04；換機前後用）：唯讀開 DB，不連網、不呼叫模型。"""
    import sqlite3
    from .db import MIGRATIONS
    if not paths.db.is_file():
        _report(results, "FAIL", "資料庫", f"找不到 {paths.db}")
        return
    conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        _report(results, "OK" if ok == "ok" else "FAIL", "integrity_check", ok)
        v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        _report(results, "FAIL" if v > len(MIGRATIONS) else "OK", "schema 版本", f"{v}（程式 {len(MIGRATIONS)}）")
        for name, sql, level in _STATE_COUNTS:
            n = conn.execute(sql).fetchone()[0]
            _report(results, level if n else "OK", name, str(n))
        missing = []
        for tk, tf, cap in conn.execute("SELECT ticker, json_extract(data,'$.thesis_file'), "
                                        "json_extract(data,'$.cap_input_file') FROM registry"):
            if tf and not (paths.state / tf).is_file():   # 相對 state；舊列可能是絕對路徑
                missing.append(f"論點檔 {tf}")
            if cap and not (paths.state / "cap_inputs" / f"{tk}.json").is_file():
                missing.append(f"上限輸入 cap_inputs/{tk}.json")
        _report(results, "FAIL" if missing else "OK", "論點檔與 cap_inputs", "、".join(missing) or "齊全")
    except sqlite3.Error as e:
        _report(results, "FAIL", "資料庫", f"{type(e).__name__}: {e}")
    finally:
        conn.close()
    sj = paths.state / "scheduler.json"
    try:
        st = json.loads(sj.read_text(encoding="utf-8")) if sj.is_file() else None
    except (OSError, ValueError) as e:
        _report(results, "FAIL", "scheduler.json", f"無法讀取：{e}")
        return
    if st is None:
        _report(results, "WARN", "scheduler.json", "不存在（排程不會自動初選，見 D09）")
        return
    _report(results, "OK" if st.get("last_screen") else "WARN", "scheduler.json last_screen", str(st.get("last_screen")))
    _check_sched_runs(paths, st, results)
    gave = st.get("gave_up") or []
    _report(results, "FAIL" if gave else "OK", "scheduler.json gave_up", "、".join(gave) or "無")


def _check_sched_runs(paths, st: dict, results: list) -> None:
    """scheduler.json 的 last_screen／last_watch_review 在 runs 表要有對應 done run（M8-R4-04）：快照復原後
    scheduler.json 比 runs 新會讓排程跳過該做的初選／觀察檢查。--mark-screened 換機重做時 last_screen 無 screen run 屬正常。"""
    import sqlite3
    checks = (("last_screen", "SELECT 1 FROM runs WHERE run_id=? AND status='done'", lambda d: (f"screen-{d}",)),
              ("last_watch_review", "SELECT 1 FROM runs WHERE run_id IN (?, ?) AND status='done'",
               lambda d: (f"review-{d}-watch", f"review-{d}-all")))
    try:
        conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        for key, sql, args in checks:
            d = st.get(key)
            if not d:
                continue
            if conn.execute(sql, args(str(d))).fetchone() is None:
                _report(results, "WARN", f"scheduler.json {key} 對 runs",
                        f"{d} 在 runs 找不到對應 done run（快照復原後應改回不晚於快照的值；--mark-screened 換機屬正常）")
            else:
                _report(results, "OK", f"scheduler.json {key} 對 runs", str(d))
    except sqlite3.Error as e:
        _report(results, "WARN", "scheduler.json 對 runs", f"{type(e).__name__}: {e}")
    finally:
        conn.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    results: list[tuple[str, str, str]] = []
    if args.state:   # 只查本機 state，不需設定檔、略過網路與模型
        _check_state(get_paths(getattr(args, "state_dir", None)), results)
    else:
        cfg = _load_or_report(getattr(args, "config", None))
        if cfg is None:
            return 1
        _check_network(cfg, args.timeout, results)
        if not args.skip_models:
            _check_models(cfg, results, get_paths(getattr(args, "state_dir", None)))
    fails = sum(1 for tag, _, _ in results if tag == "FAIL")
    print(f"共 {len(results)} 項，失敗 {fails} 項")
    return 1 if fails else 0


# ---------------------------------------------------------------- run-job
def cmd_run_job(args: argparse.Namespace) -> int:
    if not args.fake:
        print("[FAIL] 未給 --fake：真實工作 M3 才支援真實工作")
        return 2
    fixtures = Path(args.fake)
    if not fixtures.is_dir():
        print(f"[FAIL] 找不到 fixture 目錄 {fixtures}")
        return 1
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1

    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            from trader.llm.fake import FakeRunner
            from trader.llm.router import Router
            from trader.jobs.engine import Engine
        except ImportError as e:
            print(f"[FAIL] 工作引擎尚未就緒：{e}")
            return 1

        ticker = args.ticker.upper()
        run_id = f"manual-{args.asof}"
        router = Router(cfg, paths.routing_json, dry_run=True, fake=FakeRunner(fixtures_dir=fixtures))
        engine = Engine(conn, router, paths, cfg)
        engine.start_run(run_id, "manual", args.asof)
        engine.add_job(JobSpec(
            job_id=f"{run_id}:{ticker}:{args.job_type}:1",
            run_id=run_id,
            job_type=args.job_type,
            ticker=ticker,
            asof=args.asof,
        ))
        result = engine.run(run_id)
    finally:
        conn.close()

    print(f"run {run_id}：{json.dumps(result, ensure_ascii=False)}")
    return 1 if result.get("failed") else 0


def cmd_daily(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .pipelines.daily import DailyError, run_daily
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            summary = run_daily(cfg, paths, conn, args.date, skip_review=args.skip_review, dry_run=args.dry_run,
                                force=args.force)
        except DailyError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return 1 if "error" in summary else 0


def cmd_ledger(args: argparse.Namespace) -> int:
    from .ledger import broker
    from .ledger.book import GLOBAL, LedgerError, adjust, pause, show
    from .report.metrics import anomaly_hint
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    today = getattr(args, "date", None) or _today()
    try:
        migrate(conn)
        try:
            if args.ledger_cmd == "show":
                # 不連網：以最後一筆淨值的估值價估值（與儀表板同源），缺價才用平均成本（M8-D5-10）
                result = show(conn, None)
                for a in result["open_anomalies"]:
                    if anomaly_hint(a):
                        a["hint"] = anomaly_hint(a)
            elif args.ledger_cmd == "cancel":
                broker.cancel_manual(conn, args.order_id, args.note, today)
                result = {"cancelled": args.order_id}
            elif args.ledger_cmd == "pause":
                if bool(args.ticker) == bool(args.all):
                    print("[FAIL] 請給一個代號，或用 --all 暫停全部（兩者擇一）")
                    return 2
                aid = pause(conn, None if args.all else args.ticker, date=today, note=args.note)
                scope = "全部代號" if args.all else args.ticker.upper()
                result = {"paused": scope, "anomaly_id": aid,
                          "unpause": "trader ledger adjust " + ("--unpause-all" if args.all else
                                                                f"--unpause {args.ticker.upper()}") + " --note 理由"}
            else:
                if args.unpause and args.unpause_all:
                    print("[FAIL] --unpause 與 --unpause-all 擇一")
                    return 2
                result = adjust(conn, date=today, cash_delta=args.cash, deposit=args.deposit,
                                ticker=args.ticker, set_qty=args.qty, set_avg_cost=args.avg_cost,
                                resolve_anomaly=args.resolve, unpause=GLOBAL if args.unpause_all else args.unpause,
                                first_buy_date=args.first_buy_date, resolve_corp_action=args.resolve_corp_action,
                                note=args.note)
                rr = result.get("registry_rescaled")
                if rr:   # cap_inputs 每股欄位隨股數比例換算（M7-RS7-01）；失敗記異常、暫停該檔並回警示（A3-M8B-03）
                    from .ledger import corp_actions as _ca
                    w = _ca.rescale_cap_after_adjust(conn, paths, rr["ticker"], rr["ratio"])
                    if w:
                        result.setdefault("warnings", []).append(w)
        except (LedgerError, broker.BrokerError) as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_trade(args: argparse.Namespace) -> int:
    """人工下單（D12）：寫一筆 MANUAL 決策日誌（附異常編號），委託交 broker 照一般方式於下一次 daily 撮合。"""
    from . import db as _db
    from .data.universe import normalize_ticker
    from .ledger import book, broker
    t = normalize_ticker(args.ticker)
    side = "BUY" if args.trade_cmd == "buy" else "SELL"
    day = args.date or _today()
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        held = book.positions(conn).get(t, {}).get("qty", 0)
        # 可賣＝持股－未成交賣單（A3-M8B2-01）：否則兩張賣單合計可超過持股，SELL_ALL／TRIM 也標錯
        pending = sum(o["qty"] for o in book.open_orders(conn, t) if o["side"] == "SELL") if side == "SELL" else 0
        avail = held - pending
        if side == "SELL" and args.qty > avail:
            print(f"[FAIL] {t} 持股 {held} 股" + (f"、未成交賣單 {pending} 股（可賣 {avail} 股）" if pending else "")
                  + f"，不足賣出 {args.qty} 股")
            return 1
        if args.anomaly is not None and conn.execute("SELECT 1 FROM anomalies WHERE anomaly_id=?",
                                                     (args.anomaly,)).fetchone() is None:
            print(f"[FAIL] 找不到異常 {args.anomaly}")
            return 1
        action = ("ADD" if held else "BUY") if side == "BUY" else ("SELL_ALL" if args.qty == avail else "TRIM")
        n = conn.execute("SELECT COUNT(*) FROM decisions WHERE decision_id LIKE ?",
                         (f"{t}-MANUAL-{day.replace('-', '')}-%",)).fetchone()[0] + 1
        did = f"{t}-MANUAL-{day.replace('-', '')}-{n}"
        log = {"review_type": "MANUAL", "action": action, "side": side, "qty": args.qty, "limit": args.limit,
               "anomaly_id": args.anomaly, "note": args.note, "reason_one_liner": f"人工下單：{args.note}"}
        try:
            with book.transaction(conn):
                book.write_row(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": day,
                                                   "run_id": None, "job_id": None, "signal": action,
                                                   "reason_code": "MANUAL", "data": log, "created_at": _db.now_iso()})
                oid = broker.place_order(conn, t, side, args.qty, args.limit, did, day)
                log["order_id"] = oid
                conn.execute("UPDATE decisions SET data=? WHERE decision_id=?", (json.dumps(log, ensure_ascii=False), did))
        except (broker.BrokerError, book.LedgerError) as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps({"decision_id": did, "order_id": oid, "action": action}, ensure_ascii=False, indent=1))
    print("委託於下一次 daily（委託日之後的交易日）撮合；撤單：trader ledger cancel " + oid + " --note 理由")
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    """decisions void：作廢決策（M8-D4-02）。不刪列：decisions／signals 的 data 寫 voided_at、voided_note、
    superseded_by；取消指向它的未成交委託。作廢後 daily、加碼許可、儀表板分析頁與檢討報告都不再採用。"""
    from . import db as _db
    from .ledger import book, broker
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        now = _db.now_iso()
        cancelled: list[str] = []
        try:
            with book.transaction(conn):
                for did in args.ids:
                    row = conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()
                    if row is None:
                        raise book.LedgerError(f"找不到決策 {did}")
                    mark = {"voided_at": now, "voided_note": args.note, "superseded_by": args.superseded_by}
                    for table in ("decisions", "signals"):
                        r = conn.execute(f"SELECT data FROM {table} WHERE decision_id=?", (did,)).fetchone()
                        if r is None:
                            continue
                        data = json.loads(r["data"]) if r["data"] else {}
                        if table == "decisions" and data.get("voided_at"):
                            raise book.LedgerError(f"決策 {did} 已於 {data['voided_at']} 作廢")
                        conn.execute(f"UPDATE {table} SET data=? WHERE decision_id=?",
                                     (json.dumps({**data, **mark}, ensure_ascii=False), did))
                    for o in conn.execute("SELECT order_id FROM orders WHERE decision_id=? AND status='open'",
                                          (did,)).fetchall():
                        broker.cancel_order(conn, o["order_id"], f"決策 {did} 已作廢：{args.note}")
                        cancelled.append(o["order_id"])
        except (book.LedgerError, broker.BrokerError) as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(f"已作廢：{'、'.join(args.ids)}；取消委託：{'、'.join(cancelled) or '無'}")
    print("重跑請用新的 run：trader analyze --asof <日期> --tickers <代號> --run-id <新 run-id>（同 run 重跑不支援）")
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    """registry retire：下市／不再追蹤的代號改 RETIRED（M8-D4-06）。只在無持股、無未成交委託時；不刪列。
    RETIRED 不在 daily 行情／公司行動範圍，也不在 review --scope watch 匯出；X5 與初選比對不變。"""
    from . import db as _db
    from .data.universe import normalize_ticker
    from .ledger import book
    t = normalize_ticker(args.ticker)
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        row = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
        why = ("登錄表沒有此代號" if row is None else "已是 RETIRED" if row["status"] == "RETIRED"
               else "仍有持股" if t in book.positions(conn) else "仍有未成交委託" if book.open_orders(conn, t) else None)
        if why:
            print(f"[FAIL] {t} {why}，不能退場")
            return 1
        data = json.loads(row["data"]) if row["data"] else {}
        data["retired"] = {"date": _today(), "note": args.note, "status_before": row["status"]}
        conn.execute("UPDATE registry SET status='RETIRED', data=?, updated_at=? WHERE ticker=?",
                     (json.dumps(data, ensure_ascii=False), _db.now_iso(), t))
        conn.commit()
    finally:
        conn.close()
    print(f"{t}：{row['status']} → RETIRED")
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    """db backup：帳本本機快照（M8-D4-01）。"""
    from .db import backup
    paths = get_paths(getattr(args, "state_dir", None))
    if not paths.db.is_file():
        print(f"[FAIL] 找不到 {paths.db}")
        return 1
    try:
        print(f"已寫出：{backup(paths.db, note=args.note)}")
    except RuntimeError as e:
        print(f"[FAIL] {e}")
        return 1
    return 0


def _run_approved(cfg, paths, conn, run_id=None) -> int:
    from .pipelines.approved import RunApprovedBusy, exit_code, run_approved
    try:
        result = run_approved(cfg, paths, conn, run_id=run_id)
    except RunApprovedBusy as e:
        print(f"[FAIL] {e}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return exit_code(result)   # 只算本次新失敗（M8-R5-04）


def _jobs_abandon(args: argparse.Namespace) -> int:
    """jobs abandon：放棄 run（或該檔）未完成的工作與同意列，不呼叫模型、不寫決策（M5_D4-01）。"""
    from .jobs.engine import abandon
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (args.run_id,)).fetchone() is None:
            print(f"[FAIL] run {args.run_id} 不存在")
            return 1
        ids = abandon(conn, args.run_id, ticker=args.ticker, note=args.note)
        st = conn.execute("SELECT status FROM runs WHERE run_id=?", (args.run_id,)).fetchone()[0]
        print(f"已放棄：{ids or '無未完成工作'}（run 狀態 {st}）")
        return 0
    finally:
        conn.close()


def _jobs_list(args: argparse.Namespace) -> int:
    """jobs list：每個 run 一行（kind、asof、狀態、工作狀態計數），其下逐列未完成（非 done）工作（M8-D5-03）。
    預算暫停、等待同意、失敗與跳過的工作都會列出。預設只列未完成的 run；--all 全部。"""
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        sql, a = "SELECT run_id, kind, asof, status FROM runs WHERE 1=1", []
        if args.run_id:
            sql, a = sql + " AND run_id=?", a + [args.run_id]
        if args.status:
            sql, a = sql + " AND status=?", a + [args.status]
        shown = 0
        for r in conn.execute(sql + " ORDER BY asof DESC, run_id DESC", a).fetchall():
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs WHERE run_id=? GROUP BY status",
                                       (r["run_id"],)).fetchall())
            todo = conn.execute("SELECT job_id, ticker, job_type, status, error FROM jobs WHERE run_id=? AND status<>'done' "
                                "ORDER BY ticker, job_id", (r["run_id"],)).fetchall()
            if not (args.all or args.run_id or args.status) and r["status"] == "done" and not todo:
                continue
            shown += 1
            print(f"{r['run_id']}  {r['kind']}  asof {r['asof']}  {r['status']}  "
                  + "、".join(f"{k} {v}" for k, v in sorted(counts.items())))
            for j in todo:
                err = " ".join(str(j["error"] or "").split())[:120]
                print(f"   {j['job_id']}  {j['ticker']}  {j['job_type']}  {j['status']}" + (f"  {err}" if err else ""))
        if not shown:
            print("無" + ("" if args.all else "未完成的 run（--all 列出全部）"))
        return 0
    finally:
        conn.close()


def cmd_jobs(args: argparse.Namespace) -> int:
    """jobs retry：failed 工作重設為 pending 並由引擎續跑（會呼叫模型；不刪 llm_calls）。jobs abandon：見 _jobs_abandon。
    jobs list：見 _jobs_list。jobs resume：不重設任何工作，直接由引擎續跑該 run（預算暫停後續跑，D44；會呼叫模型）。"""
    from .jobs.engine import Engine, retry_failed
    if args.jobs_cmd == "abandon":
        return _jobs_abandon(args)
    if args.jobs_cmd == "list":
        return _jobs_list(args)
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (args.run_id,)).fetchone() is None:
            print(f"[FAIL] run {args.run_id} 不存在")
            return 1
        if args.jobs_cmd == "resume":
            if conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND status IN ('pending','running')",
                            (args.run_id,)).fetchone() is None:
                print(f"run {args.run_id} 沒有待執行的工作，不需續跑")
                return 0
        else:
            ids = retry_failed(conn, args.run_id, ticker=args.ticker, job_id=args.job_id)
            print(f"已重設為 pending：{ids or '無（沒有可重跑的 failed 工作；CORP_ACTION 由 daily 重派；已保守處理、使用者拒絕／作廢／放棄的不重跑）'}")
            if not ids or args.no_run:
                return 0
        from .data.inputs import make_clients
        from .llm.router import Router
        try:
            sec = make_clients(cfg, paths).get("sec")
        except Exception:  # noqa: BLE001 - 缺 SEC 只影響 DOSSIER 類工作
            sec = None
        engine = Engine(conn, Router(cfg, paths.routing_json, state_dir=paths.state), paths, cfg)
        engine.ctx.sec = sec
        result = engine.run(args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        _awaiting_hint(result, args.run_id)   # 等待同意／預算再暫停時提示（M8-R5-02）
        return 1 if result.get("failed") else 0
    finally:
        conn.close()


def cmd_approvals(args: argparse.Namespace) -> int:
    from .jobs import approvals as A
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            if args.approvals_cmd == "list":
                items = A.list_items(conn, A.OPEN if not args.all else
                                     ("pending", "deferred", "approved", "rejected", "superseded", "done"))
                for x in items:
                    print(f"#{x['approval_id']:<4} {x['ticker']:<6} {x['status']:<10} {x['job_type']:<8} "
                          f"asof {x['asof']}  run {x['run_id']}  {'持股 ' if x['holding'] else ''}{x['reason'] or ''}"
                          + (f"  ［{x['decided_via'] or ''} {x['decided_at'] or ''}］" if x.get("decided_via") else "")
                          + (f"  註：{x['note']}" if x.get("note") else ""))
                s = A.summary(conn)
                print(f"待同意：{A.label(s)}（延後 {s['n_deferred']}、已同意待執行 {s['n_approved_waiting']}；"
                      f"每檔估算依{s['per_ticker']['basis']}）")
                print(s["note"])
                return 0
            ids = args.ids or None
            if args.approvals_cmd == "approve":
                if ids is None and not args.all:
                    print("[FAIL] 請給同意編號，或用 --all 同意全部待同意（不含延後）")
                    return 2
                done = A.approve(conn, ids, via="cli")
                print(f"已同意：{done or '無'}")
                if done and args.run:
                    cfg = _load_or_report(getattr(args, "config", None))
                    return 1 if cfg is None else _run_approved(cfg, paths, conn)
                if done:
                    print("執行：trader run-approved")
                return 0
            if not ids:
                print("[FAIL] 請給編號")
                return 2
            if args.approvals_cmd == "defer":
                print(f"已延後：{A.defer(conn, ids, via='cli')}")
            else:
                tz = A.config_timezone(getattr(args, "config", None))
                print(f"已拒絕，決策：{A.reject(conn, paths, ids, via='cli', note=args.note, time_zone=tz)}")
            return 0
        except A.ApprovalError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()


def cmd_run_approved(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        return _run_approved(cfg, paths, conn, run_id=args.run_id)
    finally:
        conn.close()


def cmd_dashboard(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .dashboard.server import serve
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    port = args.port or cfg.dashboard.port
    try:
        print(f"儀表板：http://127.0.0.1:{port}/（Ctrl-C 結束）")
        from .dashboard.charts import ChartService
        from .data.inputs import make_clients
        alpaca = make_clients(cfg, paths).get("alpaca")
        from dataclasses import asdict
        serve(paths, cfg.dashboard.host, port, cfg.source_path, charts=ChartService(alpaca.http) if alpaca else None,
              budget=asdict(cfg.budget))
    except ValueError as e:
        print(f"[FAIL] {e}")
        return 1
    return 0


def cmd_validate_report(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .report import validation as V
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    alpaca = None
    if not args.no_prices:
        from .data.inputs import make_clients
        alpaca = make_clients(cfg, paths).get("alpaca")
        if alpaca is None:
            print("[WARN] 沒有 Alpaca 金鑰：略過需要價格的段落")
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            r = V.build(conn, paths, args.start, args.end, alpaca=alpaca, bench=cfg.account.benchmark)
        except ValueError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    md, js = V.write(r, Path(args.out) if args.out else paths.state / "reports")
    print(md.read_text(encoding="utf-8"))
    print(f"已寫出：{md}、{js}")
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    from .dashboard import launchd as L
    if args.desktop_cmd == "status":
        for k, v in L.status().items():
            print(f"{k}: {v}")
        return 0
    if args.desktop_cmd == "uninstall":
        print(f"已移除：{L.uninstall() or '無'}")
        return 0
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    if args.desktop_cmd == "build":
        print(L.build_widget())
        return 0
    try:
        done = L.install(paths, args.port or cfg.dashboard.port, cfg.source_path, widget=not args.no_widget)
    except RuntimeError as e:
        print(f"[FAIL] {e}")
        return 1
    print(f"已安裝並載入：{done}（開機自動啟動；移除：trader desktop uninstall）")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .report import export as X
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    names = list(X.DATASETS) if args.what == "all" else [w.strip() for w in args.what.split(",") if w.strip()]
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            out = X.export(conn, names, args.format, Path(args.out) if args.out else paths.state / "exports",
                           _today())
        except ValueError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    for p in out:
        print(p)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .pipelines.analyze import AnalyzeError
    from .pipelines.bootstrap import run_bootstrap
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        tickers = [t for t in args.tickers.split(",") if t.strip()] if args.tickers else None
        summary = None
        try:
            summary = run_bootstrap(cfg, paths, conn, args.asof, tickers=tickers, top=args.top, dry_run=args.dry_run,
                                    force=args.force)
        except AnalyzeError as e:
            print(f"[FAIL] {e}")
        # 上次初選日：初選成功就寫（分析失敗也寫，否則排程會重跑整個初選，M8-D10-05）；
        # --tickers 略過初選，只有加 --mark-screened 才寫（換機重做同一批，D09；加 --dry-run 則只記日期、不分析）
        if tickers:
            screened = bool(args.mark_screened)
        else:   # 前次 dry-run 的 done 或本次初選錯誤都不算本季已篩（A3-M1E-02）
            r = conn.execute("SELECT summary FROM runs WHERE run_id=? AND status='done'",
                             (f"screen-{args.asof}",)).fetchone()
            try:
                rs = json.loads(r["summary"]) if r is not None and r["summary"] else {}
            except ValueError:
                rs = {}
            screened = (not args.dry_run and r is not None and not rs.get("dry_run") and "error" not in rs
                        and not (summary is not None and "error" in summary))
    finally:
        conn.close()
    if screened:
        from .scheduler import Scheduler
        sch = Scheduler(cfg, paths)
        st = sch.load_state()
        st["last_screen"] = max(args.asof, st.get("last_screen") or "")  # 排程據此判斷下次每季初選
        sch.save_state(st)
    if summary is None:
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    if "error" in summary:
        return 1
    ana = summary.get("analyze") or {}
    eng = ana.get("engine") or {}
    if ana.get("not_rerun"):   # 同 run 已完成的分析不會重做（M1-RS8-01）
        print(f"[WARN] {ana['run_id']} 已分析過、本次未重做：{','.join(ana['not_rerun'])}；"
              "重做請用 `trader analyze --asof 日期 --tickers … --run-id 新的run_id`")
    _awaiting_hint(eng, ana.get("run_id"))
    if eng.get("failed", 0):   # 個別分析失敗不算初選失敗：交給 job_failed 異常與 jobs retry（M8-D10-05）
        print(f"[WARN] {eng['failed']} 個分析工作失敗：見未結異常 job_failed；重跑用 "
              f"`trader jobs retry --run-id {ana.get('run_id') or 'analyze-' + args.asof}`")
    return 0


def cmd_scheduler(args: argparse.Namespace) -> int:
    from .dashboard import launchd as L
    sc = args.scheduler_cmd
    if sc == "uninstall":
        print(f"已停止並移除：{L.uninstall((L.LABEL_SCHED,)) or '無'}")
        return 0
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from . import scheduler as S
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    if sc == "reset":
        keys = S.Scheduler(cfg, paths).reset(args.task)
        print("已清除失敗／重試等待／放棄狀態：" + ("、".join(keys) if keys else "無"))
        return 0
    if sc == "status":
        print(f"{L.LABEL_SCHED}: {L.status((L.LABEL_SCHED,))[L.LABEL_SCHED]}")
        sch = S.Scheduler(cfg, paths)
        st = sch.load_state()
        print(json.dumps(st, ensure_ascii=False, indent=1) if st else "尚未執行過")
        conn = connect(paths.db)
        try:
            migrate(conn)
            todo = sch.plan(conn, dict(st))
        finally:
            conn.close()
        from datetime import datetime
        # plan 已濾掉放棄、重試等待與暫緩的工作：另列出來，才不會誤以為一切正常（M8-D5-06）
        gave = st.get("gave_up") or []
        nowts = sch.now().timestamp()
        waits = {k: t for k, t in (st.get("retry_at") or {}).items() if t > nowts}
        print("現在該做：" + ("、".join(k for k, _ in todo) if todo else
                             "無" if (gave or waits or sch.held) else "無（等下一個收盤）"))
        if gave:
            print("已放棄（`trader scheduler reset [任務]` 可重來）：" + "、".join(gave))
        for k, t in sorted(waits.items()):
            n = (st.get("failures") or {}).get(k, 0)
            stalled = "；帳本停擺（撮合、賣出、淨值不前進）" if k.startswith("daily:") and n >= S.MAX_FAILURES else ""
            print(f"重試等待：{k} 已失敗 {n} 次，{datetime.fromtimestamp(t, sch.tz):%Y-%m-%d %H:%M %Z} 後重試{stalled}")
        for w in sch.held:
            print(f"[WAIT] {w}")
        return 0
    if sc == "print":
        import plistlib
        if args.cron:
            import shlex
            log = paths.state / "logs" / "scheduler.log"
            log.parent.mkdir(parents=True, exist_ok=True)   # 重導向目錄不存在時整行不執行（M8-D7-03）
            env = " ".join(f"{k}={shlex.quote(v)}" for k, v in L._env().items())   # cron 的 PATH 沒有 claude、缺憑證
            argv = shlex.join(S._cli(cfg, paths, "scheduler", "once"))
            line = f"*/10 * * * * cd {shlex.quote(str(S.PROJECT_DIR))} && {env} {argv} >> {shlex.quote(str(log))} 2>&1"
            print(line.replace("%", "\\%"))   # crontab 的 % 是換行
        else:
            print(plistlib.dumps(L.scheduler_plist(paths, cfg.source_path)).decode())
        return 0
    if sc in ("install", "run", "once"):
        try:
            S.Scheduler(cfg, paths).check_ready(strict=(sc == "install"))
        except S.NotBootstrapped as e:
            print(f"[FAIL] 不能啟動排程：{e}")
            return 1
    if sc == "install":
        try:
            done = L.install_scheduler(paths, cfg.source_path)
        except RuntimeError as e:
            print(f"[FAIL] {e}")
            return 1
        print(f"已安裝並啟動：{done}（開機自動啟動、沒有期限；停止：trader scheduler uninstall）")
        return 0
    try:
        if sc == "once":
            done = S.Scheduler(cfg, paths).once()
            print("完成：" + ("、".join(done) if done else "無"))
        else:
            S.Scheduler(cfg, paths).run_forever(args.interval)
    except S.SchedulerBusy as e:
        print(f"[FAIL] {e}")
        return 1
    except KeyboardInterrupt:
        print("排程已停止")
    return 0


# ---------------------------------------------------------------- parser
def _common(suppress: bool) -> argparse.ArgumentParser:
    default = argparse.SUPPRESS if suppress else None
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--config", default=default, help="設定檔路徑（預設 TRADER_CONFIG 或 config.yaml）")
    p.add_argument("--state-dir", default=default, help="執行期資料目錄（預設 TRADER_STATE_DIR 或 state/）")
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader", description="波克夏式長期選股自動交易系統", parents=[_common(False)])
    sub = parser.add_subparsers(dest="command", metavar="命令")
    common = [_common(True)]

    p = sub.add_parser("init", parents=common, help="建立 state、DB，檢查設定與金鑰")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", parents=common, help="連線測試：SEC、Alpaca、FRED、各 tier 模型")
    p.add_argument("--skip-models", action="store_true", help="略過模型連線測試")
    p.add_argument("--state", action="store_true", help="只檢查本機 state（換機前後用；略過網路與模型）")
    p.add_argument("--timeout", type=float, default=10.0, help="網路逾時秒數")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run-job", parents=common, help="單一工作，除錯用")
    p.add_argument("job_type", choices=JOB_TYPES)
    p.add_argument("ticker")
    p.add_argument("--asof", required=True, metavar="DATE")
    p.add_argument("--fake", metavar="FIXTURE_DIR", help="使用 FakeRunner，從此目錄讀取回應 fixture")
    p.set_defaults(func=cmd_run_job)

    p = sub.add_parser("fetch", parents=common, help="抓資料：features.csv、prices.csv、daily.csv、公司行動、利率")
    p.add_argument("--asof", required=True, metavar="DATE")
    p.add_argument("--tickers", help="逗號分隔；省略＝整個股票池（S&P 500）")
    p.add_argument("--out", help="輸出目錄（預設 state/work/inputs-DATE）")
    p.add_argument("--freeze-pe", action="store_true", help="以整個股票池凍結當月大型股 P/E 中位")
    p.add_argument("--refreeze", action="store_true",
                   help="重算並取代當月 P/E 中位凍結值（--asof 用原凍結日、不得更晚；舊檔複製為 .bak-時間 保留；需整個股票池）")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("bootstrap", parents=common, help="初選＋深度分析（建立初始名單；每季初選也用它；會呼叫模型）")
    p.add_argument("--asof", required=True, metavar="DATE")
    p.add_argument("--tickers", help="逗號分隔；給定則略過初選，直接分析這些代號")
    p.add_argument("--top", type=int, default=15, help="核心短名單檔數（預設 15）")
    p.add_argument("--dry-run", action="store_true", help="只跑初選並列出將分析的代號，不建分析工作")
    p.add_argument("--force", action="store_true",
                   help="同日初選已完成仍重跑（舊 screens/日期/ 改名 .bak-時間 保留；分析改用新 run_id analyze-日期-r時間）")
    p.add_argument("--mark-screened", action="store_true",
                   help="與 --tickers 併用：把 --asof 記為上次初選日（換機重做同一批時用，排程才不會立刻再初選；"
                        "加 --dry-run＝只記日期、不分析）")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("screen", parents=common, help="季度初選：整個股票池 → skill screen.py → 交接檔")
    p.add_argument("--asof", required=True, metavar="DATE")
    p.add_argument("--top", type=int, default=15, help="核心短名單檔數（預設 15）")
    p.add_argument("--special-max", type=int, default=0, help="特殊情境交棒上限（預設 0）")
    p.add_argument("--inputs", metavar="DIR", help="沿用既有 features.csv／prices.csv 目錄，不重抓")
    p.add_argument("--force", action="store_true", help="同日初選已完成仍重跑（舊 screens/日期/ 改名 .bak-時間 保留）")
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("analyze", parents=common, help="深度分析：DOSSIER → FULL_A → FULL_B → RED_TEAM（會呼叫模型）")
    p.add_argument("--asof", required=True, metavar="DATE")
    p.add_argument("--tickers", help="逗號分隔；省略＝該日初選的 to_analyze")
    p.add_argument("--screen-date", metavar="DATE", help="取用哪一天的初選結果（預設同 --asof）")
    p.add_argument("--run-id", help="續跑既有 run（預設 analyze-DATE）")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("review", parents=common, help="觸發檢查：登錄表 → 訊號 → 起始工作（會呼叫模型，除非 --due-only）")
    p.add_argument("--asof", required=True, metavar="DATE")
    p.add_argument("--scope", choices=["holdings", "watch", "all"], default="all")
    p.add_argument("--run-id", help="預設 review-DATE-SCOPE")
    p.add_argument("--due-only", action="store_true", help="只產生訊號檔（due.json），不建工作、不呼叫模型")
    p.add_argument("--offline-filings", metavar="F", help="測試用申報 JSON（不連 SEC）")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("daily", parents=common, help="每日收盤流程：公司行動、撮合、觸發檢查、組合計畫、淨值")
    p.add_argument("--date", required=True, metavar="DATE")
    p.add_argument("--skip-review", action="store_true", help="略過持股觸發檢查（沿用既有決策）")
    p.add_argument("--dry-run", action="store_true", help="只算計畫，不寫入任何資料")
    p.add_argument("--force", action="store_true", help="略過「紐約當日須已收盤」檢查（M7-D7-05）")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("approvals", parents=common, help="強模型同意閘門：list／approve／defer／reject")
    asub = p.add_subparsers(dest="approvals_cmd", metavar="子命令", required=True)
    al = asub.add_parser("list", parents=[_common(True)], help="待同意清單與用量估算")
    al.add_argument("--all", action="store_true", help="含已結案（拒絕、作廢、完成）")
    aa = asub.add_parser("approve", parents=[_common(True)], help="同意（編號，或 --all＝全部待同意，不含延後）")
    aa.add_argument("ids", nargs="*", type=int)
    aa.add_argument("--all", action="store_true")
    aa.add_argument("--run", action="store_true", help="同意後立即執行（等同 trader run-approved）")
    ad = asub.add_parser("defer", parents=[_common(True)], help="延後（留在佇列，不計入一鍵全同意）")
    ad.add_argument("ids", nargs="+", type=int)
    ar = asub.add_parser("reject", parents=[_common(True)], help="拒絕：該檔記 NO_ACTION/MISSING_DATA，下次觸發再排")
    ar.add_argument("ids", nargs="+", type=int)
    ar.add_argument("--note", help="拒絕理由")
    p.set_defaults(func=cmd_approvals)

    p = sub.add_parser("jobs", parents=common,
                       help="工作管理：list（列出 run 與未完成工作）／retry（重跑 failed 工作，會呼叫模型）／resume（預算暫停後續跑）"
                            "／abandon（放棄殘留工作）")
    jsub = p.add_subparsers(dest="jobs_cmd", metavar="子命令", required=True)
    jl = jsub.add_parser("list", parents=[_common(True)], help="每個 run 一行，其下列未完成工作（預設只列未完成的 run）")
    jl.add_argument("--run-id")
    jl.add_argument("--status", help="只列此狀態的 run（例 paused_budget、failed、awaiting_approval）")
    jl.add_argument("--all", action="store_true", help="含已完成的 run")
    jr = jsub.add_parser("retry", parents=[_common(True)], help="把 run 內 failed 工作重設為 pending 並續跑")
    jr.add_argument("--run-id", required=True)
    jr.add_argument("--ticker")
    jr.add_argument("--job-id")
    jr.add_argument("--no-run", action="store_true", help="只重設，不立即執行")
    jm = jsub.add_parser("resume", parents=[_common(True)],
                         help="續跑 run 內待執行的工作（預算暫停後；不重設 failed，會呼叫模型、仍受預算約束）")
    jm.add_argument("--run-id", required=True)
    ja = jsub.add_parser("abandon", parents=[_common(True)],
                         help="放棄 run（或該檔）未完成的工作與同意列：工作改 skipped、同意列 rejected、job_failed 異常結案；"
                              "不呼叫模型、不寫決策（持股維持現狀）。請先確認沒有其他行程正在跑這個 run")
    ja.add_argument("--run-id", required=True)
    ja.add_argument("--ticker")
    ja.add_argument("--note", help="放棄理由（記在工作 error 與同意列 note）")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("run-approved", parents=common, help="執行已同意的強模型工作（會呼叫模型）")
    p.add_argument("--run-id", help="只執行此 run 的已同意項目")
    p.set_defaults(func=cmd_run_approved)

    p = sub.add_parser("scheduler", parents=common, help="常駐排程（沒有期限）：run／once／install／uninstall／status／reset／print")
    ssub = p.add_subparsers(dest="scheduler_cmd", metavar="子命令", required=True)
    sr = ssub.add_parser("run", parents=[_common(True)], help="前景常駐執行（Ctrl-C 停止）")
    sr.add_argument("--interval", type=int, default=60, help="檢查間隔秒數（預設 60）")
    ssub.add_parser("once", parents=[_common(True)], help="做完目前該做的工作就結束（給 cron 用）")
    ssub.add_parser("install", parents=[_common(True)], help="以 launchd 安裝並啟動（開機自動啟動）")
    ssub.add_parser("uninstall", parents=[_common(True)], help="停止並移除 launchd 排程")
    ssub.add_parser("status", parents=[_common(True)], help="排程狀態、現在該做、重試等待／放棄與暫緩原因")
    sx = ssub.add_parser("reset", parents=[_common(True)], help="清除失敗次數、重試等待與放棄狀態（daily 停擺查明原因後用）")
    sx.add_argument("task", nargs="?", help="任務鍵，例 daily:2026-10-16；省略＝全部")
    sp = ssub.add_parser("print", parents=[_common(True)], help="印出 launchd plist（或 --cron 的 crontab 行）")
    sp.add_argument("--cron", action="store_true")
    p.set_defaults(func=cmd_scheduler)

    p = sub.add_parser("dashboard", parents=common, help="本機儀表板 API 與網頁（127.0.0.1）")
    p.add_argument("--port", type=int, help="預設 config dashboard.port（8787）")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("ledger", parents=common, help="紙上帳本：show（摘要）、adjust（人工調整）、cancel（撤單）、pause（暫停）")
    lsub = p.add_subparsers(dest="ledger_cmd", metavar="子命令", required=True)
    lsub.add_parser("show", parents=[_common(True)],
                    help="現金、持股、委託與未結異常（以最後一筆淨值的估值價估值，見輸出 valuation／valuation_date）")
    lc = lsub.add_parser("cancel", parents=[_common(True)], help="人工撤銷未成交委託（記一筆已結案的 manual_cancel 稽核異常）")
    lc.add_argument("order_id")
    lc.add_argument("--note", required=True, help="撤單理由（必填）")
    lp = lsub.add_parser("pause", parents=[_common(True)],
                         help="人工暫停：該代號（或 --all 全部）不撮合；買進與 TRIM 減碼拒絕下單（解除後需要再人工補下），"
                              "SELL_ALL 全清照建委託、撮合時擋，解除後次日自動撮合（不要另補賣單）；"
                              "解除：ledger adjust --unpause T／--unpause-all")
    lp.add_argument("ticker", nargs="?")
    lp.add_argument("--all", action="store_true", help="暫停全部代號")
    lp.add_argument("--note", required=True, help="暫停理由（必填）")
    la = lsub.add_parser("adjust", parents=[_common(True)], help="人工調整（每次必附 --note）")
    la.add_argument("--date", metavar="DATE",
                    help="預設紐約今日；--deposit 的日期不得早於最後淨值日，等於時改記下一交易日")
    la.add_argument("--note", required=True, help="調整理由（必填）")
    la.add_argument("--cash", type=float, help="調帳：現金增減，報酬計算視為帳戶內損益（入金／出金請用 --deposit）")
    la.add_argument("--deposit", type=float, help="外部入金（正）／出金（負）：報酬計算視為外部資金，不算損益")
    la.add_argument("--ticker", help="代號（與 --qty／--avg-cost 併用）")
    la.add_argument("--qty", type=int, help="設定持股股數（0 ＝ 刪除該持股）")
    la.add_argument("--avg-cost", type=float, help="設定每股平均成本")
    la.add_argument("--first-buy-date", metavar="DATE", help="設定建倉日（分拆／併購換得的新股沿用原持股建倉日）")
    la.add_argument("--resolve", type=int, metavar="ID", help="解除指定的未結異常")
    la.add_argument("--unpause", metavar="T",
                    help="解除該代號所有暫停異常（含人工暫停）；公司行動工作失敗只解除暫停、不結案（下次 daily 照樣重派），"
                         "同一指令以 --ticker 同代號改了股數（已人工入帳）則一併結案")
    la.add_argument("--unpause-all", action="store_true", help="解除人工全域暫停（ledger pause --all）")
    la.add_argument("--resolve-corp-action", type=int, metavar="CA_ID",
                    help="待人工的公司行動列（pending_human／anomaly）結案：人工調帳完成後同一指令附上")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("trade", parents=common,
                       help="人工下單（換股、相關群減碼等需人工決定的交易）：寫 MANUAL 決策日誌，委託於下一次 daily 撮合")
    tsub = p.add_subparsers(dest="trade_cmd", metavar="子命令", required=True)
    for side, hlp in (("buy", "人工買進（限價必填；委託未成交約 21 曆日後過期）"),
                      ("sell", "人工賣出（省略 --limit＝市價；限價賣單當日有效，未觸及即作廢）")):
        tp = tsub.add_parser(side, parents=[_common(True)], help=hlp + "；不做上限、冷靜期與現金下限的自動檢查")
        tp.add_argument("--ticker", required=True)
        tp.add_argument("--qty", type=int, required=True)
        tp.add_argument("--limit", type=float, required=(side == "buy"), help="限價（每股）")
        tp.add_argument("--note", required=True, help="理由（必填，寫進決策日誌）")
        tp.add_argument("--anomaly", type=int, metavar="ID", help="對應的異常編號（switch_candidate／corr_group_trim）")
        tp.add_argument("--date", metavar="DATE", help="委託日（預設紐約今日）；於之後的交易日撮合")
    p.set_defaults(func=cmd_trade)

    p = sub.add_parser("decisions", parents=common, help="決策管理：void（作廢決策，不刪列）")
    dsub_ = p.add_subparsers(dest="decisions_cmd", metavar="子命令", required=True)
    dv = dsub_.add_parser("void", parents=[_common(True)],
                          help="作廢決策：標記 voided、取消其未成交委託；daily、加碼許可、分析頁與檢討報告不再採用")
    dv.add_argument("ids", nargs="+", metavar="DECISION_ID")
    dv.add_argument("--note", required=True, help="作廢理由（必填）")
    dv.add_argument("--superseded-by", metavar="DECISION_ID", help="取代它的新決策（選填）")
    p.set_defaults(func=cmd_decisions)

    p = sub.add_parser("registry", parents=common, help="登錄表管理：retire（下市／不再追蹤）")
    rsub = p.add_subparsers(dest="registry_cmd", metavar="子命令", required=True)
    rr = rsub.add_parser("retire", parents=[_common(True)],
                         help="改為 RETIRED（無持股、無未成交委託才可；不刪列）：不再抓行情、公司行動與觀察名單檢查")
    rr.add_argument("ticker")
    rr.add_argument("--note", required=True)
    p.set_defaults(func=cmd_registry)

    p = sub.add_parser("db", parents=common, help="資料庫：backup（本機快照到 state/backups/）")
    bsub = p.add_subparsers(dest="db_cmd", metavar="子命令", required=True)
    bb = bsub.add_parser("backup", parents=[_common(True)],
                         help="先 integrity_check，再寫 state/backups/trader-時間[-note].db；保留最近 14 份＋每月一份")
    bb.add_argument("--note")
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("desktop", parents=common, help="桌面小儀表板與開機自動啟動：build／install／uninstall／status")
    dsub = p.add_subparsers(dest="desktop_cmd", metavar="子命令", required=True)
    dsub.add_parser("build", parents=[_common(True)], help="只編譯小儀表板 app")
    di = dsub.add_parser("install", parents=[_common(True)], help="編譯並以 launchd 安裝伺服器與小儀表板（開機自動啟動）")
    di.add_argument("--port", type=int, help="預設 config dashboard.port")
    di.add_argument("--no-widget", action="store_true", help="只安裝伺服器")
    dsub.add_parser("uninstall", parents=[_common(True)], help="卸載並刪除 launchd 設定")
    dsub.add_parser("status", parents=[_common(True)], help="launchd 狀態")
    p.set_defaults(func=cmd_desktop)

    from .report.export import DATASETS, FORMATS
    p = sub.add_parser("export", parents=common, help="匯出持股、交易、分析、淨值等（CSV／JSON／XLSX）")
    p.add_argument("--what", default="all", help=f"all 或逗號分隔：{','.join(DATASETS)}")
    p.add_argument("--format", choices=FORMATS, default="xlsx")
    p.add_argument("--out", metavar="DIR", help="預設 state/exports")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("validate-report", parents=common, help="前瞻紙上交易檢討（procedure/13 §3）")
    p.add_argument("--start", metavar="DATE", help="預設首個淨值日")
    p.add_argument("--end", metavar="DATE", help="預設最後淨值日")
    p.add_argument("--no-prices", action="store_true", help="不連 Alpaca（略過每筆買進與母體報酬）")
    p.add_argument("--out", metavar="DIR", help="預設 state/reports")
    p.set_defaults(func=cmd_validate_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    if extra:
        parser.error(f"無法辨識的參數：{' '.join(extra)}")
    if not _writes_state(args) or os.environ.get("TRADER_PIPELINE_LOCK_HELD") == str(os.getppid()):
        # 排程子行程：父行程（值＝其 pid）已持寫入鎖（scheduler.subprocess_runner 設此變數）；
        # 只認直接父行程，殘留或手動匯出的變數不能繞過鎖（X25）
        return int(func(args))
    from .scheduler import PipelineBusy, pipeline_lock
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    with ExitStack() as stack:
        try:
            stack.enter_context(pipeline_lock(paths, "trader " + " ".join(sys.argv[1:] if argv is None else argv),
                                              wait=False))
        except PipelineBusy as e:
            print(f"[WAIT] 另一個會寫入 state 的指令正在執行：{e}；等它結束後再執行")
            if func is cmd_run_approved:   # 儀表板啟動的本行程：補 finished_at，網頁才不會標成「沒有結果」（A3-M8B-06）
                from .pipelines.approved import read_status, write_status
                st = read_status(paths)
                if st and st.get("pid") == os.getpid() and not st.get("finished_at"):
                    from .db import now_iso
                    write_status(paths, {**st, "finished_at": now_iso(), "exit_code": 2, "busy": True,
                                         "error": f"寫入鎖被占用：{e}"})
            return 2
        return int(func(args))


def _writes_state(args: argparse.Namespace) -> bool:
    """會寫 state 的指令（D10）：先非阻塞取 state/.pipeline.lock，取不到就 exit 2。"""
    f = args.func
    if f in (cmd_analyze, cmd_review, cmd_bootstrap, cmd_screen, cmd_run_approved, cmd_run_job, cmd_trade,
             cmd_decisions, cmd_registry):
        return True
    if f is cmd_jobs:
        return args.jobs_cmd != "list"   # retry 與 abandon 都寫 jobs／approvals（X25）；list 唯讀
    if f is cmd_daily:
        return not args.dry_run
    if f is cmd_ledger:
        return args.ledger_cmd != "show"
    if f is cmd_approvals:   # reject 寫決策、defer 改同意狀態（A3-M8B-02）
        return args.approvals_cmd in ("reject", "defer") or (args.approvals_cmd == "approve" and bool(args.run))
    if f is cmd_fetch:   # 凍結 P/E 中位寫 state（A3-M8B-02）
        return bool(args.freeze_pe or args.refreeze)
    return False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
