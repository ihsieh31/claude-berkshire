# claude-berkshire：波克夏式長期選股與紙上交易

初篩（每季 15 檔）→ 深度分析（G0–G11 閘門、價格隔離、強模型同意閘門）→ 兩週觸發檢查 → 紙上帳本與每日收盤流程。規則在兩個 skill 裡，模型工作由 `trader/` 框架依 `routing/` 調用 Claude Code（`claude -p`）。

## 專案結構

| 路徑 | 內容 |
|---|---|
| `berkshire-screen-merged/` | 初篩 skill：結構性刪除、統計排除、研究優先排序（`scripts/screen.py`） |
| `berkshire-deep-merged/` | 深度分析 skill：規則書、程序、閘門 3 問卷、`cap_calc.py`、`review_due.py` |
| `routing/` | 模型對應（`model_routing.json`）、每種工作的提示（`prompts/`）與輸出格式（`output_schemas.json`） |
| `trader/` | 執行框架：資料（SEC／Alpaca／FRED）、工作引擎、寫回、紙上帳本、排程、儀表板、桌面小工具；`tests/` 為全部測試 |
| `plan.md`、`plan_dashboard.md`、`trader/CONTRACTS*.md` | 整體規劃與各里程碑介面契約 |
| `handoff.md` | 交接紀錄：現況、決策歷程、待辦（新對話先讀這份） |
| `換機匯出與封存程序.md` | 在另一台重跑分析、匯出、本機封存舊分析與替換的完整步驟 |
| `bug report/` | 歷次審查報告與重現腳本 |

**不在版本控制內**（見 `.gitignore`）：`trader/.env`（金鑰）、`state/`（資料庫、SEC 快取、分析結果、價格）、`test_runs/`（開發期測試分析，含假價格）、審查前的程式備份、桌面小工具編譯產物。

## 快速開始

```bash
cd trader && python3 -m pip install -e ".[dev]"
```

1. `cp trader/.env.example trader/.env`，填入 `SEC_USER_AGENT`（須含 email）、Alpaca 紙上交易金鑰、FRED 金鑰；登入 `claude`。
2. `trader/config.yaml` 已附（只含環境變數名稱，沒有金鑰）；要改模型或預算再改它。
3. 建立 state 並測試連線：`cd trader && python3 -m trader.cli init && python3 -m trader.cli doctor`。
4. 第一次建立名單：`python3 -m trader.cli bootstrap --asof YYYY-MM-DD`；強模型工作會等同意：`approvals list`／`approvals approve`。
5. 儀表板與桌面小工具：`python3 -m trader.cli desktop install`（http://127.0.0.1:8787/）。排程：`scheduler install`（先讀 `handoff.md` 的前提）。
6. 測試：`cd trader && python3 -m pytest -q`；初篩自測：`python3 berkshire-screen-merged/scripts/test_screen.py`。

## 近期重要變動（2026-10-17）

- **初篩排序**：非金融六項中「營收成長偏低」改為中性，換成「5 年最差 ROE 高」。目的是提高送進深度分析者通過 G3 的比例，**未回測、不宣稱提高報酬**；2–3 季後比較 G3 命中率再決定是否保留（`berkshire-screen-merged/references/evidence_statistical.md` §2）。
- **G3 淘汰依據**：FULL_A 輸出新增 `moat_evidence`，寫回記 `reject_basis`（有反證／證據不足／其他），停止碼與 12 個月排除不變。「強」的證據標準不放寬，等 `reject_basis` 累積 2–3 季再議。
- **資料包補強**：Form 4 內部人交易（只列股數，不含價格）、財報 8-K 的 EX-99.2 等補充資料／簡報、10-K 的 EX-13 致股東年報、10-K 點名但不在 S&P 500 的競爭者、產業衝擊年樣本不足時以 SEC 同 SIC 公司補樣本。
- **G8 機會成本（2026-10-18）**：框架算好比較表各列給 FULL_B 與 HOLD_REVIEW——加碼最佳持股、最弱持股、觀察名單今天可買者（決策日收盤＋cap_inputs 重算）、3 個月國庫券、大盤（P-66 權益 ÷ GDP 分區 → P-27，區內取值為推論）；任一列高出本標的 ≥1pp 時 BUY 改 WATCH、PRICE 通道的加碼改 HOLD。訊號卡 `g8_adjusted_expected_return` 由腳本重算，排序以當日報價重算。
- 第三次全專案審查的 H1／H2／M1–M5／L2 已修，詳見 `bug report/review3/REPORT_第三次全專案審查.md` 與 `handoff.md`。

## skill 說明

兩個 skill＋一份模型對應。skill 與腳本都不呼叫模型，只輸出訊號；外部交易框架依 `routing/` 調用模型。若要在 Claude Code 內手動使用，把兩個 skill 資料夾複製到 `~/.claude/skills/`。

