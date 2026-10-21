"""SEC EDGAR 資料來源：代號對照、companyfacts／submissions（含快取）、申報清單、申報全文與初篩特徵。

快取檔名與 skill 的 fetch_edgar 相容（company_tickers.json、CIK{cik10}.json、sub_CIK{cik10}.json）。
skill 的 cached() 永不更新，因此過期重抓由本類負責。新鮮度（D36）：每份 JSON 快取旁寫 {檔名}.meta 記
fetched_ny_date（抓取當天的紐約日期）；有決策日 asof 時，fetched_ny_date ≥ min(asof, 紐約今天) 的快取對該決策日
完整、可用（asof 晚於紐約今天時，紐約今天抓的已是當下最完整），否則重抓（重抓失敗才退回舊快取並記 stale）。
沒有 asof、沒有 meta（舊版快取）或呼叫端指定 max_age_hours 時，沿用 mtime 與 max_age_hours 判斷。
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

from trader.data.http import DataError, HttpClient, SecBlocked, atomic_write_text, ny_today
from trader.paths import get_paths

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DOC_PREFIX = "https://www.sec.gov/"
SUCCESSION_FORMS = ["8-K12B", "8-K12B/A", "8-K12G3", "8-K12G3/A"]  # 申報全文只允許這個前綴

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
_CELL_TAGS = {"td", "th"}            # 儲存格結束補空白，相鄰數字格不黏成一串（29,31552,554）
_SKIP_TAGS = {"script", "style", "ix:header"}  # ix:header＝inline XBRL 隱藏的 dei／參照區，不是正文
# html_to_text 的輸出口徑版本。filing_text 快取存的是轉換後文字，改轉換器（含上面三個集合）必須遞增，
# 舊版快取檔名不同而自然失效（D35）。v1＝無版本號舊檔（儲存格黏字、含 ix:header）；v2＝2026-10-18。
CONVERTER_VERSION = 2


def _ticker_keys(ticker: str) -> set[str]:
    """代號的兩種寫法：標準形（點號 BRK.B）與 SEC 檔案用的連字號形（BRK-B）。"""
    u = ticker.strip().upper()
    return {u.replace("-", "."), u.replace(".", "-")}


class _TextExtractor(HTMLParser):
    """HTML 轉純文字：刪除 script／style／ix:header 內容；區塊標籤換行；td／th 結束補空白；其他標籤只刪標籤、保留文字。

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
        elif tag in _CELL_TAGS:
            self.parts.append(" ")

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


SIC_PAGE = 100       # SEC 公司搜尋每頁上限
SIC_MAX_PAGES = 5    # sic_companies 最多翻幾頁（M4-D9-04）
MAX_403_STREAK = 5  # features 連續幾檔 HTTP 403 就中止整批（M1-D8-03）
OLD_PAGE_MAX_AGE_HOURS = 24 * 30   # submissions 舊分頁（已封存的歷史申報）快取新鮮期（A3-M4C-02）
BLOCK_COOLDOWN_S = 600  # SEC 封鎖後幾秒自動解除、再試一次（SEC 限速封鎖約 10 分鐘；M4-R8-03）

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


