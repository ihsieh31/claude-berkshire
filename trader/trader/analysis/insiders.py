"""內部人交易（資料包補充）：決策日前 24 個月的 Form 4，彙整成每人股數表。

規則來源：G5 管理層（誘因警訊、內部人持股與買賣，NEVER-04／MKT-060～06B 的佐證）。
做法（不做判斷，只準備資料）：
- 來源＝發行人 submissions 中的 Form 4 / 4/A（申報日 < asof 且在回看期間內），讀整份申報 SGML 內的 ownershipDocument XML。
- **價格隔離**：只輸出股數、交易代碼、日期與身分；不輸出成交價、金額或任何以價格換算的數字（FULL_A 不得看到價格）。
- 只計非衍生性表（普通股）；衍生性表只計筆數。公開市場買（P）與賣（S）分開，賣出再分是否依 10b5-1 計畫。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

LOOKBACK_DAYS = 730
MAX_FILINGS = 400   # 董事遞延股數多的公司（WTW）24 個月可超過 200 份；首次抓取約 0.25 秒／份，之後讀快取
_DOC = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)
_CODE_NAMES = {"P": "公開市場買進", "S": "公開市場賣出", "A": "授予", "M": "行使衍生權利", "F": "扣股繳稅",
               "G": "贈與", "D": "處分給發行人", "C": "轉換", "J": "其他"}


@dataclass
class Person:
    name: str
    role: str
    buy: float = 0.0          # P 股數
    sell_plan: float = 0.0    # S 股數（10b5-1）
    sell_other: float = 0.0   # S 股數（非計畫或未標示）
    other: dict[str, int] = field(default_factory=dict)   # 其他代碼筆數
    held_after: float | None = None                       # 最後一筆非衍生交易後的直接持股
    held_date: str = ""
    last_date: str = ""


def _txt(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    x = el.find(path)
    if x is None:
        return ""
    v = x.find("value")
    return ((v.text if v is not None else x.text) or "").strip()


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _role(owner: ET.Element) -> str:
    rel = owner.find("reportingOwnerRelationship")
    if rel is None:
        return "缺"
    out = []
    if _txt(rel, "isOfficer") in ("1", "true"):
        out.append(_txt(rel, "officerTitle") or "高管")
    if _txt(rel, "isDirector") in ("1", "true"):
        out.append("董事")
    if _txt(rel, "isTenPercentOwner") in ("1", "true"):
        out.append("持股 10% 以上")
    if _txt(rel, "isOther") in ("1", "true"):
        out.append(_txt(rel, "otherText") or "其他")
    return "、".join(out) or "缺"


def parse_form4(xml_text: str) -> list[dict[str, Any]]:
    """一份 ownershipDocument → 交易列（不含價格）。每列：owner、role、date、code、shares、ad（A/D）、after、plan、derivative。"""
    root = ET.fromstring(xml_text)
    owners = root.findall("reportingOwner")
    name = " / ".join(_txt(o, "reportingOwnerId/rptOwnerName") for o in owners) or "缺"
    role = _role(owners[0]) if owners else "缺"
    plan_flag = _txt(root, "aff10b5One") in ("1", "true")
    notes = " ".join((f.text or "") for f in root.iter("footnote"))
    plan_note = bool(re.search(r"10b5-1", notes, re.I))
    rows = []
    for tag, deriv in (("nonDerivativeTable/nonDerivativeTransaction", False),
                       ("derivativeTable/derivativeTransaction", True)):
        for tx in root.findall(tag):
            amt = tx.find("transactionAmounts")
            post = tx.find("postTransactionAmounts")
            rows.append({
                "owner": name, "role": role,
                "date": _txt(tx, "transactionDate"),
                "code": _txt(tx, "transactionCoding/transactionCode"),
                "shares": _num(_txt(amt, "transactionShares")) or 0.0,
                "ad": _txt(amt, "transactionAcquiredDisposedCode"),
                "after": _num(_txt(post, "sharesOwnedFollowingTransaction")),
                "direct": _txt(tx, "ownershipNature/directOrIndirectOwnership") != "I",
                "plan": plan_flag or plan_note,
                "derivative": deriv,
            })
    return rows


def _accession_parts(f: dict, cik: str) -> tuple[str, str] | None:
    acc = str(f.get("accessionNumber") or "")
    return (str(f.get("cik") or cik), acc) if acc else None


def collect(sec, cik: str, asof: str, lookback_days: int = LOOKBACK_DAYS,
            max_filings: int = MAX_FILINGS) -> tuple[dict[str, Person], dict[str, Any]]:
    """抓取並彙整。回傳 (每人彙總, 統計)。單份解析失敗只略過並計數。"""
    since = (date.fromisoformat(asof) - timedelta(days=lookback_days)).isoformat()
    fl = sec.filings(cik, asof, since=since, forms=["4", "4/A"])
    fl = sorted(fl, key=lambda r: r["filingDate"])[-max_filings:]
    people: dict[str, Person] = {}
    stats = {"since": since, "filings": len(fl), "parsed": 0, "failed": 0, "truncated": len(fl) >= max_filings,
             "first_filed": fl[0]["filingDate"] if fl else None}
    for f in fl:
        parts = _accession_parts(f, cik)
        if not parts:
            stats["failed"] += 1
            continue
        try:
            m = _DOC.search(sec.full_submission_text(*parts))
            rows = parse_form4(m.group(0)) if m else None
        except Exception:  # noqa: BLE001  單份失敗不擋
            rows = None
        if rows is None:
            stats["failed"] += 1
            continue
        stats["parsed"] += 1
        for r in rows:
            if r["date"] and r["date"] >= asof:
                continue
            p = people.setdefault(r["owner"], Person(r["owner"], r["role"]))
            p.last_date = max(p.last_date, r["date"])
            if r["derivative"]:
                p.other["衍生性"] = p.other.get("衍生性", 0) + 1
                continue
            c = r["code"]
            if c == "P":
                p.buy += r["shares"]
            elif c == "S":
                if r["plan"]:
                    p.sell_plan += r["shares"]
                else:
                    p.sell_other += r["shares"]
            else:
                k = _CODE_NAMES.get(c, c or "缺")
                p.other[k] = p.other.get(k, 0) + 1
            if r["direct"] and r["after"] is not None and r["date"] >= p.held_date:
                p.held_after, p.held_date = r["after"], r["date"]
    return people, stats


def _sh(x: float | None) -> str:
    return "缺" if x is None else f"{x:,.0f}"


def build_insider_section(sec, cik: str, asof: str, **kw) -> dict[str, Any]:
    """產生內部人交易段落（markdown）。回傳 {"markdown", "stats", "summary"}。"""
    people, st = collect(sec, cik, asof, **kw)
    L = ["", f"## 內部人交易（框架彙整；Form 4，申報日 {st['since']} 之後、決策日之前；只列股數，不含價格與金額）", ""]
    if not st["filings"]:
        L.append("期間內沒有 Form 4 申報。")
        return {"markdown": "\n".join(L) + "\n", "stats": st, "summary": {}}
    L.append(f"申報 {st['filings']} 份，解析 {st['parsed']} 份，失敗 {st['failed']} 份"
             + (f"（已達上限 {st['filings']} 份，實際涵蓋 {st['first_filed']} 之後）" if st["truncated"] else "") + "。")
    buyers = [p for p in people.values() if p.buy > 0]
    sellers = [p for p in people.values() if p.sell_plan + p.sell_other > 0]
    summary = {"insiders": len(people), "open_market_buyers": len(buyers), "open_market_sellers": len(sellers),
               "shares_bought": sum(p.buy for p in buyers),
               "shares_sold_plan": sum(p.sell_plan for p in sellers),
               "shares_sold_other": sum(p.sell_other for p in sellers)}
    L.append(f"公開市場買進 {len(buyers)} 人、合計 {_sh(summary['shares_bought'])} 股；"
             f"公開市場賣出 {len(sellers)} 人、合計 {_sh(summary['shares_sold_plan'] + summary['shares_sold_other'])} 股"
             f"（其中依 10b5-1 計畫 {_sh(summary['shares_sold_plan'])} 股）。")
    L += ["", "| 申報人 | 身分 | 公開市場買進股數 | 賣出股數（10b5-1） | 賣出股數（其他） | 賣出占期初持股 | 最後直接持股 | 其他交易筆數 |",
          "|:--|:--|--:|--:|--:|--:|--:|:--|"]
    order = sorted(people.values(), key=lambda p: -(p.buy + p.sell_plan + p.sell_other))
    for p in order:
        sold = p.sell_plan + p.sell_other
        base = (p.held_after or 0) + sold - p.buy
        ratio = f"{sold / base:.0%}" if sold and base > 0 and p.held_after is not None else ""
        other = "、".join(f"{k} {v}" for k, v in sorted(p.other.items())) or ""
        L.append(f"| {p.name} | {p.role} | {_sh(p.buy) if p.buy else ''} | {_sh(p.sell_plan) if p.sell_plan else ''} | "
                 f"{_sh(p.sell_other) if p.sell_other else ''} | {ratio} | {_sh(p.held_after)} | {other} |")
    L += ["", "註：「賣出占期初持股」＝賣出股數 ÷（最後直接持股＋賣出−買進），只是粗估；授予、行使、扣股繳稅不是公開市場交易。"
          "間接持股（信託、家族）不計入最後持股。"]
    return {"markdown": "\n".join(L) + "\n", "stats": st, "summary": summary}
