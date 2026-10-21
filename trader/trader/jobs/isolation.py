"""Claude Code 沙箱的檔案隔離。

`claude -p` 的 Read／Glob／Grep 預設可讀沙箱外任何檔案（實測），價格隔離會被繞過。
做法：沙箱放在獨立根目錄（不在使用者家目錄、暫存目錄之下），再以 `--settings` 的 deny 規則
封鎖家目錄、暫存目錄、外接磁碟與專案目錄的讀寫；沙箱本身不在任何封鎖範圍內才允許執行。

看價格的工作（不在 PRICE_ISOLATED_JOBS）沙箱放在第二個根 {root}-priced，隔離工作的 settings 封鎖整個 priced 根，
兩邊互不可見（M5_D6-02）。另以 disableAllHooks 停用 hooks，並禁止模型在任何位置寫 Claude Code 設定檔
（.claude/、CLAUDE.md、CLAUDE.local.md、.mcp.json），避免注入的設定在重試或後續工作被載入而執行指令（M5-D8-01）。
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

from ..paths import PKG_ROOT

# 封鎖的系統位置（存在才列入；macOS 的 /tmp、/var 實際在 /private 下）
_SYSTEM_DENY = ["/private", "/tmp", "/var", "/Volumes", "/mnt", "/media", "/srv", "/opt", "/root"]
# macOS 另有使用者可寫的系統位置（Homebrew 的 /usr/local、/Applications、/Library）：模型的 Write 不得寫到那裡（A3-M5E-04）
_DARWIN_DENY = ["/usr/local", "/Applications", "/Library"]
_TOOLS = ("Read", "Edit", "Write")
# Claude Code 會從工作目錄載入的專案設定／記憶檔（hooks、apiKeyHelper、MCP 等可執行指令）；模型不得寫，呼叫前清除
CLAUDE_CONFIG_NAMES = (".claude", "CLAUDE.md", "CLAUDE.local.md", ".mcp.json")
PRICED_SUFFIX = "-priced"


class IsolationError(Exception):
    """沙箱根目錄落在封鎖範圍內，或平台無預設根目錄。"""


def default_sandbox_root() -> Path:
    """macOS：/Users/Shared/berkshire-sandbox-{使用者}（家目錄之外）。其他平台需在設定檔指定 sandbox.root。"""
    if sys.platform == "darwin":
        return Path("/Users/Shared") / f"berkshire-sandbox-{getpass.getuser()}"
    raise IsolationError("此平台沒有預設沙箱根目錄；請在 config.yaml 設定 sandbox.root（不可在家目錄或暫存目錄下）")


def deny_roots(extra: list[str] | None = None, state_dir: Path | None = None) -> list[Path]:
    """要封鎖的目錄（已解析實際路徑、去重）：家目錄、系統暫存與掛載點、專案根目錄、state 目錄、使用者額外指定。"""
    sys_deny = _SYSTEM_DENY + (_DARWIN_DENY if sys.platform == "darwin" else [])
    cands = [Path.home(), PKG_ROOT, *(Path(p) for p in sys_deny)]
    if state_dir is not None:
        cands.append(Path(state_dir))
    if os.environ.get("TRADER_STATE_DIR"):
        cands.append(Path(os.environ["TRADER_STATE_DIR"]))
    cands += [Path(os.path.expanduser(p)) for p in (extra or [])]
    out: list[Path] = []
    for p in cands:
        if not p.exists():
            continue
        r = p.resolve()
        if r not in out:
            out.append(r)
    for p in cands:  # 未解析的原路徑也封（/tmp 與 /private/tmp 兩種寫法都擋）
        a = Path(os.path.abspath(p))
        if p.exists() and a not in out:
            out.append(a)
    return out


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_sandbox(sandbox: Path, denies: list[Path]) -> None:
    """沙箱（實際路徑）不得位於任何封鎖目錄之下，否則模型讀不到自己的輸入、或封鎖形同虛設。"""
    real = Path(sandbox).resolve()
    for d in denies:
        if _under(real, d):
            raise IsolationError(f"沙箱 {real} 位於封鎖目錄 {d} 之下；請把 sandbox.root 設在家目錄與暫存目錄之外")


def priced_root(root: Path) -> Path:
    """看價格工作的沙箱根：與 root 同層的 {root}-priced。"""
    root = Path(root)
    return root.with_name(root.name + PRICED_SUFFIX)


def prepare_root(root: Path, denies: list[Path]) -> Path:
    """建立沙箱根目錄與其 priced 根（權限 700）並檢查位置。回傳 root 的實際路徑。
    先解析 root：root 末段是符號連結時，priced 根須建在實際路徑旁，否則沙箱與 FULL_A 封鎖對不上（M5-RS6-01）。"""
    root = Path(root).resolve()
    for d in (root, priced_root(root)):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        check_sandbox(d, denies)
    return root.resolve()


def settings_json(denies: list[Path]) -> str:
    """`claude --settings` 的 JSON：停用所有 hooks；對每個封鎖目錄禁止 Read（含 Glob／Grep）、Edit、Write；
    任何位置都不得寫 Claude Code 設定／記憶檔（沙箱、兄弟沙箱、沙箱根的上層如 /Users/Shared 皆然）。"""
    rules = [f"{t}(/{d.as_posix()}/**)" for d in denies for t in _TOOLS]
    for name in CLAUDE_CONFIG_NAMES:
        pats = [f"//**/{name}"] + ([f"//**/{name}/**"] if name == ".claude" else [])
        rules += [f"{t}({p})" for p in pats for t in ("Edit", "Write")]
    return json.dumps({"disableAllHooks": True, "permissions": {"deny": rules}}, ensure_ascii=False)
