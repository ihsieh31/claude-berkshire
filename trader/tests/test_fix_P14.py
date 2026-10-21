"""P14（M4-E 內部人交易表口徑）：他家發行人 Form 4、4/A 重複、直接／間接口徑、日期欄。全部離線。"""
from __future__ import annotations

from trader.analysis import insiders as I
from trader.analysis.dossier_prep import PRICE_TEXT_PATTERNS

CEO = "<isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle>"
TEN = "<isTenPercentOwner>1</isTenPercentOwner>"


def _doc(owner, role_xml, txs, issuer_cik="0000000001", plan="0", orig=None, deriv=()):
    """txs：(date, code, shares, ad, after, direct 'D'/'I')；deriv：衍生性表 (date, code, shares)；orig＝4/A 的原申報日。"""
    rows = "".join(f"""
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>{d}</value></transactionDate>
            <transactionCoding><transactionCode>{c}</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{s}</value></transactionShares>
                <transactionPricePerShare><value>123.45</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{a}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>{di}</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>""" for d, c, s, ad, a, di in txs)
    drows = "".join(f"""
        <derivativeTransaction>
            <transactionDate><value>{d}</value></transactionDate>
            <transactionCoding><transactionCode>{c}</transactionCode></transactionCoding>
            <transactionAmounts><transactionShares><value>{s}</value></transactionShares>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </derivativeTransaction>""" for d, c, s in deriv)
    issuer = f"<issuer><issuerCik>{issuer_cik}</issuerCik></issuer>" if issuer_cik is not None else ""
    od = f"<dateOfOriginalSubmission>{orig}</dateOfOriginalSubmission>" if orig else ""
    return f"""<SEC-DOCUMENT><ownershipDocument>
    {od}
    {issuer}
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0002</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship>{role_xml}</reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>{plan}</aff10b5One>
    <nonDerivativeTable>{rows}</nonDerivativeTable>
    <derivativeTable>{drows}</derivativeTable>
</ownershipDocument></SEC-DOCUMENT>"""


class FakeSec:
    def __init__(self, filings):
        self.fl = filings   # [(form, filingDate, acc, sgml, cik or None)]

    def filings(self, cik, asof, since=None, forms=None):
        out = []
        for form, fd, acc, _, c in self.fl:
            r = {"form": form, "filingDate": fd, "accessionNumber": acc}
            if c:
                r["cik"] = c
            out.append(r)
        return out

    def full_submission_text(self, cik, acc):
        return next(s for _, _, a, s, _ in self.fl if a == acc)


def test_other_issuer_form4_excluded():
    """M4-D1-11：本公司以 10% 股東身分申報別家發行人的 Form 4 不入表。"""
    sec = FakeSec([
        ("4", "2026-06-02", "own", _doc("Smith Jane", CEO, [("2026-06-01", "S", "100", "D", "900", "D")]), None),
        ("4", "2026-06-03", "oth", _doc("FEDEX CORP", TEN, [("2026-06-01", "S", "119753703", "D", "29751545", "D")],
                                        issuer_cik="0002082247"), None),
    ])
    people, st = I.collect(sec, "0000000001", "2026-10-16")
    assert "FEDEX CORP" not in people and "Smith Jane" in people
    assert st["other_issuer"] == 1
    md = I.build_insider_section(sec, "1", "2026-10-16")["markdown"]
    assert "FEDEX CORP" not in md and "29,751,545" not in md


def test_predecessor_issuer_kept_and_missing_issuer_kept():
    """前身 CIK 的申報（filings 列帶 cik）照收；缺 issuerCik 的文件不排除。"""
    sec = FakeSec([
        ("4", "2026-01-02", "pre", _doc("Old Guy", CEO, [("2026-01-01", "P", "10", "A", "10", "D")],
                                        issuer_cik="0000000777"), "0000000777"),
        ("4", "2026-02-02", "noi", _doc("No Issuer", CEO, [("2026-02-01", "P", "5", "A", "5", "D")],
                                        issuer_cik=None), None),
    ])
    people, st = I.collect(sec, "0000000001", "2026-10-16")
    assert set(people) == {"Old Guy", "No Issuer"} and st["other_issuer"] == 0


def test_amendment_not_double_counted():
    """M4-D1-12：4/A 重報同一交易只計一次（以較晚申報為準）。"""
    # 實例（GL Darden）：4/A 重報同一筆賣出，只更正了交易後持股
    sec = FakeSec([
        ("4", "2026-03-05", "orig", _doc("Darden James", CEO, [("2026-03-04", "S", "18318", "D", "51093", "D")]), None),
        ("4/A", "2026-04-28", "amend", _doc("Darden James", CEO, [("2026-03-04", "S", "18318", "D", "58451", "D")],
                                            plan="1"), None),
    ])
    people, _ = I.collect(sec, "0000000001", "2026-10-16")
    p = people["Darden James"]
    assert p.sell_plan + p.sell_other == 18318
    assert p.sell_plan == 18318 and p.held_after == 58451   # 以較晚的 4/A 為準


