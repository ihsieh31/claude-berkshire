# 02｜資料來源、欄位與資料品質

## 1. 來源（全部免費）

| 來源 | 用途 | 規格 |
|---|---|---|
| SEC 代號對照 `https://www.sec.gov/files/company_tickers.json` | 代號 → CIK | 快取一次 |
| SEC companyfacts `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | 年度財報（XBRL） | 每檔一次 |
| SEC submissions `https://data.sec.gov/submissions/CIK##########.json` | SIC（金融分組） | 每檔一次 |
| 月價格：Yahoo Finance chart 介面（非官方、免費） | 最新價、36 月波動、3 年回撤 | 月線、還原價；間隔 ≥0.5 秒 |
| 使用者自備價格 CSV（`--manual`，欄位 ticker,date,adj_close） | 備援 | 任何來源 |

- SEC 請求一律帶 User-Agent（環境變數 `SEC_USER_AGENT`，名稱＋聯絡 email），間隔 ≥0.21 秒（≤5 req/s，低於 SEC 上限 10 req/s）。
- 抓過的 JSON 存在 `--cache` 目錄，重跑不再請求。
- 代號對照會跟著註冊主體改變；對照到新主體而沒有歷史財報時，腳本記「無年度淨利」並判 PASS-資料不足（不刪），請以正式申報人工補。

## 2. 欄位定義（`fetch_edgar.py` 輸出）

| 欄位 | 定義 | EDGAR 標籤（依序補齊） |
|---|---|---|
| net_income | 最近財年 GAAP 淨利（營業盈餘代理） | NetIncomeLoss → ProfitLoss → NetIncomeLossAvailableToCommonStockholdersBasic |
| ni5_mean | 近 5 年淨利平均（≥3 年才算） | 同上 |
| revenue、rev_cagr_5y | 營收與 5 年年複合 | Revenues → RevenueFromContractWithCustomerExcludingAssessedTax → SalesRevenueNet → …IncludingAssessedTax → InterestAndDividendIncomeOperating |
| roe、roe_med5、roe_min5、roe_sd5 | 淨利 ÷ 平均權益；5 年中位、最小（需 5 年）、標準差 | StockholdersEquity → …IncludingPortionAttributableToNoncontrollingInterest |
| roa | 淨利 ÷ 期末總資產 | Assets |
| loss_yrs5 | 近 5 年淨利 <0 的年數（需 5 年） | — |
| eps_cagr5 | 稀釋 EPS 5 年年複合（取最新申報版，含拆股追溯） | EarningsPerShareDiluted → Basic |
| op_margin_5y_std | 營業利益率 5 年標準差 | OperatingIncomeLoss |
| fcf | 營業現金流 − 資本支出 | NetCashProvidedByUsedInOperatingActivities；PaymentsToAcquirePropertyPlantAndEquipment |
| shares、share_change_3y | 稀釋加權股數（最新申報版）與 3 年變化 | WeightedAverageNumberOfDilutedSharesOutstanding → Basic |
| debt_to_equity | **有息負債**（長期＋一年內到期＋短期借款＋商業本票）÷ 權益 | LongTermDebtNoncurrent／LongTermDebt；LongTermDebtCurrent／DebtCurrent／ShortTermBorrowings／CommercialPaper |
| interest_coverage_med5 | （稅前盈餘＋利息費用）÷ 利息費用，5 年中位（初篩近似；規則書P-06 的正式分子分母在深度分析算） | InterestExpense；IncomeLossFromContinuingOperationsBeforeIncomeTaxes… |
| is_financial | SIC 6000–6799 | submissions.sic |
| years_of_data | 近 10 年有淨利的年數 | — |
| ni_prev、eq_prev | 前一財年淨利、權益（J4、J7） | 同上 |
| n_ni5、ni_sum5、ni5_med | 近 5 年有淨利資料的年數、合計（≥3 年）、中位數（深度分析困境正常化用） | 同上 |
| operating_cash_flow、ocf_sum5、ocf_pos5、n_ocf5 | 最近一年營業現金流；5 年合計、為正年數、有資料年數（F5 旗標；抽取值為 0 視為缺，`01_rules_and_sources.md` §1） | NetCashProvidedByUsedInOperatingActivities |
| asof、fy_last_filed、max_filed_used | 決策日；最近財年淨利的 SEC 申報日；用到的最晚申報日（時點自檢） | companyfacts 的 filed 欄 |
| eps_g1 | 最近一年稀釋 EPS 變化（前一年 EPS >0 才算；T2） | EarningsPerShareDiluted |
| n_roe5 | 近 5 年 ROE 有資料年數（深度分析等級用） | — |
| util | SIC 4900–4999（公用事業；深度分析負債紅旗例外） | submissions.sic |

時點規則：只用 10-K／10-K/A 的全年值（fp＝FY、期間 330–400 天）；同財年預設取**首次申報**版（防用到事後重編）；**只用申報日早於決策日 `--asof` 者（filed < asof；同日申報也不用）**，未給 `--asof` 時預設今天。輸出 `fy_last_filed`（最近財年淨利申報日）與 `max_filed_used`（實際用到的最晚申報日）；`max_filed_used` ≥ 決策日即中止。`screen.py --asof` 再檢查一次 `fy_last_filed`。股數與 EPS 例外取決策日前最新版，因為後續年報會依拆股追溯調整。

## 3. 價格衍生（`fetch_prices.py` 輸出）
`price`、`price_date`、`vol_36m`、`drawdown_3y`、`dd52`（最新月末 ÷ 近 13 個月末最高 − 1，近似距 52 週高點跌幅；困境標記 T3）。`screen.py` 以 `price ×（封面股數，依其申報日後的拆股調整；過期或缺則用年報加權稀釋股數）` 得市值，用於統計排除與排名；結構性刪除不用價格。

## 4. 資料品質檢查（每一筆刪除都查；通過者抽查）
1. 單位與量級：同欄跨年是否有千倍跳動。
2. 股數與拆股：淨利 ÷ 股數 ≈ EPS；股數 3 年變化 >+50% 或 <−50% 先查拆股或合併。
3. 金融分組：SIC 落在 6000–6799 者才用金融口徑；保險與銀行的 FCF、利息保障不適用（顯示值只作參考）。
4. 權益 ≤0 或極小：ROE 失真，改看有形資本報酬（深度分析 G3）。
5. 證券損益占淨利 >20% 的公司：P/E、ROE 以 GAAP 淨利計會失真，名單備註「待重編」（規則書第 0 節第 5 點）。
6. 缺漏：`data_notes` 原樣帶入交接檔；缺資料的條件不成立（不刪）。
7. 營業現金流恰為 0：多半是未抽到，視為缺值。
8. 時點：`fy_last_filed` 必須早於決策日；回測或重跑歷史時，決策日一律取「該財年申報日之後」，不得取財年結束日。

## 5. 深度分析需要、初篩不必抓的資料（交接時提醒）
年報文字與附註、委託書、近 4–8 季財報、債務到期表、同業資料、30 年期公債殖利率、大型股母體 P/E 中位檔。完整清單見 `../../berkshire-deep-analysis/references/08_data_requirements.md`。
