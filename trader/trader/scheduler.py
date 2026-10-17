"""常駐排程（M7）：沒有期限，直到使用者停止（`trader scheduler uninstall` 或 Ctrl-C）。

每個交易日（紐約時間 schedule.daily_close 之後）依序：
1. 每月凍結 P/E 中位：當月第 schedule.monthly_freeze 日之後的第一個交易日，且當月尚未凍結 → `fetch --freeze-pe`。
2. 每日收盤 `daily --date D`（含持股觸發檢查）。電腦關機或睡眠錯過的交易日，醒來後依日期順序補跑
   （只補到排程第一次啟動日 start_date，不回補更早的歷史）。
3. 觀察名單檢查：距上次 ≥ 14 天 → `review --scope watch`。
4. 每季初選＋深度分析：schedule.quarterly_screen 的日期（MM-DD）過後第一次 → `bootstrap --asof D`；
   上次初選日由 `trader bootstrap` 寫入狀態檔。
安裝前提（`scheduler install`）：初始 bootstrap 已完成（登錄表有分析結果、沒有未完成的分析工作或待同意項目）。
執行中（run／once／tick）只要求登錄表非空；有待同意或未完成工作時 daily、freeze、report 照跑，
只暫緩會新增強模型工作的 watch 與季度初選，待處理完再補。
5. 每月檢討報告：進入新月份 → `validate-report`（寫到 state/reports/monthly/）。

每項工作以子行程執行（輸出 state/logs/scheduler-tasks.log）。失敗 30 分鐘後重試，連續 3 次失敗即放棄該項並記一筆
異常（不暫停交易），其餘工作照常。強模型工作照同意閘門停在等待同意。
狀態檔 state/scheduler.json；同一時間只允許一個排程（state/.scheduler.lock）；
執行工作時持有 state/.pipeline.lock，`run-approved` 也會等這把鎖，避免兩者同時寫帳本。
"""
from __future__ import annotations

import fcntl
import json
import os
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
from .data.pe_median import freeze_path
from .paths import PROJECT_DIR, Paths

STATE_FILE = "scheduler.json"
LOCK_FILE = ".scheduler.lock"
PIPELINE_LOCK = ".pipeline.lock"
RETRY_MINUTES = 30
MAX_FAILURES = 3
WATCH_EVERY_DAYS = 14
TASK_TIMEOUT_S = 8 * 3600


class SchedulerBusy(Exception):
    """已有另一個排程在執行。"""


class NotBootstrapped(Exception):
    """初始 bootstrap（初選＋15 檔分析）尚未完成，不得啟動排程。"""


def registry_block(conn) -> str | None:
    """執行中的前提：登錄表至少要有分析結果。回傳不能執行的理由。"""
    if conn.execute("SELECT COUNT(*) FROM registry").fetchone()[0] == 0:
        return "登錄表是空的：請先 `trader bootstrap --asof 日期` 完成初始 15 檔分析"
    return None


def pending_work(conn) -> str | None:
    """還有待同意項目或未完成的分析工作時回傳說明；否則 None。"""
    n = conn.execute("SELECT COUNT(*) FROM approvals WHERE status IN ('pending','deferred','approved')").fetchone()[0]
    if n:
        return f"還有 {n} 檔強模型分析待同意或待執行：完成（或拒絕）後再啟動排程"
    n = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running','awaiting_approval')").fetchone()[0]
    if n:
        return f"還有 {n} 個分析工作未完成：完成後再啟動排程"
    return None


def bootstrap_block(conn) -> str | None:
    """安裝排程前提：登錄表已有分析結果，且沒有未完成的分析工作或待同意項目。回傳不能啟動的理由。"""
    return registry_block(conn) or pending_work(conn)


