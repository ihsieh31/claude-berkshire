"""命令列介面：trader init／doctor／run-job（M0），其餘子命令先註冊、執行時回 2。"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Config, ConfigError, load_config, resolve_config_path
from .db import connect, migrate
from .llm.base import call_with_retry, extract_json
from .models import JOB_TYPES, JobSpec
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
    conn = connect(paths.db)
    try:
        migrate(conn)
        tickers = [t for t in args.tickers.split(",")] if args.tickers else None
        out = Path(args.out) if args.out else paths.work / f"inputs-{args.asof}"
        summary = fetch_inputs(cfg, paths, conn, args.asof, out, tickers=tickers, freeze_pe=args.freeze_pe)
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    from .pipelines.screen import run_screen
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        summary = run_screen(cfg, paths, conn, args.asof, top=args.top, special_max=args.special_max,
                             inputs_dir=Path(args.inputs) if args.inputs else None)
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
    _awaiting_hint(summary["engine"])
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
    _awaiting_hint(summary.get("engine"))
    if "error" in summary:
        return 1
    return 0 if (summary.get("engine") or {}).get("failed", 0) == 0 else 1


def _awaiting_hint(engine_result: dict | None) -> None:
    n = (engine_result or {}).get("awaiting_approval", 0)
    if n:
        print(f"[WAIT] {n} 個強模型工作等待同意：trader approvals list → trader approvals approve … → trader run-approved")


def _http_get(url: str, headers: dict | None = None, timeout: float = 10.0) -> Any:
    """網路請求的進入點；測試以 monkeypatch 替換，避免真的連網。"""
    return httpx.get(url, headers=headers or {}, timeout=timeout, follow_redirects=True)


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


def _check_models(cfg: Config, results: list) -> None:
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
        with tempfile.TemporaryDirectory() as sandbox:
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
                runner = build_runner(tier)
                res = call_with_retry(runner, job, retries=0)
                parsed = res.parsed_json or extract_json(res.text)
                if isinstance(parsed, dict) and parsed.get("ok") is True:
                    _report(results, "OK", label, f"回應正確（{res.latency_s:.1f}s）")
                else:
                    _report(results, "FAIL", label, "回應不是 {\"ok\": true}")
            except Exception as e:  # noqa: BLE001 - 任何錯誤都要顯示為 FAIL
                _report(results, "FAIL", label, _redact(f"{type(e).__name__}: {e}", secrets))


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load_or_report(getattr(args, "config", None))
    if cfg is None:
        return 1
    results: list[tuple[str, str, str]] = []
    _check_network(cfg, args.timeout, results)
    if not args.skip_models:
        _check_models(cfg, results)
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
            summary = run_daily(cfg, paths, conn, args.date, skip_review=args.skip_review, dry_run=args.dry_run)
        except DailyError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return 1 if "error" in summary else 0


def cmd_ledger(args: argparse.Namespace) -> int:
    from datetime import date as _today_cls
    from .ledger.book import LedgerError, adjust, show
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            if args.ledger_cmd == "show":
                # 不連網：持股以平均成本估值（closes=None），摘要內 missing_close 會列出缺價代號
                result = show(conn, None)
            else:
                result = adjust(conn, date=args.date or _today_cls.today().isoformat(), cash_delta=args.cash,
                                ticker=args.ticker, set_qty=args.qty, set_avg_cost=args.avg_cost,
                                resolve_anomaly=args.resolve, unpause=args.unpause,
                                first_buy_date=args.first_buy_date, note=args.note)
        except LedgerError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


def _run_approved(cfg, paths, conn, run_id=None) -> int:
    from .pipelines.approved import RunApprovedBusy, run_approved
    try:
        result = run_approved(cfg, paths, conn, run_id=run_id)
    except RunApprovedBusy as e:
        print(f"[FAIL] {e}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 1 if any(r.get("failed") for r in result["runs"].values()) else 0


def cmd_jobs(args: argparse.Namespace) -> int:
    """jobs retry：failed 工作重設為 pending 並由引擎續跑（會呼叫模型；不刪 llm_calls）。"""
    from .jobs.engine import Engine, retry_failed
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
        ids = retry_failed(conn, args.run_id, ticker=args.ticker, job_id=args.job_id)
        print(f"已重設為 pending：{ids or '無（沒有可重跑的 failed 工作；CORP_ACTION 由 daily 重派）'}")
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
                          f"asof {x['asof']}  run {x['run_id']}  {'持股 ' if x['holding'] else ''}{x['reason'] or ''}")
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
                print(f"已拒絕，決策：{A.reject(conn, paths, ids, via='cli', note=args.note)}")
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
        serve(paths, cfg.dashboard.host, port, cfg.source_path, charts=ChartService(alpaca.http) if alpaca else None)
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
    from datetime import date as _d
    from .report import export as X
    paths = get_paths(getattr(args, "state_dir", None)).ensure()
    names = list(X.DATASETS) if args.what == "all" else [w.strip() for w in args.what.split(",") if w.strip()]
    conn = connect(paths.db)
    try:
        migrate(conn)
        try:
            out = X.export(conn, names, args.format, Path(args.out) if args.out else paths.state / "exports",
                           _d.today().isoformat())
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
        try:
            summary = run_bootstrap(cfg, paths, conn, args.asof, tickers=tickers, top=args.top, dry_run=args.dry_run)
        except AnalyzeError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    if "error" in summary:
        return 1
    eng = (summary.get("analyze") or {}).get("engine") or {}
    if not args.dry_run and not args.tickers:
        from .scheduler import Scheduler
        sch = Scheduler(cfg, paths)
        st = sch.load_state()
        st["last_screen"] = args.asof  # 排程據此判斷下次每季初選
        sch.save_state(st)
    _awaiting_hint(eng)
    return 0 if eng.get("failed", 0) == 0 else 1


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
        print("現在該做：" + ("、".join(k for k, _ in todo) if todo else "無（等下一個收盤）"))
        return 0
    if sc == "print":
        import plistlib
        if args.cron:
            argv = " ".join(f'"{a}"' for a in S._cli(cfg, paths, "scheduler", "once"))
            print(f'*/10 * * * * cd "{S.PROJECT_DIR}" && {argv} >> "{paths.state}/logs/scheduler.log" 2>&1')
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
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("bootstrap", parents=common, help="初選＋深度分析（建立初始名單；每季初選也用它；會呼叫模型）")
    p.add_argument("--asof", required=True, metavar="DATE")
    p.add_argument("--tickers", help="逗號分隔；給定則略過初選，直接分析這些代號")
    p.add_argument("--top", type=int, default=15, help="核心短名單檔數（預設 15）")
    p.add_argument("--dry-run", action="store_true", help="只跑初選並列出將分析的代號，不建分析工作")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("screen", parents=common, help="季度初選：整個股票池 → skill screen.py → 交接檔")
    p.add_argument("--asof", required=True, metavar="DATE")
    p.add_argument("--top", type=int, default=15, help="核心短名單檔數（預設 15）")
    p.add_argument("--special-max", type=int, default=0, help="特殊情境交棒上限（預設 0）")
    p.add_argument("--inputs", metavar="DIR", help="沿用既有 features.csv／prices.csv 目錄，不重抓")
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

    p = sub.add_parser("jobs", parents=common, help="工作管理：retry（重跑 failed 工作，會呼叫模型）")
    jsub = p.add_subparsers(dest="jobs_cmd", metavar="子命令", required=True)
    jr = jsub.add_parser("retry", parents=[_common(True)], help="把 run 內 failed 工作重設為 pending 並續跑")
    jr.add_argument("--run-id", required=True)
    jr.add_argument("--ticker")
    jr.add_argument("--job-id")
    jr.add_argument("--no-run", action="store_true", help="只重設，不立即執行")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("run-approved", parents=common, help="執行已同意的強模型工作（會呼叫模型）")
    p.add_argument("--run-id", help="只執行此 run 的已同意項目")
    p.set_defaults(func=cmd_run_approved)

    p = sub.add_parser("scheduler", parents=common, help="常駐排程（沒有期限）：run／once／install／uninstall／status／print")
    ssub = p.add_subparsers(dest="scheduler_cmd", metavar="子命令", required=True)
    sr = ssub.add_parser("run", parents=[_common(True)], help="前景常駐執行（Ctrl-C 停止）")
    sr.add_argument("--interval", type=int, default=60, help="檢查間隔秒數（預設 60）")
    ssub.add_parser("once", parents=[_common(True)], help="做完目前該做的工作就結束（給 cron 用）")
    ssub.add_parser("install", parents=[_common(True)], help="以 launchd 安裝並啟動（開機自動啟動）")
    ssub.add_parser("uninstall", parents=[_common(True)], help="停止並移除 launchd 排程")
    ssub.add_parser("status", parents=[_common(True)], help="排程狀態與現在該做的工作")
    sp = ssub.add_parser("print", parents=[_common(True)], help="印出 launchd plist（或 --cron 的 crontab 行）")
    sp.add_argument("--cron", action="store_true")
    p.set_defaults(func=cmd_scheduler)

    p = sub.add_parser("dashboard", parents=common, help="本機儀表板 API 與網頁（127.0.0.1）")
    p.add_argument("--port", type=int, help="預設 config dashboard.port（8787）")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("ledger", parents=common, help="紙上帳本：show（摘要）、adjust（人工調整）")
    lsub = p.add_subparsers(dest="ledger_cmd", metavar="子命令", required=True)
    ls = lsub.add_parser("show", parents=[_common(True)], help="現金、持股、委託與未結異常")
    ls.add_argument("--date", metavar="DATE", help="僅供標示（摘要不連網取價）")
    la = lsub.add_parser("adjust", parents=[_common(True)], help="人工調整（每次必附 --note）")
    la.add_argument("--date", metavar="DATE", help="預設今日")
    la.add_argument("--note", required=True, help="調整理由（必填）")
    la.add_argument("--cash", type=float, help="現金增減（正數入金、負數出金）")
    la.add_argument("--ticker", help="代號（與 --qty／--avg-cost 併用）")
    la.add_argument("--qty", type=int, help="設定持股股數（0 ＝ 刪除該持股）")
    la.add_argument("--avg-cost", type=float, help="設定每股平均成本")
    la.add_argument("--first-buy-date", metavar="DATE", help="設定建倉日（分拆／併購換得的新股沿用原持股建倉日）")
    la.add_argument("--resolve", type=int, metavar="ID", help="解除指定的未結異常")
    la.add_argument("--unpause", metavar="T", help="解除該代號所有暫停異常")
    p.set_defaults(func=cmd_ledger)

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
    return int(func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
