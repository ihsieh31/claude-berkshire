# 02｜資料來源、欄位與資料品質

## 1. 來源（全部免費）

| 來源 | 用途 | 規格 |
|---|---|---|
| SEC 代號對照 `https://www.sec.gov/files/company_tickers.json` | 代號 → CIK | 快取一次 |
| SEC companyfacts `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | 年度財報（XBRL） | 每檔一次 |
| SEC submissions `https://data.sec.gov/submissions/CIK##########.json` | SIC（金融分組） | 每檔一次 |
| 月價格：Yahoo Finance chart 介面（非官方、免費） | 最新價、36 月波動、3 年回撤 | 月線、還原價；間隔 ≥0.5 秒（失敗也等）。月 K 線時間戳為月初、當月那根收盤是抓取當下：給了早於今天的 `--asof` 時丟掉含 asof 的當月 K 線（price＝上月底收盤）；`screen.py` 檢查 `price_date` 不得晚於 `--asof` |
| 使用者自備價格 CSV（`--manual`，欄位 ticker,date,adj_close） | 備援 | 任何來源 |

- SEC 請求一律帶 User-Agent（環境變數 `SEC_USER_AGENT`，名稱＋聯絡 email），間隔 ≥0.21 秒（≤5 req/s，低於 SEC 上限 10 req/s）。
- 抓過的 JSON 存在 `--cache` 目錄；快取檔最後修改日不早於 `--asof` 時直接用，早於 `--asof` 時重抓（重抓失敗沿用舊快取，並在 `data_notes` 註記；代號對照表則印警告；HTTP 403 不沿用舊快取，照常計入連續 403 中止，代號對照表遇 403 直接中止並提示檢查 `SEC_USER_AGENT`）。搬移或複製快取目錄請保留檔案時間戳（`tar`／`cp -p`），否則檔案修改日變成搬移當天，不會判為過期。
- 代號對照會跟著註冊主體改變（如 XOM 改組後指向新 CIK 0002115436）；`fetch_edgar.py` 依前身 CIK 對照（預設讀 repo 內 `trader/trader/data/cik_predecessors.json`，skill 單獨使用時以 `--predecessors` 自備同格式檔）併入舊 CIK 的財報並在 data_notes 記「含前身 CIK」；對照檔沒有的改組，新主體沒有歷史財報時記「無年度淨利」並判 PASS-資料不足（不刪），請以正式申報人工補。

## 2. 欄位定義（`fetch_edgar.py` 輸出）

