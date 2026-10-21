"""X36：analyze 的 base_params 把 prices.csv 的 splits 欄傳給 DOSSIER，內部人表才會換算拆股（M4-D4-02 生效）。"""
from __future__ import annotations

import json

import pandas as pd

from trader.pipelines.analyze import _price_rows, base_params


def _rows(tmp_path):
    csv = tmp_path / "prices.csv"
    pd.DataFrame([
        {"ticker": "AAA", "price": 10.0, "price_date": "2026-10-14", "price_src": "alpaca",
         "splits": "2024-06-10:10"},
        {"ticker": "BBB", "price": 20.0, "price_date": "2026-10-14", "price_src": "alpaca", "splits": ""},
    ]).to_csv(csv, index=False)
    return _price_rows(csv)


def test_base_params_passes_prices_csv_splits(tmp_path):
    rows = _rows(tmp_path)
    assert base_params(rows["AAA"], None, {}, {}, None)["splits"] == "2024-06-10:10"
    # 空字串經 read_csv 變 NaN → 確定無拆股 ""（表頭不加「未換算」註）
    assert base_params(rows["BBB"], None, {}, {}, None)["splits"] == ""
    # 沒有價格列／列上沒有 splits 欄（review 的收盤 dict）→ None＝未取得
    assert base_params({}, None, {}, {}, None)["splits"] is None
    assert base_params({"price": 5.0, "price_date": "2026-10-14"}, None, {}, {}, None)["splits"] is None
    json.dumps(base_params(rows["BBB"], None, {}, {}, None))           # params 要能存成 JSON（不得帶 NaN）


def test_dossier_insiders_receives_splits_from_params(tmp_path, monkeypatch):
    from trader.jobs import deep
    from trader.models import JobSpec
    import trader.analysis.insiders as ins_mod

    seen = {}

    def fake(sec, cik, asof, **k):
        seen.update(k)
        return {"stats": {}, "summary": {}, "markdown": "\n## 內部人交易\n\n無\n"}
    monkeypatch.setattr(ins_mod, "build_insider_section", fake)
    params = json.loads(json.dumps(base_params(_rows(tmp_path)["AAA"], None, {}, {}, None)))
    job = JobSpec(job_id="r:AAA:DOSSIER:1", run_id="r", job_type="DOSSIER", ticker="AAA", asof="2026-10-15",
                  params=params)
    td = tmp_path / "work"
    (td / "dossier").mkdir(parents=True)
    deep.DossierHandler()._insiders(job, None, "0000000001", td, [])
    assert seen["splits"] == "2024-06-10:10"
