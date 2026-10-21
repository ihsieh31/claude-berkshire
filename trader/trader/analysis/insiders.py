"""內部人交易（資料包補充）：決策日前 24 個月的 Form 4，彙整成每人股數表。

規則來源：G5 管理層（誘因警訊、內部人持股與買賣，NEVER-04／MKT-060～06B 的佐證）。
做法（不做判斷，只準備資料）：
- 來源＝發行人 submissions 中的 Form 4 / 4/A（申報日 < asof 且晚於回看起點），讀整份申報 SGML 內的 ownershipDocument XML。
- **回看期以交易日判斷**：申報日只決定讀哪些申報；入表的交易列須交易日 ≥ 回看起點（asof − 730 天）且 < asof。
  原申報在回看期外的 4/A 會重報舊交易（例：2026 年申報的 4/A 帶入 2024 年初的交易），這些列照樣參與下方 4/A 配對，
  但交易日早於回看起點就不計；4/A 把交易日更正進／出回看期時，以更正後的日期為準。缺交易日的列照計。
- **價格隔離**：只輸出股數、交易代碼、日期與身分；不輸出成交價、金額或任何以價格換算的數字（FULL_A 不得看到價格）。
- 只計非衍生性表（普通股）；衍生性表只計筆數。公開市場買（P）與賣（S）分開，賣出再分是否依 10b5-1 計畫。
- 只收發行人＝目標公司（或其前身）的申報；本公司以申報人身分對別家發行人申報的 Form 4 略過（stats['other_issuer']）。
- **處理順序**：依申報日升冪；同一天申報的原始 4 先於 4/A（sec.filings 同日新的在前，4/A 若先處理會被當成新列，
  原 4 後到又另算一次）。
- **4/A 逐列取代**先前申報的同一交易列，只計一次（持股數、直接／間接、日期等以 4/A 為準），依序三步：
  1. 嚴格比對（申報人、交易日、代碼、股數、直接／間接、是否衍生性、同份申報內第 n 次出現）；
  2. 比對不到的列做兩輪退一步配對，候選只限 4/A 所註原申報日（dateOfOriginalSubmission）當天的原始 Form 4、
     且這份 4/A 尚未占用（嚴格比對已占用的列不再配）的列，依出現順序配對：
     第一輪比 (申報人、交易日、代碼、是否衍生性)（更正股數、直接／間接）；
     第二輪不看交易日，比 (申報人、代碼、是否衍生性)（更正交易日）；
  3. 仍配不到才新增。沒有 dateOfOriginalSubmission 的 4/A 只做嚴格比對。被取代的列沿用原申報日供之後的 4/A 比對。
  4/A 常只列更正的那幾列（或 0 列），所以不整份取代原申報。已知限制：退一步配對只看原申報日、申報人與代碼，
  4/A 若只補報一筆原申報沒有的交易，可能誤取代原申報中同人同代碼的另一列（第二輪不限交易日）。
- 「賣出占期初持股」只用直接持股口徑；間接持股（信託、家族）賣出另列一欄。
- **拆股基準**（M4-D4-02）：呼叫端給拆股字串（'YYYY-MM-DD:新股數/舊股數;…'，ex_date ≤ asof；與 prices.csv 的 splits
  同格式）時，每列股數與交易後持股乘上「交易日之後、決策日以前」的拆股比例，換到決策日基準（因子用 skill screen.split_factor，
  與初篩股數同一元件）；表頭寫明。未給（None）→ 不換算，表頭註明「未換算拆股」。
- **證券類別**：非衍生性表有兩種以上 securityTitle（去掉逗號、$、par 之後的面額等說明）時，每人依類別分列並在申報人欄標示；
  只有一種時不分。已知限制：同一類股寫法不同（Common Stock／Common Shares）也會被分列。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from trader.data.http import SecBlocked

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
    buy_direct: float = 0.0   # 其中直接持股的 P 股數
    sell_direct: float = 0.0  # 其中直接持股的 S 股數（比例只用直接口徑）
    sell_indirect: float = 0.0  # 其中間接持股（信託、家族）的 S 股數
    other: dict[str, int] = field(default_factory=dict)   # 其他代碼筆數
    title: str = ""                                       # 證券類別（只在多類別時填；securityTitle 正規化）
    held_after: float | None = None                       # 最後一筆非衍生交易後的直接持股
    held_date: str = ""
    last_date: str = ""
    last_trade_date: str = ""                             # 末筆公開市場（P/S）交易日


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
    """一份 ownershipDocument → 交易列（不含價格）。每列：owner、role、title（證券類別）、date、code、shares、ad（A/D）、after、plan、derivative、issuer_cik、
    orig_date（4/A 的 dateOfOriginalSubmission；原始 4 為空）。"""
    root = ET.fromstring(xml_text)
    issuer_cik = _txt(root, "issuer/issuerCik")
    orig_date = _txt(root, "dateOfOriginalSubmission")
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
                "title": _title(_txt(tx, "securityTitle")),
                "date": _txt(tx, "transactionDate"),
                "code": _txt(tx, "transactionCoding/transactionCode"),
                "shares": _num(_txt(amt, "transactionShares")) or 0.0,
                "ad": _txt(amt, "transactionAcquiredDisposedCode"),
                "after": _num(_txt(post, "sharesOwnedFollowingTransaction")),
                "direct": _txt(tx, "ownershipNature/directOrIndirectOwnership") != "I",
                "plan": plan_flag or plan_note,
                "derivative": deriv,
                "issuer_cik": issuer_cik,
                "orig_date": orig_date,
            })
    return rows


def _title(s: str) -> str:
    """securityTitle 正規化：去掉逗號、分號、$ 或 par 之後的說明（面額等；不讓金額進資料包），合併空白、首字大寫。"""
    t = re.split(r"[,;$]|\bpar\b", s or "", maxsplit=1, flags=re.I)[0]
    return re.sub(r"\s+", " ", t).strip().title() or "缺"


def _split_tools():
    from trader.data.prices_adapter import _skill   # skill 的 screen.split_factor／_events（拆股因子唯一一份）
    return _skill("screen")


def _accession_parts(f: dict, cik: str) -> tuple[str, str] | None:
    acc = str(f.get("accessionNumber") or "")
    return (str(f.get("cik") or cik), acc) if acc else None


def collect(sec, cik: str, asof: str, lookback_days: int = LOOKBACK_DAYS,
            max_filings: int = MAX_FILINGS, splits: str | None = None) -> tuple[dict[str, Person], dict[str, Any]]:
    """抓取並彙整。回傳 ({申報人: 彙總}, 統計)；非衍生性表有兩種以上證券類別時鍵為「申報人（類別）」、各類別分列
    （衍生性列歸「申報人（衍生性證券）」）。單份解析失敗只略過並計數。
    splits 給定時股數換到決策日拆股基準（見模組說明）；stats['splits_applied'] 為回看期內套用的拆股事件。"""
    since = (date.fromisoformat(asof) - timedelta(days=lookback_days)).isoformat()
    fl = sec.filings(cik, asof, since=since, forms=["4", "4/A"])
    n_all = len(fl)   # A3-M4C-04：截斷以切片前的份數判斷（恰好 max_filings 份不算截斷）
    fl = sorted(fl, key=lambda r: (r["filingDate"], r.get("form") == "4/A"))[-max_filings:]   # 同日原 4 先於 4/A
    people: dict[str, Person] = {}
    stats = {"since": since, "filings": len(fl), "parsed": 0, "failed": 0, "other_issuer": 0,
             "truncated": n_all > max_filings, "first_filed": fl[0]["filingDate"] if fl else None}
    txs: dict[tuple, dict] = {}   # 4/A 取代先前申報的同一交易列（4/A 常只更正持股數，鍵不含 after）
    origin: dict[tuple, str] = {}  # 每列源自哪天申報的原始 4（被 4/A 取代後仍沿用），供 4/A 退一步比對
    for f in fl:
        parts = _accession_parts(f, cik)
        if not parts:
            stats["failed"] += 1
            continue
        try:
            m = _DOC.search(sec.full_submission_text(*parts))
            rows = parse_form4(m.group(0)) if m else None
        except SecBlocked:   # A3-M4E-01：SEC 封鎖不吞成單份失敗（呼叫端寫進資料狀態「缺：內部人交易彙整失敗（SecBlocked）」）
            raise
        except Exception:  # noqa: BLE001  單份失敗不擋
            rows = None
        if rows is None:
            stats["failed"] += 1
            continue
        stats["parsed"] += 1
        issuer = rows[0]["issuer_cik"] if rows else ""
        if issuer.isdigit() and int(issuer) not in {int(parts[0]), int(cik)}:   # 別家發行人（本公司為申報人）
            stats["other_issuer"] += 1
            continue
        amend = f.get("form") == "4/A"
        seen: dict[tuple, int] = {}
        keys = []
        for r in rows:
            k = (r["owner"], r["date"], r["code"], r["shares"], r["direct"], r["derivative"])
            seen[k] = seen.get(k, 0) + 1   # 同一份申報內的相同列各自保留
            keys.append((*k, seen[k]))
        orig = rows[0]["orig_date"] if amend and rows else ""
        if orig:   # 4/A：嚴格比對到的先占用，其餘依出現順序配對原申報日當天原始 4 尚未占用的同類列
            used = {k for k in keys if k in txs}
            for loose in (lambda x: (x["owner"], x["date"], x["code"], x["derivative"]),
                          lambda x: (x["owner"], x["code"], x["derivative"])):   # 先同交易日，再不看日期（4/A 更正交易日）
                for i, r in enumerate(rows):
                    if keys[i] in txs:   # 已嚴格比對或上一輪已配對
                        continue
                    k2 = next((x for x in txs if x not in used and origin[x] == orig
                               and loose(txs[x]) == loose(r)), None)
                    if k2 is not None:
                        keys[i] = k2
                        used.add(k2)
        for r, k in zip(rows, keys):
            if not amend and k in txs:
                k = (*k, parts[1])   # 兩份原始 4 的相同列都算
            if k not in origin:
                origin[k] = "" if amend else f["filingDate"]
            txs[k] = r
    S = _split_tools() if splits else None
    stats["splits_given"] = splits is not None
    stats["splits_applied"] = [f"{d} {r}:1" for d, r in S._events(splits)
                               if r and S.is_split_ratio(r) and since < d <= asof[:10]] if S else []

    def in_window(r):   # 回看期以交易日判斷（4/A 可能重報回看期外的舊交易）
        return not r["date"] or since <= r["date"] < asof
    titles = {r.get("title") for r in txs.values() if in_window(r) and not r["derivative"] and r.get("title") != "缺"}
    multi = len(titles) > 1
    stats["security_titles"] = sorted(t for t in titles if t)
    for r in txs.values():
        if not in_window(r):
            continue
        if S is not None and r["date"]:   # 換到決策日拆股基準（交易日當天＝已除權，不乘）
            f = S.split_factor(splits, r["date"], asof)
            if f != 1.0:
                r = {**r, "shares": r["shares"] * f, "after": None if r["after"] is None else r["after"] * f}
        title = ("衍生性證券" if r["derivative"] else r.get("title") or "缺") if multi else ""
        p = people.setdefault(f"{r['owner']}（{title}）" if title else r["owner"], Person(r["owner"], r["role"], title=title))
        p.last_date = max(p.last_date, r["date"])
        if r["derivative"]:
            p.other["衍生性"] = p.other.get("衍生性", 0) + 1
            continue
        c = r["code"]
        if c in ("P", "S"):
            p.last_trade_date = max(p.last_trade_date, r["date"])
        if c == "P":
            p.buy += r["shares"]
            p.buy_direct += r["shares"] if r["direct"] else 0.0
        elif c == "S":
            if r["direct"]:
                p.sell_direct += r["shares"]
            else:
                p.sell_indirect += r["shares"]
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
    L = ["", f"## 內部人交易（框架彙整；Form 4，交易日 {st['since']} 起、決策日之前；只列股數，不含價格與金額）", ""]
    if not st["filings"]:
        L.append("期間內沒有 Form 4 申報。")
        return {"markdown": "\n".join(L) + "\n", "stats": st, "summary": {}}
    L.append(f"申報 {st['filings']} 份，解析 {st['parsed']} 份，失敗 {st['failed']} 份"
             + (f"（已達上限 {st['filings']} 份，實際涵蓋 {st['first_filed']} 之後）" if st["truncated"] else "") + "。")
    if not st.get("splits_given"):
        L.append("股數未換算拆股（框架未取得拆股資料）：回看期內若有拆股，拆股前後股數口徑不同，合計與比例可能失真。")
    elif st.get("splits_applied"):
        L.append("股數已換算為決策日拆股基準（拆股：" + "、".join(st["splits_applied"]) + "）。")
    buyers = [p for p in people.values() if p.buy > 0]
    n_people = len({p.name for p in people.values()})
    sellers = [p for p in people.values() if p.sell_plan + p.sell_other > 0]
    summary = {"insiders": n_people, "open_market_buyers": len(buyers), "open_market_sellers": len(sellers),
               "shares_bought": sum(p.buy for p in buyers),
               "shares_sold_plan": sum(p.sell_plan for p in sellers),
               "shares_sold_other": sum(p.sell_other for p in sellers),
               "shares_sold_indirect": sum(p.sell_indirect for p in sellers)}
    L.append(f"公開市場買進 {len(buyers)} 人、合計 {_sh(summary['shares_bought'])} 股；"
             f"公開市場賣出 {len(sellers)} 人、合計 {_sh(summary['shares_sold_plan'] + summary['shares_sold_other'])} 股"
             f"（其中依 10b5-1 計畫 {_sh(summary['shares_sold_plan'])} 股）。")
    L += ["", "| 申報人 | 身分 | 公開市場買進股數 | 賣出股數（10b5-1） | 賣出股數（其他） | 賣出股數（間接持股） | "
          "賣出占期初持股（直接） | 末筆公開市場交易日 | 最後直接持股 | 持股日期 | 其他交易筆數 |",
          "|:--|:--|--:|--:|--:|--:|--:|:--|--:|:--|:--|"]
    order = sorted(people.values(), key=lambda p: -(p.buy + p.sell_plan + p.sell_other))
    for p in order:
        indirect = p.sell_indirect
        base = (p.held_after or 0) + p.sell_direct - p.buy_direct
        ratio = f"{p.sell_direct / base:.0%}" if p.sell_direct and base > 0 and p.held_after is not None else ""
        other = "、".join(f"{k} {v}" for k, v in sorted(p.other.items())) or ""
        L.append(f"| {p.name}{'（' + p.title + '）' if p.title else ''} | {p.role} | {_sh(p.buy) if p.buy else ''} | {_sh(p.sell_plan) if p.sell_plan else ''} | "
                 f"{_sh(p.sell_other) if p.sell_other else ''} | {_sh(indirect) if indirect else ''} | {ratio} | "
                 f"{p.last_trade_date} | {_sh(p.held_after)} | {p.held_date if p.held_after is not None else ''} | {other} |")
    L += ["", "註：「賣出股數（10b5-1）」「賣出股數（其他）」含直接與間接持股；「賣出股數（間接持股）」為其中來自信託、家族等間接持股的部分。"
          "「賣出占期初持股（直接）」只用直接持股口徑＝直接賣出 ÷（最後直接持股＋直接賣出−直接買進），只是粗估；"
          "授予、行使、扣股繳稅不是公開市場交易。間接持股不計入最後持股。"
          + (f"另有 {st['other_issuer']} 份為本公司以申報人身分對其他發行人的申報，已排除。" if st.get("other_issuer") else "")]
    return {"markdown": "\n".join(L) + "\n", "stats": st, "summary": summary}