def test_two_original_form4_same_row_both_counted():
    """兩份原始 Form 4（非 4/A）恰好同列，不視為重複。"""
    tx = [("2026-03-04", "S", "100", "D", "1000", "D")]
    sec = FakeSec([("4", "2026-03-05", "a", _doc("Twice", CEO, tx), None),
                   ("4", "2026-03-06", "b", _doc("Twice", CEO, tx), None)])
    people, _ = I.collect(sec, "0000000001", "2026-10-16")
    assert people["Twice"].sell_other == 200


def test_identical_rows_within_one_filing_both_counted():
    """同一份申報內兩列完全相同（例如兩個信託）仍各計一次；去重只跨申報。"""
    tx = [("2026-03-04", "S", "100", "D", "1000", "I"), ("2026-03-04", "S", "100", "D", "1000", "I")]
    sec = FakeSec([("4", "2026-03-05", "a", _doc("Trusty", CEO, tx), None)])
    people, _ = I.collect(sec, "0000000001", "2026-10-16")
    assert people["Trusty"].sell_other == 200


def test_indirect_sales_not_in_ratio():
    """M4-D2-04：間接持股（信託）賣出不進比例分子；間接賣出另列一欄。"""
    sec = FakeSec([
        ("4", "2026-03-05", "a", _doc("Wilson Thomas", CEO, [
            ("2026-03-04", "S", "201684", "D", "800000", "D"),
            ("2026-03-04", "S", "395191", "D", "0", "I"),
        ]), None),
    ])
    res = I.build_insider_section(sec, "0000000001", "2026-10-16")
    md = res["markdown"]
    row = next(line for line in md.splitlines() if line.startswith("| Wilson Thomas"))
    # 直接口徑：201,684 ÷ (800,000 + 201,684) = 20%；舊口徑 596,875 ÷ 996,875 = 60%
    assert "| 20% |" in row and "60%" not in row
    assert "395,191" in row
    assert "賣出股數（間接持股）" in md
    assert res["summary"]["shares_sold_plan"] + res["summary"]["shares_sold_other"] == 596875
    assert res["summary"]["shares_sold_indirect"] == 395191


def test_only_indirect_sales_no_ratio():
    sec = FakeSec([("4", "2026-03-05", "a", _doc("Hoker", CEO, [
        ("2026-03-04", "S", "15303", "D", "0", "I"),
        ("2026-01-04", "A", "1000", "A", "12500", "D")]), None)])
    md = I.build_insider_section(sec, "0000000001", "2026-10-16")["markdown"]
    row = next(line for line in md.splitlines() if line.startswith("| Hoker"))
    assert "%" not in row and "15,303" in row


def test_table_has_dates():
    """M4-D5-06：表格含末筆公開市場交易日與持股日期。"""
    sec = FakeSec([
        ("4", "2025-02-28", "a", _doc("Smith Jane", CEO, [("2025-02-27", "S", "1000", "D", "9000", "D")]), None),
        ("4", "2025-06-02", "b", _doc("Smith Jane", CEO, [("2025-06-01", "A", "300", "A", "9300", "D")]), None),
    ])
    md = I.build_insider_section(sec, "0000000001", "2026-10-16")["markdown"]
    assert "末筆公開市場交易日" in md and "持股日期" in md
    row = next(line for line in md.splitlines() if line.startswith("| Smith Jane"))
    assert "2025-02-27" in row and "2025-06-01" in row and "9,300" in row
    assert "123.45" not in md
    assert not any(p.search(line) for line in md.splitlines() for p in PRICE_TEXT_PATTERNS)


def test_amendment_changes_direct_to_indirect_counted_once():
    """M4-D1-12（GL BLINN 實例）：原 4 賣 3000 股標直接，4/A 同日同代碼改成間接 → 只計一次、算間接。"""
    sec = FakeSec([
        ("4", "2025-01-07", "orig", _doc("Blinn Mark", CEO, [("2025-01-06", "S", "3000", "D", "6495", "D")]), None),
        ("4/A", "2026-02-10", "amend", _doc("Blinn Mark", CEO, [("2025-01-06", "S", "3000", "D", "3000", "I")],
                                            orig="2025-01-07"), None),
    ])
    people, _ = I.collect(sec, "0000000001", "2026-10-16")
    p = people["Blinn Mark"]
    assert p.sell_plan + p.sell_other == 3000
    assert p.sell_direct == 0 and p.sell_indirect == 3000
    assert p.held_after is None   # 原 4 的直接列已被 4/A 取代