def read_fetched_ny_date(path: Path) -> str | None:
    """快取檔 path 的抓取日（{檔名}.meta 的 fetched_ny_date）；沒有或壞掉 → None（D36）。"""
    try:
        v = json.loads(Path(f"{path}.meta").read_text(encoding="utf-8")).get("fetched_ny_date")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and v else None


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
        # 呼叫端未逐次傳 asof 時用的決策日（D36；DOSSIER 前置期間由 jobs/deep 設定，結束還原）
        self.asof = None
        self._clock = time.monotonic  # 封鎖冷卻計時（測試可替換）
        self._blocked_at: float | None = None

    @property
    def asof(self) -> str | None:
        return self._asof

    @asof.setter
    def asof(self, value: str | None) -> None:
        """設定（或還原）決策日時重設 subject_cik：DOSSIER 前置設 asof 後第一個查 companyfacts 的 CIK 就是本工作的
        公司（build_dossier_base 最先查它），資料狀態只逐一列它與前身的舊快取（M4-R8-02）。"""
        self._asof = value
        self.subject_cik: str | None = None

    def _request(self, fn, url: str):
        """經 HttpClient 送出請求。SEC 封鎖（SecBlocked）滿 BLOCK_COOLDOWN_S 秒後解除並重試一次；
        仍被封鎖則重新計時，期間照舊直接拋 SecBlocked、不送請求（M4-R8-03）。"""
        h = self.http
        was = getattr(h, "blocked", None)
        if was and self._blocked_at is not None and self._clock() - self._blocked_at >= BLOCK_COOLDOWN_S:
            h.blocked = was = None
        try:
            return fn(url)
        except SecBlocked:
            if not was or self._blocked_at is None:  # 這次真的送出並被封鎖（或首次得知）才開始計時
                self._blocked_at = self._clock()
            raise

    def predecessors(self, cik: str) -> list[str]:
        """現行 CIK 的前身 CIK（10 碼）清單；無前身回傳空清單。"""
        return list(self._predecessors.get(_norm_cik(cik), []))

    # ---- 快取與抓取 ----
    def _fresh(self, path: Path, max_age_s: float | None = None) -> bool:
        age = self.max_age_s if max_age_s is None else max_age_s
        return path.exists() and (time.time() - path.stat().st_mtime) < age

    @staticmethod
    def _read_json(path: Path) -> Any:
        """讀快取 JSON；檔案不存在或半截（解析失敗）回傳 None，視同沒有快取。"""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _usable(self, path: Path, max_age_hours: float | None, asof: str | None) -> bool:
        """D36：max_age_hours 指定 → 依 mtime；有 asof 且有 meta → fetched_ny_date ≥ min(asof, 紐約今天)；
        其他 → 預設 mtime 期限。取 min：asof 晚於紐約今天（台北清晨帶台北日期）時，紐約今天抓的已是當下最完整，
        不必每次重抓；紐約日期追上 asof 後需求變回 asof，會再重抓一次（P41R）。"""
        if max_age_hours is not None:
            return self._fresh(path, max_age_hours * 3600)
        asof = asof if asof is not None else self.asof
        fetched = read_fetched_ny_date(path) if asof else None
        if fetched is not None:
            return path.exists() and fetched >= min(str(asof)[:10], ny_today())
        return self._fresh(path)

    def _cached_json(self, name: str, url: str, max_age_hours: float | None = None, asof: str | None = None) -> Any:
        """快取對 asof 完整（見 _usable）則讀快取，否則重抓並寫回快取與 meta（原子寫入）；半截快取視同沒有快取。
        max_age_hours 覆寫本次的新鮮期（0＝強制重抓）；asof＝決策日（None 時用 self.asof）。
        重抓失敗仍走舊快取備援並記 stale（舊快取可能早於決策日，呼叫端寫進資料包 stale_fallback）。"""
        path = self.cache_dir / name
        if self._usable(path, max_age_hours, asof):
            data = self._read_json(path)
            if data is not None:
                return data
        try:
            data = self._request(self.http.get_json, url)
        except DataError:
            # SEC 暫時失敗：有舊快取就用（時點過濾仍由 asof 保證；只是可能漏掉最新申報），並記警告
            old = self._read_json(path)
            if old is not None:
                self.stale.append(name)
                return old
            raise
        atomic_write_text(path, json.dumps(data))
        atomic_write_text(Path(f"{path}.meta"), json.dumps({"fetched_ny_date": ny_today()}))  # 先寫資料再寫 meta
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
        text = self._request(self.http.get, url).text
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
        return text

    def exhibit_text(self, cik: str, accession: str, ex_type: str = "EX-99") -> str | None:
        """申報中附件的純文字；ex_type＝"EX-99" 時接受 EX-99、EX-99.1、EX-99.01…，取編號最小的一份；沒有則 None。"""
        found = self.exhibit_texts(cik, accession, ex_type)
        return found[0][1] if found else None

    def exhibit_index(self, cik: str, accession: str) -> list[tuple[str, str]] | None:
        """申報索引頁（{accession}-index.htm，數 KB）列出的 [(文件類型, 文件網址)]；快取不過期（申報內容不變）。
        索引頁取不到（非封鎖的 DataError）或解析不到表格 → None（呼叫端退回整份 SGML）。
        A3-M4C-01：多數 10-K 沒有 EX-13，先看索引，免得為了找附件下載並永久快取整份 SGML（含圖檔、XBRL，常數十 MB）。"""
        if getattr(self, "http", None) is None:
            return None
        acc = str(accession)
        url = f"{DOC_PREFIX}Archives/edgar/data/{int(str(cik))}/{acc.replace('-', '')}/{acc}-index.htm"
        path = self.cache_dir / "docs" / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}_index.html"
        if path.exists():
            html = path.read_text(encoding="utf-8")
        else:
            try:
                html = self._request(self.http.get, url).text
            except SecBlocked:
                raise
            except DataError:
                return None
        rows: list[tuple[str, str]] = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            cells = [re.sub(r"<[^>]+>|&nbsp;", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
            href = re.search(r'href="(?:/ix\?doc=)?(/Archives/edgar/data/[^"#?]+)"', tr, re.I)
            if len(cells) >= 4 and href:   # 欄位：Seq、Description、Document、Type、Size
                rows.append((cells[3].upper(), DOC_PREFIX.rstrip("/") + href.group(1)))
        if not rows:
            return None
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, html)
        return rows

    def exhibit_texts(self, cik: str, accession: str, ex_type: str = "EX-99") -> list[tuple[str, str]]:
        """申報中同類附件全部（[(附件類型, 純文字)]，依編號升冪）；PDF／圖檔等非文字附件略過。
        先讀申報索引（exhibit_index）：沒有該類附件 → 空清單、不下載 SGML；有 → 只抓那幾份附件（filing_text，轉換後文字快取）。
        索引取不到才退回整份 SGML（A3-M4C-01）。"""
        want = ex_type.upper()

        def order(typ: str) -> list[float]:
            return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", typ[len(want):])] or [0.0]

        idx = self.exhibit_index(cik, accession)
        if idx is not None:
            docs = sorted({(t, u) for t, u in idx if (t == want or t.startswith(want + "."))
                           and re.search(r"\.(htm|html|txt|xml)$", u, re.I)}, key=lambda x: (order(x[0]), x[1]))
            return [(t, self.filing_text(u)) for t, u in docs]
        sgml = self.full_submission_text(cik, accession)
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
        found.sort(key=lambda x: order(x[0]))
        return [(typ, html_to_text(body)) for typ, body in found]

    def sic_companies(self, sic: str) -> list[str]:
        """SEC 公司搜尋：該 SIC 四碼下曾申報 10-K 的公司 CIK（10 碼，SEC 依名稱排序）；含已下市者，呼叫端以財報年度過濾。
        一頁取滿 SIC_PAGE 筆就以 &start= 翻頁，至不足一頁為止，最多 SIC_MAX_PAGES 頁；仍取滿則記 warnings（名單截斷）。
        截斷警告每次查詢都記（不以整個 run 共用的 warnings 去重）：呼叫端以本工作的切片取用並去重（M4-R8-01）。
        快取 30 天；檔名 sic_{SIC}_v2.json（舊版 sic_{SIC}.json 只有第一頁，不再讀；M4-D9-04）。"""
        sic = str(sic).strip()
        if not re.fullmatch(r"\d{4}", sic):
            return []
        path = self.cache_dir / f"sic_{sic}_v2.json"
        truncated = (f"SEC 公司搜尋 SIC {sic} 超過 {SIC_MAX_PAGES * SIC_PAGE} 家，名單截斷（只取前 "
                     f"{SIC_MAX_PAGES} 頁）")
        if path.exists() and (time.time() - path.stat().st_mtime) < 30 * 86400:
            cached = self._read_json(path)
            if cached is not None:
                # 快取的截斷名單（5 頁全滿）每次讀都要再警告，否則 30 天內資料包看不到截斷（P41R）
                if len(cached) >= SIC_MAX_PAGES * SIC_PAGE:
                    self.warnings.append(truncated)
                return cached
        ciks: list[str] = []
        for page in range(SIC_MAX_PAGES):
            url = (f"{DOC_PREFIX}cgi-bin/browse-edgar?action=getcompany&SIC={sic}&type=10-K&dateb=&owner=include"
                   f"&start={page * SIC_PAGE}&count={SIC_PAGE}&output=atom")
            got = [c.zfill(10) for c in re.findall(r"<cik>(\d+)</cik>", self._request(self.http.get, url).text)]
            ciks += got
            if len(got) < SIC_PAGE:
                break
        else:
            self.warnings.append(truncated)
        ciks = list(dict.fromkeys(ciks))
        atomic_write_text(path, json.dumps(ciks))
        return ciks

    def cik_for(self, ticker: str) -> str | None:
        return self.ticker_map().get(ticker.strip().upper().replace("-", "."))

    # ---- 原始 JSON ----
    def companyfacts(self, cik: str, asof: str | None = None) -> dict:
        cik10 = str(cik).zfill(10)
        if self._asof is not None and self.subject_cik is None:
            self.subject_cik = cik10  # 見 asof setter（M4-R8-02）
        return self._cached_json(f"CIK{cik10}.json", FACTS_URL.format(cik=cik10), asof=asof)

    def merged_companyfacts(self, cik: str, asof: str | None = None) -> dict:
        """併入前身 CIK 的 companyfacts（無前身＝原樣；前身失敗只記 warnings）。所有框架表共用的單一入口（M4-D2-12）。"""
        cik10 = str(cik).zfill(10)
        return self._with_predecessor_facts(cik10, self.companyfacts(cik10, asof), asof)[0]

    def submissions(self, cik: str, max_age_hours: float | None = None, asof: str | None = None) -> dict:
        cik10 = str(cik).zfill(10)
        return self._cached_json(f"sub_CIK{cik10}.json", SUBMISSIONS_URL.format(cik=cik10), max_age_hours, asof)

    # ---- 申報清單與全文 ----
    def filings(self, cik: str, asof: str, since: str | None = None,
                forms: list[str] | None = None, max_age_hours: float | None = None) -> list[dict]:
        """申報日嚴格早於 asof（等於 asof 排除）；since 給定時申報日嚴格晚於 since；依申報日升冪。
        有前身 CIK 時併入前身的申報（同樣過濾），每筆加 cik（10 碼）標示來源；無前身時結果與原行為相同（無 cik 欄）。
        前身抓取失敗只記 warnings，不中斷。max_age_hours 覆寫 submissions 快取新鮮期（0＝當次重抓；分頁不受影響）。"""
        cik10 = str(cik).zfill(10)
        pre = self.predecessors(cik10)
        if not pre:
            return self._filings_one(cik10, asof, since, forms, max_age_hours)
        out: list[dict] = []
        for c in [cik10, *pre]:
            try:
                rows = self._filings_one(c, asof, since, forms, max_age_hours)
            except Exception as e:
                if c == cik10:  # 現行公司本身失敗照常拋出
                    raise
                self.warnings.append(f"{cik10}：前身 CIK {c} 申報清單抓取失敗（{type(e).__name__}），未併入")
                continue
            out.extend({**r, "cik": c} for r in rows)
        out.sort(key=lambda r: r["filingDate"])
        return out

    def _filings_one(self, cik10: str, asof: str, since: str | None,
                     forms: list[str] | None, max_age_hours: float | None = None) -> list[dict]:
        subm = self.submissions(cik10, max_age_hours, asof)
        recent = self._merge_pages(subm, since, asof)
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

    def _merge_pages(self, subm: dict, since: str | None, asof: str | None = None) -> dict:
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
            # 舊分頁是已封存的歷史申報（內容不再變），用長新鮮期，不隨每個新決策日重抓（A3-M4C-02）
            page = self._cached_json(f["name"], f"https://data.sec.gov/submissions/{f['name']}",
                                     max_age_hours=OLD_PAGE_MAX_AGE_HOURS)
            n = len(page.get("filingDate") or [])
            for c in cols:
                merged[c] += list(page.get(c) or [""] * n)
        return merged

    def filing_text(self, url: str) -> str:
        """申報全文純文字；只允許 https://www.sec.gov/；申報內容不會變，快取不過期。
        快取的是轉換後文字，檔名帶 CONVERTER_VERSION：轉換器改版後舊檔不再被讀，重抓重轉。"""
        if not url.startswith(DOC_PREFIX):
            raise ValueError(f"只允許 SEC 申報網址（{DOC_PREFIX}）：{url}")
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        path = self.cache_dir / "docs" / f"{key}_v{CONVERTER_VERSION}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        text = html_to_text(self._request(self.http.get, url).text)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
        return text

    # ---- 初篩特徵（重用 skill 的 fetch_edgar.features，不複製計算邏輯） ----
    def features(self, tickers: list[str], asof: str, latest: bool = False) -> pd.DataFrame:
        """逐檔算初篩特徵；單檔失敗只標該檔。SEC 封鎖（SecBlocked）、前視自檢失敗（fetch_edgar.LookaheadError）
        或連續 MAX_403_STREAK 檔 HTTP 403 → 中止整批並拋出（不再逐檔續打 SEC；錯誤訊息說明檢查 UA、稍後重跑）。
        注意：有舊快取時 SecBlocked 被 _cached_json 退回舊快取並記 self.stale，不在此拋出；比例門檻由
        inputs.fetch_inputs／screen.run_screen 判斷（A3-M1E-01）。"""
        fe = _load_fetch_edgar()
        lookahead = getattr(fe, "LookaheadError", ())   # 前視自檢失敗：整批中止往上拋（A3-M1C-02）
        rows: list[dict] = []
        streak_403 = 0
        for t in tickers:
            tk = t.strip().upper().replace("-", ".")  # 代號標準形
            cik = self.cik_for(tk)
            if not cik:
                rows.append({"ticker": tk, "data_notes": "SEC 對照表找不到代號"})
                continue
            try:
                facts = self.companyfacts(cik, asof)
                sub = self.submissions(cik, asof=asof)  # SIC 用現行公司的
                facts, used, failed = self._with_predecessor_facts(cik, facts, asof)
                row = fe.features(tk, cik, facts, sub.get("sic", ""), asof, latest)
                if used:
                    row["data_notes"] = _append_note(row.get("data_notes"), "含前身 CIK " + "、".join(used))
                if failed:  # 前身缺漏會讓年度序列變短，必須在 data_notes 留痕
                    row["data_notes"] = _append_note(row.get("data_notes"),
                                                     "前身 CIK " + "、".join(failed) + " 抓取失敗未併入")
                rows.append(row)
                streak_403 = 0
            except SecBlocked:
                raise
            except lookahead:
                raise
            except Exception as e:  # 單檔失敗不影響其他檔；該檔列未評（型別＋訊息前 120 字，A3-M1C-02）
                rows.append({"ticker": tk, "cik": cik, "data_notes": f"抓取失敗：{type(e).__name__}: {str(e)[:120]}"})
                streak_403 = streak_403 + 1 if "HTTP 403" in str(e) else 0
                if streak_403 >= MAX_403_STREAK:
                    raise DataError(f"SEC 拒絕存取（連續 {streak_403} 檔 HTTP 403）：檢查 SEC_USER_AGENT 或稍後重跑") from e
        return pd.DataFrame(rows)

    def _with_predecessor_facts(self, cik10: str, facts: dict,
                                asof: str | None = None) -> tuple[dict, list[str], list[str]]:
        """回傳 (併入後 facts, 成功併入的前身, 抓取失敗的前身)。無前身或全部失敗時 facts 原樣回傳。"""
        used: list[str] = []
        failed: list[str] = []
        pre_facts: list[dict] = []
        for pc in self.predecessors(cik10):
            try:
                pre_facts.append(self.companyfacts(pc, asof))
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

    def offline_filings(self, ciks: dict[str, str], asof: str,
                        max_age_hours: float | None = None) -> dict[str, list[dict]]:
        """給 review_due --offline 用：{代號: 申報清單}。max_age_hours 同 filings（觸發檢查傳 0 強制當次重抓）。"""
        return {tk: self.filings(cik, asof, max_age_hours=max_age_hours) for tk, cik in ciks.items()}
