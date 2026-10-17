"""工作沙箱與價格隔離。

價格隔離的原則：除 FULL_B 外，沙箱內不得出現任何價格（PRICE_ISOLATED_JOBS）。
FULL_B 只能在 gate3 答覆檔「早於」開檔時間（已先完成 gate3）的前提下，由 open_price_file 取得價格檔。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..models import PRICE_ISOLATED_JOBS, JobSpec


class PriceLeakError(Exception):
    """價格資料流入不該看到價格的沙箱。"""


class PriceGateError(Exception):
    """尚未通過 gate3 就想開價格檔。"""


PRICE_KEYS = frozenset({
    "price", "price_date", "market_cap", "earn_yield", "fcf_yield", "norm_earn_yield", "pe", "pe_ratio",
    "core_rank", "research_priority", "vol_36m", "drawdown_3y", "price_context", "review_price",
    "auto_buy_cap", "sell_review_above", "absurd_price_line",
})

_OPENED_AT_FILE = "price_file_opened_at.txt"


def _safe(name: str) -> str:
    """把任意字串轉成安全的目錄名稱。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _safe_rel(name: str) -> PurePosixPath:
    """檔名必須是沙箱內的相對路徑，不得含 .. 或絕對路徑。"""
    p = PurePosixPath(name.replace("\\", "/"))
    if not name or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"不安全的檔名：{name!r}")
    return p


def is_price_key(k: str) -> bool:
    """是否為價格相關的鍵：完全符合 PRICE_KEYS，或名稱含 price／market_cap。"""
    s = str(k).lower()
    return s in PRICE_KEYS or "price" in s or "market_cap" in s


def scrub_prices(obj: Any) -> Any:
    """遞迴刪除所有價格鍵，回傳新物件（不修改原物件）。"""
    if isinstance(obj, dict):
        return {k: scrub_prices(v) for k, v in obj.items() if not is_price_key(k)}
    if isinstance(obj, list):
        return [scrub_prices(v) for v in obj]
    return obj