| 欄位 | 定義 | EDGAR 標籤（依序補齊） |
|---|---|---|
| net_income | 最近財年 GAAP 歸屬母公司淨利（營業盈餘代理） | NetIncomeLoss → NetIncomeLossAvailableToCommonStockholdersBasic → ProfitLoss（含少數股權，前兩者皆缺才用） |
| ni5_mean | 近 5 年淨利平均（≥3 年才算） | 同上 |
| revenue、rev_cagr_5y | 營收與 5 年年複合 | Revenues → RevenueFromContractWithCustomerExcludingAssessedTax → SalesRevenueNet → …IncludingAssessedTax → InterestAndDividendIncomeOperating |
| roe、roe_med5、roe_min5、roe_sd5 | 淨利 ÷ 平均權益（平均權益 ≤0 為缺）；5 年中位、最小（需 5 年）、標準差 | StockholdersEquity（只用歸屬母公司權益；含少數股權者口徑不同，不拿來補） |
| roa | 淨利 ÷ 期末總資產 | Assets |
| loss_yrs5 | 近 5 年淨利 <0 的年數（需 5 年） | — |
| eps_cagr5 | 稀釋 EPS 5 年年複合。**未做拆股調整**：5 年前的 EPS 只在較舊年報（拆股前基準），近 5 年拆過股者嚴重低估（如 ORLY、CMG）。初篩排名、結構性規則、交接檔都不用，`screen.py` 不帶進 result.csv；深度分析的 eps_cagr5 由前半自填，不是這一欄 | EarningsPerShareDiluted → Basic |
| op_margin_5y_std | 營業利益率 5 年標準差 | OperatingIncomeLoss |
| fcf | 營業現金流 − 資本支出 | NetCashProvidedByUsedInOperatingActivities；PaymentsToAcquirePropertyPlantAndEquipment |
| shares、share_change_3y、shares_3y_ago、shares_3y_filed | 稀釋加權股數（最新申報版）與 3 年變化。最新 10-K 只涵蓋近 3 個財年，fy−3 的股數取自較舊 10-K（該 10-K 申報後的拆股未追溯）：`fetch_edgar.py` 輸出未調整值與其申報日，`screen.py` 以 `shares_3y_filed` 之後、`shares_filed_date` 以前的拆股事件調到最新基準後重算 | WeightedAverageNumberOfDilutedSharesOutstanding → Basic |
| debt_to_equity | **有息負債** ÷ 權益（`debt_series`：長期債＋一年內到期長期債＋短期借款，分組取數再相加；長期債用的是含一年內到期部分的標籤時不再加一年內到期；某年各組皆缺為缺值，**最近一年缺 → 本欄缺值、計中性，不當 0**） | 長期債：LongTermDebtNoncurrent → LongTermDebtAndCapitalLeaseObligations → …FinanceLeaseObligationsNoncurrent → SeniorNotes／NotesPayable／LongTermDebt（後三者含一年內到期；同年有多個時取最大值，因其中任一個可能只是一部分債務）；一年內到期：LongTermDebtCurrent → LongTermDebtAndCapitalLeaseObligationsCurrent → …FinanceLeaseObligationsCurrent；短期借款：ShortTermBorrowings → CommercialPaper；前兩組皆缺用 DebtCurrent；長期債組缺用 DebtLongtermAndShorttermCombinedAmount |
| interest_coverage_med5 | （稅前盈餘＋利息費用）÷ 利息費用，5 年中位（初篩近似；規則書P-06 的正式分子分母在深度分析算） | 損益表利息費用：InterestExpense → InterestExpenseNonoperating → InterestAndDebtExpense → InterestExpenseDebt → InterestExpenseOperating（全部利息費用歸在營業項下，如 DGX）；不用現金流量表的 InterestPaidNet；InterestExpenseLongTermDebt 只是長期債部分、低估利息，刻意不收；IncomeLossFromContinuingOperationsBeforeIncomeTaxes… |
| is_financial | SIC 6000–6799 | submissions.sic |
| years_of_data | 近 10 年有淨利的年數 | — |
| ni_prev、eq_prev | 前一財年淨利、權益（J4、J7） | 同上 |
| n_ni5、ni_sum5、ni5_med | 近 5 年有淨利資料的年數、合計（≥3 年）、中位數（深度分析困境正常化用） | 同上 |
| operating_cash_flow、ocf_sum5、ocf_pos5、n_ocf5 | 最近一年營業現金流；5 年合計、為正年數、有資料年數（F5 旗標；抽取值為 0 視為缺，於 `scripts/screen_quant.py` 判定 F5 時處理，見其註解） | NetCashProvidedByUsedInOperatingActivities |
| asof、fy_last_filed、max_filed_used | 決策日；最近財年淨利的 SEC 申報日；用到的最晚申報日（時點自檢） | companyfacts 的 filed 欄 |
| fy_last_end | 最近財年淨利的期末日（`screen.py` 判斷財年後分拆的基準；舊 features 沒有此欄時退用 fy_last_filed） | companyfacts 的 end 欄 |
| eps_g1 | 最近一年稀釋 EPS 變化（前一年 EPS >0 才算；T2） | EarningsPerShareDiluted |
| n_roe5 | 近 5 年 ROE 有資料年數（深度分析等級用） | — |
| util | SIC 4900–4999（公用事業；深度分析負債紅旗例外） | submissions.sic |

