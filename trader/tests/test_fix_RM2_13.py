"""RM2-13（RM2-10／M2-RS6-01）：DOSSIER 回寫走 dossier_prep.sanitize_text（遮回購 → 遮每股金額 → 逐句刪價格句）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader import db
from trader.jobs import deep
from trader.jobs.handlers import HandlerCtx
from trader.jobs.validate import load_schemas
from trader.models import JobSpec
from trader.paths import get_paths

ASOF = "2026-10-15"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_STATE_DIR", str(tmp_path / "state"))
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield HandlerCtx(paths=paths, schemas=load_schemas(paths.schemas_json), work_dir=paths.work, conn=conn)
    conn.close()


def test_dossier_post_uses_sanitize_text(ctx, tmp_path, monkeypatch):
    sb = tmp_path / "sb"
    (sb / "dossier").mkdir(parents=True)
    (sb / "dossier" / "KO.md").write_text(
        "# KO\n營收 10,000 百萬美元，每股股利 2.00 美元。\n"
        "可轉債轉換價每股 132.45 美元。\n預期股息殖利率 2.6%。\n"
        "RSU 加權平均授予日公允價值為每股 187.42 美元。\n", encoding="utf-8")
    job = JobSpec(job_id="r1:KO:DOSSIER:1", run_id="r1", job_type="DOSSIER", ticker="KO", asof=ASOF, params={})
    job.sandbox = str(sb)
    monkeypatch.setattr(deep, "financial_precheck", lambda td, dq: None)
    monkeypatch.setattr(deep, "next_job", lambda *a, **k: "NEXT")
    (deep._td(job, ctx) / "dossier").mkdir(parents=True, exist_ok=True)   # prepare 已建
    result: dict = {}
    assert deep.DossierHandler().post(job, result, ctx) == ["NEXT"]
    td = deep._td(job, ctx)
    out = (td / "dossier" / "KO.md").read_text(encoding="utf-8")
    for num in ("132.45", "2.6%", "187.42"):
        assert num not in out, out
    assert "營收 10,000 百萬美元" in out
    assert {"framework_price_lines_removed", "framework_buyback_shares_masked",
            "framework_per_share_masked"} <= set(result)
    assert result["framework_price_lines_removed"] + result["framework_per_share_masked"] >= 1
    dq = json.loads((td / "dossier" / "KO_dq.json").read_text(encoding="utf-8"))
    assert "framework_per_share_masked" in dq
