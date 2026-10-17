# M1 資料層介面契約

先讀：`CONTRACTS.md` 的「通用」段、`trader/paths.py`、`trader/data/http.py`（已完成，不得修改；所有網路都經 `HttpClient`）。
測試一律用 `httpx.MockTransport` 注入 `HttpClient(transport=..., sleep=lambda s: None)`，不得連網。
快取根目錄：`paths.cache`（各模組子目錄如下）。日期一律 `YYYY-MM-DD` 字串。代號標準形：大寫、點號（`BRK.B`）。
**時點紀律**：所有查詢帶 `asof`，只回傳 asof 可得者（各函式註明 `<` 或 `<=`）。
**安全**：金鑰、User-Agent 只從參數傳入（呼叫端讀環境變數）；不得寫死任何 email；錯誤訊息遮罩金鑰（`HttpClient(secrets=[...])`）。

## trader/data/alpaca.py（代理 E）

```python
DATA_URL = "https://data.alpaca.markets"; TRADING_URL = "https://paper-api.alpaca.markets"
class AlpacaClient:
    def __init__(self, key: str, secret: str, feed: str = "iex", http: HttpClient | None = None)
        # 預設 HttpClient(min_interval=0.31, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, secrets=[key, secret])
    def bars(self, symbols: list[str], start: str, end: str, adjustment: str = "raw", timeframe: str = "1Day") -> pd.DataFrame
        # GET {DATA_URL}/v2/stocks/bars  params: symbols(逗號，每批 ≤100 檔), timeframe, start, end, adjustment(raw|split|dividend|all), feed, limit=10000, page_token
        # 回應 {"bars": {SYM: [{"t","o","h","l","c","v","vw"}...]}, "next_page_token"}；跟到 next_page_token 為 null
        # 回傳欄位 ticker,date,open,high,low,close,volume,vwap；date＝t 轉 America/New_York 的日期；依 ticker,date 排序；無資料回空 DataFrame（欄位齊全）
    def corporate_actions(self, symbols: list[str], start: str, end: str, types: list[str] | None = None) -> list[dict]
        # GET {DATA_URL}/v1/corporate-actions  params: symbols, types(逗號，可省), start, end, limit=1000, page_token
        # 回應 {"corporate_actions": {"forward_splits":[...], "reverse_splits":[...], "cash_dividends":[...], "stock_dividends":[...],
        #       "spin_offs":[...], "cash_mergers":[...], "stock_mergers":[...], "stock_and_cash_mergers":[...], "name_changes":[...], 其他鍵…}, "next_page_token"}
        # 正規化每筆為：{"ticker","type","ex_date","process_date","record_date","payable_date","ratio_new_per_old","cash_per_share","new_ticker","raw"}
        #   type＝鍵名去掉複數 s（forward_split、reverse_split、cash_dividend、stock_dividend、spin_off、cash_merger、stock_merger、stock_and_cash_merger、name_change；未知鍵照此規則）
        #   forward/reverse_split：ticker=symbol，ratio=new_rate/old_rate；cash_dividend：cash_per_share=rate；stock_dividend：ratio=1+rate
        #   spin_off：ticker=source_symbol，new_ticker=new_symbol；cash_merger：ticker=acquiree_symbol，cash_per_share=rate；
        #   stock_merger／stock_and_cash_merger：ticker=acquiree_symbol，new_ticker=acquirer_symbol，ratio=acquirer_rate/acquiree_rate（缺則 None），cash_per_share=cash_rate（有才填）；
        #   name_change：ticker=old_symbol，new_ticker=new_symbol；ex_date 缺 → 用 effective_date 或 process_date
        #   缺的欄位為 None；raw 保留原物件；ticker 經 normalize（大寫、"-"→"."）
    def news(self, symbols: list[str], start: str, end: str) -> list[dict]
        # GET {DATA_URL}/v1beta1/news  params: symbols, start, end, limit=50, sort=asc, page_token；回應 {"news":[...], "next_page_token"}
        # 每則新聞對每個「同時在 symbols 與該則 symbols 中」的代號各輸出一筆：{"id","ticker","date"(created_at 轉紐約日期),"created_at","headline","source","url","summary"}
    def calendar(self, start: str, end: str) -> list[dict]     # GET {TRADING_URL}/v2/calendar → [{"date","open","close"}]
    def clock(self) -> dict                                    # GET {TRADING_URL}/v2/clock
```

## trader/calendar.py（代理 E）

