"""SEC 資料來源測試：全部經 httpx.MockTransport，不連網。"""
from __future__ import annotations

import os
import time

import httpx
import pytest

from trader.data import sec
from trader.data.http import HttpClient

UA = "test test@example.com"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CIK = "0001067983"
FACTS_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"
SUBS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000005/k.htm"

TICKERS = {
    "0": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
}
FACTS = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [
    {"form": "10-K", "fp": "FY", "start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-20", "val": 100},
]}}}}}
SUBS = {
    "sic": "6331",
    "filings": {"recent": {
        "form": ["10-Q", "10-K", "8-K", "10-K"],
        "filingDate": ["2026-10-01", "2026-02-20", "2026-10-01", "2025-02-20"],
        "reportDate": ["2026-09-30", "2025-12-31", "2026-09-30", "2024-12-31"],
        "items": ["", "", "2.02", ""],
        "accessionNumber": ["0001067983-26-000010", "0001067983-26-000005",
                            "0001067983-26-000011", "0001067983-25-000003"],
        "primaryDocument": ["q.htm", "k.htm", "e.htm", "k2.htm"],
    }},
}


def make_http(routes: dict, calls: list[str]) -> HttpClient:
    """routes：{不含查詢字串的 URL: JSON 物件 | 字串（text） | httpx.Response}；未知網址回 404。"""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.copy_with(query=None)))
        body = routes.get(str(request.url.copy_with(query=None)))
        if body is None:
            return httpx.Response(404, text="not found")
        if isinstance(body, httpx.Response):
            return body
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None,
                      headers={"User-Agent": UA})


def base_routes() -> dict:
    return {TICKERS_URL: TICKERS, FACTS_URL: FACTS, SUBS_URL: SUBS}


def age_file(path, hours: float) -> None:
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


# ---- 建構與代號 ----
def test_ua_without_at_rejected(tmp_path):
    with pytest.raises(ValueError):
        sec.SecClient("no-email-here", tmp_path)


def test_ticker_map_has_both_forms(tmp_path):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    m = c.ticker_map()
    assert m["BRK.B"] == CIK and m["BRK-B"] == CIK
    assert c.cik_for("brk.b") == CIK
    assert c.cik_for("BRK-B") == CIK
    assert c.cik_for("NOPE") is None


# ---- 快取 ----
def test_companyfacts_cache_hit_and_expiry(tmp_path):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    assert c.companyfacts(CIK) == FACTS
    assert c.companyfacts(CIK) == FACTS  # 快取命中，不重抓
    assert calls.count(FACTS_URL) == 1

    path = tmp_path / f"CIK{CIK}.json"
    age_file(path, hours=21)  # 超過 max_age_hours=20 → 重抓
    c.companyfacts(CIK)
    assert calls.count(FACTS_URL) == 2


def test_submissions_cache_expiry_uses_mtime(tmp_path):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls), max_age_hours=1)
    c.submissions(CIK)
    age_file(tmp_path / f"sub_CIK{CIK}.json", hours=0.5)
    c.submissions(CIK)
    assert calls.count(SUBS_URL) == 1  # 未過期
    age_file(tmp_path / f"sub_CIK{CIK}.json", hours=2)
    c.submissions(CIK)
    assert calls.count(SUBS_URL) == 2  # 已過期


# ---- 申報清單 ----
def test_filings_time_filter_excludes_asof_day(tmp_path):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    rows = c.filings(CIK, asof="2026-10-01")
    # 2026-10-01 等於 asof 必須排除；2026-02-20 與 2025-02-20 保留，依申報日升冪
    assert [r["filingDate"] for r in rows] == ["2025-02-20", "2026-02-20"]
    assert rows[1]["url"] == DOC_URL
    assert rows[1]["items"] == "" and rows[1]["reportDate"] == "2025-12-31"


def test_filings_since_and_forms(tmp_path):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    # since 嚴格晚於：2025-02-20 當天也排除
    rows = c.filings(CIK, asof="2027-01-01", since="2025-02-20")
    assert [r["form"] for r in rows] == ["10-K", "10-Q", "8-K"]
    assert [r["filingDate"] for r in rows] == ["2026-02-20", "2026-10-01", "2026-10-01"]
    only_10k = c.filings(CIK, asof="2027-01-01", forms=["10-K"])
    assert [r["form"] for r in only_10k] == ["10-K", "10-K"]


