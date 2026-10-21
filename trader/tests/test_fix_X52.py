"""X52：sec_status 在主體未判定（subject_cik 為空，未設 asof 就呼叫）時照舊逐一列出全部舊快取。不上網。"""
from __future__ import annotations

import json

import httpx

import trader.data.sec as S
from trader.analysis.dossier_prep import sec_status
from trader.data.http import HttpClient


def test_status_without_subject_lists_all_stale(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "CIK0000000777.json").write_text(json.dumps({"cik": 777, "facts": {}}), encoding="utf-8")
    (cache / "CIK0000000777.json.meta").write_text(json.dumps({"fetched_ny_date": "2020-01-01"}), encoding="utf-8")
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)), sleep=lambda s: None, retries=0)
    sec = S.SecClient("test a@b.c", cache, http=http)
    sec.companyfacts("777", asof="2026-10-19")      # 未設 sec.asof → 主體未判定
    assert sec.subject_cik is None
    lines, stale, _ = sec_status(sec)
    assert stale == ["CIK0000000777.json"]
    assert len(lines) == 1 and "CIK0000000777.json" in lines[0] and "不是本公司申報" not in lines[0]