```python
def nyse_holidays(year: int) -> set[date]
    # 元旦（週六不補前一年 12/31；週日補週一）、MLK（1 月第 3 個週一）、總統日（2 月第 3 個週一）、耶穌受難日（復活節前週五，用 Anonymous Gregorian 演算法）、
    # 陣亡將士（5 月最後週一）、六月節（2022 年起 6/19）、獨立日（7/4）、勞動節（9 月第 1 個週一）、感恩節（11 月第 4 個週四）、聖誕（12/25）；
    # 除元旦外，週六 → 前週五、週日 → 後週一
class TradingCalendar:
    def __init__(self, days: list[str] | None = None, source: str = "rules")   # days 給定（Alpaca）→ 只認這些日；範圍外的日期退回規則
    @classmethod
    def load(cls, client: "AlpacaClient | None", cache_path: Path, start: str, end: str) -> "TradingCalendar"
        # client 有 → 抓 calendar 並存 cache_path（JSON）；DataError 或 client=None → 讀快取；再不行 → 純規則（source="rules"）
    def is_trading_day(self, d: str | date) -> bool
    def next_trading_day(self, d) -> str      # 嚴格大於 d
    def prev_trading_day(self, d) -> str      # 嚴格小於 d
    def trading_days(self, start, end) -> list[str]   # 含頭尾
```
測試：2026 年 NYSE 假日清單（1/1、1/19、2/16、4/3、5/25、6/19、7/3（7/4 週六）、9/7、11/26、12/25）；2027-12-31（2028 元旦週六不補）；next/prev 跨週末與假日。

## trader/data/sec.py（代理 F）

```python
class SecClient:
    def __init__(self, user_agent: str, cache_dir: Path, http: HttpClient | None = None, max_age_hours: float = 20)
        # user_agent 不含 "@" → ValueError；預設 HttpClient(min_interval=0.11, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        # cache_dir 例：paths.cache / "edgar"；檔名與 skill 的 fetch_edgar 相容：company_tickers.json、CIK{cik10}.json、sub_CIK{cik10}.json
        # 快取檔 mtime 超過 max_age_hours → 重抓（companyfacts 與 submissions 會更新；skill 腳本的 cached() 永不更新，所以由本類負責更新）
    def ticker_map(self) -> dict[str, str]           # 代號（標準形與 "-" 形都放）→ 10 位 CIK
    def cik_for(self, ticker: str) -> str | None
    def companyfacts(self, cik: str) -> dict         # https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
    def submissions(self, cik: str) -> dict          # https://data.sec.gov/submissions/CIK{cik}.json
    def filings(self, cik: str, asof: str, since: str | None = None, forms: list[str] | None = None) -> list[dict]
        # 由 submissions["filings"]["recent"] 組：{"form","filingDate","reportDate","items","accessionNumber","primaryDocument","url"}
        # url 規則同 review_due.sec_filings；只留 since < filingDate < asof（since=None 不限下界）；forms 給定則過濾；依 filingDate 升冪
    def filing_text(self, url: str) -> str
        # 只允許 https://www.sec.gov/ 開頭；快取 cache_dir/"docs"/{sha1(url)}.txt；HTML → 純文字：刪 script/style、
        # 區塊標籤（p,div,br,tr,li,h1-h6,table）換行、其餘標籤刪除、html.unescape、合併多餘空白（保留換行）；用標準庫 html.parser
    def features(self, tickers: list[str], asof: str, latest: bool = False) -> pd.DataFrame
        # 逐列重現 skill 的 fetch_edgar.main：import ../berkshire-screen-merged/scripts/fetch_edgar.py（sys.path 插入 paths.skill_screen/"scripts"），
        # 對每檔呼叫 fetch_edgar.features(tk, cik, facts, sub.get("sic",""), asof, latest)；找不到 CIK → {"ticker", "data_notes":"SEC 對照表找不到代號"}；
        # 例外 → {"ticker","cik","data_notes": f"抓取失敗：{type(e).__name__}"}。不得改寫或複製 features 的計算邏輯。
    def offline_filings(self, ciks: dict[str, str], asof: str) -> dict[str, list[dict]]   # {ticker: filings(cik, asof)}，給 review_due --offline
```
測試：用假的 company_tickers／submissions／companyfacts（小型 JSON）；快取命中不重抓、過期重抓；filings 時點過濾（等於 asof 的要排除）；filing_text HTML 轉換與非 sec.gov URL 被拒；UA 無 @ 被拒；features 對假 companyfacts 至少不崩潰並回傳 ticker 列（可用 monkeypatch 替換 fetch_edgar.features 驗證呼叫參數）。

## trader/data/fred.py（代理 F）

```python
class FredClient:
    def __init__(self, api_key: str | None, cache_dir: Path, http: HttpClient | None = None, max_age_hours: float = 20)
    def series(self, series_id: str = "DGS30") -> pd.Series
        # 有 key：GET https://api.stlouisfed.org/fred/series/observations params series_id, api_key, file_type=json, observation_start=1977-01-01
        #   → observations[{date, value}]，value "." 為缺值丟棄；
        # 無 key：GET https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}（公開 CSV，欄位 observation_date／DATE 與 series_id）
        # 回傳 float Series（index 為日期字串，升冪，百分比原值如 4.75）；快取 cache_dir/f"{series_id}.json"（過期重抓；抓失敗而有快取 → 用快取）
    def t30(self, asof: str) -> tuple[float, str]          # 最後一個 date < asof 的值，回 (小數 0.0475, 日期)；沒有 → DataError
    def t30_median_20y(self, asof: str) -> float           # [asof 前 20 年, asof) 區間日值中位數，小數
```
測試：JSON 與 CSV 兩種來源；"." 缺值；asof 當日的值不可用；20 年中位。

