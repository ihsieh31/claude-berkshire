"""內部人交易（Form 4）測試：解析、彙整、價格隔離。全部離線。"""
from __future__ import annotations

from trader.analysis import insiders as I
from trader.analysis.dossier_prep import PRICE_TEXT_PATTERNS


def _doc(owner, role_xml, txs, plan="0", footnote=""):
    rows = []
    for date, code, shares, ad, after, price in txs:
        rows.append(f"""
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>{date}</value></transactionDate>
            <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>""")
    return f"""<SEC-DOCUMENT><TEXT><XML><?xml version="1.0"?>
<ownershipDocument>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0001</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship>{role_xml}</reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>{plan}</aff10b5One>
    <nonDerivativeTable>{''.join(rows)}</nonDerivativeTable>
    <footnotes><footnote id="F1">{footnote}</footnote></footnotes>
</ownershipDocument></XML></TEXT></SEC-DOCUMENT>"""


CEO = "<isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle>"
DIR = "<isDirector>1</isDirector>"


class FakeSec:
    def __init__(self, docs):
        self.docs = docs  # accession -> sgml
        self.calls = []

    def filings(self, cik, asof, since=None, forms=None):
        self.calls.append((cik, asof, since, tuple(forms or [])))
        return [{"form": "4", "filingDate": f"2026-0{i + 1}-15", "accessionNumber": acc}
                for i, acc in enumerate(self.docs)]

    def full_submission_text(self, cik, acc):
        if self.docs[acc] is None:
            raise RuntimeError("boom")
        return self.docs[acc]


def test_parse_form4_has_no_price_field():
    rows = I.parse_form4(I._DOC.search(_doc("Smith Jane", CEO, [("2026-03-02", "S", "1000", "D", "9000", "187.55")])).group(0))
    assert rows[0]["code"] == "S" and rows[0]["shares"] == 1000 and rows[0]["after"] == 9000
    assert all("price" not in k for r in rows for k in r)


def test_collect_and_section():
    docs = {
        "a1": _doc("Smith Jane", CEO, [("2026-01-10", "S", "1000", "D", "9000", "187.55")], plan="1"),
        "a2": _doc("Smith Jane", CEO, [("2026-02-10", "S", "500", "D", "8500", "190.10"),
                                      ("2026-02-11", "F", "200", "D", "8300", "190.10")]),
        "a3": _doc("Doe John", DIR, [("2026-03-05", "P", "2,000", "A", "12000", "150.00")],
                   footnote="Purchased in the open market."),
        "a4": None,   # 抓取失敗只計數
    }
    sec = FakeSec(docs)
    res = I.build_insider_section(sec, "0000000001", "2026-10-16")
    assert sec.calls[0][2] == "2024-10-16" and set(sec.calls[0][3]) == {"4", "4/A"}
    st, sm = res["stats"], res["summary"]
    assert (st["filings"], st["parsed"], st["failed"]) == (4, 3, 1)
    assert sm["open_market_buyers"] == 1 and sm["shares_bought"] == 2000
    assert sm["shares_sold_plan"] == 1000 and sm["shares_sold_other"] == 500
    md = res["markdown"]
    assert "| Smith Jane | Chief Executive Officer |" in md and "8,300" in md   # 最後直接持股取最後一筆
    assert "扣股繳稅 1" in md
    assert "15%" in md   # 1500 ÷ (8300 + 1500)
    for bad in ("187.55", "190.10", "150.00"):
        assert bad not in md
    assert not any(p.search(line) for line in md.splitlines() for p in PRICE_TEXT_PATTERNS)


def test_no_filings():
    res = I.build_insider_section(FakeSec({}), "1", "2026-10-16")
    assert "沒有 Form 4" in res["markdown"]


def test_transactions_on_or_after_asof_ignored():
    sec = FakeSec({"a1": _doc("X", DIR, [("2026-10-16", "P", "100", "A", "100", "1")])})
    people, _ = I.collect(sec, "1", "2026-10-16")
    assert "X" not in people
