"""路徑：套件根目錄（規則唯讀）與執行期 state 目錄。

state 目錄優先序：get_paths(state_dir) 參數 > 環境變數 TRADER_STATE_DIR > {套件根}/state。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent  # berkshire/trader
PKG_ROOT = PROJECT_DIR.parent                           # berkshire/（含 skill 與 routing）


@dataclass(frozen=True)
class Paths:
    root: Path   # 套件根（唯讀規則）
    state: Path  # 執行期資料

    # ---- 規則（唯讀） ----
    @property
    def routing_json(self) -> Path:
        return self.root / "routing" / "model_routing.json"

    @property
    def schemas_json(self) -> Path:
        return self.root / "routing" / "output_schemas.json"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "routing" / "prompts"

    @property
    def skill_deep(self) -> Path:
        return self.root / "berkshire-deep-merged"

    @property
    def skill_screen(self) -> Path:
        return self.root / "berkshire-screen-merged"

    # ---- 執行期 ----
    @property
    def db(self) -> Path:
        return self.state / "trader.db"

    @property
    def registry_json(self) -> Path:
        return self.state / "registry.json"

    @property
    def theses(self) -> Path:
        return self.state / "theses"

    @property
    def work(self) -> Path:
        return self.state / "work"

    @property
    def llm_calls(self) -> Path:
        return self.state / "llm_calls"

    @property
    def exports(self) -> Path:
        return self.state / "exports"

    @property
    def cache(self) -> Path:
        return self.state / "cache"

    def ensure(self) -> "Paths":
        for p in (self.state, self.theses, self.work, self.llm_calls, self.exports, self.cache):
            p.mkdir(parents=True, exist_ok=True)
        return self


def get_paths(state_dir: str | os.PathLike | None = None, root: str | os.PathLike | None = None) -> Paths:
    st = state_dir or os.environ.get("TRADER_STATE_DIR") or (PKG_ROOT / "state")
    return Paths(root=Path(root).resolve() if root else PKG_ROOT, state=Path(st).resolve())