def test_amendment_correcting_shares_and_derivative_replaced():
    """FDX 董事衍生性授予（4/A 改股數）、BBY Watson 授予（4/A 改股數）：退一步比對後只計一次。"""
    sec = FakeSec([
        ("4", "2026-06-26", "f", _doc("Adams Gina", CEO, [], deriv=[("2026-06-25", "A", "3567")]), None),
        ("4/A", "2026-07-01", "fa", _doc("Adams Gina", CEO, [], deriv=[("2026-06-25", "A", "3478")],
                                         orig="2026-06-26"), None),
        ("4", "2025-03-21", "b", _doc("Watson Mathew", CEO, [("2025-03-20", "A", "5104", "A", "20000", "D")]), None),
        ("4/A", "2025-04-01", "ba", _doc("Watson Mathew", CEO, [("2025-03-20", "A", "9187", "A", "24083", "D")],
                                         orig="2025-03-21"), None),
    ])
    people, _ = I.collect(sec, "0000000001", "2026-10-16")
    assert people["Adams Gina"].other == {"衍生性": 1}
    assert people["Watson Mathew"].other == {"授予": 1} and people["Watson Mathew"].held_after == 24083


def test_amendment_loose_match_limited_to_original_submission_date():
    """退一步比對只找 4/A 所註原申報日當天的原始 4；其他日申報的同類列不被取代。"""
    sec = FakeSec([
        ("4", "2025-03-21", "x", _doc("Lee", CEO, [("2025-03-20", "S", "100", "D", "1100", "D")]), None),
        ("4", "2025-03-25", "y", _doc("Lee", CEO, [("2025-03-20", "S", "200", "D", "900", "D")]), None),
        ("4/A", "2025-04-01", "ya", _doc("Lee", CEO, [("2025-03-20", "S", "250", "D", "850", "D")],
                                         orig="2025-03-25"), None),
    ])
    people, _ = I.collect(sec, "0000000001", "2026-10-16")
    assert people["Lee"].sell_other == 350   # 取代 y（200→250）；誤取代 x 會是 450，不取代會是 550


def test_partial_or_empty_amendment_does_not_replace_whole_filing():
    """4/A 只列更正的一列、或 0 列：原申報其他列照算（不整份取代）。"""
    sec = FakeSec([
        ("4", "2026-03-05", "o", _doc("Partial", CEO, [("2026-03-04", "S", "100", "D", "900", "D"),
                                                        ("2026-03-04", "S", "50", "D", "850", "D")]), None),
        ("4/A", "2026-03-20", "a1", _doc("Partial", CEO, [("2026-03-04", "S", "60", "D", "840", "D")],
                                         orig="2026-03-05"), None),
        ("4/A", "2026-03-21", "a0", _doc("Partial", CEO, [], orig="2026-03-05"), None),
    ])
    people, _ = I.collect(sec, "0000000001", "2026-10-16")
    assert people["Partial"].sell_other == 110   # 依出現順序：第一列 100 被更正為 60，50 不動；0 列的 4/A 不動任何列


def test_indirect_sales_accumulated_directly_no_negative_zero():
    """間接賣出直接累加：小數股數 10b5-1／其他交錯時不再印出 -0。"""
    sec = FakeSec([
        ("4", "2026-03-05", "a", _doc("Frac", CEO, [("2026-03-04", "S", "0.1", "D", "10", "D")]), None),
        ("4", "2026-03-06", "b", _doc("Frac", CEO, [("2026-03-05", "S", "0.2", "D", "9.8", "D")], plan="1"), None),
        ("4", "2026-03-07", "c", _doc("Frac", CEO, [("2026-03-06", "S", "0.35", "D", "9.45", "D")]), None),
    ])
    res = I.build_insider_section(sec, "0000000001", "2026-10-16")
    assert res["summary"]["shares_sold_indirect"] == 0
    row = next(line for line in res["markdown"].splitlines() if line.startswith("| Frac"))
    assert row.split("|")[6].strip() == ""   # 間接欄留空（舊寫法 0.2＋(0.1＋0.35)−(0.1＋0.2＋0.35) 有浮點殘差，印成 -0）