def _has_price_key(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(is_price_key(k) or _has_price_key(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_price_key(v) for v in obj)
    return False


def create_sandbox(work_dir: Path, job: JobSpec) -> Path:
    """建立工作沙箱：{work_dir}/{run_id}/{ticker}/_{job_type}_{n}。已存在則清空重建；設定 job.sandbox。"""
    n = job.job_id.rsplit(":", 1)[-1]
    path = Path(work_dir) / _safe(job.run_id) / _safe(job.ticker) / f"_{job.job_type}_{_safe(n)}"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    job.sandbox = path
    return path


STALE_SANDBOX_HOURS = 24      # 超過此時數仍留著的看價格沙箱視為殘留（排程單一工作上限 8 小時）


def _is_job_sandbox(path: Path, root: Path) -> bool:
    """{root}/{run_id}/{ticker}/_{JOB}_{n} 形式、且在 root 之下的目錄。"""
    p, r = Path(path).resolve(), Path(root).resolve()
    return p.is_dir() and p.name.startswith("_") and p.parent.parent.parent == r


def remove_sandbox(path: Path, root: Path) -> bool:
    """刪除看得到價格的工作沙箱（輸出已收回 work 目錄）：兄弟沙箱彼此可讀，留著會洩漏價格給隔離工作。"""
    if not _is_job_sandbox(path, root):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def purge_stale_sandboxes(root: Path, now: float | None = None) -> list[str]:
    """清掉殘留的非隔離工作沙箱（當機或舊版本留下；可能含價格檔）。隔離工作的沙箱本來就不含價格，保留。"""
    root = Path(root)
    if not root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - STALE_SANDBOX_HOURS * 3600
    removed = []
    for p in root.glob("*/*/_*"):
        jt = p.name[1:].rsplit("_", 1)[0]
        if jt in PRICE_ISOLATED_JOBS or not p.is_dir() or p.stat().st_mtime >= cutoff:
            continue
        if remove_sandbox(p, root):
            removed.append(str(p))
    return removed


def ticker_dir(work_dir: Path, job: JobSpec) -> Path:
    """同一檔股票各工作共用的輸出區：{work_dir}/{run_id}/{ticker}。"""
    return Path(work_dir) / _safe(job.run_id) / _safe(job.ticker)


def stage_inputs(sandbox: Path, files: list[Path | tuple[Path, str]], job_type: str) -> list[str]:
    """把輸入檔複製進沙箱，回傳沙箱內的相對檔名。

    tuple 的第二項為沙箱內的目的名稱。隔離工作（PRICE_ISOLATED_JOBS）：
    - 檔名是 *_price.json → PriceLeakError（任何複製動作之前就拒絕）；
    - .json 先刪除價格鍵再寫入；無法解析的 JSON 無法確認是否含價格，同樣拒絕；
    - 最後以 assert_price_free 確認沙箱乾淨。
    """
    sandbox = Path(sandbox)
    isolated = job_type in PRICE_ISOLATED_JOBS
    plan: list[tuple[Path, PurePosixPath]] = []
    for item in files:
        if isinstance(item, tuple):
            src, name = Path(item[0]), item[1]
        else:
            src = Path(item)
            name = src.name
        rel = _safe_rel(name)
        if isolated and rel.name.endswith("_price.json"):
            raise PriceLeakError(f"{job_type} 不得取得價格檔：{rel}")
        plan.append((src, rel))

    names: list[str] = []
    for src, rel in plan:
        dest = sandbox / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isolated and rel.suffix == ".json":
            try:
                data = json.loads(Path(src).read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                # 無法解析就無法確認不含價格，寧可拒絕
                raise PriceLeakError(f"{job_type} 的輸入 {rel} 無法解析，無法確認不含價格") from e
            dest.write_text(json.dumps(scrub_prices(data), ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            shutil.copyfile(src, dest)
        names.append(rel.as_posix())

    if isolated:
        assert_price_free(sandbox)
    return names


def assert_price_free(sandbox: Path) -> None:
    """掃描沙箱：有 *_price.json，或任一 .json（遞迴）含價格鍵 → PriceLeakError。"""
    sandbox = Path(sandbox)
    for p in sorted(sandbox.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(sandbox).as_posix()
        if p.name.endswith("_price.json"):
            raise PriceLeakError(f"沙箱內有價格檔：{rel}")
        if p.suffix == ".json":
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if _has_price_key(data):
                raise PriceLeakError(f"沙箱內的 {rel} 含價格鍵")


def open_price_file(sandbox: Path, price_file: Path, gate3_answers: Path, now: float | None = None) -> str:
    """FULL_B 專用：在 gate3 答覆檔之後才開價格檔。

    - gate3 答覆檔不存在 → PriceGateError；
    - 其 mtime >= 開檔時間（now，預設為現在）→ PriceGateError（表示答覆不是先於開檔完成）；
    - 通過：複製價格檔進沙箱，寫入 price_file_opened_at.txt，回傳開檔時間（ISO 8601 UTC）。
    """
    sandbox = Path(sandbox)
    gate3_answers = Path(gate3_answers)
    if not gate3_answers.exists():
        raise PriceGateError("缺 gate3 答覆檔，不得開價格檔")
    now_ts = time.time() if now is None else now
    if gate3_answers.stat().st_mtime >= now_ts:
        raise PriceGateError("gate3 答覆檔的時間不早於開檔時間，視為尚未先完成 gate3")
    price_file = Path(price_file)
    shutil.copyfile(price_file, sandbox / price_file.name)
    opened_at = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (sandbox / _OPENED_AT_FILE).write_text(opened_at, encoding="utf-8")
    return opened_at


def collect_outputs(sandbox: Path, dest: Path, names: list[str]) -> list[str]:
    """把模型輸出檔從沙箱複製到 dest（通常是 ticker_dir）。不存在的檔略過。回傳已複製的檔名。"""
    sandbox = Path(sandbox)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in names:
        rel = _safe_rel(name)
        src = sandbox / rel
        if not src.is_file():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
        copied.append(rel.as_posix())
    return copied
