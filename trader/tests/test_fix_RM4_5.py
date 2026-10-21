"""RM4-5：SEC 客戶端複審修復（M4-R8-01 截斷警告跨檔、M4-R8-02 資料狀態舊快取行、M4-R8-03 封鎖自動解除）。
全部用 httpx.MockTransport 假回應，不上網。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import trader.data.sec as S
from trader.analysis.dossier_prep import sec_status
from trader.data.http import HttpClient, SecBlocked

UA = "test a@b.c"


def _client(tmp_path: Path, handler, retries: int = 0) -> S.SecClient:
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, retries=retries)
    return S.SecClient(UA, tmp_path / "cache", http=http)


# ---------------------------------------------------------------- M4-R8-01
def _full_pages(req: httpx.Request) -> httpx.Response:
    start = int(req.url.params.get("start", 0))
    return httpx.Response(200, text="".join(f"<cik>{start + i + 1}</cik>" for i in range(100)))


def test_sic_truncation_warning_reaches_second_dossier_in_same_run(tmp_path):
    """同一 run 共用 SecClient：第二檔同 SIC 的資料包切片也要看到截斷警告（修前為空）。"""
    sec = _client(tmp_path, _full_pages)
    out = []
    for _ in range(2):  # 兩個 DOSSIER 工作，各自以進場時的長度切片
        n0, w0 = len(sec.stale), len(sec.warnings)
        sec.sic_companies("6022")
        sec.sic_companies("6022")            # 同一工作內查兩次：切片仍只出現一次
        out.append(sec_status(sec, n0, w0)[2])
    want = [w for w in out[0] if "SIC 6022" in w and "名單截斷" in w]
    assert len(want) == 1 and out[1] == want


def test_sic_truncation_warning_from_cache_reaches_second_job(tmp_path):
    """讀 30 天快取的截斷名單同樣每次查詢都記（第二工作切片有）。"""
    _client(tmp_path, _full_pages).sic_companies("6411")          # 寫入截斷快取
    sec = _client(tmp_path, lambda r: pytest.fail("不應再請求"))
    sec.sic_companies("6411")
    n0, w0 = len(sec.stale), len(sec.warnings)
    sec.sic_companies("6411")
    assert any("名單截斷" in w for w in sec_status(sec, n0, w0)[2])


# ---------------------------------------------------------------- M4-R8-02
def _stale_cache(cache: Path, name: str) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    body = {"sic": "6022", "filings": {"recent": {}}} if name.startswith("sub_") else {"cik": 1, "facts": {}}
    (cache / name).write_text(json.dumps(body), encoding="utf-8")
    (cache / f"{name}.meta").write_text(json.dumps({"fetched_ny_date": "2020-01-01"}), encoding="utf-8")


def test_status_lists_only_own_and_predecessor_stale_files(tmp_path):
    """SEC 503、全部走舊快取：狀態只逐一列本公司與前身的檔，同業池 500 份只寫份數；完整清單仍回傳給 _sources。"""
    cache = tmp_path / "cache"
    own, pre = "0000000777", "0000000778"
    for c in [own, pre]:
        _stale_cache(cache, f"CIK{c}.json")
        _stale_cache(cache, f"sub_CIK{c}.json")
    for i in range(500):
        _stale_cache(cache, f"sub_CIK{i + 1:010d}.json")
    pmap = tmp_path / "pre.json"
    pmap.write_text(json.dumps({"map": {own: [pre]}}), encoding="utf-8")
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)), sleep=lambda s: None, retries=0)
    sec = S.SecClient(UA, cache, http=http, predecessors_path=pmap)
    sec.asof = "2026-10-19"          # deep 的 DOSSIER 前置
    n0, w0 = len(sec.stale), len(sec.warnings)
    sec.merged_companyfacts(own)     # build_dossier_base 最先查本公司（＋前身）
    sec.submissions(own)
    sec.submissions(pre)
    for i in range(500):             # peers.py 池內 SIC 查詢
        sec.submissions(f"{i + 1}")
    lines, stale, _ = sec_status(sec, n0, w0)
    assert len(stale) == 504                                         # _sources.json 的 sec_stale 仍是完整清單
    assert len(lines) == 2 and sum(len(x) for x in lines) < 400       # 修前單行 11,542 字
    assert lines[0].startswith("缺：SEC 抓取失敗改用舊快取（可能漏掉最新申報")
    for c in [own, pre]:
        assert f"CIK{c}.json" in lines[0] and f"sub_CIK{c}.json" in lines[0]
    assert "sub_CIK0000000001.json" not in lines[0] and "500 份" in lines[1]
    sec.asof = None                  # 前置結束還原 → 主體重設
    assert sec.subject_cik is None


def test_status_without_own_stale_has_only_count_line(tmp_path):
    """本公司資料新鮮、只有同業舊快取：沒有「可能漏掉最新申報」行（exp2 情境）。"""
    cache = tmp_path / "cache"
    for i in range(3):
        _stale_cache(cache, f"sub_CIK{i + 1:010d}.json")
    sec = _client(tmp_path, lambda r: httpx.Response(503))
    sec.asof = "2026-10-19"
    n0, w0 = len(sec.stale), len(sec.warnings)
    for i in range(3):
        sec.submissions(f"{i + 1}")
    lines, stale, _ = sec_status(sec, n0, w0)
    assert len(stale) == 3 and len(lines) == 1 and "3 份" in lines[0] and "可能漏掉" not in lines[0]


def test_status_with_fake_sec_keeps_listing_all():
    """沒有 subject_cik 的替身（舊介面）照舊逐一列出。"""
    class Fake:
        stale = ["CIK0000000002.json"]
        warnings: list = []
    lines, stale, _ = sec_status(Fake())
    assert stale == ["CIK0000000002.json"] and "CIK0000000002.json" in lines[0]


# ---------------------------------------------------------------- M4-R8-03
def _block_then(ok_after: int, calls: list):
    def h(req):
        calls.append(str(req.url))
        if len(calls) <= ok_after:
            return httpx.Response(403, text="Your Request Rate Threshold exceeded")
        return httpx.Response(200, text="<p>ok</p>")
    return h


def test_block_lifts_after_cooldown_and_retries_once(tmp_path):
    calls: list = []
    sec = _client(tmp_path, _block_then(1, calls))
    now = [1000.0]
    sec._clock = lambda: now[0]
    url = "https://www.sec.gov/Archives/a.htm"
    with pytest.raises(SecBlocked):
        sec.filing_text(url)
    now[0] += S.BLOCK_COOLDOWN_S - 1
    with pytest.raises(SecBlocked):  # 冷卻未滿：不送請求
        sec.filing_text(url)
    assert len(calls) == 1
    now[0] += 1
    assert sec.filing_text(url) == "ok" and len(calls) == 2   # 600 秒到：解除並重試成功


def test_still_blocked_after_cooldown_stops_again(tmp_path):
    calls: list = []
    sec = _client(tmp_path, _block_then(99, calls))
    now = [0.0]
    sec._clock = lambda: now[0]
    url = "https://www.sec.gov/Archives/b.htm"
    with pytest.raises(SecBlocked):
        sec.filing_text(url)
    now[0] += 300
    with pytest.raises(SecBlocked):   # 封鎖中的拒絕不重設計時
        sec.filing_text(url)
    now[0] += 300
    with pytest.raises(SecBlocked):   # 到期重試一次，仍封鎖
        sec.filing_text(url)
    assert len(calls) == 2
    now[0] += S.BLOCK_COOLDOWN_S - 1
    with pytest.raises(SecBlocked):   # 照現行停止（重新計時，期間不送請求）
        sec.filing_text(url)
    assert len(calls) == 2


def test_block_cooldown_applies_to_json_cache_path(tmp_path):
    """_cached_json 路徑（無舊快取）同樣在冷卻後重試。"""
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(403, text="Undeclared Automated Tool")
        return httpx.Response(200, json={"sic": "1000", "filings": {"recent": {}}})
    sec = _client(tmp_path, h)
    now = [0.0]
    sec._clock = lambda: now[0]
    with pytest.raises(SecBlocked):
        sec.submissions("5")
    now[0] += S.BLOCK_COOLDOWN_S
    assert sec.submissions("5")["sic"] == "1000" and n["i"] == 2