時點規則：只用 10-K／10-K/A 的全年值（期間 330–400 天；不看 fp，有公司把年報標成 Q4；時點型數字須是年度期末）；財年標籤：期末（往前移 14 天後）在 7–12 月者＝期末年；在 1–6 月者依這類 10-K 的 DocumentFiscalYearFocus（companyfacts 的 fy 欄）與期末前一年的差決定是期末年或前一年：申報日最近 2 份差值一致就依它（DEI 慣例變過的公司如 TJX、STZ 約 2021 年起改稱期末年，依現在的自稱），不一致退回全部這類 10-K 的多數（fy 欄偶有單份填錯，如 CrowdStrike、Salesforce，不被帶偏）（52/53 週制期末不撞號；換過會計年度的公司，7–12 月期末年度不受舊慣例影響）。**過渡年例外**：會計年度變更時（由 1–6 月期末改 7–12 月期末：LHX FY2019、GPN FY2016、MOS FY2013、ADM FY2012；或 7 月改 12 月：FERG FY2025），最後一個舊年度與過渡期會落在同一財年；同財年兩個期末相距 >30 天即警示，該財年所有欄位統一取**有全年（12 個月）營收或淨利的最晚期末**（沒有任何全年損益時才用總資產的最晚期末；只涵蓋 5–7 個月的過渡期 10-KT 資產負債表日不算年度期末，所以 MOS FY2013、FERG FY2025 取舊年度期末，LHX FY2019 取 2020-01-03、GPN FY2016 取 2016-12-31），其他期末的數字不用，沒有該期末數字的欄位該年留缺（不混期間）。財年標籤仍可能與公司自稱差一年時，**以欄頭期末日為準**；同財年預設取**首次申報**版（跨同一欄位的所有標籤比申報日，同日才依標籤優先序；防用到事後重編）；**只用申報日早於決策日 `--asof` 者（filed < asof；同日申報也不用）**，未給 `--asof` 時預設今天。輸出 `fy_last_filed`（最近財年淨利申報日）與 `max_filed_used`（實際用到的最晚申報日）；`max_filed_used` ≥ 決策日即中止。`screen.py --asof` 再檢查一次 `fy_last_filed`。股數與 EPS 例外取決策日前最新版，因為後續年報會依拆股追溯調整。

## 3. 價格衍生（`fetch_prices.py` 輸出）
`price`、`price_date`、`vol_36m`、`drawdown_3y`、`dd52`（最新月末 ÷ 近 13 個月末最高 − 1，近似距 52 週高點跌幅；困境標記 T3）。`screen.py` 以 `price ×（封面股數，依其申報日後的拆股調整；過期或缺則用年報加權稀釋股數）` 得市值，用於統計排除與排名；結構性刪除不用價格。

- `splits`（`YYYY-MM-DD:比例;…`，比例＝新股數/舊股數）：只有整數或簡單分數（r 或 1/r 乘上 1–10 的分母後近似整數，`screen.is_split_ratio`，含 6:5、11:10 等股票股利）才算拆股並乘到股數。其他非整數（Yahoo 把分拆的價格調整係數也放在 splits，如 FDX 1.241、HON 0.9535）不乘股數，改當分拆事件。
- `spinoffs`（`YYYY-MM-DD[:係數];…`）：自動交易系統的價格層（trader `prices_adapter`）另外分出的分拆事件；skill 的 `fetch_prices.py` 不輸出此欄，分拆係數仍混在 splits 裡，由 `screen.py` 依上一點分出。
- 分拆日晚於 `fy_last_end`、且 ≤ 決策日：市值已是分拆後、最近年報仍含分出事業 → `earn_yield`／`fcf_yield`／`norm_earn_yield` 設缺（落入資料不足）並寫 `dq_note`，等分拆後第一份年報（D69）。

