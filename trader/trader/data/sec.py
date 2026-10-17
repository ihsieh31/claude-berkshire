"""SEC EDGAR 資料來源：代號對照、companyfacts／submissions（含快取）、申報清單、申報全文與初篩特徵。

快取檔名與 skill 的 fetch_edgar 相容（company_tickers.json、CIK{cik10}.json、sub_CIK{cik10}.json）。
skill 的 cached() 永不更新，因此過期重抓由本類負責（依 mtime 與 max_age_hours 判斷）。
所有網路請求都經 HttpClient。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pandas as pd

from trader.data.http import DataError, HttpClient
from trader.paths import get_paths

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DOC_PREFIX = "https://www.sec.gov/"
SUCCESSION_FORMS = ["8-K12B", "8-K12B/A", "8-K12G3", "8-K12G3/A"]  # 申報全文只允許這個前綴

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
_SKIP_TAGS = {"script", "style"}


def _ticker_keys(ticker: str) -> set[str]:
    """代號的兩種寫法：標準形（點號 BRK.B）與 SEC 檔案用的連字號形（BRK-B）。"""
    u = ticker.strip().upper()
    return {u.replace("-", "."), u.replace(".", "-")}


class _TextExtractor(HTMLParser):
    """HTML 轉純文字：刪除 script／style 內容；區塊標籤換行；其他標籤只刪標籤、保留文字。

    convert_charrefs=True 時 HTMLParser 會對文字做 html.unescape，等同於逐段反跳脫。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """HTML 轉純文字，合併多餘空白但保留換行（連續空行最多保留一行）。"""
    p = _TextExtractor()
    p.feed(raw)
    p.close()
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in "".join(p.parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _load_fetch_edgar():
    """匯入 skill 的 fetch_edgar（不修改它）；模組層級 UA 與本類無關，本類不呼叫其 get／cached。"""
    scripts = str(get_paths().skill_screen / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import fetch_edgar  # noqa: E402  由 sys.path 載入 skill 腳本

    return fetch_edgar


PREDECESSORS_FILE = Path(__file__).with_name("cik_predecessors.json")  # 套件內前身對照（資料檔）


def _norm_cik(cik: Any) -> str:
    """CIK 正規化為 10 碼；非數字或超過 10 位直接拒絕（對照檔錯誤要大聲失敗）。"""
    s = str(cik).strip()
    if not s.isdigit() or len(s) > 10:
        raise ValueError(f"CIK 格式錯誤：{cik!r}")
    return s.zfill(10)


def _read_predecessor_map(path: Path) -> dict[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    m = raw.get("map") if isinstance(raw, dict) else None
    if not isinstance(m, dict):
        raise ValueError(f"前身對照檔缺少 map 物件：{path}")
    out: dict[str, list[str]] = {}
    for new, olds in m.items():
        if not isinstance(olds, list):
            raise ValueError(f"前身對照值須為清單：{path} 鍵 {new}")
        out[_norm_cik(new)] = [_norm_cik(o) for o in olds]
    return out


def load_predecessors(user_path: Path | None = None) -> dict[str, list[str]]:
    """先讀套件內 cik_predecessors.json，再以使用者檔（若存在）的 map 覆蓋同鍵（整個清單取代，不合併）。"""
    out = _read_predecessor_map(PREDECESSORS_FILE)
    if user_path is not None and Path(user_path).exists():
        out.update(_read_predecessor_map(Path(user_path)))
    return out


def merge_companyfacts(current: dict, predecessors: list[dict]) -> dict:
    """併入前身 companyfacts：每個 taxonomy→concept→unit 的 list 串接（前身在前、現行在後），
    完全相同的 dict 去重；其他頂層欄位（cik、entityName 等）與 concept 的非 units 欄位用現行公司的。
    輸入不被修改。去重後 fetch_edgar 的「同財年取首次申報」規則不變（時點紀律保持）。"""
    out = {k: v for k, v in current.items() if k != "facts"}
    facts: dict[str, dict[str, dict]] = {}
    for src in [*predecessors, current]:  # 現行公司最後處理，非 units 欄位以現行為準
        for tax, concepts in (src.get("facts") or {}).items():
            tgt_tax = facts.setdefault(tax, {})
            for concept, body in concepts.items():
                tgt = tgt_tax.setdefault(concept, {"units": {}})
                for k, v in body.items():
                    if k != "units":
                        tgt[k] = v
                for unit, rows in (body.get("units") or {}).items():
                    tgt["units"].setdefault(unit, []).extend(rows)
    for tax in facts.values():
        for concept in tax.values():
            for unit, rows in concept["units"].items():
                seen: set[str] = set()
                dedup: list = []
                for r in rows:
                    key = json.dumps(r, sort_keys=True, ensure_ascii=False)
                    if key not in seen:
                        seen.add(key)
                        dedup.append(r)
                concept["units"][unit] = dedup
    out["facts"] = facts
    return out


def _append_note(existing: Any, note: str) -> str:
    """data_notes 以「；」接在原字串後。"""
    if isinstance(existing, str) and existing:
        return f"{existing}；{note}"
    return note


class SecClient:
    def __init__(self, user_agent: str, cache_dir: Path, http: HttpClient | None = None,
                 max_age_hours: float = 20, predecessors_path: Path | None = None):
        # SEC 要求 User-Agent 帶聯絡 email；沒有 @ 直接拒絕
        if "@" not in user_agent:
            raise ValueError("SEC User-Agent 需含聯絡 email（格式：名稱 email）")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_age_s = max_age_hours * 3600
        self.http = http or HttpClient(
            min_interval=0.11,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        )
        self._tickers: dict[str, str] | None = None
        self._predecessors = load_predecessors(predecessors_path)
        self.stale: list[str] = []  # 本次因抓取失敗而改用的舊快取檔名
        self.warnings: list[str] = []  # 非致命問題（例如前身 CIK 抓取失敗），供呼叫端記錄

    def predecessors(self, cik: str) -> list[str]:
        """現行 CIK 的前身 CIK（10 碼）清單；無前身回傳空清單。"""
        return list(self._predecessors.get(_norm_cik(cik), []))

    # ---- 快取與抓取 ----
    def _fresh(self, path: Path) -> bool:
        return path.exists() and (time.time() - path.stat().st_mtime) < self.max_age_s

    def _cached_json(self, name: str, url: str) -> Any:
        """快取未過期則讀快取，否則重抓並寫回快取。"""
        path = self.cache_dir / name
        if self._fresh(path):
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            data = self.http.get_json(url)
        except DataError:
            # SEC 暫時失敗：有舊快取就用（時點過濾仍由 asof 保證；只是可能漏掉最新申報），並記警告
            if path.exists():
                self.stale.append(name)
                return json.loads(path.read_text(encoding="utf-8"))
            raise
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    # ---- 代號與 CIK ----
    def ticker_map(self) -> dict[str, str]:
        """代號（標準形與連字號形都放）→ 10 位 CIK。"""
        if self._tickers is None:
            raw = self._cached_json("company_tickers.json", SEC_TICKERS_URL)
            m: dict[str, str] = {}
            for v in raw.values():
                cik = str(v["cik_str"]).zfill(10)
                for key in _ticker_keys(v["ticker"]):
                    m[key] = cik
            self._tickers = m
        return dict(self._tickers)

    def company_titles(self) -> dict[str, str]:
        """代號（標準形）→ SEC 登記公司名稱。"""
        raw = self._cached_json("company_tickers.json", SEC_TICKERS_URL)
        return {str(v["ticker"]).strip().upper().replace("-", "."): str(v.get("title") or "") for v in raw.values()}

    def full_submission_text(self, cik: str, accession: str) -> str:
        """整份申報的 SGML 原文（含全部附件）；只取 SEC 網址，快取不過期。"""
        acc = str(accession)
        url = f"{DOC_PREFIX}Archives/edgar/data/{int(str(cik))}/{acc.replace('-', '')}/{acc}.txt"
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        path = self.cache_dir / "docs" / f"{key}.sgml"
        if path.exists():
            return path.read_text(encoding="utf-8")
        text = self.http.get(url).text
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text

    def exhibit_text(self, cik: str, accession: str, ex_type: str = "EX-99") -> str | None:
        """申報中附件的純文字；ex_type＝"EX-99" 時接受 EX-99、EX-99.1、EX-99.01…，取編號最小的一份；沒有則 None。"""
        found = self.exhibit_texts(cik, accession, ex_type)
        return found[0][1] if found else None

    def exhibit_texts(self, cik: str, accession: str, ex_type: str = "EX-99") -> list[tuple[str, str]]:
        """申報中同類附件全部（[(附件類型, 純文字)]，依編號升冪）；PDF／圖檔等非文字附件略過。"""
        sgml = self.full_submission_text(cik, accession)
        want = ex_type.upper()
        found: list[tuple[str, str]] = []
        for doc in re.split(r"<DOCUMENT>", sgml)[1:]:
            m = re.search(r"<TYPE>([^\s<]+)", doc)
            typ = m.group(1).strip().upper() if m else ""
            if typ != want and not typ.startswith(want + "."):
                continue
            fn = re.search(r"<FILENAME>([^\s<]+)", doc)
            if fn and not re.search(r"\.(htm|html|txt|xml)$", fn.group(1), re.I):
                continue
            t = re.search(r"<TEXT>(.*?)(</TEXT>|$)", doc, re.S)
            if t:
                found.append((typ, t.group(1)))
        found.sort(key=lambda x: [float(n) for n in re.findall(r"\d+(?:\.\d+)?", x[0][len(want):])] or [0.0])
        return [(typ, html_to_text(body)) for typ, body in found]

    def sic_companies(self, sic: str, count: int = 100) -> list[str]:
        """SEC 公司搜尋：該 SIC 四碼下曾申報 10-K 的公司 CIK（10 碼）；含已下市者，呼叫端以財報年度過濾。"""
        sic = str(sic).strip()
        if not re.fullmatch(r"\d{4}", sic):
            return []
        path = self.cache_dir / f"sic_{sic}.json"
        if path.exists() and (time.time() - path.stat().st_mtime) < 30 * 86400:
            return json.loads(path.read_text(encoding="utf-8"))
        url = (f"{DOC_PREFIX}cgi-bin/browse-edgar?action=getcompany&SIC={sic}&type=10-K&dateb=&owner=include"
               f"&count={int(count)}&output=atom")
        text = self.http.get(url).text
        ciks = list(dict.fromkeys(c.zfill(10) for c in re.findall(r"<cik>(\d+)</cik>", text)))
        path.write_text(json.dumps(ciks), encoding="utf-8")
        return ciks

    def cik_for(self, ticker: str) -> str | None:
        return self.ticker_map().get(ticker.strip().upper().replace("-", "."))

    # ---- 原始 JSON ----
    def companyfacts(self, cik: str) -> dict:
        cik10 = str(cik).zfill(10)
        return self._cached_json(f"CIK{cik10}.json", FACTS_URL.format(cik=cik10))

    def submissions(self, cik: str) -> dict:
        cik10 = str(cik).zfill(10)
        return self._cached_json(f"sub_CIK{cik10}.json", SUBMISSIONS_URL.format(cik=cik10))

    # ---- 申報清單與全文 ----
    def filings(self, cik: str, asof: str, since: str | None = None,
                forms: list[str] | None = None) -> list[dict]:
        """申報日嚴格早於 asof（等於 asof 排除）；since 給定時申報日嚴格晚於 since；依申報日升冪。
        有前身 CIK 時併入前身的申報（同樣過濾），每筆加 cik（10 碼）標示來源；無前身時結果與原行為相同（無 cik 欄）。
        前身抓取失敗只記 warnings，不中斷。"""
        cik10 = str(cik).zfill(10)
        pre = self.predecessors(cik10)
        if not pre:
            return self._filings_one(cik10, asof, since, forms)
        out: list[dict] = []
        for c in [cik10, *pre]:
            try:
                rows = self._filings_one(c, asof, since, forms)
            except Exception as e:
                if c == cik10:  # 現行公司本身失敗照常拋出
                    raise
                self.warnings.append(f"{cik10}：前身 CIK {c} 申報清單抓取失敗（{type(e).__name__}），未併入")
                continue
            out.extend({**r, "cik": c} for r in rows)
        out.sort(key=lambda r: r["filingDate"])
        return out

    def _filings_one(self, cik10: str, asof: str, since: str | None,
                     forms: list[str] | None) -> list[dict]:
        subm = self.submissions(cik10)
        recent = self._merge_pages(subm, since)
        form_col = recent.get("form", [])
        n = len(form_col)

        def col(name: str) -> list:
            return recent.get(name) or [""] * n

        dates, accs, docs = col("filingDate"), col("accessionNumber"), col("primaryDocument")
        reports, items = col("reportDate"), col("items")
        form_set = set(forms) if forms is not None else None
        out: list[dict] = []
        for i in range(n):
            fd = dates[i]
            if fd >= asof:
                continue
            if since is not None and fd <= since:
                continue
            if form_set is not None and form_col[i] not in form_set:
                continue
            acc, doc = accs[i], docs[i]
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc.replace('-', '')}/{doc}"
                   if acc and doc else "")
            out.append({"form": form_col[i], "filingDate": fd, "reportDate": reports[i],
                        "items": items[i], "accessionNumber": acc, "primaryDocument": doc, "url": url})
        out.sort(key=lambda r: r["filingDate"])
        return out

    def _merge_pages(self, subm: dict, since: str | None) -> dict:
        """recent 只含最近約 1000 筆；since 早於 recent 最早申報日時，補讀 filings.files 中涵蓋 since 之後的舊分頁。"""
        recent = subm["filings"]["recent"]
        dates = recent.get("filingDate") or []
        if since is None or not dates or min(dates) <= since:
            return recent
        cols = ("form", "filingDate", "reportDate", "items", "accessionNumber", "primaryDocument")
        merged = {c: list(recent.get(c) or [""] * len(dates)) for c in cols}
        for f in subm["filings"].get("files") or []:
            if (f.get("filingTo") or "") <= since:
                continue
            page = self._cached_json(f["name"], f"https://data.sec.gov/submissions/{f['name']}")
            n = len(page.get("filingDate") or [])
            for c in cols:
                merged[c] += list(page.get(c) or [""] * n)
        return merged

    def filing_text(self, url: str) -> str:
        """申報全文純文字；只允許 https://www.sec.gov/；申報內容不會變，快取不過期。"""
        if not url.startswith(DOC_PREFIX):
            raise ValueError(f"只允許 SEC 申報網址（{DOC_PREFIX}）：{url}")
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        path = self.cache_dir / "docs" / f"{key}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        text = html_to_text(self.http.get(url).text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text

    # ---- 初篩特徵（重用 skill 的 fetch_edgar.features，不複製計算邏輯） ----
    def features(self, tickers: list[str], asof: str, latest: bool = False) -> pd.DataFrame:
        fe = _load_fetch_edgar()
        rows: list[dict] = []
        for t in tickers:
            tk = t.strip().upper().replace("-", ".")  # 代號標準形
            cik = self.cik_for(tk)
            if not cik:
                rows.append({"ticker": tk, "data_notes": "SEC 對照表找不到代號"})
                continue
            try:
                facts = self.companyfacts(cik)
                sub = self.submissions(cik)  # SIC 用現行公司的
                facts, used, failed = self._with_predecessor_facts(cik, facts)
                row = fe.features(tk, cik, facts, sub.get("sic", ""), asof, latest)
                if used:
                    row["data_notes"] = _append_note(row.get("data_notes"), "含前身 CIK " + "、".join(used))
                if failed:  # 前身缺漏會讓年度序列變短，必須在 data_notes 留痕
                    row["data_notes"] = _append_note(row.get("data_notes"),
                                                     "前身 CIK " + "、".join(failed) + " 抓取失敗未併入")
                rows.append(row)
            except Exception as e:  # 單檔失敗不影響其他檔；該檔列未評
                rows.append({"ticker": tk, "cik": cik, "data_notes": f"抓取失敗：{type(e).__name__}"})
        return pd.DataFrame(rows)

    def _with_predecessor_facts(self, cik10: str, facts: dict) -> tuple[dict, list[str], list[str]]:
        """回傳 (併入後 facts, 成功併入的前身, 抓取失敗的前身)。無前身或全部失敗時 facts 原樣回傳。"""
        used: list[str] = []
        failed: list[str] = []
        pre_facts: list[dict] = []
        for pc in self.predecessors(cik10):
            try:
                pre_facts.append(self.companyfacts(pc))
                used.append(pc)
            except Exception as e:  # 前身失敗：用現行 facts 照常算，不中斷
                failed.append(pc)
                self.warnings.append(f"{cik10}：前身 CIK {pc} companyfacts 抓取失敗（{type(e).__name__}），未併入")
        if not pre_facts:
            return facts, used, failed
        return merge_companyfacts(facts, pre_facts), used, failed

    def succession_filings(self, cik: str, asof: str) -> list[dict]:
        """承繼註冊申報（8-K12B／8-K12G3 及其修正）：公司改組成新控股公司時，新 CIK 會申報此類表格。"""
        return self.filings(cik, asof, forms=SUCCESSION_FORMS)

    def offline_filings(self, ciks: dict[str, str], asof: str) -> dict[str, list[dict]]:
        """給 review_due --offline 用：{代號: 申報清單}。"""
        return {tk: self.filings(cik, asof) for tk, cik in ciks.items()}
