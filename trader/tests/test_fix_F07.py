"""F07（M4 修復批 C）：SEC 文字轉換、快取版本與原子寫入、HTTP 轉址與 SEC 封鎖。全部離線（MockTransport／本地樣本）。"""
from __future__ import annotations

import json
import re

import httpx
import pytest

from trader.data import alpaca, fred, http as http_mod, sec, yahoo
from trader.data.http import DataError, HttpClient

UA = "test test@example.com"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CIK = "0001067983"
FACTS_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000005/k.htm"
TICKERS = {"0": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE"}}

# r2 PHM 8-K EX-99 型態的本地樣本（Workiva 表格：標籤格、數字格、$ 與括號另成一格）
EX99_SAMPLE = """<html><body>
<div style="display:none"><ix:header><ix:hidden><ix:nonNumeric name="dei:AmendmentFlag">false</ix:nonNumeric>
<ix:nonNumeric name="dei:DocumentFiscalYearFocus">2026</ix:nonNumeric></ix:hidden>
<ix:references><link:schemaRef xlink:href="phm-20260930.xsd"/></ix:references></ix:header></div>
<p>Consolidated Balance Sheets</p>
<table>
<tr><td><span>Land</span></td><td>29,315</td><td>52,554</td></tr>
<tr><th>Item</th><th>2026</th><th>2025</th></tr>
<tr><td>Net income</td><td>$</td><td>3,336,825</td><td></td><td>$</td><td>3,801,823</td></tr>
<tr><td>Other</td><td>(1,234</td><td>)</td><td>5.67</td><td>8.90</td></tr>
</table>
</body></html>"""

# 兩個以上逗號千分位數字黏在一起、小數黏小數、括號黏數字（審查 t6.py 的判定式）
_GLUED = re.compile(r"\b\d{1,3}(?:,\d{3})+\d(?:,\d{3})+\b|\d\.\d{2}\d+\.\d{2}\b|\)\d|\d\(\d")


def _mock(handler) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, headers={"User-Agent": UA})


# ---------------------------------------------------------------- M4-D1-09 表格儲存格黏字
def test_html_to_text_separates_table_cells():
    t = sec.html_to_text("<table><tr><td>A</td><td>1,234</td><td>5,678</td></tr></table>")
    assert "1,2345,678" not in t
    assert re.search(r"A\s+1,234\s+5,678", t)


def test_html_to_text_th_cells_and_sample_has_no_glued_numbers():
    t = sec.html_to_text(EX99_SAMPLE)
    assert "Land29,315" not in t and "29,31552,554" not in t
    assert "Land 29,315 52,554" in t
    assert "Item 2026 2025" in t
    assert "3,336,8253,801,823" not in t
    assert not _GLUED.findall(t), _GLUED.findall(t)


def test_html_to_text_inline_tags_still_join_within_a_cell():
    # 儲存格內的行內標籤（span、b）不得被拆開：1,2<span>34</span> 仍是 1,234
    assert sec.html_to_text("<p>Revenue was 1,2<span>34</span> million</p>") == "Revenue was 1,234 million"
    assert "Total 1,234" in sec.html_to_text("<table><tr><td>Total</td><td><b>1,2</b>34</td></tr></table>")


# ---------------------------------------------------------------- M4-D1-10 ix:header 隱藏區
def test_html_to_text_drops_inline_xbrl_header():
    t = sec.html_to_text(EX99_SAMPLE)
    assert "dei:" not in t and "AmendmentFlag" not in t and "false" not in t
    assert "Consolidated Balance Sheets" in t
    assert t.startswith("Consolidated Balance Sheets")