`screen.py` 另加的追溯欄（都在 `result.csv`；交接檔帶 `mcap_shares_source`、`unrated_fields`、`dq_note`）：

| 欄位 | 定義 |
|---|---|
| mcap_shares_source、mcap_shares_date | 市值所用股數：`cover`（封面股數）或 `annual_weighted`（缺封面股數，改用年報加權稀釋股數；不寫 dq_note，報告資料品質表照列）；該股數的申報日 |
| unrated_fields | 缺值計 0.5 的研究優先因子，以及因缺值無法評估的結構性規則（如 `J1:loss_yrs5`）；只有欄名、不含數值 |
| dq_note | 資料品質註記（見第 4 節）；只寫事件日與性質，不寫分拆係數、殖利率等價格衍生值（會進價格盲的 screen_context） |
| spinoff_factors | 觸發財年後分拆或含合股註記的分拆價格調整係數（`日期:係數;…`；兩個股價的比值，屬價格衍生）；只在 result.csv 與報告，不進交接檔 |
| sel050_type | SIC 粗對應的 SEL-050 預設太難型（D66 名額上限用、D72 補對照；屬推論；仍未對照的型態見 SKILL.md 第 4 節）；只在 result.csv 與報告 |
| override_note | 人工撤銷結構性刪除的紀錄（`--overrides`，D62；撤銷者另補困境碼 `R_人工撤銷刪除`）；只在 result.csv 與報告，交接檔另列 `overrides` |
| override_expired | 撤銷檔中自 date 起滿 12 個月、不再生效的列（日期與理由）；交接檔另列 `overrides_expired` |

## 4. 資料品質檢查（每一筆刪除都查；通過者抽查）
1. 單位與量級：同欄跨年是否有千倍跳動。
2. 股數與拆股：淨利 ÷ 股數 ≈ EPS；拆股調整後股數 3 年變化仍 >+50% 或 <−50% → `screen.py` 寫 `dq_note`，先查拆股或合併；真實合併或 IPO 稀釋（如 +100%～+250%）保留原值照常計分（百分位排名不受極端值影響）；> +900%（3 年成 10 倍以上，如 WRB +95,289% 的千倍單位錯誤）才視為單位錯誤，該欄設缺值（計中性）。分拆防線：分拆係數不乘股數；財年後分拆 → 殖利率類設缺（第 3 節）；分拆係數 <1 且落在所用股數基準日之後、決策日以前 → dq_note「分拆事件同日含合股、需人工處理」（純分拆係數必 >1，<1 代表同日藏了合股，股數未依合股調整；係數本身看 `spinoff_factors`）。
3. 金融分組：SIC 落在 6000–6799 者才用金融口徑（REIT 6798 歸非金融組，排名與 F5 皆同；D20）；保險與銀行的 FCF、利息保障不適用（顯示值只作參考）。
4. 權益 ≤0 或極小：ROE 失真，改看有形資本報酬（深度分析 G3）。
5. 證券損益占淨利 >20% 的公司：P/E、ROE 以 GAAP 淨利計會失真，應備註「待重編」（規則書第 0 節第 5 點）。**目前未自動檢查**（`fetch_edgar.py` 沒抓證券損益欄，`screen.py` 不寫 dq_note）：人工複核與深度分析時對保險、控股型公司核對；自動化列為延後項。
6. 缺漏：`data_notes` 原樣帶入交接檔；缺資料的條件不成立（不刪）。
7. 營業現金流恰為 0：多半是未抽到，視為缺值。
8. 時點：`fy_last_filed` 必須早於決策日；回測或重跑歷史時，決策日一律取「該財年申報日之後」，不得取財年結束日。

## 5. 深度分析需要、初篩不必抓的資料（交接時提醒）
年報文字與附註、委託書、近 4–8 季財報、債務到期表、同業資料、30 年期公債殖利率、大型股母體 P/E 中位檔。完整清單見 `../../berkshire-deep-merged/references/procedure/08_data_requirements.md`。