def test_same_day_amendment_listed_before_original_counted_once():
    """M4-D1-12（GL Haworth 實例）：原 4 與 4/A 同日申報、sec.filings 把 4/A 排在前 → 仍只計一次（原 4 先處理）。"""
    tx = [("2025-01-06", "M", "9000", "A", "30794", "D"),
          ("2025-01-06", "S", "4591", "D", "26203", "D"), ("2025-01-06", "S", "4409", "D", "21794", "D")]
    sec = FakeSec([
        ("4/A", "2025-01-07", "amend", _doc("Haworth Jennifer", CEO, tx, orig="2025-01-07",
                                            deriv=[("2025-01-06", "M", "9000")]), None),
        ("4", "2025-01-07", "orig", _doc("Haworth Jennifer", CEO, tx, deriv=[("2025-01-06", "M", "9000")]), None),
        ("4", "2026-02-28", "later", _doc("Haworth Jennifer", CEO, [("2026-02-27", "S", "20500", "D", "1294", "D")]), None),
    ])
    p = I.collect(sec, "0000000001", "2026-10-16")[0]["Haworth Jennifer"]
    assert p.sell_other == 29500 and p.sell_direct == 29500   # 舊版 38,500（2025-01-06 兩筆賣出算兩次）
    assert p.other == {"行使衍生權利": 1, "衍生性": 1}


def test_amendment_correcting_transaction_date_counted_once():
    """M4-D1-12（GL MITCHELL 實例）：原 4 把交易日誤填 2023-02-27，4/A 改成 2024-11-07 → 只計一次、日期以 4/A 為準。"""
    bad = [("2023-02-27", "M", "8000", "A", "15980", "D"),
           ("2023-02-27", "S", "1054", "D", "14926", "D"), ("2023-02-27", "S", "6946", "D", "7980", "D")]
    good = [("2024-11-07",) + t[1:] for t in bad]
    sec = FakeSec([
        ("4", "2024-11-12", "orig", _doc("Mitchell Robert", CEO, bad), None),
        ("4/A", "2024-12-05", "amend", _doc("Mitchell Robert", CEO, good, orig="2024-11-12"), None),
        ("4", "2026-08-10", "later", _doc("Mitchell Robert", CEO, [("2026-08-07", "S", "144600", "D", "7980", "D")]), None),
    ])
    people, _ = I.collect(sec, "0000000001", "2026-10-16")
    p = people["Mitchell Robert"]
    assert p.sell_other == 152600   # 舊版 160,600（兩組日期都算）
    assert p.other == {"行使衍生權利": 1}


def test_lookback_by_transaction_date_amendment_of_old_filing_excluded():
    """X11（GL BLINN 實例）：2026-02-10 申報的 4/A，原申報在回看期外，帶入 2024-04-25 的間接買進 → 交易日早於回看起點不計。"""
    old = [("2024-04-25", "P", "2000", "A", "2000", "I")]
    sec = FakeSec([
        ("4", "2025-01-07", "orig", _doc("Blinn Mark", CEO, [("2025-01-06", "S", "3000", "D", "6495", "D")]), None),
        ("4/A", "2026-02-10", "amend", _doc("Blinn Mark", CEO, old + [("2025-01-06", "S", "3000", "D", "3000", "I")],
                                            orig="2025-01-07"), None),
    ])
    people, st = I.collect(sec, "0000000001", "2026-10-15")
    assert st["since"] == "2024-10-15"
    p = people["Blinn Mark"]
    assert p.buy == 0 and p.buy_direct == 0          # 修前 2,000（回看期外的舊交易被算進）
    assert p.sell_indirect == 3000 and p.sell_direct == 0   # 4/A 取代原列的配對照舊
    assert p.last_date == "2025-01-06"


def test_lookback_by_transaction_date_amendment_moves_row_out_of_window():
    """4/A 把交易日更正到回看起點之前：以更正後日期為準，原列被取代且不計；回看起點當天的交易照計。"""
    sec = FakeSec([
        ("4", "2024-11-12", "o", _doc("Edge Ann", CEO, [("2024-11-07", "S", "500", "D", "1500", "D"),
                                                        ("2024-10-15", "P", "70", "A", "2000", "D")]), None),
        ("4/A", "2024-12-05", "a", _doc("Edge Ann", CEO, [("2024-10-07", "S", "500", "D", "1500", "D")],
                                        orig="2024-11-12"), None),
    ])
    p = I.collect(sec, "0000000001", "2026-10-15")[0]["Edge Ann"]
    assert p.sell_other == 0          # 只取代、不計；不會留下原 2024-11-07 那列
    assert p.buy == 70                # 交易日＝回看起點當天仍計
    md = I.build_insider_section(sec, "0000000001", "2026-10-15")["markdown"]
    assert "交易日 2024-10-15 起" in md