# ---------------------------------------------------------------- M4-D9-02／D35 文字快取帶轉換器版本
def test_filing_text_ignores_old_unversioned_cache(tmp_path):
    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, text="<table><tr><td>Land</td><td>29,315</td><td>52,554</td></tr></table>")

    c = sec.SecClient(UA, tmp_path, http=_mock(handler))
    import hashlib
    key = hashlib.sha1(DOC_URL.encode("utf-8")).hexdigest()
    old = tmp_path / "docs" / f"{key}.txt"          # 修前格式：舊轉換器黏字結果
    old.parent.mkdir(parents=True)
    old.write_text("Land29,31552,554", encoding="utf-8")
    t = c.filing_text(DOC_URL)
    assert t == "Land 29,315 52,554"
    assert len(calls) == 1                           # 舊快取不被讀，重抓重轉
    new = tmp_path / "docs" / f"{key}_v{sec.CONVERTER_VERSION}.txt"
    assert new.read_text(encoding="utf-8") == t
    assert c.filing_text(DOC_URL) == t and len(calls) == 1   # 新版快取命中


def test_converter_version_bump_invalidates_cache(tmp_path, monkeypatch):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=_mock(lambda r: (calls.append(1), httpx.Response(200, text="<p>x</p>"))[1]))
    c.filing_text(DOC_URL)
    monkeypatch.setattr(sec, "CONVERTER_VERSION", sec.CONVERTER_VERSION + 1)
    c.filing_text(DOC_URL)
    assert len(calls) == 2


# ---------------------------------------------------------------- M4-D9-01 原子寫入與半截快取
def test_atomic_write_failure_leaves_no_target(tmp_path, monkeypatch):
    target = tmp_path / "a.json"

    def boom(src, dst):
        raise OSError("killed")

    monkeypatch.setattr(http_mod.os, "replace", boom)
    with pytest.raises(OSError):
        http_mod.atomic_write_text(target, "{}")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []            # 暫存檔也清掉


def test_sec_cached_json_truncated_file_refetches(tmp_path):
    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json=TICKERS)

    (tmp_path / "company_tickers.json").write_text('{"0": {"cik_str": 10679', encoding="utf-8")  # 半截 JSON
    c = sec.SecClient(UA, tmp_path, http=_mock(handler))
    assert c.cik_for("BRK.B") == CIK
    assert len(calls) == 1
    assert json.loads((tmp_path / "company_tickers.json").read_text(encoding="utf-8")) == TICKERS


def test_sec_truncated_stale_cache_with_fetch_failure_raises_data_error(tmp_path):
    p = tmp_path / "company_tickers.json"
    p.write_text('{"0": {"cik', encoding="utf-8")
    import os
    os.utime(p, (0, 0))                               # 過期
    c = sec.SecClient(UA, tmp_path, http=_mock(lambda r: httpx.Response(500)))
    with pytest.raises(DataError):                    # 不是 JSONDecodeError
        c.ticker_map()


def test_fred_truncated_cache_refetches(tmp_path):
    (tmp_path / "DGS30.json").write_text('[["2026-01-02", 4.', encoding="utf-8")
    csv_text = "observation_date,DGS30\n2026-01-02,4.75\n"
    f = fred.FredClient(None, tmp_path, http=_mock(lambda r: httpx.Response(200, text=csv_text)))
    assert f.t30("2026-02-01") == (0.0475, "2026-01-02")
    assert json.loads((tmp_path / "DGS30.json").read_text(encoding="utf-8")) == [["2026-01-02", 4.75]]


def test_yahoo_truncated_cache_refetches(tmp_path, monkeypatch):
    (tmp_path / "AAA.json").write_text('{"timestamp": [1', encoding="utf-8")
    body = {"chart": {"result": [{"timestamp": [], "indicators": {"quote": [{}]}}]}}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(body).encode()

    monkeypatch.setattr(yahoo.urllib.request, "urlopen", lambda req, timeout=30: R())
    y = yahoo.YahooClient(cache_dir=tmp_path, min_interval=0)
    assert y._chart("AAA") == body["chart"]["result"][0]
    assert json.loads((tmp_path / "AAA.json").read_text(encoding="utf-8")) == body["chart"]["result"][0]