def test_offline_filings(tmp_path):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    out = c.offline_filings({"BRK.B": CIK}, asof="2026-10-01")
    assert list(out) == ["BRK.B"]
    assert len(out["BRK.B"]) == 2


# ---- 申報全文 ----
HTML_DOC = (
    "<html><head><style>p{color:red}</style><script>var x=1;</script></head>"
    "<body><h1>Title &amp; Co</h1><p>First&nbsp;para <b>bold</b></p>"
    "<div>Line<br>two</div><table><tr><td>A</td><td>B</td></tr></table></body></html>"
)


def test_filing_text_html_conversion_and_cache(tmp_path):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http({DOC_URL: HTML_DOC}, calls))
    text = c.filing_text(DOC_URL)
    assert "Title & Co" in text
    assert "First para bold" in text
    assert "Line\ntwo" in text
    assert "var x" not in text and "color:red" not in text
    assert "<" not in text
    assert c.filing_text(DOC_URL) == text
    assert calls.count(DOC_URL) == 1  # 第二次走快取


def test_filing_text_rejects_non_sec_url(tmp_path):
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http({}, calls))
    with pytest.raises(ValueError):
        c.filing_text("https://evil.example.com/k.htm")
    with pytest.raises(ValueError):
        c.filing_text("http://www.sec.gov/k.htm")
    assert calls == []  # 拒絕時不發出請求


# ---- 特徵 ----
def test_features_passes_arguments_and_reports_missing(tmp_path, monkeypatch):
    fe = sec._load_fetch_edgar()
    seen: list[tuple] = []

    def fake_features(tk, cik, facts, sic, asof, latest):
        seen.append((tk, cik, facts, sic, asof, latest))
        return {"ticker": tk, "cik": cik, "asof": asof, "data_notes": "fake"}

    monkeypatch.setattr(fe, "features", fake_features)
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    df = c.features(["brk-b", "NOPE"], asof="2026-10-16", latest=True)

    assert seen == [("BRK.B", CIK, FACTS, "6331", "2026-10-16", True)]
    assert list(df["ticker"]) == ["BRK.B", "NOPE"]
    assert df.loc[1, "data_notes"] == "SEC 對照表找不到代號"


def test_features_error_row(tmp_path, monkeypatch):
    fe = sec._load_fetch_edgar()

    def boom(*a, **k):
        raise RuntimeError("前視")

    monkeypatch.setattr(fe, "features", boom)
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    df = c.features(["BRK-B"], asof="2026-10-16")
    assert df.loc[0, "ticker"] == "BRK.B"
    assert df.loc[0, "data_notes"] == "抓取失敗：RuntimeError: 前視"   # 型別＋訊息（A3-M1C-02）


def test_features_real_skill_does_not_crash(tmp_path):
    """用極小的假 companyfacts 跑一次真的 fetch_edgar.features：不崩潰，回傳該代號列。"""
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    df = c.features(["BRK-B"], asof="2026-10-16")
    assert len(df) == 1
    assert df.loc[0, "ticker"] == "BRK.B"


def test_fetch_failure_falls_back_to_stale_cache(tmp_path):
    calls = []
    c = sec.SecClient(UA, tmp_path, http=make_http(base_routes(), calls))
    c.submissions(CIK)
    age_file(tmp_path / f"sub_CIK{CIK}.json", 48)
    down = sec.SecClient(UA, tmp_path, http=make_http({SUBS_URL: httpx.Response(503)}, []))
    assert down.submissions(CIK)["sic"] == "6331"
    assert down.stale == [f"sub_CIK{CIK}.json"]


def test_filings_reads_older_pages_when_since_predates_recent(tmp_path):
    older = {"form": ["8-K"], "filingDate": ["2024-06-01"], "reportDate": [""], "items": ["1.01"],
             "accessionNumber": ["0001067983-24-000001"], "primaryDocument": ["old.htm"]}
    subs = {"sic": "6331", "filings": {"recent": SUBS["filings"]["recent"], "files": [
        {"name": "CIK0001067983-submissions-001.json", "filingFrom": "2020-01-01", "filingTo": "2024-12-31"}]}}
    page_url = "https://data.sec.gov/submissions/CIK0001067983-submissions-001.json"
    calls = []
    c = sec.SecClient(UA, tmp_path, http=make_http({SUBS_URL: subs, page_url: older}, calls))
    assert [f["filingDate"] for f in c.filings(CIK, "2026-10-16", since="2025-03-01")][0] == "2026-02-20"
    assert page_url not in calls  # since 在 recent 範圍內 → 不讀舊分頁
    got = c.filings(CIK, "2026-10-16", since="2024-01-01")
    assert got[0]["filingDate"] == "2024-06-01" and got[0]["items"] == "1.01"
    assert page_url in calls


