"""開機自動啟動（M6d）：launchd 使用者代理兩個——儀表板伺服器（常駐）與桌面小儀表板。

- 伺服器：python -m trader.cli dashboard（KeepAlive）。PATH 帶上 `claude` 所在目錄（同意後背景 run-approved 要用）、
  SSL_CERT_FILE 指 certifi（python.org 版 Python 連 SEC 需要）。
- 小儀表板：desktop/BerkshireWidget/build/BerkshireWidget.app（先以 build.sh 編譯）。
- 排程（M7）：python -m trader.cli scheduler run（KeepAlive；沒有期限，`trader scheduler uninstall` 才停）。
plist 只含路徑，不含任何金鑰（金鑰仍在 trader/.env，由 trader 自行讀取）。
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from ..paths import PROJECT_DIR, Paths

LABEL_SERVER = "local.berkshire.dashboard"
LABEL_WIDGET = "local.berkshire.widget"
LABEL_SCHED = "local.berkshire.scheduler"
AGENTS = Path.home() / "Library" / "LaunchAgents"
DESKTOP_DIR = PROJECT_DIR / "desktop" / "BerkshireWidget"
APP_BIN = DESKTOP_DIR / "build" / "BerkshireWidget.app" / "Contents" / "MacOS" / "BerkshireWidget"


def _env_path() -> str:
    dirs = []
    claude = shutil.which("claude")
    if claude:
        dirs.append(str(Path(claude).parent))
    dirs += [str(Path(sys.executable).parent), "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    out: list[str] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return ":".join(out)


def _env() -> dict:
    env = {"PATH": _env_path()}
    try:
        import certifi
        env["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass
    return env


def server_plist(paths: Paths, port: int, config_path: Path | None) -> dict:
    args = [sys.executable, "-m", "trader.cli", "dashboard", "--state-dir", str(paths.state), "--port", str(port)]
    if config_path:
        args += ["--config", str(config_path)]
    env = _env()
    logs = paths.state / "logs"
    return {"Label": LABEL_SERVER, "ProgramArguments": args, "WorkingDirectory": str(PROJECT_DIR),
            "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Background",
            "StandardOutPath": str(logs / "dashboard.log"), "StandardErrorPath": str(logs / "dashboard.log")}


def widget_plist(paths: Paths, port: int) -> dict:
    logs = paths.state / "logs"
    return {"Label": LABEL_WIDGET, "ProgramArguments": [str(APP_BIN), "--state", str(paths.state), "--port", str(port)],
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ProcessType": "Interactive",
            "StandardOutPath": str(logs / "widget.log"), "StandardErrorPath": str(logs / "widget.log")}


def scheduler_plist(paths: Paths, config_path: Path | None) -> dict:
    args = [sys.executable, "-m", "trader.cli", "scheduler", "run", "--state-dir", str(paths.state)]
    if config_path:
        args += ["--config", str(config_path)]
    logs = paths.state / "logs"
    return {"Label": LABEL_SCHED, "ProgramArguments": args, "WorkingDirectory": str(PROJECT_DIR),
            "EnvironmentVariables": _env(), "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Background",
            "StandardOutPath": str(logs / "scheduler.log"), "StandardErrorPath": str(logs / "scheduler.log")}


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("launchctl") is None:   # Linux 等非 macOS：沒有 launchd，回失敗而不是例外（M8-D1-07）
        return subprocess.CompletedProcess(["launchctl", *args], 127, "", "此系統沒有 launchctl（非 macOS；排程請用 scheduler print --cron）")
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def build_widget() -> Path:
    r = subprocess.run(["sh", str(DESKTOP_DIR / "build.sh")], capture_output=True, text=True)
    if r.returncode != 0 or not APP_BIN.is_file():
        raise RuntimeError("小儀表板編譯失敗：\n" + (r.stdout + r.stderr)[-2000:])
    return APP_BIN


def install(paths: Paths, port: int, config_path: Path | None, widget: bool = True) -> list[str]:
    """寫 plist 到 ~/Library/LaunchAgents 並載入（已載入者先卸載再載入）。回傳完成的 label。
    先載入伺服器；小儀表板編譯失敗只印警告到 stderr（伺服器照常載入，A3-M8C2-14）。"""
    AGENTS.mkdir(parents=True, exist_ok=True)
    (paths.state / "logs").mkdir(parents=True, exist_ok=True)
    done = _load([(LABEL_SERVER, server_plist(paths, port, config_path))])
    if widget:
        try:
            build_widget()
        except RuntimeError as e:
            print(f"[WARN] {e}\n伺服器已載入；小儀表板未安裝（修好後重跑，或加 --no-widget 略過）", file=sys.stderr)
            return done
        done += _load([(LABEL_WIDGET, widget_plist(paths, port))])
    return done


def _load(plists: list[tuple[str, dict]]) -> list[str]:
    done = []
    uid = str(os.getuid())
    for label, pl in plists:
        f = AGENTS / f"{label}.plist"
        _launchctl("bootout", f"gui/{uid}/{label}")
        f.write_bytes(plistlib.dumps(pl))
        r = _launchctl("bootstrap", f"gui/{uid}", str(f))
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap {label} 失敗：{r.stderr.strip()}")
        done.append(label)
    return done


def install_scheduler(paths: Paths, config_path: Path | None) -> list[str]:
    AGENTS.mkdir(parents=True, exist_ok=True)
    (paths.state / "logs").mkdir(parents=True, exist_ok=True)
    return _load([(LABEL_SCHED, scheduler_plist(paths, config_path))])


def uninstall(labels: tuple[str, ...] = (LABEL_WIDGET, LABEL_SERVER)) -> list[str]:
    uid = str(os.getuid())
    out = []
    for label in labels:
        _launchctl("bootout", f"gui/{uid}/{label}")
        f = AGENTS / f"{label}.plist"
        if f.exists():
            f.unlink()
            out.append(label)
    return out


def status(labels: tuple[str, ...] = (LABEL_SERVER, LABEL_WIDGET, LABEL_SCHED)) -> dict[str, str]:
    uid = str(os.getuid())
    res = {}
    for label in labels:
        r = _launchctl("print", f"gui/{uid}/{label}")
        if r.returncode != 0:
            res[label] = "未載入"
            continue
        state = next((ln.split("=", 1)[1].strip() for ln in r.stdout.splitlines() if ln.strip().startswith("state =")), "?")
        res[label] = state
    return res
