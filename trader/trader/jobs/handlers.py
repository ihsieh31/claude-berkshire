"""工作處理器（代理 D）：每種工作的沙箱準備、輸出收集，以及後續工作的動態升級。

升級規則來源：routing/model_routing.json 的 next／escalate 欄位。
後置腳本（gate3_check、cap_calc、build_dossier）在 M3 才實作，這裡只留 pre_script／post_script 掛鉤，預設不做事。

檔案位置約定：
- 工作沙箱：{work}/{run_id}/{ticker}/_{job_type}_{n}（由 sandbox.create_sandbox 建立）
- 同檔共用輸出區：{work}/{run_id}/{ticker}（由 sandbox.ticker_dir 取得）；工作的輸出檔收集到這裡，
  後續工作再從這裡取輸入。FULL_B 的價格檔預設為 {ticker_dir}/{ticker}_price.json，可用 params["price_file"] 覆寫。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import db
from ..models import JobSpec
from ..paths import Paths
from .context import Section
from .sandbox import collect_outputs, create_sandbox, open_price_file, stage_inputs, ticker_dir


@dataclass
class HandlerCtx:
    paths: Paths
    schemas: dict
    work_dir: Path
    conn: sqlite3.Connection
    sandbox_root: Path | None = None   # 沙箱根目錄（claude_code 時在隔離位置）；None＝work_dir
    config: Any = None
    sec: Any = None                    # SecClient（DOSSIER 前置用；pipeline 注入）

    @property
    def sandbox_base(self) -> Path:
        return self.sandbox_root or self.work_dir

    def next_seq(self, job: JobSpec, job_type: str) -> int:
        """同 run、同檔、同類型的下一個序號（從 1 起算）。"""
        n = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE run_id=? AND ticker=? AND job_type=?",
            (job.run_id, job.ticker, job_type),
        ).fetchone()[0]
        return int(n) + 1


def next_job(job: JobSpec, job_type: str, ctx: HandlerCtx, params: dict[str, Any] | None = None) -> JobSpec:
    """建立由 job 動態升級而來的新工作；依賴 job；params 附上建立時間（FULL_B 的開檔檢查用）。"""
    n = ctx.next_seq(job, job_type)
    p = dict(params or {})
    p.setdefault("created_ts", time.time())
    return JobSpec(
        job_id=f"{job.run_id}:{job.ticker}:{job_type}:{n}",
        run_id=job.run_id,
        job_type=job_type,
        ticker=job.ticker,
        asof=job.asof,
        deps=[job.job_id],
        params=p,
    )


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return "（無法以文字讀取）"


def _sections(sandbox: Path, names: list[str]) -> list[Section]:
    """把沙箱內的輸入檔轉成提示段落（claude_code 後端只列檔名，不會用到內容）。"""
    return [Section(name=f"輸入檔：{n}", text=_read_text(sandbox / n)) for n in names]


def _ticker_inputs(job: JobSpec, ctx: HandlerCtx, required: list[str], optional: list[str]) -> list[Path]:
    """從同檔輸出區取輸入：required 缺檔即拋 FileNotFoundError；optional 有則取。"""
    td = ticker_dir(ctx.work_dir, job)
    req = [td / n for n in required]
    missing = [p.name for p in req if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"缺少前置輸出：{', '.join(missing)}")
    return req + [td / n for n in optional if (td / n).is_file()]


class Handler:
    """預設處理器：沙箱 → 輸入 → 收集輸出 → 無後續工作。子類依需要覆寫。"""

    job_type: str = ""

    def inputs(self, job: JobSpec, ctx: HandlerCtx) -> list[Any]:
        """要複製進沙箱的輸入（路徑清單）；預設取 params["inputs"]。"""
        return list(job.params.get("inputs", []))

    def prepare(self, job: JobSpec, ctx: HandlerCtx) -> list[Section]:
        sandbox = create_sandbox(ctx.sandbox_base, job)  # 會設定 job.sandbox
        names = stage_inputs(sandbox, self.inputs(job, ctx), job.job_type)
        return _sections(sandbox, names)

    def post(self, job: JobSpec, result: dict, ctx: HandlerCtx) -> list[JobSpec]:
        names = [n for n in (result.get("files") or []) if isinstance(n, str)]
        if names and job.sandbox is not None:
            collect_outputs(job.sandbox, ticker_dir(ctx.work_dir, job), names)
        return self.next_jobs(job, result, ctx)

    def next_jobs(self, job: JobSpec, result: dict, ctx: HandlerCtx) -> list[JobSpec]:
        """依輸出決定動態新增的工作；可在 result 上附加欄位（如 final_signal）。"""
        return []

    def on_invalid(self, job: JobSpec, errors: list[str], ctx: HandlerCtx) -> list[JobSpec]:
        """重試後仍不合格：決策日誌記 NO_ACTION／MISSING_DATA，不以模型自由文字取代。回傳替代的後續工作（不依賴本工作）。"""
        db.insert(ctx.conn, "decisions", {
            "decision_id": job.job_id,  # 每個工作唯一；正式決策編號格式於 M3 再定
            "ticker": job.ticker,
            "decision_date": job.asof,
            "run_id": job.run_id,
            "job_id": job.job_id,
            "signal": "NO_ACTION",
            "reason_code": "MISSING_DATA",
            "data": {"job_type": job.job_type, "errors": list(errors)},
            "created_at": db.now_iso(),
        })
        return []

    def prompt_values(self, job: JobSpec, ctx: HandlerCtx, backend: str) -> dict[str, str]:
        """提示佔位符的值（覆寫預設的 pkg、work 等）。"""
        return {}

    def prompt_suffix(self, job: JobSpec) -> str:
        """附加在提示最後的文字（例如框架退回的修正要求）。"""
        return ""

    def pre_script(self, job: JobSpec, ctx: HandlerCtx) -> None:
        """M3 掛鉤：工作執行前的腳本（M0 不做事）。"""
        return None

    def post_script(self, job: JobSpec, ctx: HandlerCtx) -> list[str]:
        """M3 掛鉤：工作完成後的腳本檢查，回傳錯誤清單（M0 不做事）。"""
        return []


from .deep import DossierHandler, FullAHandler, FullBHandler, RedTeamHandler  # noqa: E402  （deep 依賴上方的 Handler）
from .review_jobs import PriceHandler, ThesisHandler, TriageHandler  # noqa: E402  （review_jobs 同樣依賴上方的 Handler）
from .corp_jobs import CorpActionHandler  # noqa: E402  （corp_jobs 同樣依賴上方的 Handler）
from .hold_jobs import HoldReviewHandler  # noqa: E402

HANDLERS: dict[str, Handler] = {
    h.job_type: h() for h in (
        DossierHandler, TriageHandler, PriceHandler, ThesisHandler,
        FullAHandler, FullBHandler, RedTeamHandler, CorpActionHandler, HoldReviewHandler,
    )
}