# ---- 前身 CIK（改組後新 CIK 承接舊 CIK 歷史財報）----
NEW = "0000999999"   # 現行（新）CIK
OLD = "0000999998"   # 前身（舊）CIK
NEW_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{NEW}.json"
OLD_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{OLD}.json"
NEW_SUBS_URL = f"https://data.sec.gov/submissions/CIK{NEW}.json"
OLD_SUBS_URL = f"https://data.sec.gov/submissions/CIK{OLD}.json"
TICKERS_PRE = {"0": {"cik_str": 999999, "ticker": "NEWX", "title": "New Holdco"}}


def _ni(end: str, filed: str, val: float) -> dict:
    """一筆 10-K 全年淨利（期間型，約一年）。"""
    return {"form": "10-K", "fp": "FY", "start": end[:4] + "-01-01", "end": end, "filed": filed, "val": val}


def _facts(name: str, rows: list[dict]) -> dict:
    return {"cik": "", "entityName": name, "facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": rows}}}}}


OLD_FACTS = _facts("Old Corp", [_ni("2023-12-31", "2024-02-20", 10), _ni("2024-12-31", "2025-02-20", 20)])
NEW_FACTS = _facts("New Corp", [_ni("2024-12-31", "2025-02-20", 20),  # 與前身完全相同 → 去重
                                _ni("2025-12-31", "2026-02-20", 30)])
NEW_SUBS = {"sic": "2911", "filings": {"recent": {
    "form": ["8-K12B", "10-Q"], "filingDate": ["2026-07-01", "2026-08-03"],
    "reportDate": ["2026-07-01", "2026-06-30"], "items": ["1.01", ""],
    "accessionNumber": ["0000999999-26-000001", "0000999999-26-000002"],
    "primaryDocument": ["a.htm", "q.htm"],
}}}
OLD_SUBS = {"sic": "2911", "filings": {"recent": {
    "form": ["10-K", "8-K", "10-K", "10-K"],
    "filingDate": ["2024-02-20", "2026-10-16", "2025-02-20", "2025-02-19"],
    "reportDate": ["2023-12-31", "2026-10-16", "2024-12-31", "2024-12-31"],
    "items": ["", "", "", ""],
    "accessionNumber": ["0000999998-24-000001", "0000999998-26-000009",
                        "0000999998-25-000002", "0000999998-25-000001"],
    "primaryDocument": ["k23.htm", "e.htm", "k24.htm", "k24a.htm"],
}}}


def _pre_file(tmp_path, mapping: dict) -> "Path":
    import json
    p = tmp_path / "pre.json"
    p.write_text(json.dumps({"map": mapping}), encoding="utf-8")
    return p


def _pre_routes(old_ok: bool = True) -> dict:
    r = {TICKERS_URL: TICKERS_PRE, NEW_URL: NEW_FACTS, NEW_SUBS_URL: NEW_SUBS, OLD_SUBS_URL: OLD_SUBS}
    if old_ok:
        r[OLD_URL] = OLD_FACTS
    return r


def test_packaged_xom_predecessor_mapping(tmp_path):
    """套件內對照檔含 XOM：新 CIK 2115436 → 舊 CIK 34088（10 碼）。"""
    c = sec.SecClient(UA, tmp_path / "c")
    assert c.predecessors("2115436") == ["0000034088"]
    assert c.predecessors("0000034088") == []