# ---------------------------------------------------------------- M4-D8-01 不跟隨轉址（金鑰標頭不送往他域）
def test_alpaca_redirect_not_followed(monkeypatch):
    seen: list[tuple[str, dict]] = []

    def handler(req):
        seen.append((req.url.host, dict(req.headers)))
        if req.url.host == "data.alpaca.markets":
            return httpx.Response(302, headers={"Location": "https://evil.example.com/steal"})
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})

    real = alpaca.HttpClient

    def factory(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        kw["sleep"] = lambda s: None
        return real(**kw)

    monkeypatch.setattr(alpaca, "HttpClient", factory)
    a = alpaca.AlpacaClient("KEYID123", "SECRET456")
    with pytest.raises(DataError) as ei:
        a.bars(["AAPL"], "2026-01-01", "2026-01-31")
    assert [h for h, _ in seen] == ["data.alpaca.markets"]       # 沒送往 evil.example.com
    assert "SECRET456" not in str(ei.value) and "KEYID123" not in str(ei.value)


def test_http_client_follows_redirects_by_default():
    def handler(req):
        if req.url.path == "/a":
            return httpx.Response(301, headers={"Location": "https://www.sec.gov/b"})
        return httpx.Response(200, text="ok")

    assert _mock(handler).get("https://www.sec.gov/a").text == "ok"


def test_fred_keyed_client_does_not_follow_redirects(tmp_path):
    f = fred.FredClient("FREDKEY", tmp_path)
    assert f.http.follow_redirects is False
    assert fred.FredClient(None, tmp_path).http.follow_redirects is True   # 公開 CSV 無秘密，維持跟隨


# ---------------------------------------------------------------- M4-D8-02（＋M1-D8-03）SEC 403 封鎖及早停止
BLOCK_BODY = "<html><h1>Your Request Originates from an Undeclared Automated Tool</h1>Request Rate Threshold Exceeded</html>"


def test_sec_block_stops_features_after_one_request(tmp_path):
    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        if str(req.url).startswith(TICKERS_URL):
            return httpx.Response(200, json={str(i): {"cik_str": 100 + i, "ticker": f"T{i}", "title": "x"}
                                             for i in range(50)})
        return httpx.Response(403, text=BLOCK_BODY)

    c = sec.SecClient(UA, tmp_path, http=_mock(handler))
    with pytest.raises(http_mod.SecBlocked) as ei:
        c.features([f"T{i}" for i in range(50)], "2026-10-15")
    assert len(calls) == 2                            # tickers＋第一檔 companyfacts，之後不再打
    assert "重跑" in str(ei.value)
    with pytest.raises(http_mod.SecBlocked):          # 同一客戶端之後的請求不再送出
        c.http.get("https://www.sec.gov/x")
    assert len(calls) == 2


def test_generic_403_five_in_a_row_aborts_features(tmp_path):
    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        if str(req.url).startswith(TICKERS_URL):
            return httpx.Response(200, json={str(i): {"cik_str": 100 + i, "ticker": f"T{i}", "title": "x"}
                                             for i in range(50)})
        return httpx.Response(403, text="Forbidden")

    c = sec.SecClient(UA, tmp_path, http=_mock(handler))
    with pytest.raises(DataError, match="403"):
        c.features([f"T{i}" for i in range(50)], "2026-10-15")
    assert len(calls) <= 6


def test_sec_block_uses_stale_cache_without_more_requests(tmp_path):
    import os
    p = tmp_path / "company_tickers.json"
    p.write_text(json.dumps(TICKERS), encoding="utf-8")
    os.utime(p, (0, 0))
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=_mock(lambda r: (calls.append(1), httpx.Response(403, text=BLOCK_BODY))[1]))
    assert c.cik_for("BRK.B") == CIK                  # 舊快取照用
    assert c.company_titles()                         # 不再送請求
    assert len(calls) == 1