## trader/data/universe.py（代理 G）

```python
SP500_SOURCES = ["https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"]  # 欄位 Symbol 等
def normalize_ticker(s: str) -> str          # strip、大寫、"-"→"."；"/"→"."
def load_universe(paths: Paths, conn, http: HttpClient | None = None, refresh: bool = False, max_age_days: float = 7) -> tuple[list[str], str]
    # 回 (代號清單 已排序去重, 來源 "override"|"download"|"cache"|"cache_stale")
    # 1. paths.state/"universe_override.txt" 存在 → 每行一檔（# 註解、空行略過）→ 來源 override
    # 2. 快取 paths.cache/"universe"/"sp500.csv" 未過期且非 refresh → cache
    # 3. 下載（依序試 SP500_SOURCES）→ 存快取 → download；全失敗但有快取 → cache_stale；都沒有 → DataError
    # 名單（排序後 "\n".join）的 sha256 與 universe_versions 最後一列不同才 insert（asof＝今天 UTC 日期、source、tickers JSON）
    # 下載結果少於 450 檔 → 視為來源異常（DataError，改用快取）
```

## trader/data/pe_median.py（代理 G）

```python
def compute_pe_median(features: pd.DataFrame, prices: pd.DataFrame, asof: str) -> dict
    # 市值口徑與 skill 完全一致：import ../berkshire-screen-merged/scripts/screen.py 的 build(features, prices, None, asof) 取 market_cap
    # pe_norm = market_cap / ni5_mean（ni5_mean > 0）；pe_ttm = market_cap / net_income（> 0）；
    # 回 {"asof","n_norm","pe_norm_median","n_ttm","pe_ttm_median","n_fin","fin_pe_norm_median"(is_financial==1 者),"method":"說明字串"}；樣本 < 30 → 該中位為 None
def freeze_path(paths: Paths, month: str) -> Path      # paths.cache/"pe_median"/f"{month}.json"，month＝"YYYY-MM"
def freeze_monthly(paths: Paths, month: str, compute: Callable[[], dict]) -> dict   # 檔案存在 → 直接讀（凍結，不重算）；否則 compute() 寫檔後回傳
def get_frozen(paths: Paths, asof: str) -> dict | None  # asof 所在月份的凍結檔；沒有 → 上一個月；都沒有 → None（P4′ 不適用）
```

## trader/data/prices_adapter.py（代理 G）

```python
def splits_string(actions: list[dict], ticker: str, asof: str) -> str
    # 取該檔 type ∈ {forward_split, reverse_split} 且 ex_date <= asof 者，依 ex_date 升冪，格式 "YYYY-MM-DD:ratio;..."（ratio＝ratio_new_per_old，format "%.6g"）——與 skill screen.split_factor 相容
def screen_prices(raw: pd.DataFrame, adj_all: pd.DataFrame, actions: list[dict], asof: str) -> pd.DataFrame
    # 每檔：s = DataFrame(date, close=raw close, adj_close=adj_all close)（以 date 內併，date <= asof）
    # 直接呼叫 skill 的 fetch_prices.feats(s(date 轉 Timestamp), pd.Timestamp(asof)) 取 price、price_date、vol_36m、drawdown_3y、dd52（不得複製公式）
    # 欄位 ticker,price,price_date,vol_36m,drawdown_3y,dd52,price_src("alpaca"),splits；無資料的代號 price_src="失敗：無資料"
def daily_closes(split_adj: pd.DataFrame, asof: str, n_days: int = 130) -> pd.DataFrame
    # review_due 的 daily.csv：ticker,date,close（拆股調整、date <= asof、每檔最後 n_days 筆）
def fetch_price_inputs(client, tickers: list[str], asof: str, years: int = 4) -> dict
    # client 需有 bars()、corporate_actions()；start＝asof 前 years 年（含 37 個月以上）
    # 抓 raw、all（全調整）兩組日線＋公司行動 → {"prices": screen_prices(...), "daily": daily_closes(split 調整日線...), "actions": [...], "raw": raw}
    # split 調整日線：另抓 adjustment="split"
```
測試：假 client（簡單類別）；拆股前後 raw 與 all 不同時，price＝raw 收盤、vol 用 adj；splits 字串只含 asof 之前；與 `screen.split_factor` 搭配後市值基準正確（構造一個 2:1 拆股：封面股數申報在拆股前 → 股數 ×2）。
