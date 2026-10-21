"""工作處理器（代理 D）：每種工作的沙箱準備、輸出收集，以及後續工作的動態升級。

升級規則以本模組與各工作處理器（deep.py、review_jobs.py、hold_jobs.py 等）的程式為準；routing/model_routing.json 的 next／escalate 只是說明文字。
掛鉤：pre_script（工作執行前，例如 build_dossier、cap_calc 重算）；post（收輸出、後置檢查與回寫，回傳後續工作）；
on_invalid（輸出重試後仍不合格：寫保守結論或回傳替代工作 → 引擎標 conservative_fallback，設計決策 D43）；
on_fail（路由、準備、傳輸、後置例外、依賴跳過等非格式失敗的保守紀錄，預設不做事，M5_D4-06）。
on_invalid／on_fail 回傳的替代工作，引擎一律拿掉對本工作的依賴（M5-D1-01）。

檔案位置約定：
- 工作沙箱：{work}/{run_id}/{ticker}/_{job_type}_{n}（由 sandbox.create_sandbox 建立）
- 同檔共用輸出區：{work}/{run_id}/{ticker}（由 sandbox.ticker_dir 取得）；工作的輸出檔收集到這裡，
  後續工作再從這裡取輸入。FULL_B 的價格檔預設為 {ticker_dir}/{ticker}_price.json，可用 params["price_file"] 覆寫。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import JobSpec
from ..paths import Paths
from .context import Section
from .sandbox import create_sandbox, open_price_file, stage_inputs, ticker_dir


@dataclass
class HandlerCtx:
    paths: Paths
    schemas: dict
    work_dir: Path
    conn: sqlite3.Connection
    sandbox_root: Path | None = None   # 沙箱根目錄（claude_code 時在隔離位置）；None＝work_dir
    config: Any = None
    sec: Any = None                    # SecClient（DOSSIER 前置用；pipeline 注入）
    prices: Any = None                 # 研究用價格客戶端（P-30 日線；pipeline 注入，M4-R3-02）

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
    """建立由 job 動態升級而來的新工作；依賴 job；params 附上建立時間（FULL_B 的開檔檢查用）。
    job 已產生過同類型的後續工作（post 重跑：jobs retry、續跑）→ 沿用其 job_id，讓 add_job 去重（M5-D1-02）。"""
    row = ctx.conn.execute(
        "SELECT job_id FROM jobs WHERE run_id=? AND ticker=? AND job_type=? AND deps IN (?, ?) ORDER BY rowid LIMIT 1",
        (job.run_id, job.ticker, job_type, json.dumps([job.job_id]), json.dumps([job.job_id], ensure_ascii=False))
    ).fetchone()
    n = row[0].rsplit(":", 1)[-1] if row is not None else ctx.next_seq(job, job_type)
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
        # 不依模型 files 清單收檔：要收檔的子類各自只收固定檔名（模型可列任意檔名植入同檔輸出區；A3-M3E-02）
        return self.next_jobs(job, result, ctx)

    def next_jobs(self, job: JobSpec, result: dict, ctx: HandlerCtx) -> list[JobSpec]:
        """依輸出決定動態新增的工作；可在 result 上附加欄位（如 final_signal）。"""
        return []

    def on_invalid(self, job: JobSpec, errors: list[str], ctx: HandlerCtx) -> list[JobSpec]:
        """重試後仍不合格：寫該工作的保守結論（不以模型自由文字取代），回傳替代的後續工作（引擎會拿掉對本工作的依賴）。
        每個處理器必須自己實作；沒有實作 → 引擎把工作標 failed（真正的失敗），不寫任何決策（M5-D8-05）。"""
        raise NotImplementedError(f"{job.job_type} 沒有不合格輸出的保守處理")

    def on_fail(self, job: JobSpec, error: str, ctx: HandlerCtx) -> list[JobSpec]:
        """非格式失敗（工作仍記 failed／skipped）時的保守紀錄；回傳替代工作。預設不做事，
        由引擎 run 結束時的 finalize_run 收尾（設計決策 D46）。"""
        return []

    def prompt_values(self, job: JobSpec, ctx: HandlerCtx, backend: str) -> dict[str, str]:
        """提示佔位符的值（覆寫預設的 pkg、work 等）。"""
        return {}

    def prompt_suffix(self, job: JobSpec) -> str:
        """附加在提示最後的文字（例如框架退回的修正要求）。"""
        return ""

    def pre_script(self, job: JobSpec, ctx: HandlerCtx) -> None:
        """工作執行前的腳本（預設不做事）。"""
        return None


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