def test_merge_companyfacts_order_dedupe_and_top_level():
    import copy
    cur_before = copy.deepcopy(NEW_FACTS)
    merged = sec.merge_companyfacts(NEW_FACTS, [OLD_FACTS])
    rows = merged["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    # 前身在前、現行在後；完全相同的 2024 筆只保留一筆
    assert [r["end"] for r in rows] == ["2023-12-31", "2024-12-31", "2025-12-31"]
    assert merged["entityName"] == "New Corp"          # 其他頂層欄位用現行公司的
    assert NEW_FACTS == cur_before                      # 輸入不被修改


def test_features_merges_predecessor_and_annotates_notes(tmp_path):
    calls: list[str] = []
    pre = _pre_file(tmp_path, {NEW: [OLD]})
    c = sec.SecClient(UA, tmp_path / "c", http=make_http(_pre_routes(), calls), predecessors_path=pre)
    r = c.features(["NEWX"], asof="2026-10-16").iloc[0]
    assert r["fy_last"] == 2025 and r["net_income"] == 30
    assert r["years_of_data"] == 3                      # 2023 只存在於前身 → 有併入
    assert r["name"] == "New Corp"
    assert r["data_notes"].endswith("；含前身 CIK " + OLD)
    assert "盈餘＝GAAP 淨利代理" in r["data_notes"]
    assert OLD_URL in calls and c.warnings == []

    # 對照：無前身時同一份現行資料只有 2 年，且 data_notes 不含前身字樣
    plain = sec.SecClient(UA, tmp_path / "p", http=make_http(_pre_routes(), []), predecessors_path=_pre_file(tmp_path, {}))
    r0 = plain.features(["NEWX"], asof="2026-10-16").iloc[0]
    assert r0["years_of_data"] == 2 and "前身" not in r0["data_notes"]


def test_features_predecessor_fetch_failure_does_not_break(tmp_path):
    pre = _pre_file(tmp_path, {NEW: [OLD]})
    c = sec.SecClient(UA, tmp_path / "c", http=make_http(_pre_routes(old_ok=False), []), predecessors_path=pre)
    r = c.features(["NEWX"], asof="2026-10-16").iloc[0]
    assert r["fy_last"] == 2025 and r["net_income"] == 30 and r["years_of_data"] == 2
    assert "前身 CIK " + OLD + " 抓取失敗未併入" in r["data_notes"]
    assert "含前身" not in r["data_notes"]
    assert c.warnings and OLD in c.warnings[0]


def test_user_override_file_replaces_same_key(tmp_path):
    # 使用者覆寫：同鍵整個清單取代（新 CIK 的前身改為空）；不同鍵則新增
    c = sec.SecClient(UA, tmp_path / "c", http=make_http({}, []),
                      predecessors_path=_pre_file(tmp_path, {"0002115436": [], "0000999999": ["0000999998"]}))
    assert c.predecessors("2115436") == []
    assert c.predecessors(NEW) == [OLD]


def test_user_override_missing_file_is_ignored(tmp_path):
    c = sec.SecClient(UA, tmp_path / "c", http=make_http({}, []), predecessors_path=tmp_path / "nope.json")
    assert c.predecessors("2115436") == ["0000034088"]  # 套件內對照仍生效


def test_user_override_rejects_bad_cik(tmp_path):
    with pytest.raises(ValueError):
        sec.SecClient(UA, tmp_path / "c", http=make_http({}, []),
                      predecessors_path=_pre_file(tmp_path, {"ABC": ["0000034088"]}))


def test_filings_merges_predecessor_with_cik_and_filters(tmp_path):
    pre = _pre_file(tmp_path, {NEW: [OLD]})
    c = sec.SecClient(UA, tmp_path / "c", http=make_http(_pre_routes(), []), predecessors_path=pre)
    rows = c.filings(NEW, asof="2026-10-16")
    # 2026-10-16 等於 asof 排除（前身那筆也一樣）；依申報日升冪；每筆帶 cik
    assert [r["filingDate"] for r in rows] == ["2024-02-20", "2025-02-19", "2025-02-20", "2026-07-01", "2026-08-03"]
    assert [r["cik"] for r in rows] == [OLD, OLD, OLD, NEW, NEW]
    assert rows[0]["url"].startswith(f"https://www.sec.gov/Archives/edgar/data/{int(OLD)}/")
    only_10k = c.filings(NEW, asof="2026-10-16", forms=["10-K"])
    assert [r["cik"] for r in only_10k] == [OLD, OLD, OLD]
    since = c.filings(NEW, asof="2026-10-16", since="2025-02-19")
    assert [r["filingDate"] for r in since] == ["2025-02-20", "2026-07-01", "2026-08-03"]


def test_filings_predecessor_failure_warns_and_keeps_current(tmp_path):
    pre = _pre_file(tmp_path, {NEW: [OLD]})
    routes = _pre_routes()
    routes.pop(OLD_SUBS_URL)  # 前身 submissions 404
    c = sec.SecClient(UA, tmp_path / "c", http=make_http(routes, []), predecessors_path=pre)
    rows = c.filings(NEW, asof="2026-10-16")
    assert [r["cik"] for r in rows] == [NEW, NEW]
    assert c.warnings and OLD in c.warnings[0]


def test_no_predecessor_output_unchanged(tmp_path):
    """無前身：filings 行為不變（不加 cik 欄）；features 的 data_notes 不加註記。"""
    c = sec.SecClient(UA, tmp_path / "c", http=make_http(base_routes(), []),
                      predecessors_path=_pre_file(tmp_path, {}))
    rows = c.filings(CIK, asof="2026-10-01")
    assert set(rows[0]) == {"form", "filingDate", "reportDate", "items", "accessionNumber", "primaryDocument", "url"}
    assert c.warnings == []


def test_exhibit_text_picks_lowest_ex99(tmp_path):
    from trader.data.sec import SecClient
    sgml = ("<SEC-DOCUMENT><DOCUMENT>\n<TYPE>8-K\n<TEXT><html>main</html></TEXT></DOCUMENT>"
            "<DOCUMENT>\n<TYPE>EX-99.2\n<TEXT><p>slides</p></TEXT></DOCUMENT>"
            "<DOCUMENT>\n<TYPE>EX-99.1\n<TEXT><p>press release body</p></TEXT></DOCUMENT>")
    c = SecClient.__new__(SecClient)
    c.full_submission_text = lambda cik, acc: sgml
    assert "press release body" in c.exhibit_text("1", "0000000001-26-000001", "EX-99")
    assert c.exhibit_text("1", "0000000001-26-000001", "EX-10") is None
    sgml2 = "<DOCUMENT>\n<TYPE>EX-99\n<TEXT>plain ex99</TEXT></DOCUMENT>"
    c.full_submission_text = lambda cik, acc: sgml2
    assert "plain ex99" in c.exhibit_text("1", "x", "EX-99")


SGML_MULTI = """<SEC-DOCUMENT>
<DOCUMENT><TYPE>8-K<FILENAME>e.htm<TEXT><html>main</html></TEXT></DOCUMENT>
<DOCUMENT><TYPE>EX-99.2<FILENAME>sup.htm<TEXT><html><p>Retention 91%</p></html></TEXT></DOCUMENT>
<DOCUMENT><TYPE>EX-99.1<FILENAME>pr.htm<TEXT><html><p>Revenue up</p></html></TEXT></DOCUMENT>
<DOCUMENT><TYPE>EX-99.3<FILENAME>deck.pdf<TEXT>%PDF-binary</TEXT></DOCUMENT>
</SEC-DOCUMENT>"""


def test_exhibit_texts_all_sorted_skip_pdf(tmp_path):
    url = "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000011/0001067983-26-000011.txt"
    c = sec.SecClient(UA, tmp_path, http=make_http({url: SGML_MULTI}, []))
    got = c.exhibit_texts(CIK, "0001067983-26-000011", "EX-99")
    assert [t for t, _ in got] == ["EX-99.1", "EX-99.2"]
    assert "Retention 91%" in got[1][1]
    assert c.exhibit_text(CIK, "0001067983-26-000011", "EX-99").strip() == "Revenue up"


def test_sic_companies_parses_and_caches(tmp_path):
    atom = "<feed><entry><content><company-info><cik>0000353184</cik></company-info></content></entry>" \
           "<entry><content><company-info><cik>1048911</cik></company-info></content></entry></feed>"
    calls: list[str] = []
    c = sec.SecClient(UA, tmp_path, http=make_http({"https://www.sec.gov/cgi-bin/browse-edgar": atom}, calls))
    assert c.sic_companies("4513") == ["0000353184", "0001048911"]
    assert c.sic_companies("4513") == ["0000353184", "0001048911"] and len(calls) == 1   # 第二次讀快取
    assert c.sic_companies("45") == []
