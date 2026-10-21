"""常駐排程（M7）：沒有期限，直到使用者停止（`trader scheduler uninstall` 或 Ctrl-C）。

每個交易日（紐約時間 schedule.daily_close 之後）依序：
1. 每月凍結 P/E 中位：當月第 schedule.monthly_freeze 日之後的第一個交易日，且當月尚未凍結 → `fetch --freeze-pe`。
2. 每日收盤 `daily --date D`（含持股觸發檢查）。電腦關機或睡眠錯過的交易日，醒來後依日期順序補跑
   （只補到排程第一次啟動日 start_date，不回補更早的歷史）。
3. 已同意的強模型工作 → `run-approved`（同意＝使用者已授權花費）。
4. 觀察名單檢查：距上次 ≥ schedule.review_watch（biweekly＝14 天、weekly＝7 天）→ `review --scope watch`。
5. 每季初選＋深度分析：schedule.quarterly_screen 的日期（MM-DD）過後第一次 → `bootstrap --asof D`。
   上次初選日＝scheduler.json 的 last_screen 與 runs 表「已完成且有分析 run 的 screen-*」取較晚者；
   兩者都沒有時不自動初選，記 [WAIT] 與一筆 scheduler_no_last_screen 異常（D09；換機用
   `bootstrap --tickers … --mark-screened`）。初選完成但分析中斷時，重試改跑 `analyze --asof D` 續跑。
6. 每月檢討報告：進入新月份 → `validate-report`（寫到 state/reports/monthly/）。
安裝前提（`scheduler install`）：初始 bootstrap 已完成（登錄表有分析結果、沒有待同意或等待同意的工作）。
執行中（run／once／tick）只要求登錄表非空。待同意項目，或有更早的 daily 尚未完成時，daily、freeze、report 照跑，
只暫緩會用「今天」產生新分析的 watch 與季度初選（記 [WAIT]，同一原因一天記一次），待處理完再補；
run-approved 不受待同意影響，只在所有 daily 補完後才排。

每項工作以子行程執行（輸出 state/logs/scheduler-tasks.log）。失敗 30 分鐘後重試。
- daily 永不放棄（D08）：連續 3 次失敗後改成每次新收盤後重試一次（仍依日期順序，模型步驟受每日預算約束），
  並記一筆 pause_trading=0 的 ledger_stalled 異常（未結時不重寫；該日 daily 成功即結案）。
- 觀察名單檢查連續 3 次失敗：記 scheduler_task_failed 異常，下一個觀察週期再試（D81，不永久放棄）。
- 其餘工作連續 3 次失敗即放棄並記 scheduler_task_failed 異常，其餘工作照常。
daily 成功後寫一份帳本本機快照 state/backups/（db.backup：保留最近 14 份＋每月一份；失敗只記錄，M8-D4-01）。
`trader scheduler reset [任務]` 清除失敗次數、重試等待與放棄狀態（含 analyze 續跑標記）。
排程本身（tick）連續例外 30 次 → 記一筆 scheduler_error 異常（未結不重寫，M8-D4-05）。
已同意列只剩失敗工作（close_finished 留 approved，M8-D5-04）時不排 run-approved，等使用者 retry／abandon；
預算暫停（runs.status='paused_budget'）的 run 也不排，只由使用者「續跑」或 `trader jobs resume` 續跑（D44，approvals.runnable）。
analyze 續跑：子行程 exit≠0 但 analyze run 已跑完（只是個別工作失敗）即算完成；放棄、有更新季度到期，
或 analyze-D 為 paused_budget（D44，記一次 [WAIT]）時丟棄續跑標記。強模型工作照同意閘門停在等待同意。
狀態檔 state/scheduler.json；同一時間只允許一個排程（state/.scheduler.lock）；
執行工作時持有 state/.pipeline.lock（等待取得），子行程設 TRADER_PIPELINE_LOCK_HELD=<排程 pid>，
cli 只在 os.getppid() 等於該值時不再取鎖（X25）；手動的寫入指令由 cli.main 非阻塞取同一把鎖，取不到就提示並 exit 2（D10）。
鎖是 flock：行程結束（含被 SIGTERM／SIGKILL）即釋放。排程本身收到 SIGTERM 時先結束子行程群組再退出。
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from . import db
from .calendar import TradingCalendar
from .config import Config
from .data.pe_median import _read_frozen, freeze_path
from .paths import PROJECT_DIR, Paths

STATE_FILE = "scheduler.json"
LOCK_FILE = ".scheduler.lock"
PIPELINE_LOCK = ".pipeline.lock"
RETRY_MINUTES = 30
MAX_FAILURES = 3
WATCH_EVERY_DAYS = {"biweekly": 14, "weekly": 7}   # schedule.review_watch（config 驗證只接受這兩個值）
TASK_TIMEOUT_S = 8 * 3600
TERM_GRACE_S = 30.0   # 逾時送 SIGTERM 後等引擎收尾的秒數，之後 SIGKILL 整個行程群組
ERROR_ANOMALY_AFTER = 30   # tick 連續例外這麼多次 → 記一筆 scheduler_error 異常（同類未結不重寫，M8-D4-05）


class SchedulerBusy(Exception):
    """已有另一個排程在執行。"""


class PipelineBusy(Exception):
    """寫入鎖被另一個指令占用；訊息為持鎖者。"""


class NotBootstrapped(Exception):
    """初始 bootstrap（初選＋15 檔分析）尚未完成，不得啟動排程。"""


def registry_block(conn) -> str | None:
    """執行中的前提：登錄表至少要有分析結果。回傳不能執行的理由。"""
    if conn.execute("SELECT COUNT(*) FROM registry").fetchone()[0] == 0:
        return "登錄表是空的：請先 `trader bootstrap --asof 日期` 完成初始 15 檔分析"
    return None


def pending_work(conn) -> str | None:
    """還有等使用者決定的項目（待同意／延後的同意列、等待同意的工作）時回傳說明；否則 None。
    已同意（approved）由排程跑 run-approved；孤兒 running、預算暫停的 pending 不擋排程（M7-D2-02）。"""
    n = conn.execute("SELECT COUNT(*) FROM approvals WHERE status IN ('pending','deferred')").fetchone()[0]
    if n:
        return f"還有 {n} 檔強模型分析待同意：同意（或拒絕）後再繼續"
    n = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='awaiting_approval'").fetchone()[0]
    if n:
        return f"還有 {n} 個分析工作等待同意：處理後再繼續"
    return None


def bootstrap_block(conn) -> str | None:
    """安裝排程前提：登錄表已有分析結果，且沒有未完成的分析工作或待同意項目。回傳不能啟動的理由。"""
    return registry_block(conn) or pending_work(conn)


@contextmanager
def pipeline_lock(paths: Paths, holder: str = "", wait: bool = True) -> Iterator[None]:
    """所有寫 state 的指令共用的寫入鎖。wait=False 取不到即拋 PipelineBusy（訊息為持鎖者）。
    取得後把持鎖者寫進鎖檔，供被擋下的指令顯示。"""
    with open(paths.state / PIPELINE_LOCK, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PipelineBusy(_holder_text(paths)) from None
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{holder or '?'}（pid {os.getpid()}）\n")
            fh.flush()
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _holder_text(paths: Paths) -> str:
    try:
        return (paths.state / PIPELINE_LOCK).read_text(encoding="utf-8").strip() or "持鎖者不明"
    except OSError:
        return "持鎖者不明"


def pipeline_holder(paths: Paths) -> str | None:
    """寫入鎖被占用時回傳持鎖者，否則 None（只試探、不持有）。"""
    f = paths.state / PIPELINE_LOCK
    if not f.exists():
        return None
    with open(f, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _holder_text(paths)
        fcntl.flock(fh, fcntl.LOCK_UN)
    return None


def _cli(cfg: Config, paths: Paths, *args: str) -> list[str]:
    argv = [sys.executable, "-m", "trader.cli", *args, "--state-dir", str(paths.state)]
    if cfg.source_path:
        argv += ["--config", str(cfg.source_path)]
    return argv


def subprocess_runner(paths: Paths) -> Callable[[list[str]], int]:
    def run(argv: list[str]) -> int:
        log = paths.state / "logs" / "scheduler-tasks.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n==== {datetime.now().isoformat(timespec='seconds')} {' '.join(argv[3:])}\n")
            fh.flush()
            # 子行程自成行程群組：逾時（或排程本身被中斷）時結束整個群組，不留下孤兒（M5_D4-02）
            # 父行程（tick）已持寫入鎖：子行程不再取，否則自鎖（M7-D7-04、M8-D1-01）；
            # 值為本行程 pid，cli 只在 getppid() 相符時跳過取鎖，手動匯出此變數不能繞過鎖（X25）
            proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=str(PROJECT_DIR), start_new_session=True,
                                    env={**os.environ, "TRADER_PIPELINE_LOCK_HELD": str(os.getpid())})
            try:
                return proc.wait(timeout=TASK_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                stop_group(proc)
                fh.write(f"[FAIL] 逾時 {TASK_TIMEOUT_S}s\n")
                return 124
            except BaseException:
                stop_group(proc)
                raise
    return run


@contextmanager
def _sigterm_exits() -> Iterator[None]:
    """排程本身收到 SIGTERM（launchd unload、kill）→ 轉成 SystemExit：subprocess_runner 的 BaseException 分支
    先 stop_group 收掉子行程群組、再釋放寫入鎖退出，不留孤兒繼續寫 state（P22／P31 交接）。"""
    def _exit(signum, frame):
        raise SystemExit(128 + signum)
    try:
        old = signal.signal(signal.SIGTERM, _exit)
    except ValueError:   # 非主執行緒（測試）：無法安裝處理器
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def stop_group(proc: subprocess.Popen, grace_s: float = TERM_GRACE_S) -> None:
    """先對行程群組送 SIGTERM（trader 引擎據此收尾、結束 claude 子行程），grace_s 秒後仍在則 SIGKILL。"""
    for sig, wait_s in ((signal.SIGTERM, grace_s), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue


class Scheduler:
    def __init__(self, cfg: Config, paths: Paths, runner: Callable[[list[str]], int] | None = None,
                 now: Callable[[], datetime] | None = None, calendar: Callable[[str, str], TradingCalendar] | None = None,
                 log: Callable[[str], None] | None = None):
        self.cfg, self.paths = cfg, paths
        self.runner = runner or subprocess_runner(paths)
        self.tz = ZoneInfo(cfg.schedule.get("timezone") or "America/New_York")
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calendar = calendar or self._load_calendar
        self.log = log or (lambda m: print(f"{datetime.now().isoformat(timespec='seconds')} {m}", flush=True))
        self.state_path = paths.state / STATE_FILE

    # ------------------------------------------------------------ 狀態
    def load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, st: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _load_calendar(self, start: str, end: str) -> TradingCalendar:
        alpaca = None
        try:
            from .data.inputs import make_clients
            alpaca = make_clients(self.cfg, self.paths).get("alpaca")
        except Exception:  # noqa: BLE001 — 沒有金鑰就用快取或規則
            pass
        return TradingCalendar.load(alpaca, self.paths.cache / "calendar_scheduler.json", start, end)

    # ------------------------------------------------------------ 判斷
    def _local_now(self) -> datetime:
        return self.now().astimezone(self.tz)

    def _close_time(self) -> tuple[int, int]:
        h, m = str(self.cfg.schedule.get("daily_close") or "17:30").split(":")
        return int(h), int(m)

    def _daily_done(self, conn, d: str) -> bool:
        r = conn.execute("SELECT status FROM runs WHERE run_id=?", (f"daily-{d}",)).fetchone()
        return bool(r) and r[0] == "done"

    def _has_nav(self, conn) -> bool:
        return conn.execute("SELECT COUNT(*) FROM nav_daily").fetchone()[0] >= 2

    def _last_screen(self, conn, st: dict[str, Any]) -> str | None:
        """上次初選日：scheduler.json 與 runs 表取較晚者（M8-D10-05）。runs 認「非 dry_run 的 screen 已完成」
        （summary.dry_run，M8-R3-02；沒有要分析的代號就不會有 analyze run，不再要求）；舊列沒有旗標時沿用舊條件（有 analyze run）。"""
        r = conn.execute("SELECT MAX(s.asof) FROM runs s WHERE s.run_id LIKE 'screen-%' AND s.status='done' AND ("
                         "(CASE WHEN json_valid(s.summary) THEN json_extract(s.summary, '$.dry_run') END) = 0 OR "
                         "((CASE WHEN json_valid(s.summary) THEN json_extract(s.summary, '$.dry_run') END) IS NULL "
                         "AND EXISTS (SELECT 1 FROM runs a WHERE a.run_id = 'analyze-' || s.asof)))").fetchone()[0]
        vals = [v for v in (st.get("last_screen"), r) if v]
        return max(vals) if vals else None

    def _watch_days(self) -> int:
        return WATCH_EVERY_DAYS.get(str(self.cfg.schedule.get("review_watch") or "biweekly"), 14)

    def _last_quarter_date(self, today: date) -> str | None:
        """today（含）之前最近一個每季初選日。"""
        best = None
        for y in (today.year, today.year - 1):
            for md in self.cfg.schedule.get("quarterly_screen") or []:
                try:
                    q = date(y, int(md[:2]), int(md[3:5]))
                except ValueError:
                    continue
                if q <= today and (best is None or q > best):
                    best = q
        return best.isoformat() if best else None

    def plan(self, conn, st: dict[str, Any]) -> list[tuple[str, list[str]]]:
        """回傳現在該做、且未在重試等待或已放棄的工作（依序）。self.held 記下被暫緩的理由（供 [WAIT] 與 status）。"""
        self.held: list[str] = []
        local = self._local_now()
        today = local.date()
        start = st.setdefault("start_date", today.isoformat())
        cal = self.calendar((date.fromisoformat(start) - timedelta(days=10)).isoformat(),
                            (today + timedelta(days=10)).isoformat())
        if getattr(cal, "source", "alpaca") != "alpaca":   # 日曆退回快取／規則時留底（M7-D5-08）
            st["calendar_source"] = cal.source
        else:
            st.pop("calendar_source", None)
        h, m = self._close_time()
        after_close = (local.hour, local.minute) >= (h, m)
        last = today.isoformat() if cal.is_trading_day(today) and after_close else cal.prev_trading_day(today)
        if last < start:
            return []
        mono = self.now().timestamp()
        gave = set(st.get("gave_up", []))
        retry = st.get("retry_at", {})

        def ready(k: str) -> bool:   # daily／watch 不永久放棄（D08、D81）：舊狀態檔殘留的放棄鍵也不擋
            return (k.startswith(("daily:", "watch:")) or k not in gave) and retry.get(k, 0) <= mono

        tasks: list[tuple[str, list[str]]] = []
        # 最早未完成的 daily：一次只排這一天；它在重試等待時後面的日子也不跑（淨值與撮合依帳本順序）
        daily_todo = next((d for d in cal.trading_days(start, last) if not self._daily_done(conn, d)), None)
        # 暫緩會用「今天」產生新分析的工作：待同意（D 段 H1），或更早的 daily 未完成（否則補跑時讀到未來分析，M7-D6-01）
        hold = pending_work(conn) or (f"每日收盤 {daily_todo} 尚未完成，先補完再做" if daily_todo else None)
        q = self._last_quarter_date(date.fromisoformat(last))
        ls = self._last_screen(conn, st)
        screen_due = ls is not None and q is not None and ls < q
        screen_key = f"screen:{q}"   # 穩定身分：失敗計數不因日期換而歸零（M7-D2-03）
        reanalyze = st.get("reanalyze")   # 初選已完成但分析中斷 → 改跑 analyze 續跑
        if reanalyze and q is not None and reanalyze < q:   # 已有更新的季度到期：舊續跑不得擋新初選（P23R）
            st.pop("reanalyze")
            reanalyze = None
        if reanalyze and conn.execute("SELECT 1 FROM runs WHERE run_id=? AND status='paused_budget'",
                                      (f"analyze-{reanalyze}",)).fetchone():
            # D44：預算暫停的分析不自動續跑（analyze --asof D 會接著跑暫停中的工作），只由使用者續跑
            st.pop("reanalyze")
            self.held.append(f"analyze-{reanalyze} 預算暫停（D44）：排程不續跑，請按儀表板「續跑」或 "
                             f"`trader jobs resume --run-id analyze-{reanalyze}`")
            reanalyze = None
        if reanalyze and conn.execute("SELECT 1 FROM runs WHERE run_id=? AND status='abandoned'",
                                      (f"analyze-{reanalyze}",)).fetchone():
            st.pop("reanalyze")   # 使用者已放棄該分析（jobs abandon）：清掉續跑標記，不再自動續跑（M5-RS4-01）
            reanalyze = None
        # 1. 每月凍結：只做當月；本輪會跑初選時由初選順便凍結
        freeze_day = int(str(self.cfg.schedule.get("monthly_freeze") or "01"))
        month = today.isoformat()[:7]
        screen_runs = screen_due and not hold and not reanalyze and ready(screen_key)
        if (not screen_runs and last[:7] == month and int(last[8:10]) >= freeze_day
                and _read_frozen(freeze_path(self.paths, month)) is None):  # 半截檔不算已凍結
            tasks.append((f"freeze:{month}", ["fetch", "--asof", last, "--freeze-pe"]))
        # 2. 每日收盤（補跑錯過的交易日）
        if daily_todo:
            tasks.append((f"daily:{daily_todo}", ["daily", "--date", daily_todo]))
        if not daily_todo:
            # 3. 已同意的強模型工作（M7-D2-02）
            # 只剩失敗工作的已同意列（close_finished 留 approved，M8-D5-04）不再每天空跑，等使用者 retry／abandon
            from .jobs.approvals import runnable
            if runnable(conn):
                tasks.append((f"approved:{last}", ["run-approved"]))
        lw = st.get("last_watch_review")
        watch_due = lw is None or (date.fromisoformat(last) - date.fromisoformat(lw)).days >= self._watch_days()
        if not hold:
            # 4. 觀察名單（鍵用上次檢查日：穩定身分）
            if watch_due:
                tasks.append((f"watch:{lw or 'init'}", ["review", "--scope", "watch", "--asof", last]))
            # 5. 每季初選＋深度分析
            if reanalyze:
                tasks.append((f"analyze:{reanalyze}", ["analyze", "--asof", reanalyze]))
            elif screen_due:
                tasks.append((screen_key, ["bootstrap", "--asof", last]))
        elif watch_due or screen_due or reanalyze:
            self.held.append(f"觀察名單／季度初選暫緩：{hold}")
        if ls is None and q is not None:   # D09：不自動初選，等使用者設定上次初選日
            self.held.append("找不到上次初選日：不自動初選。請執行 `trader bootstrap --asof 日期`，"
                             "或換機重做同一批時 `trader bootstrap --asof 日期 --tickers … --mark-screened`")
        # 6. 每月檢討報告
        if st.get("last_report_month") != month and self._has_nav(conn):
            out = str(self.paths.state / "reports" / "monthly")
            tasks.append((f"report:{month}", ["validate-report", "--out", out]))
        return [(k, a) for k, a in tasks if ready(k)]

    # ------------------------------------------------------------ 執行
    def _done(self, conn, st: dict[str, Any], key: str, args: list[str]) -> None:
        kind = key.split(":", 1)[0]
        asof = args[args.index("--asof") + 1] if "--asof" in args else None
        if kind == "watch":
            st["last_watch_review"] = asof
            # D81：watch 不永久放棄，成功即結案先前的 watch 失敗異常（鍵帶上次檢查日，成功後換新鍵：一併結案；A3-M7A-08）
            n = conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? "
                             "WHERE kind='scheduler_task_failed' AND status='open' "
                             "AND json_extract(detail, '$.task') LIKE 'watch:%'",
                             (db.now_iso(), f"{key} 已完成（觀察名單檢查恢復）")).rowcount
            if n:
                conn.commit()
        elif kind == "screen":
            st["last_screen"] = asof
        elif kind == "analyze":
            st.pop("reanalyze", None)
        elif kind == "report":
            st["last_report_month"] = key.split(":", 1)[1]
        elif kind == "daily":   # 帳本恢復前進：結案停擺異常
            n = conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? "
                             "WHERE kind='ledger_stalled' AND status='open'",
                             (db.now_iso(), f"{key} 已完成")).rowcount
            if n:
                conn.commit()
            try:   # 帳本本機快照（M8-D4-01）：失敗只記錄，不讓 daily 變失敗
                self.log(f"[BACKUP] {db.backup(self.paths.db, note=key.replace(':', '-'))}")
            except Exception as e:  # noqa: BLE001
                self.log(f"[WARN] 帳本快照失敗：{type(e).__name__}: {e}")
        st.get("failures", {}).pop(key, None)
        st.get("retry_at", {}).pop(key, None)

    def _next_close_ts(self) -> float:
        """下一個交易日收盤（schedule.daily_close）的時間戳；今天是交易日且尚未到收盤時間就是今天。"""
        local = self._local_now()
        h, m = self._close_time()
        cal = self.calendar((local.date() - timedelta(days=10)).isoformat(),
                            (local.date() + timedelta(days=20)).isoformat())
        d = local.date()
        for _ in range(30):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)
            if t > local and cal.is_trading_day(d):
                return t.timestamp()
            d += timedelta(days=1)
        return self.now().timestamp() + 86400

    def _failed(self, conn, st: dict[str, Any], key: str, code: int, started_at: str = "") -> None:
        fails = st.setdefault("failures", {})
        fails[key] = fails.get(key, 0) + 1
        detail = {"task": key, "exit_code": code, "failures": fails[key], "started_at": started_at,
                  "log": "state/logs/scheduler-tasks.log"}
        if fails[key] >= MAX_FAILURES and key.startswith("daily:"):
            # D08：daily 不放棄（放棄＝之後每天都不撮合、不賣出、不記淨值），改成每次新收盤後重試一次
            nxt = self._next_close_ts()
            st.setdefault("retry_at", {})[key] = nxt
            if conn.execute("SELECT 1 FROM anomalies WHERE kind='ledger_stalled' AND status='open'").fetchone() is None:
                db.insert(conn, "anomalies", {"ticker": None, "kind": "ledger_stalled",
                                              "detail": json.dumps(detail, ensure_ascii=False),
                                              "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
                conn.commit()
            when = datetime.fromtimestamp(nxt, self.tz).isoformat(timespec="minutes")
            self.log(f"[STALLED] {key} 連續 {fails[key]} 次失敗：帳本停在這天（撮合、賣出、淨值都不前進），"
                     f"下次收盤後（{when}）再試；查原因後可 `trader scheduler reset` 立即重試")
        elif fails[key] >= MAX_FAILURES and key.startswith("watch:"):
            # D81：觀察名單檢查不永久放棄（穩定鍵＝放棄後永遠不再檢查），改成下一個 watch 週期再試一次並保留異常提示
            nxt = self.now().timestamp() + self._watch_days() * 86400
            st.setdefault("retry_at", {})[key] = nxt
            if conn.execute("SELECT 1 FROM anomalies WHERE kind='scheduler_task_failed' AND status='open' "
                            "AND json_extract(detail, '$.task') = ?", (key,)).fetchone() is None:
                db.insert(conn, "anomalies", {"ticker": None, "kind": "scheduler_task_failed",
                                              "detail": json.dumps(detail, ensure_ascii=False),
                                              "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
                conn.commit()
            when = datetime.fromtimestamp(nxt, self.tz).isoformat(timespec="minutes")
            self.log(f"[RETRY LATER] {key} 連續 {fails[key]} 次失敗，已記異常；下一個觀察週期（{when}）再試（D81）")
        elif fails[key] >= MAX_FAILURES:
            st.setdefault("gave_up", []).append(key)
            st.get("retry_at", {}).pop(key, None)
            if key.startswith("analyze:"):   # 放棄續跑：清 reanalyze，否則之後每季初選都被擋（P23R）
                st.pop("reanalyze", None)
                detail["note"] = (f"初選 {key[8:]} 的深度分析未完成，排程已放棄續跑（下一季初選照常）；"
                                  f"可手動 `trader analyze --asof {key[8:]}` 或 `trader jobs retry --run-id analyze-{key[8:]}`")
            db.insert(conn, "anomalies", {"ticker": None, "kind": "scheduler_task_failed",
                                          "detail": json.dumps(detail, ensure_ascii=False),
                                          "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
            conn.commit()
            self.log(f"[GIVE UP] {key} 連續 {fails[key]} 次失敗，已記異常（`trader scheduler reset {key}` 可重來）")
        else:
            st.setdefault("retry_at", {})[key] = self.now().timestamp() + RETRY_MINUTES * 60
            self.log(f"[RETRY] {key} 失敗（exit {code}），{RETRY_MINUTES} 分鐘後重試")

    def reset(self, task: str | None = None) -> list[str]:
        """清除失敗次數、重試等待與放棄狀態（全部或單一任務鍵）；回傳被清除的鍵。"""
        st = self.load_state()
        keys = set()
        for name in ("failures", "retry_at"):
            d = st.get(name, {})
            for k in [k for k in d if task in (None, k)]:
                keys.add(k)
                d.pop(k)
        gave = st.get("gave_up", [])
        keys |= {k for k in gave if task in (None, k)}
        st["gave_up"] = [k for k in gave if task not in (None, k)]
        r = st.get("reanalyze")
        if r and task in (None, f"analyze:{r}"):   # 一併清分析續跑，免得它繼續擋季度初選（P23R）
            keys.add(f"analyze:{r}")
            st.pop("reanalyze")
        self.save_state(st)
        return sorted(keys)

    def _waits(self, conn, st: dict[str, Any]) -> None:
        """暫緩理由一天只記一次；找不到上次初選日另記一筆異常（未結不重寫）。"""
        day = self._local_now().date().isoformat()
        logged = st.setdefault("wait_logged", {})
        for why in self.held:
            if logged.get(why) != day:
                logged[why] = day
                self.log(f"[WAIT] {why}")
            if why.startswith("找不到上次初選日") and conn.execute(
                    "SELECT 1 FROM anomalies WHERE kind='scheduler_no_last_screen' AND status='open'").fetchone() is None:
                db.insert(conn, "anomalies", {"ticker": None, "kind": "scheduler_no_last_screen",
                                              "detail": json.dumps({"note": why}, ensure_ascii=False),
                                              "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
                conn.commit()
        for why in [w for w in logged if w not in self.held]:
            logged.pop(why)
        if not any(w.startswith("找不到上次初選日") for w in self.held) and conn.execute(
                "UPDATE anomalies SET status='resolved', resolved_at=?, resolution='已有上次初選日' "
                "WHERE kind='scheduler_no_last_screen' AND status='open'", (db.now_iso(),)).rowcount:
            conn.commit()

    def _ran_through(self, conn, key: str, args: list[str]) -> bool:
        """子行程 exit≠0 但流程已跑完：watch／analyze 只有部分工作失敗（交給 job_failed 異常，M7-D2-03、P23R）。"""
        asof = args[args.index("--asof") + 1] if "--asof" in args else ""
        run_id = {"watch": f"review-{asof}-watch", "analyze": f"analyze-{asof}"}.get(key.split(":", 1)[0])
        if run_id is None:
            return False
        r = conn.execute("SELECT status, summary FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return bool(r) and r[0] != "running" and "error" not in json.loads(r[1] or "{}")

    def check_ready(self, strict: bool = True) -> None:
        """strict（安裝時）：登錄表非空且無待同意／未完成工作；非 strict（執行中）：只擋登錄表為空。"""
        conn = db.connect(self.paths.db)
        try:
            db.migrate(conn)
            why = bootstrap_block(conn) if strict else registry_block(conn)
        finally:
            conn.close()
        if why:
            raise NotBootstrapped(why)

    def tick(self) -> list[str]:
        """做完目前所有該做的工作（一項一項做，每做完一項重新判斷），回傳完成的工作。"""
        self.check_ready(strict=False)   # 待同意不得讓 daily／撮合／淨值／賣出停擺（H1）
        done: list[str] = []
        while True:
            conn = db.connect(self.paths.db)
            try:
                db.migrate(conn)
                st = self.load_state()
                todo = self.plan(conn, st)
                self._waits(conn, st)
                self.save_state(st)  # 保存 start_date
            finally:
                conn.close()
            if not todo:
                return done
            key, args = todo[0]
            self.log(f"[RUN] {key}")
            started = self.now().isoformat(timespec="seconds")
            with pipeline_lock(self.paths, f"排程 {key}"):
                code = self.runner(_cli(self.cfg, self.paths, *args))
            conn = db.connect(self.paths.db)
            try:
                st = self.load_state()
                ok = code == 0 or self._ran_through(conn, key, args)
                if ok and key.startswith("daily:"):
                    ok = self._daily_done(conn, key[6:])
                elif ok and key.startswith("freeze:"):
                    ok = _read_frozen(freeze_path(self.paths, key[7:])) is not None
                if (not ok and key.startswith("screen:") and conn.execute(
                        "SELECT 1 FROM runs WHERE run_id=? AND status='done'", (f"screen-{args[2]}",)).fetchone()):
                    # 初選已完成、分析中斷：記為已初選，改排 analyze 續跑（同 asof 再 bootstrap 會被擋）
                    st["reanalyze"] = args[2]
                    ok = True
                    self.log(f"[RETRY] {key} 初選已完成但分析未完成（exit {code}），改排 analyze --asof {args[2]} 續跑")
                if ok:
                    self._done(conn, st, key, args)
                    done.append(key)
                    self.log(f"[OK] {key}")
                else:
                    self._failed(conn, st, key, code, started)
                st["last_tick"] = self.now().isoformat(timespec="seconds")
                self.save_state(st)
            finally:
                conn.close()

    def run_forever(self, interval_s: int = 60) -> None:
        with open(self.paths.state / LOCK_FILE, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SchedulerBusy("已有另一個排程在執行") from None
            self.check_ready(strict=False)
            self.log("排程啟動（沒有期限；停止：trader scheduler uninstall 或 Ctrl-C）")
            with _sigterm_exits():
                errors = 0
                while True:
                    try:
                        self.tick()
                        errors = 0
                    except NotBootstrapped as e:
                        self.log(f"[WAIT] {e}")
                    except Exception as e:  # noqa: BLE001 — 排程本身不中斷
                        errors += 1
                        self.log(f"[ERROR] {type(e).__name__}: {e}")
                        if errors >= ERROR_ANOMALY_AFTER:
                            self._record_error(e, errors)
                    time.sleep(interval_s)

    def _record_error(self, e: Exception, n: int) -> None:
        """tick 連續例外：寫一筆 scheduler_error 異常（未結時不重寫），讓儀表板看得到排程停擺（M8-D4-05）。寫不進去只記 log。"""
        try:
            conn = db.connect(self.paths.db)
            try:
                if conn.execute("SELECT 1 FROM anomalies WHERE kind='scheduler_error' AND status='open'").fetchone() is None:
                    db.insert(conn, "anomalies", {"ticker": None, "kind": "scheduler_error", "status": "open",
                                                  "pause_trading": 0, "created_at": db.now_iso(),
                                                  "detail": json.dumps({"error": f"{type(e).__name__}: {e}"[:500],
                                                                        "consecutive": n,
                                                                        "log": "state/logs/scheduler.log"},
                                                                       ensure_ascii=False)})
                    conn.commit()
            finally:
                conn.close()
        except Exception as e2:  # noqa: BLE001
            self.log(f"[ERROR] 無法記 scheduler_error 異常：{type(e2).__name__}: {e2}")

    def once(self) -> list[str]:
        with open(self.paths.state / LOCK_FILE, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SchedulerBusy("已有另一個排程在執行") from None
            with _sigterm_exits():
                return self.tick()
