"""X17（P12 M4-D6-4 剩餘部分）：review 的 TRIAGE（8-K）與 THESIS（10-Q／8-K 節錄）取文改走
dossier_prep.sanitize_filing_text 單一入口，回購股數、金額與每股均價不得進入寫出的申報檔。"""
from __future__ import annotations

import pytest

from trader import db
from trader.jobs.handlers import HandlerCtx
from trader.jobs.review_jobs import ThesisHandler, TriageHandler
from trader.jobs.validate import load_schemas
from trader.models import JobSpec
from trader.paths import get_paths

ASOF = "2026-10-16"
BUYBACK = "We repurchased 1,733,574 shares for $450 million at an average price of $259.60 per share."
LEAKS = ("1,733,574", "$450 million", "259.60")
# 同一句另有兩種原文樣態：與其他句同一行（舊的整行刪會連「Margins improved.」一起丟），
# 以及網頁轉文字時被折成兩行（舊的逐行刪只刪到含 average price 的下一行，股數與金額留下）。
BODY = (f"{BUYBACK} Margins improved.\n"
        "We repurchased 1,733,574 shares for $450 million\nat an average price of $259.60 per share.\n")


class FakeSec:
    def __init__(self, rows, texts):
        self.rows, self.texts = rows, texts

    def cik_for(self, ticker):
        return "0000000001"

    def filings(self, cik, asof, since=None, forms=None):
        return [f for f in self.rows if f["filingDate"] < asof and (since is None or f["filingDate"] > since)
                and (forms is None or f["form"] in forms)]

    def filing_text(self, url):
        return self.texts[url]


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield paths, conn
    conn.close()


def _ctx(paths, conn, sec):
    return HandlerCtx(paths=paths, schemas=load_schemas(paths.schemas_json), work_dir=paths.work, conn=conn, sec=sec)


def _job(job_type, params):
    return JobSpec(job_id=f"r1:KO:{job_type}:1", run_id="r1", job_type=job_type, ticker="KO", asof=ASOF, params=params)


def _written(paths, sub):
    files = [p for p in paths.work.rglob("*.txt") if p.parent.name == sub]
    assert files, f"沒有寫出 {sub}/ 檔"
    return "".join(p.read_text(encoding="utf-8") for p in files)


def test_triage_8k_buyback_masked(env):
    paths, conn = env
    url = "https://www.sec.gov/Archives/edgar/data/1/a/doc.htm"
    sec = FakeSec([], {url: f"Item 8.01 Other events.\nRevenue rose 5%.\n\n\n\n\n{BODY}"})
    job = _job("TRIAGE", {"triage": [{"type": "8-K", "date": "2026-10-01", "items": "8.01", "url": url}]})
    TriageHandler().pre_script(job, _ctx(paths, conn, sec))
    text = _written(paths, "triage_sec")
    for s in LEAKS:
        assert s not in text
    assert "Item 8.01 Other events." in text and "Margins improved." in text


@pytest.mark.parametrize("form", ["10-Q", "8-K"])
def test_thesis_excerpt_buyback_masked(env, form):
    paths, conn = env
    url = "https://www.sec.gov/Archives/edgar/data/1/q/q.htm"
    head = "Item 2. Management's Discussion and Analysis\n" if form == "10-Q" else "Item 8.01\n"
    body = f"{head}MDA_MARKER\n{BODY}"
    sec = FakeSec([{"form": form, "filingDate": "2026-10-01", "url": url, "accessionNumber": "0000000001-26-000001"}],
                  {url: body})
    job = _job("THESIS", {"last_review_date": "2026-01-01"})
    ThesisHandler().pre_script(job, _ctx(paths, conn, sec))
    text = _written(paths, "thesis_sec")
    for s in LEAKS:
        assert s not in text
    assert "MDA_MARKER" in text and "Margins improved." in text