| skill | 做什麼 | 頻率 |
|---|---|---|
| `berkshire-screen-merged` | 股票池 → 刪結構性垃圾 → 排除統計高風險 → 研究優先排序 → 前 15 檔 | 每季（財報季後）；大盤跌逾 20% 可加跑 |
| `berkshire-deep-merged` | 逐檔 G0–G11 判斷買／觀察／不買、買價上限、部位；兩週觸發檢查決定誰需要重看 | 新候選：初篩後；既有標的：每兩週跑 `review_due.py`，論點沒動就不分析 |

## 來源與取捨

| 來源 | 取用 | 理由 |
|---|---|---|
| opus 初篩 | 統計排除三條、特殊情境道、研究優先排名、前 15 | 唯一產出固定名單、有兩段期間一致的實證 |
| hark 初篩 | 結構性刪除 J1/J3/J4/J7、申報日時點、EDGAR 抓取腳本、困境標記、會計旗標 | 申報日時點防前視；刪除規則未誤刪歷史實際買進 |
| hark 深度分析＋規則書 | 閘門流程、參數、買價上限、確定性等級、部位、自動買進三閘門、論點式賣出、紙上交易驗證 | 數字可計算、可稽核、無價格停損 |
| opus 深度分析 | 20 份思考背景（能力圈、護城河、會計紅旗、產業、心理誤判等） | 判斷每一步「怎麼想」；已清掉與硬規則衝突的句子、具名案例與人物語氣 |

## 合併版新增的設計（都標為推論、未經回測）

1. **自動買進上限＝min(隱含成長 10% 上限, 基準情境價值)**：原上限在成長偏低的公司會高於基準價值，等於買在一買進就觸發賣出檢視的價位。
2. **特殊情境「困境中但歷史經濟力佳」**：統計排除＋困境標記＋5 年虧損 ≤1、ROE 中位 ≥12%。預設不交深度分析（`--special-max 0`）。
3. **兩週觸發檢查 `review_due.py`**（純規則、不用模型；非重大 8-K 與新聞標題另列 `triage`，由框架交模型初判）：新年報、重大 8-K、滿 12 個月 → 完整重跑；新季報、成本 −12%、單日 ±4%、自上次跌 ≥20%、單季跌 ≥25% → 論點檢查；觀察名單跌破上限、持股未達目標且低於上限、超過賣出檢視線或荒謬級線、利率變動 ≥1pp → 只重跑估值到部位；其餘 → 不分析。
4. 金融股判定：缺 SIC 時的近似條件加上「權益為正、ROA <3%」，避免負權益的非金融股被當成金融股。

## 能不能賺錢（照實說）

- 有證據的：初篩**降低踩雷**（3 年大跌機率下降、被刪者壞結果約 3 倍）。
- 沒有證據的：排名提高報酬（逐期模擬不顯著）、整條流程的報酬（深度分析靠判斷，無法完整回測）。
- 因此上線順序：**先 3 個月紙上交易**（`berkshire-deep-merged/references/procedure/13_forward_validation.md`），每筆決策寫入日誌，期滿對大盤與初篩母體檢討，再決定是否用真錢、用多少。

## 驗證紀錄

- `berkshire-screen-merged/scripts/test_screen.py`：合成資料自測（刪除、例外、排除、特殊道、拆股市值、前視中止、交接檔）。
- `berkshire-deep-merged/scripts/test_review_due.py`：觸發檢查自測（16 種情境）。
- `cap_calc.py`、`gate3_check.py`：範例輸入、D 級／缺值、價格隔離時間戳皆測過。
- `build_dossier.py`：真實 SEC 資料產出 10 年表與資料品質警示；合成資料驗證 EPS 不一致、整數 EPS、現金流偏低、缺年四種警示。
- 試點：sonnet 子代理對一檔大型股跑完整深度分析（只給數字、不給年報文字）→ 正確停在 `MISSING_DATA`、價格隔離有遵守；回饋 23 項已全部處理。
- 真實資料：本機 SEC XBRL 與日線股價，40 檔大型股跑通初篩；發現並修正封面股數過期、金融股近似誤判、特殊道收錄過寬三個問題。
- `trader/`：772 個測試全過（2026-10-18）；SQLite 完整性檢查 ok；`daily --dry-run`、`review --due-only` 對真實 Alpaca／SEC／FRED 驗證過。
- 2026-10-15 第一批 15 檔真實深度分析：零 BUY（OTIS＝WATCH C 級、LOW＝TOO_HARD、其餘 REJECT）。這批將在換機重跑後封存為樣本，不再採用（見 `換機匯出與封存程序.md`）。

## 已知限制

- 證據來自美國大型股；其他市場、小型股未驗證。
- `fetch_prices.py` 用 Yahoo 非官方介面；月線最後一根含決策日之後的價格，回測要用 `--manual` 日價。
- 多類股公司可能停報封面合計股數，改用年報加權稀釋股數，市值可能略有誤差（報告會標示）。
- 深度分析需要年報文字、附註、委託書；只有數字時正確結論是 `MISSING_DATA`。
