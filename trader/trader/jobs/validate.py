"""模型輸出驗證。

output_schemas.json 不是 JSON Schema，而是「範例形狀」，字串裡的規則決定實際檢查方式：
- 以 `（…）` 包住的全形註解先去掉，再依下列規則判斷（見 `_str_spec`）。
- 錯誤訊息的路徑從工作類型開始，例如 `THESIS.kill_criteria_status[0].status`。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

# 全形括號註解：例如 "數字或 null（照抄 cap_calc）"
_PAREN = re.compile(r"（[^（）]*）")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# decision_id 格式：{ticker}-{YYYYMMDD}-{n}
_DECISION_RE = re.compile(r"^([A-Z0-9.\-]+)-(\d{8})-(\d+)$")
# 選項中出現這些記號代表「自由文字」，不做列舉比對
_FREE_MARKERS = ("…", "..", "：")


def load_schemas(path: Path) -> dict:
    """讀取 output_schemas.json（UTF-8）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check(job_type: str, obj: Any, schemas: dict) -> tuple[Any, list[str]]:
    """檢查某工作的輸出。回傳 (正規化後的物件, 錯誤清單)；錯誤清單為空代表合格。

    正規化：把 "true"／"false" 字串轉成 bool；允許 null 的列舉把字串 "null" 轉成 None。不修改傳入的物件。
    """
    if job_type not in schemas or job_type.startswith("_"):
        raise ValueError(f"未知的工作類型：{job_type}")
    spec = schemas[job_type]
    # 模型偶爾多包一層 {"<工作類型>": {...}}：格式本身沒有這個鍵時拆開
    if (isinstance(obj, dict) and list(obj) == [job_type] and isinstance(obj[job_type], dict)
            and not (isinstance(spec, dict) and job_type in spec)):
        obj = obj[job_type]
    errors: list[str] = []
    out = _node(spec, obj, job_type, errors)
    return out, errors


def error_feedback(errors: list[str]) -> str:
    """產生重試用的繁中說明：列出錯誤並要求只輸出合格 JSON。"""
    lines = ["上一次輸出未通過格式檢查，錯誤如下："]
    lines += [f"- {e}" for e in errors] or ["- （未列出具體錯誤）"]
    lines.append("請只輸出一個合格的 JSON 物件（形狀依本工作的 output schema），"
                 "列舉值必須與規定完全相同（陣列每個元素各填一個選項，不要把整串選項當成一個值），"
                 "不要用工作名稱包一層，不要加任何說明文字。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 內部實作

def _node(spec: Any, val: Any, path: str, errors: list[str]) -> Any:
    """依範例值 spec 的型別分派檢查。"""
    if isinstance(spec, dict):
        return _obj(spec, val, path, errors)
    if isinstance(spec, list):
        return _lst(spec, val, path, errors)
    if isinstance(spec, bool):  # 必須先判斷 bool，因為 bool 是 int 的子類別
        return _bool(val, path, errors)
    if isinstance(spec, (int, float)):
        if _is_num(val):
            return val
        errors.append(f"{path}：應為數字，實際為 {val!r}")
        return val
    if isinstance(spec, str):
        return _str_spec(spec, val, path, errors)
    return val


def _obj(spec: dict, val: Any, path: str, errors: list[str]) -> Any:
    """物件：必填鍵＝所有不以 _ 開頭的鍵；額外鍵允許；遞迴檢查。"""
    if not isinstance(val, dict):
        errors.append(f"{path}：應為物件")
        return val
    out = dict(val)
    for key, sub in spec.items():
        if key.startswith("_"):
            continue
        sub_path = f"{path}.{key}"
        if key not in val:
            errors.append(f"{sub_path}：缺少必填鍵")
            continue
        out[key] = _node(sub, val[key], sub_path, errors)
    return out


def _lst(spec: list, val: Any, path: str, errors: list[str]) -> Any:
    """list：空範例 → 任意 list；否則每個元素依第一個元素的規則檢查。"""
    if not isinstance(val, list):
        errors.append(f"{path}：應為陣列")
        return val
    if not spec:
        return val
    item_spec = spec[0]
    return [_node(item_spec, v, f"{path}[{i}]", errors) for i, v in enumerate(val)]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _bool(val: Any, path: str, errors: list[str]) -> Any:
    """bool 欄位：接受 bool；字串 "true"／"false"（忽略大小寫與前後空白）正規化成 bool。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str) and val.strip().lower() in ("true", "false"):
        return val.strip().lower() == "true"
    errors.append(f"{path}：應為 true 或 false，實際為 {val!r}")
    return val


def _valid_date(s: str) -> bool:
    if not _DATE_RE.match(s):
        return False
    try:
        _dt.date.fromisoformat(s)
    except ValueError:  # 例如 2026-02-30
        return False
    return True


_RANGE = re.compile(r"^([A-Z]+)(\d+)\.\.\1?(\d+)$")


def _expand_ranges(options: list[str]) -> list[str]:
    """選項 "G7..G11" 展開成 G7、G8、…、G11（嚴格列舉）；其他選項不動。"""
    out: list[str] = []
    for o in options:
        m = _RANGE.match(o)
        if m and int(m.group(2)) <= int(m.group(3)):
            out += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(o)
    return out


def _str_spec(spec: str, val: Any, path: str, errors: list[str]) -> Any:
    """字串範例：依 contract 的 7 條規則（順序很重要）判斷。"""
    base = _PAREN.sub("", spec).strip()

    # 1. bool
    if base.startswith("true | false"):
        return _bool(val, path, errors)

    # 2. 數字
    if base == "數字或 null":
        if val is None or _is_num(val):
            return val
        errors.append(f"{path}：應為數字或 null，實際為 {val!r}")
        return val
    if base == "小數或 0":
        if _is_num(val):
            return val
        errors.append(f"{path}：應為數字，實際為 {val!r}")
        return val

    # 3. 日期
    if base in ("YYYY-MM-DD", "YYYY-MM-DD 或 null"):
        if val is None and base.endswith("或 null"):
            return val
        if isinstance(val, str) and _valid_date(val):
            return val
        errors.append(f"{path}：應為 YYYY-MM-DD 的有效日期（{base}），實際為 {val!r}")
        return val

    # 4. 字串或 null
    if base == "字串或 null":
        if val is None or isinstance(val, str):
            return val
        errors.append(f"{path}：應為字串或 null，實際為 {val!r}")
        return val

    # 5. decision_id 格式
    if "{YYYYMMDD}" in base:
        m = _DECISION_RE.match(val) if isinstance(val, str) else None
        if m and _valid_date(f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:]}"):
            return val
        errors.append(f"{path}：應為 {{ticker}}-YYYYMMDD-n（日期須有效），實際為 {val!r}")
        return val

    # 6. 列舉或自由文字
    if "|" in base:
        options = _expand_ranges([o.strip() for o in base.split("|")])
        has_null = "null" in options
        if has_null and (val is None or val == "null"):  # 列舉允許 null 時，字串 "null" 視同 null
            return None
        if any(mark in o for o in options for mark in _FREE_MARKERS):
            # 含 … 等記號的選項：自由字串
            if isinstance(val, str):
                return val
            errors.append(f"{path}：應為文字，實際為 {val!r}")
            return val
        allowed = [o for o in options if o != "null"]
        if isinstance(val, str) and val in allowed:
            return val
        errors.append(f"{path}：值 {val!r} 不在允許選項內（{' | '.join(options)}）")
        return val

    # 7. 其他：任意字串
    if isinstance(val, str):
        return val
    errors.append(f"{path}：應為字串，實際為 {val!r}")
    return val
