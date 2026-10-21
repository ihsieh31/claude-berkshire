"""提示組裝：把共通規則、工作模板、輸出 schema 與輸入段落組成一份提示，並控制 token 上限。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..models import JobSpec
from .validate import load_schemas


class ContextOverflowError(Exception):
    """超過 token 上限，且已無可刪的段落。"""


@dataclass
class Section:
    name: str
    text: str
    droppable: int = 0  # 0 = 不可刪；數字越大越先刪（knowledge=2、references=1）


# 輸出檔說明：非 claude_code 模式下，模型必須以 files 欄位回傳檔案內容
_FILE_NOTE = (
    "## 輸出檔案\n"
    "要寫的檔案放在輸出 JSON 的 files：{檔名: 內容}，檔名相對於工作目錄，"
    "不得含 .. 或絕對路徑；內容為字串（或 JSON 物件，框架會以 JSON 寫出）。"
)

NO_PKG = "（本工作不提供規則檔）"   # {pkg} 預設值（處理器未覆寫時；A3-M3E-02）

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3000 <= o <= 0x9FFF or 0xFF00 <= o <= 0xFFEF


def estimate_tokens(text: str) -> int:
    """粗估 token 數：CJK 字元每字 1；其他字元每 4 字 1；向上取整。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


def render_template(text: str, values: dict[str, str]) -> str:
    """只替換 {鍵} 且鍵存在於 values 的佔位符；其餘大括號（例如 JSON 範例）原樣保留。"""
    return _PLACEHOLDER.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        text,
    )


def _sandbox_files(sandbox: Path | None) -> list[str]:
    if sandbox is None or not Path(sandbox).is_dir():
        return []
    root = Path(sandbox)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


SCHEMA_NOTE = ("讀法：`\"A | B | C\"` 表示從中擇一，值只能是其中一個選項；陣列內若是這種字串，"
               "代表每個元素各填一個選項、可有多個元素，不要把整串選項照抄成一個值。"
               "最外層就是上面這個物件本身，不要再用工作名稱包一層。")


def build_prompt(job: JobSpec, backend: str, max_tokens: int, pkg_root: Path,
                 sections: list[Section] | Callable[[], list[Section]] | None = None,
                 values: dict[str, str] | None = None) -> str:
    """組成一個工作的完整提示。

    順序：_COMMON.md → prompts/{job_type}.md → output_schemas.json 該工作的節 → 輸入段落。
    - backend == "claude_code"（代理式，正式路徑）：輸入段落只列沙箱內的檔名，不內嵌內容（Claude Code 自己讀檔）；
      sections 不使用。sections 可為無參數函式（設計決策 D48：段落只在 API 後端才組，代理式不讀全文）。
    - 其他 backend（API／fake）：內嵌 sections 文字，並說明輸出檔放在 files 欄位。
      超過 max_tokens：依 droppable 由大到小整段刪除（同級依列表順序），並在提示末尾列出被刪的段落（D48）；
      仍超過 → ContextOverflowError。
    """
    pkg_root = Path(pkg_root)
    prompts = pkg_root / "routing" / "prompts"
    common = (prompts / "_COMMON.md").read_text(encoding="utf-8")
    template = (prompts / f"{job.job_type}.md").read_text(encoding="utf-8")
    schemas = load_schemas(pkg_root / "routing" / "output_schemas.json")
    if job.job_type not in schemas:
        raise ValueError(f"output_schemas.json 沒有 {job.job_type} 這一節")

    vals = {
        # 預設不給真實專案根（含 state）：需要規則檔的處理器以 prompt_values 指向沙箱內 pkg/（A3-M3E-02）
        "pkg": NO_PKG,
        "asof": job.asof,
        "ticker": job.ticker,
        "work": str(job.sandbox) if job.sandbox else "",
    }
    vals.update(values or {})

    head = (render_template(common, vals).rstrip() + "\n\n"
            + render_template(template, vals).rstrip())
    # 輸出格式也代入 {ticker} 等（files 範例寫成 {ticker}/report_A.md；M5_D10-08）
    schema_part = (f"## 輸出格式（{job.job_type}）\n```json\n"
                   + render_template(json.dumps(schemas[job.job_type], ensure_ascii=False, indent=2), vals)
                   + "\n```\n" + SCHEMA_NOTE)

    if backend == "claude_code":
        listing = "\n".join(f"- {f}" for f in _sandbox_files(job.sandbox)) or "（無）"
        text = "\n\n".join([head, schema_part, "## 輸入檔\n" + listing])
        if estimate_tokens(text) > max_tokens:
            raise ContextOverflowError(
                f"{job.job_type} 提示估 {estimate_tokens(text)} tokens，超過上限 {max_tokens}")
        return text

    secs = list((sections() if callable(sections) else sections) or [])

    def build(keep: list[int]) -> str:
        parts = [head, schema_part, _FILE_NOTE]
        parts += [f"## {secs[i].name}\n{secs[i].text}" for i in keep]
        dropped = [secs[i].name for i in range(len(secs)) if i not in keep]
        if dropped:   # D48：刪段要標示，模型與日誌才知道少了哪些規則或原文
            parts.append("## 已刪段落（提示超過上限，以下段落未附；相關規則以 SKILL.md 為準，無法判斷處照實標缺）\n"
                         + "\n".join(f"- {n}" for n in dropped))
        return "\n\n".join(parts)

    keep = list(range(len(secs)))
    text = build(keep)
    levels = sorted({s.droppable for s in secs if s.droppable > 0}, reverse=True)
    for level in levels:
        if estimate_tokens(text) <= max_tokens:
            break
        for i in list(keep):
            if secs[i].droppable == level:
                keep.remove(i)
                text = build(keep)
                if estimate_tokens(text) <= max_tokens:
                    break
    if estimate_tokens(text) > max_tokens:
        raise ContextOverflowError(
            f"{job.job_type} 提示估 {estimate_tokens(text)} tokens，超過上限 {max_tokens} 且已無可刪段落")
    return text
