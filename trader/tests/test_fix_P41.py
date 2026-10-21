"""P41：同業段（M4-D1-13／14、M4-D2-12、M4-D5-05、M4-D9-04、M4-D10-06＋D39）、內部人拆股基準（M4-D4-02）、
SEC 快取新鮮度（D36）。全部離線（MockTransport／假物件）。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pandas as pd
import pytest

import test_sec as TS
from test_insiders import CEO, DIR, FakeSec as InsSec
from test_peers import FakePeerSec, OutsideSec
from trader.analysis import insiders as I
from trader.analysis import peers as P
from trader.data import sec as S
from trader.data.http import HttpClient, ny_today

ASOF = "2026-10-16"


# ---------------------------------------------------------------- 同業段

def test_roe_series_missing_year_does_not_use_two_year_old_equity():
    """M4-D1-13：缺 2016 時 2017 ROE 為 nan（不得用 2015 權益）。"""
    df = pd.DataFrame({"net_income": {2014: 10.0, 2015: 10.0, 2017: 10.0, 2018: 10.0},
                       "equity": {2014: 100.0, 2015: 100.0, 2017: 100.0, 2018: 100.0}})
    r = P.roe_series(df)
    assert pd.isna(r.get(2017))
    assert r.get(2015) == pytest.approx(0.1) and r.get(2018) == pytest.approx(0.1)


def test_pool_counts_same_cik_once_and_excludes_own_other_class(tmp_path):
    """M4-D1-14：同 CIK 兩代號只占一席；本公司另一類股不算同業。"""
    sec = FakePeerSec()
    sec.cik["HDX"] = "2"      # HD 的另一類股（同 CIK）
    sec.cik["SELFB"] = "1"    # 本公司另一類股
    uni = ["SELF", "SELFB", "HD", "HDX", "A", "B", "C", "D", "E"]
    res = P.build_peer_section(sec, "SELF", "1", ASOF, uni, None, tmp_path, {})
    assert res["shock_sample"]["pool"] == 5            # HD、A、B、C、D
    md = res["markdown"]
    assert "| HD |" in md and "| HDX |" not in md and "| SELFB |" not in md


class _PredPeerSec(FakePeerSec):
    """本公司新 CIK 自己沒有歷史財報（改組），前身併入後才有（XOM 型）。"""
    def companyfacts(self, cik):
        return {"facts": {}} if cik == "1" else self.facts[cik]

    def merged_companyfacts(self, cik):
        return self.facts[cik]


def test_peers_use_predecessor_merged_facts(tmp_path):
    """M4-D2-12：有前身時 own 序列含前身年度（本公司列與衝擊年都算得出來）。"""
    sec = _PredPeerSec()
    res = P.build_peer_section(sec, "SELF", "1", ASOF, list(sec.cik), None, tmp_path, {})
    md = res["markdown"]
    assert "| SELF | 本公司 |" in md and "缺：無法計算（本公司財報年度缺）" not in md


def test_secclient_merged_companyfacts_includes_predecessor(tmp_path):
    pre = TS._pre_file(tmp_path, {TS.NEW: [TS.OLD]})
    c = S.SecClient(TS.UA, tmp_path / "c", http=TS.make_http(TS._pre_routes(), []), predecessors_path=pre)
    rows = c.merged_companyfacts(TS.NEW)["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    assert [r["end"] for r in rows] == ["2023-12-31", "2024-12-31", "2025-12-31"]


def test_shock_summary_does_not_say_none_when_samples_insufficient(tmp_path):
    """M4-D5-05：全部年度樣本不足時摘要不出現「無」，並寫 procedure/08 §1 的處置與 _peers 欄位。"""
    sec = FakePeerSec()
    res = P.build_peer_section(sec, "SELF", "1", ASOF, ["SELF", "HD", "E"], None, tmp_path, {})
    md = res["markdown"]
    assert "產業共同衝擊年：無" not in md
    assert "產業共同衝擊年：全部年度樣本不足，未判定" in md
    assert "未剔除產業衝擊年（缺同業）" in md
    assert res["shock_undetermined"] == list(range(2016, 2026))


def test_shock_summary_partial_insufficient_lists_years():
    """部分年度不足：不寫成「無」，列出無法判定的年度。"""
    peer_roe = {f"P{i}": pd.Series({y: 0.2 for y in range(2015, 2026)}) for i in range(5)}
    peer_roe["P0"] = peer_roe["P0"].drop(2016)       # 2016 只剩 4 家
    df, shocks = P.shock_years(peer_roe, list(range(2016, 2026)))
    assert shocks == [] and list(df.loc[df["flag"] == "樣本不足", "year"]) == [2016]


def test_shock_extra_candidates_sorted_by_cik_not_name(tmp_path, monkeypatch):
    """M4-D9-04：SEC 補樣本在同一 SIC 四碼內依 CIK 排序（SEC 名單是字母序）。"""
    class Sec(OutsideSec):
        def sic_companies(self, sic):
            return ["0000000011", "0000000009"]      # SEC 字母序：11 在前
    monkeypatch.setattr(P, "MAX_SHOCK_EXTRA", 1)
    res = P.build_peer_section(Sec(), "SELF", "1", ASOF, ["SELF", "HD", "A"], None, tmp_path, {})
    assert res["shock_sample"]["sec_extra_names"] == ["CIK0000000009"]


def test_peer_section_states_population_source_gics_and_survivorship(tmp_path):
    """D39／M4-D10-06：段落寫明母體來源（決策日當時版本）、GICS 為現行分類、存活者偏差。"""
    sec = FakePeerSec()
    res = P.build_peer_section(sec, "SELF", "1", ASOF, list(sec.cik), None, tmp_path, {},
                               universe_source="version:2026-09-30")
    md = res["markdown"]
    assert "決策日當時的成分股版本（2026-09-30" in md
    assert "GICS 子產業用的是現行分類" in md and "存活者" in md
    assert "價" not in md.replace("價值", "")
    assert "可能含決策日之後才納入者" in P.universe_note("cache_not_pit")


def test_dossier_peers_loads_universe_as_of_decision_date(tmp_path, monkeypatch):
    """M4-D10-06：deep._peers 以 asof 取股票池（universe_versions 當時版本）。"""
    from types import SimpleNamespace
    from trader.jobs import deep
    from trader.models import JobSpec
    import trader.analysis.peers as peers_mod
    import trader.data.universe as uni_mod

    seen = {}

    def fake_load(paths, conn, **k):
        seen.update(k)
        return ["SELF"], "version:2026-09-30"
    monkeypatch.setattr(uni_mod, "load_universe", fake_load)
    monkeypatch.setattr(peers_mod, "load_gics", lambda p: {})
    monkeypatch.setattr(peers_mod, "build_peer_section", lambda *a, **k: {
        "markdown": "x", "missing": [], "peer_files": [], "universe_source": k.get("universe_source")})
    job = JobSpec(job_id="r:SELF:DOSSIER:1", run_id="r", job_type="DOSSIER", ticker="SELF", asof="2026-08-03",
                  params={})
    td = tmp_path / "td"
    (td / "dossier").mkdir(parents=True)
    ctx = SimpleNamespace(paths=SimpleNamespace(state=tmp_path), conn=None)
    md, src = deep.DossierHandler()._peers(job, ctx, object(), "1", td, [], [])
    assert seen.get("asof") == "2026-08-03" and src == "version:2026-09-30"


def _sic_http(sizes: dict[int, int], seen: list):
    def handler(req: httpx.Request) -> httpx.Response:
        st = int(req.url.params.get("start", "0"))
        seen.append(st)
        n = sizes.get(st, 0)
        return httpx.Response(200, text="<feed>" + "".join(f"<cik>{st + i + 1}</cik>" for i in range(n)) + "</feed>")
    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, headers={"User-Agent": TS.UA})


def test_sic_companies_paginates_and_ignores_old_one_page_cache(tmp_path):
    """M4-D9-04：第一頁 100 筆時會請求 start=100；舊版只有第一頁的快取不再讀。"""
    (tmp_path / "sic_4512.json").write_text(json.dumps([f"{i:010d}" for i in range(1, 101)]), encoding="utf-8")
    seen: list[int] = []
    c = S.SecClient(TS.UA, tmp_path, http=_sic_http({0: 100, 100: 30}, seen))
    got = c.sic_companies("4512")
    assert seen == [0, 100] and len(got) == 130 and c.warnings == []
    assert c.sic_companies("4512") == got and seen == [0, 100]     # 第二次讀快取


def test_sic_companies_truncation_warns(tmp_path):
    seen: list[int] = []
    c = S.SecClient(TS.UA, tmp_path, http=_sic_http({i * 100: 100 for i in range(10)}, seen))
    assert len(c.sic_companies("6411")) == S.SIC_MAX_PAGES * 100 and len(seen) == S.SIC_MAX_PAGES
    assert any("名單截斷" in w for w in c.warnings)


# ---------------------------------------------------------------- 內部人拆股基準（M4-D4-02）

def _doc(owner, role_xml, txs, title="Common Stock"):
    rows = "".join(f"""
        <nonDerivativeTransaction>
            <securityTitle><value>{t or title}</value></securityTitle>
            <transactionDate><value>{d}</value></transactionDate>
            <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
            <transactionAmounts><transactionShares><value>{sh}</value></transactionShares>
                <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode></transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>""" for d, code, sh, ad, after, t in txs)
    return f"""<SEC-DOCUMENT><TEXT><XML><ownershipDocument>
    <reportingOwner><reportingOwnerId><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship>{role_xml}</reportingOwnerRelationship></reportingOwner>
    <nonDerivativeTable>{rows}</nonDerivativeTable></ownershipDocument></XML></TEXT></SEC-DOCUMENT>"""


def _split_docs():
    return {"a1": _doc("Smith Jane", CEO, [("2025-03-01", "S", "5000", "D", "5000", "")]),
            "a2": _doc("Smith Jane", CEO, [("2025-09-01", "S", "1500", "D", "73500", "")])}


def test_insider_shares_converted_to_decision_date_split_basis():
    """10:1 類拆股前賣出換算後比例正確（15:1；拆股前賣 5,000、拆股後賣 1,500 → 期初持股的 51%）。"""
    res = I.build_insider_section(InsSec(_split_docs()), "1", ASOF, splits="2025-06-10:15")
    md = res["markdown"]
    assert "51%" in md and "76,500" in md
    assert "股數已換算為決策日拆股基準（拆股：2025-06-10 15:1）" in md
    assert res["summary"]["shares_sold_other"] == 76500


def test_insider_without_split_data_says_not_converted():
    md = I.build_insider_section(InsSec(_split_docs()), "1", ASOF)["markdown"]
    assert "股數未換算拆股" in md and "8%" in md


def test_insider_spinoff_factor_not_applied_and_no_split_no_note():
    md = I.build_insider_section(InsSec(_split_docs()), "1", ASOF, splits="2025-06-10:1.241")["markdown"]
    assert "8%" in md and "已換算" not in md and "未換算" not in md
    md2 = I.build_insider_section(InsSec(_split_docs()), "1", ASOF, splits="")["markdown"]
    assert "已換算" not in md2 and "未換算" not in md2


def test_insider_multiple_security_classes_listed_separately():
    docs = {"a1": _doc("Page Larry", DIR, [("2026-03-02", "S", "1000", "D", "9000", "Class A Common Stock"),
                                           ("2026-03-02", "S", "400", "D", "600", "Class B Common Stock, $0.001 par")])}
    people, st = I.collect(InsSec(docs), "1", ASOF)
    assert set(people) == {"Page Larry（Class A Common Stock）", "Page Larry（Class B Common Stock）"}
    assert people["Page Larry（Class A Common Stock）"].held_after == 9000
    md = I.build_insider_section(InsSec(docs), "1", ASOF)["markdown"]
    assert "| Page Larry（Class B Common Stock） |" in md and "$0.001" not in md
    assert "10%" in md and "40%" in md      # 1000÷10000、400÷1000：類別不混算


# ---------------------------------------------------------------- D36 快取新鮮度

SUB_OLD = {"sic": "6331", "filings": {"recent": {
    "form": ["10-K"], "filingDate": ["2026-02-20"], "reportDate": [""], "items": [""],
    "accessionNumber": ["0001067983-26-000005"], "primaryDocument": ["k.htm"]}}}
SUB_NEW = {"sic": "6331", "filings": {"recent": {
    "form": ["10-K", "8-K"], "filingDate": ["2026-02-20", "2026-10-16"], "reportDate": ["", ""], "items": ["", "8.01"],
    "accessionNumber": ["0001067983-26-000005", "0001067983-26-000099"], "primaryDocument": ["k.htm", "e.htm"]}}}


def _seed(tmp_path, name, obj, fetched=None, hours=1.0):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    t = time.time() - hours * 3600
    os.utime(p, (t, t))
    if fetched:
        Path(f"{p}.meta").write_text(json.dumps({"fetched_ny_date": fetched}), encoding="utf-8")
    return p


def test_cache_fetched_before_decision_date_is_refetched_even_if_recent(tmp_path):
    """快取 1 小時前抓（抓取日 10-16），asof＝10-17：10-16 的 8-K 不在快取 → 必須重抓（舊規則 20 小時內不重抓）。"""
    _seed(tmp_path, f"sub_CIK{TS.CIK}.json", SUB_OLD, fetched="2026-10-16", hours=1)
    calls: list[str] = []
    c = S.SecClient(TS.UA, tmp_path, http=TS.make_http({TS.SUBS_URL: SUB_NEW}, calls))
    got = c.filings(TS.CIK, "2026-10-17", forms=["8-K"])
    assert [f["filingDate"] for f in got] == ["2026-10-16"] and calls == [TS.SUBS_URL]
    assert S.read_fetched_ny_date(tmp_path / f"sub_CIK{TS.CIK}.json") == ny_today()


def test_cache_fetched_on_or_after_decision_date_is_permanent(tmp_path):
    """抓取日 ≥ asof：快取對該決策日完整，48 小時前抓的也不重抓（補跑不再每 20 小時重抓）。"""
    _seed(tmp_path, f"sub_CIK{TS.CIK}.json", SUB_OLD, fetched="2026-10-16", hours=48)
    _seed(tmp_path, f"CIK{TS.CIK}.json", TS.FACTS, fetched="2026-10-20", hours=500)
    calls: list[str] = []
    c = S.SecClient(TS.UA, tmp_path, http=TS.make_http(TS.base_routes(), calls))
    assert [f["filingDate"] for f in c.filings(TS.CIK, "2026-10-16")] == ["2026-02-20"]
    assert c.companyfacts(TS.CIK, asof="2026-10-15") == TS.FACTS
    assert calls == []
    c.companyfacts(TS.CIK)          # 沒有 asof → 照舊用 mtime 期限（500 小時 → 重抓）
    assert calls == [TS.FACTS_URL]


def test_incomplete_cache_with_fetch_failure_is_stale_fallback(tmp_path):
    _seed(tmp_path, f"sub_CIK{TS.CIK}.json", SUB_OLD, fetched="2026-10-10", hours=1)
    c = S.SecClient(TS.UA, tmp_path, http=TS.make_http({TS.SUBS_URL: httpx.Response(503)}, []))
    assert [f["filingDate"] for f in c.filings(TS.CIK, "2026-10-17")] == ["2026-02-20"]
    assert c.stale == [f"sub_CIK{TS.CIK}.json"]


def test_client_asof_default_used_when_call_has_no_asof(tmp_path):
    """SecClient.asof（DOSSIER 前置期間由 deep 設定）：companyfacts 不帶 asof 時以它判斷。"""
    _seed(tmp_path, f"CIK{TS.CIK}.json", TS.FACTS, fetched="2026-10-15", hours=1)
    calls: list[str] = []
    c = S.SecClient(TS.UA, tmp_path, http=TS.make_http(TS.base_routes(), calls))
    c.companyfacts(TS.CIK)
    assert calls == []                    # 無 asof：1 小時內照舊新鮮
    c.asof = "2026-10-16"
    c.companyfacts(TS.CIK)
    assert calls == [TS.FACTS_URL]        # 抓取日 10-15 < 決策日 10-16 → 重抓


def test_dossier_pre_script_sets_and_restores_sec_asof(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from trader.jobs import deep
    from trader.models import JobSpec

    seen = []
    monkeypatch.setattr(deep.DossierHandler, "_prep", lambda self, job, ctx, sec, td: seen.append(sec.asof))
    sec = SimpleNamespace(asof="2026-01-01")
    ctx = SimpleNamespace(sec=sec, work_dir=tmp_path / "work")
    job = JobSpec(job_id="r:X:DOSSIER:1", run_id="r", job_type="DOSSIER", ticker="X", asof=ASOF, params={})
    deep.DossierHandler().pre_script(job, ctx)
    assert seen == [ASOF] and sec.asof == "2026-01-01"


# ---------------------------------------------------------------- P41R 返工

def test_shock_summary_partial_insufficient_says_none_among_determinable(tmp_path):
    """M4-D5-05：部分年度樣本不足、其餘無衝擊 → 摘要寫「可判定的年度中無」並列出不足年度。"""
    sec = FakePeerSec()
    for concept in sec.facts["6"]["facts"]["us-gaap"].values():     # D 缺 FY2016 → 2016 只剩 4 家
        concept["units"]["USD"] = [r for r in concept["units"]["USD"] if not r["end"].startswith("2016")]
    res = P.build_peer_section(sec, "SELF", "1", ASOF, list(sec.cik), None, tmp_path, {})
    md = res["markdown"]
    assert "產業共同衝擊年：可判定的年度中無" in md and "產業共同衝擊年：無" not in md
    assert 2016 in res["shock_undetermined"] and "FY2016" in md


class _PredDupSec(FakePeerSec):
    """池內 HD（CIK 2）與本公司（CIK 1）各有前身 CIK；merged_companyfacts 已併入前身歷史。"""
    def __init__(self):
        super().__init__()
        self.facts["0000000098"] = self.facts["1"]
        self.facts["0000000099"] = self.facts["2"]
        self.facts["0000000050"] = self.facts["3"]     # 與池無關的 SEC 同 SIC 公司（應保留）

    def predecessors(self, c):
        return {"0000000001": ["0000000098"], "0000000002": ["0000000099"]}.get(str(c).zfill(10), [])

    def sic_companies(self, code):
        return ["0000000098", "0000000099", "0000000050"] if code == "5211" else []


def test_shock_extra_excludes_predecessors_of_pool_and_self(tmp_path):
    """M4-D2-12（P41R）：池內同業與本公司的前身 CIK 不得再當 SEC 補充樣本（同一公司重複計入）。"""
    res = P.build_peer_section(_PredDupSec(), "SELF", "1", ASOF, list(FakePeerSec().cik), None, tmp_path, {})
    assert res["shock_sample"]["sec_extra_names"] == ["CIK0000000050"]
    assert res["shock_sample"]["sec_extra"] == 1


def test_cache_usable_when_asof_is_after_ny_today(tmp_path):
    """D36（P41R）：asof＝紐約明天（台北清晨帶台北日期）時，紐約今天抓的快取可用，不得每次重抓。"""
    from datetime import date, timedelta
    calls: list[str] = []
    c = S.SecClient(TS.UA, tmp_path, http=TS.make_http(TS.base_routes(), calls))
    c.asof = (date.fromisoformat(ny_today()) + timedelta(days=1)).isoformat()
    for _ in range(3):
        c.companyfacts(TS.CIK)
    assert calls == [TS.FACTS_URL]


def test_sic_truncation_warns_again_when_read_from_cache(tmp_path):
    """M4-D9-04（P41R）：截斷名單寫進 30 天快取後，讀快取的新 client 也要記同一條截斷警告。"""
    seen: list[int] = []
    c1 = S.SecClient(TS.UA, tmp_path, http=_sic_http({i * 100: 100 for i in range(10)}, seen))
    got = c1.sic_companies("6411")
    w = [x for x in c1.warnings if "名單截斷" in x]
    assert len(w) == 1
    c2 = S.SecClient(TS.UA, tmp_path, http=_sic_http({}, seen))
    assert c2.sic_companies("6411") == got and len(seen) == S.SIC_MAX_PAGES     # 讀快取、未再請求
    assert c2.warnings == w
    from trader.analysis.dossier_prep import sec_status
    n = len(c2.warnings)                                                          # 下一個工作的切片起點
    c2.sic_companies("6411")
    c2.sic_companies("6411")
    assert sec_status(c2, 0, n)[2] == w   # 同一工作切片內只出現一次（去重在 sec_status，M4-R8-01）