@contextmanager
def pipeline_lock(paths: Paths) -> Iterator[None]:
    """排程工作與 run-approved 共用的寫入鎖（阻塞等待）。"""
    with open(paths.state / PIPELINE_LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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
            try:
                return subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      cwd=str(PROJECT_DIR), timeout=TASK_TIMEOUT_S).returncode
            except subprocess.TimeoutExpired:
                fh.write(f"[FAIL] 逾時 {TASK_TIMEOUT_S}s\n")
                return 124
    return run


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
        """回傳現在該做、且未在重試等待或已放棄的工作（依序）。"""
        local = self._local_now()
        today = local.date()
        start = st.setdefault("start_date", today.isoformat())
        cal = self.calendar((date.fromisoformat(start) - timedelta(days=10)).isoformat(),
                            (today + timedelta(days=10)).isoformat())
        h, m = self._close_time()
        after_close = (local.hour, local.minute) >= (h, m)
        last = today.isoformat() if cal.is_trading_day(today) and after_close else cal.prev_trading_day(today)
        if last < start:
            return []
        tasks: list[tuple[str, list[str]]] = []
        busy = pending_work(conn) is not None   # 待同意／未完成工作：暫緩會新增強模型工作的 watch、screen
        q = self._last_quarter_date(date.fromisoformat(last))
        ls = st.get("last_screen")
        screen_due = ls is None or (q is not None and ls < q)
        # 1. 每月凍結：只做當月；當天要初選時由初選順便凍結
        freeze_day = int(str(self.cfg.schedule.get("monthly_freeze") or "01"))
        month = today.isoformat()[:7]
        if (not (screen_due and not busy) and last[:7] == month and int(last[8:10]) >= freeze_day
                and not freeze_path(self.paths, month).exists()):
            tasks.append((f"freeze:{month}", ["fetch", "--asof", last, "--freeze-pe"]))
        # 2. 每日收盤（補跑錯過的交易日）：一次只排最早未完成的一天；它在重試等待或已放棄時，後面的日子也不跑
        #    （淨值與撮合依帳本順序，跳過失敗日先跑後一天會亂序）
        for d in cal.trading_days(start, last):
            if not self._daily_done(conn, d):
                tasks.append((f"daily:{d}", ["daily", "--date", d]))
                break
        # 3. 觀察名單
        lw = st.get("last_watch_review")
        if not busy and (lw is None or (date.fromisoformat(last) - date.fromisoformat(lw)).days >= WATCH_EVERY_DAYS):
            tasks.append((f"watch:{last}", ["review", "--scope", "watch", "--asof", last]))
        # 4. 每季初選＋深度分析（last_screen 由 bootstrap 寫入）
        if screen_due and not busy:
            tasks.append((f"screen:{last}", ["bootstrap", "--asof", last]))
        # 5. 每月檢討報告
        if st.get("last_report_month") != month and self._has_nav(conn):
            out = str(self.paths.state / "reports" / "monthly")
            tasks.append((f"report:{month}", ["validate-report", "--out", out]))
        mono = self.now().timestamp()
        gave = set(st.get("gave_up", []))
        retry = st.get("retry_at", {})
        return [(k, a) for k, a in tasks if k not in gave and retry.get(k, 0) <= mono]

    # ------------------------------------------------------------ 執行
    def _done(self, st: dict[str, Any], key: str) -> None:
        kind, arg = key.split(":", 1)
        if kind == "watch":
            st["last_watch_review"] = arg
        elif kind == "screen":
            st["last_screen"] = arg
        elif kind == "report":
            st["last_report_month"] = arg
        st.get("failures", {}).pop(key, None)
        st.get("retry_at", {}).pop(key, None)

    def _failed(self, conn, st: dict[str, Any], key: str, code: int) -> None:
        fails = st.setdefault("failures", {})
        fails[key] = fails.get(key, 0) + 1
        if fails[key] >= MAX_FAILURES:
            st.setdefault("gave_up", []).append(key)
            st.get("retry_at", {}).pop(key, None)
            db.insert(conn, "anomalies", {"ticker": None, "kind": "scheduler_task_failed",
                                          "detail": json.dumps({"task": key, "exit_code": code, "failures": fails[key],
                                                                "log": "state/logs/scheduler-tasks.log"}, ensure_ascii=False),
                                          "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
            conn.commit()
            self.log(f"[GIVE UP] {key} 連續 {fails[key]} 次失敗，已記異常")
        else:
            st.setdefault("retry_at", {})[key] = self.now().timestamp() + RETRY_MINUTES * 60
            self.log(f"[RETRY] {key} 失敗（exit {code}），{RETRY_MINUTES} 分鐘後重試")

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
                self.save_state(st)  # 保存 start_date
            finally:
                conn.close()
            if not todo:
                return done
            key, args = todo[0]
            self.log(f"[RUN] {key}")
            with pipeline_lock(self.paths):
                code = self.runner(_cli(self.cfg, self.paths, *args))
            conn = db.connect(self.paths.db)
            try:
                st = self.load_state()
                ok = code == 0
                if ok and key.startswith("daily:"):
                    ok = self._daily_done(conn, key[6:])
                elif ok and key.startswith("freeze:"):
                    ok = freeze_path(self.paths, key[7:]).exists()
                if ok:
                    self._done(st, key)
                    done.append(key)
                    self.log(f"[OK] {key}")
                else:
                    self._failed(conn, st, key, code)
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
            while True:
                try:
                    self.tick()
                except NotBootstrapped as e:
                    self.log(f"[WAIT] {e}")
                except Exception as e:  # noqa: BLE001 — 排程本身不中斷
                    self.log(f"[ERROR] {type(e).__name__}: {e}")
                time.sleep(interval_s)

    def once(self) -> list[str]:
        with open(self.paths.state / LOCK_FILE, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SchedulerBusy("已有另一個排程在執行") from None
            return self.tick()
