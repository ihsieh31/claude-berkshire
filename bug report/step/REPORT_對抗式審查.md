# 對抗式審查報告（第一性原理拆解 · Step-5，含自我驗證版）

> 審查日期：2026-10-17（第二版：加入逐項自我驗證與過度工程化評估）
> 審查範圍：`berkshire/` 全庫（2 個 skill 包、`routing/`、`trader/` 99 支 Python、`state/` 實際資料、`test_runs/` 實跑紀錄）
> 原則：**只讀不改**。驗證測試與本報告只放在 `bug report/step/`（`test_adversarial.py`，22 支全過）。
> 對照基準：專案既有測試 **656 passed**（乾淨 TMPDIR；TMPDIR 重用時 WorkBuddy 沙箱會把 FileExistsError 轉成 PermissionError 造成假失敗——已證實與專案無關）。

---

## 0. 一句話結論（驗證後）

架構與工程品質高：價格隔離、時點紀律、原子寫入、失敗關閉都落地，獲利邏輯數學無誤。**自我驗證後的真實缺陷收斂為 1 個高、5 個中、4 個低**；其中 M1（原 H2）經複查後果比初判輕（有下單時的第二道程式防線），M3（原 H3）複查後確認是**既定設計而非 bug**。**沒有發現會直接造成錯誤成交或必然虧損的邏輯缺陷**；最該先修的是 H1（一行）。過度工程化方面：整體**不成立**——複雜度都可追溯到明確需求且全面 fail-closed；只有 3 處局部簡化機會。

---

## 1. 從第一性原理拆解：這個系統到底在做什麼

還原到最原始的問題：**「在不預測市場的前提下，用可稽核的規則，從大型股裡找出少數值得長期持有的企業，在價格低於保守價值時買入、論點被證偽時賣出」**。

| 層 | 第一性問題 | 實作 | 審查判定 |
|---|---|---|---|
| 資料 | 決策當時「可得」的事實？ | SEC／Alpaca／FRED，一律 `filed < asof`、`價格 ≤ asof` | 紮實 |
| 初篩 | 先研究誰？ | screen.py：刪垃圾→統計排除→分道→排序 | 規則對照正確；1 個缺資料瑕疵 |
| 深度分析 | 看得懂？生意好？人可信？便宜？ | G0–G11＋三情境估值＋閘門 3 問卷 | 嚴謹；價格隔離有 1 個已知漏洞 |
| 工作引擎 | 讓模型在「不知道價格」下判斷？ | 沙箱＋deny＋gate3 mtime 閘＋雙段式 | 有效；把關責任分配有缺口 |
| 組合層 | 買多少、何時加、何時賣？ | sizing.py：三閘門、權重、流動性、建倉計畫 | 保守可稽核 |
| 帳本 | 錢與股怎麼記？ | PaperBroker、公司行動覆核、原子交易 | 正確；1 個按鍵級 bug |
| 儀表板／檢討 | 我做得對嗎？ | 唯讀本機 API、前瞻驗證報告 | 安全到位；比較基準失真 |

**核心哲學（正確且罕見）**：模型判斷與程式把關分離——數字以 cap_calc 為準、閘門 3 以 gate3_check 為準、隔離以沙箱為準。執行度約 90%；所有缺陷都是「某道該程式把關的規則落在了模型自述地帶」。

---

## 2. 發現清單（含逐項驗證結果）

### 🔴 高風險（1 項）

#### H1. SPY 比較基準永遠不含股利——鍵名錯誤【驗證：確認，真實 bug】
- **位置**：`trader/pipelines/daily.py:294-296`：`bench_div = sum(a.get("cash") or 0.0 …)`。
- **驗證過程**：grep 全庫 `benchmark_dividend` → 唯一生產者是 daily.py:294-296，唯一消費者是 `report/metrics.py:60`（`div = …get("benchmark_dividend")`，算 SPY 總報酬指數）。而 Alpaca 公司行動經 `alpaca._normalize_action` 正規化後的鍵是 **`cash_per_share`**，輸出 dict 中**不存在 `cash` 鍵** → `a.get("cash")` 恆 None → 0.0。
- **後果**：SPY 指數是價格報酬而非總報酬，被系統性低估約 1 個百分點級／年（SPY 股利殖利率歷史水準）→ 儀表板「超額報酬」與月報「對大盤」同步失真——這是全系統唯一回答「能不能賺錢」的兩個數字。
- **為何沒被既有測試抓到**：nav_daily 目前為空（尚未跑過 daily），且測試未覆蓋 record_nav 的股利路徑。
- **修復**：`a.get("cash")` → `a.get("cash_per_share")`，補一支「餵一筆 SPY 除息、斷言 benchmark_dividend > 0」的測試。

### 🟡 中風險（5 項）

#### M1. FULL_B 買進路徑沒把關「閘門 2：價格 < 上限」【驗證：真實，但嚴重度由高降中】
- **位置**：`analysis/checks.py::cap_signal_by_gates` 只檢查 gate3 與初篩刪除。
- **複查發現的緩衝（初判遺漏）**：`portfolio/sizing.py:311-312` 在下單時獨立複檢 `quote×(1+滑價) < auto_buy_cap` → **即使記了一筆 price>cap 的 BUY，也不會產生委託**（已加測試 `test_order_time_check_blocks_the_trade` 證明）。加碼路徑（plan_tranches）同樣有 `報價未低於上限` 拒單。g_impl_gt_cap / C 級的情境也都被此檢查連帶擋下（價格 > P2′ ≥ 上限；C 級 target_weight=0 拒單）。
- **真實模型證據**：`test_runs/m3-OTIS-rerun` 的決策日誌中模型自行填入 `gate2_price_below_cap: false`（價格 70.39 > 上限 49.29）——**數值正確**。即這是「框架沒給模型兜底」問題，不是「模型已出錯」問題。
- **實際後果**：① 決策日誌／登錄表可能記下永不成交的 BUY（前瞻驗證會把它計入 BUY 決策，解讀失真）；② 與 PRICE 路徑的把關強度不一致（PRICE 有完整 gate2 程式檢查）；③ 月報 compliance 的 bad_buys 檢查讀的是模型自述閘門，抓不到這個不一致。
- **修復**：把 PRICE 的 gate2 判定式（price<auto、grade∈{A,B}、not g_impl_gt_cap、not flips）抽成共用函式，在 cap_signal_by_gates 對 BUY 一律執行；不過 → 降 WATCH 並記日誌。

#### M2. 閘門 3 有效性取自模型自述，框架算出的 gate3_result 沒有回寫決策日誌【驗證：確認，潛在而非已發生】
- `review_jobs._gate3_status` 讀 `decisions.data.gate3_pass`（模型寫進 decision_log.json 的值）；框架在 `FullAHandler.post` 算出的 `gate3_result.json` 只餵給當次 FULL_B，`writeback` 完全不碰 gate3（grep 確認）。
- **真實證據**：OTIS 實跑中模型自述 `gate3_pass=true` 與框架 `gate3_result.gate3_pass=true` **一致**——所以這是潛在缺口：模型漏填 → PRICE 永不通過（fail-closed，安全但卡死加碼）；模型填寫與框架計算分歧時以自述為準。
- **修復**：`write_full_result` 時把 `gate3_result.json` 的 gate3_pass 覆寫進日誌（一行）。

#### M3. PRICE（弱模型）產生的 BUY 不經 opus 同意閘門與紅隊【驗證：確認是既定設計，非 coding bug】
- **複查**：`routing/model_routing.json` 明寫 PRICE＝`weak`、`BUY_CANDIDATE → PORTFOLIO`；M6 同意閘門的設計目的是**控制 opus 呼叫成本**（handoff：「不再直接呼叫 opus」），不是交易審批。PRICE 路徑的三閘門有兩道程式把關（gate1 來自 screen_decision、gate2 來自 cap_calc 腳本），第三道（gate3 十二個月 freshness）見 M2。
- **真實不一致**：skill 文字「擬 BUY → RED_TEAM」對每筆 BUY 都成立，PRICE 路徑跳過紅隊。這是**策略選擇而非缺陷**，但值得使用者 conscious decision：要嘛讓 PRICE 的 BUY 也進 approvals 佇列，要嘛在報表標註「PRICE 來源、未經紅隊」。
- **初判用語修正**：不應說「繞過」（暗示漏洞），而是「同意閘門的涵蓋範圍與 skill 的 RED_TEAM 要求之間存在既定落差」。

#### M4. J4 缺 roe_med5 時照樣刪除，違反「缺資料永遠不是刪除理由」【驗證：確認，發生機率低】
- `screen_quant.J4` 例外條件 `(loss_yrs5 <= 1) & (roe_med5 >= 0.12)`，roe_med5 為 NaN 時例外為 False → 刪除；SKILL §3 明文「缺欄位 → 條件不成立（不刪）」。
- **發生條件**：權益／淨利歷史足以觸發 J4，但 ROE 可計算年數 <3（平均權益 ≤0 的年份不計）——罕見但不為零。
- **修復**：缺 roe_med5 → 視為例外無法驗證 → 不刪、標 PASS-困境交 §6 人工複核。

#### M5. 帳本啟用後給 FULL_B 的組合狀態沒有現金與已承諾【驗證：確認】
- `_portfolio_state`：有持股時只回 `{holdings, note}`；現金、可動用現金、檔數、已承諾全消失。G8 機會成本（勝過加碼最佳持股／國庫券／現金）恰好需要這些。紙上交易開始建倉後即觸發。
- **修復**：一律帶入 cash、committed、n_holdings、min_cash。

#### M6. PRICE 把 gate2 寫成中文字串，月報合規檢查誤判【驗證：確認，含真實日誌證據】
- `write_price_result`（框架自己）把 `gate2_price_below_cap: "通過"`（字串）寫進 DB；`validation._truthy` 只認 `True／"true"`；`sizing.gate_failures._truthy` 認「通過」。三處對同一欄位判定不一致。
- **後果**：月報 compliance 的 bad_buys 會把**每個** PRICE 來源的 BUY/ADD 標兩條假問題（gate2 未過＋問卷作答檔缺），淹沒真正的合規問題。決策日誌 schema 也宣告該欄為 `true | false`。
- **註**：真實 PRICE 測試中模型自己寫的日誌是正確的 bool（OTIS: false）；是框架回寫時改壞的。

### 🟢 低風險（4 項）

#### L1. 資料包「平均回購價」欄洩漏歷史價格水準【驗證：確認（真實 HPQ 資料包有該欄），已知項】
- `build_dossier.py` 的 `R["平均回購價"]`；scrub 清單抓不到（中英文皆無「回購價」樣式）；FULL_A（明令不看價格）會看到歷史股價，有錨定風險。handoff §4c 已列為待決定事項。修復：刪欄或移入 price_context。

#### L2. ≥40% 跳空事件無法判「無公司行動」，且跳空來源的比例無獨立驗證【驗證：前半是設計、後半是真缺口】
- 前半：CORP_ACTION 提示明寫「不明價格跳空不得答無公司行動」——**保守暫停是刻意設計**，任何大單日漲跌（財報暴漲、軋空）都會暫停該檔等人工，營運負擔高但安全。
- 後半（真缺口）：跳空事件沒有 Alpaca 比例可交叉驗證；模型若把「股票股利」比例寫成 `pb/pa`，價格比與市值守恆兩道覆核都會通過（已加測試證明），會以虛構行動調整真實股數。修復：跳空來源的 ratio 必須另有獨立來源，否則一律 needs_human。

#### L3. 零角正確性問題【驗證：確認，可達性低】
- 現金股利無發放日（模型未填且 Alpaca 無 payable_date）→ 應收永留帳上（NAV 不失真，現金比失真）。
- 正常化盈餘為負時荒謬級線為負（`50×(norm_eps or oe)`，oe<0 → 永不觸發「至少賣一半」）。發生條件極窄（cap_input 持久化時 oe<0，但 auto_buy_cap 會是 None）。
- skill `fetch_prices.feats` 月線含決策日後價格——README 已記載、trader 層已用 Alpaca 日線避開。

---

## 3. 已確認「不是問題」的重要設計（驗證紀錄）

經刻意攻擊測試判定正確的項目，避免後人重複懷疑：

1. **cap_calc.py 數學**：三情境、g_impl 二分（邊界 −50%／+min(50%, r_d)、r≤g_term 回 inf）、P2′/P4′/結論測試、grade 降級——與 rulebook §6 逐條一致。
2. **screen_quant 時點**：`filed >= asof` 跳過＋max_filed 自檢，前視防護到位。
3. **價格隔離**：沙箱 deny 規則＋FULL_B gate3 mtime 閘＋assert_price_free 事後掃描；論點檔進 THESIS/TRIAGE 前逐句刪價格字句；新聞標題過濾目標價／評等。
4. **撮合紀律**：限價成交價不劣於限價、隔日撮合、現金不足部分成交後標 filled——無當日收盤前視。
5. **公司行動覆核**：比例×Alpaca、比例×價格比、市值守恆三道；零股與現金對價完整；交易內檔案變更可還原；已入帳行動以 (ticker, effective_date) 去重。
6. **寫入原子性**：decisions/signals/registry 同交易；論點檔先寫失敗刪除。
7. **儀表板安全**：Host 白名單＋權令牌比對＋Origin＋CSP＋本機綁定。
8. **排程**：bootstrap 前提、單實例鎖、pipeline 鎖、失敗退避、季篩日期狀態機正確（ls<q 才重跑）。
9. **M1 的金錢防線**（本次複查確認）：plan_buys／plan_tranches 在下單時獨立複檢價格<上限——模型自述失真也不會錯成交。
10. **state 實況**：15 檔 DOSSIER 完成、4 檔 FULL_A 判斷與規則相符、11 檔待同意、無持倉無異常——與 handoff 一致。

---

## 4. 過度工程化評估【驗證結論：整體不成立，3 處局部簡化機會】

**量化基線**：trader 程式 12,157 行＋測試 9,483 行（測試/程式 ≈ 0.78，健康區間）＋skill 腳本 1,584 行。

**逐項追蹤「複雜度 → 需求」的對應關係**（找不到對應的才算過度工程）：

| 機制 | 行數量級 | 對應的明確需求 | 判定 |
|---|---|---|---|
| 沙箱隔離（deny 規則、獨立根目錄、mtime 閘） | ~300 | 「價格最後看」是核心規則；且實測過 Read 可繞出沙箱 | 必要 |
| gate3 mtime 閘（作答先於開價格檔） | ~20 | 程式化證明「作答前沒看價格」 | 必要 |
| 公司行動三道覆核 | ~120 | 拆合股直接改股數與成本，錯一次就錯錢 | 必要 |
| xbrl_shares 多類股換算 | 613 | 封面股數停報的多類股公司（BRK.B、V、HSY…）市值會錯；有 P/E 合理性與 fail-closed 把關 | 邊界但合理（金錢系統） |
| peers.py 同業資料 | ~290 | P-01 第三方查證是等級上限條件 | 合理（但抓取量重，見下） |
| approvals 閘門＋儀表板 | ~400 | 使用者明示要求（M6「直接全部做完」） | 必要（需求驅動） |
| deep_quant.py | 182 | SKILL.md 自述「逐檔流程不使用」，trader 從不匯入 | **冗餘** |

**三處局部簡化機會**（都不是缺陷，是維護成本）：
1. **deep_quant.py**：182 行、逐檔流程不使用、trader 零引用。要嘛刪除，要嘛明確標為研究工具並排除在版控外。
2. **CORP_ACTION 的模型呼叫對 Alpaca 結構化事件多餘**：Alpaca 已提供 type/ratio/cash，verify() 本來就是拿它們交叉驗證模型輸出——對 Alpaca 來源事件可直接套用（數字同源，驗證恆過），模型只留給 8-K／跳空來源與「無公司行動」噪音過濾。可省掉大部分 CORP_ACTION 呼叫（每次 $0.04、11–13 次 API 往返）。屬重構，有行為改變風險，現設計可辯護。
3. **peers.py 抓取量**：每個 DOSSIER 對同 SIC2 同業抓 companyfacts（母體 503 檔、SIC2 群常 30–80 家）。有 20 小時磁碟快取，一季 15 檔內分攤，首次較重——監控即可，不需改。

**總結**：沒有「為複雜而複雜」的機制；全面 fail-closed（寧可拒單、記 MISSING_DATA、不猜比例）與「會用於金錢交易」的定位一致。**判定：不過度工程化。**

---

## 5. 修正優先序（依驗證後嚴重度）

| 優先 | 項目 | 工作量 | 驗證後的理由 |
|---|---|---|---|
| 1 | H1 SPY 股利鍵名 | 1 行＋1 測試 | 直接扭曲唯一能回答「能不能賺錢」的數字 |
| 2 | M1 FULL_B gate2 共用把關 | 共用函式＋呼叫點＋測試 | 決策日誌完整性；金錢防線已存在但只有一道 |
| 3 | M6 日誌 gate2 型別統一 | 小 | 月報誤判每個 PRICE 買進 |
| 4 | M2 gate3_result 回寫日誌 | 1 行 | 讓 PRICE 的 gate3 讀框架值而非自述 |
| 5 | M5 組合狀態補現金 | 小 | 建倉後 G8 才有完整 inputs |
| 6 | M4 J4 缺資料處理 | 小 | 規則書面 vs 程式的不一致 |
| 7 | L1 平均回購價（已知項，待使用者決定） | 小 | 價格隔離完整性 |
| 8 | L2 跳空比例獨立來源 | 中 | 公司行動錯帳的最後一道 |
| 9 | M3 政策決定（非 bug） | 使用者裁量 | PRICE BUY 是否要同意／紅隊 |
| 10 | L3 各項 | 小 | 邊角 |

---

## 6. 驗證方法與產物

- **自我驗證手段**：① 每項發現回到程式碼 grep 生產者／消費者全鏈路（H1 唯一生產者/消費者）；② 用 `test_runs/` 真實模型輸出做陽性對照（OTIS 決策日誌的 gate2=false 正確、gate3 自述與框架計算一致）；③ 為 M1 補「下單時價格複檢擋下」的緩衝測試，重新量定嚴重度；④ 對「過度工程化」逐機制追蹤需求對應與 LOC；⑤ 專案既有測試以乾淨 TMPDIR 回歸（656 passed），排除 6 個假失敗（TMPDIR 重用→沙箱把 FileExistsError 轉 PermissionError，與專案無關）。
- **測試檔**：`bug report/step/test_adversarial.py`，22 支全過——每支對應一項發現（紅燈＝重現），另含 2 支「緩衝／真實一致性」驗證。
- **執行**：`TMPDIR=<乾淨可寫目錄> …/envs/default/bin/python -m pytest "bug report/step/test_adversarial.py" -v`。
- **全程唯讀**：未修改 `trader/`、skill、routing、state、test_runs 下任何檔案；唯一寫入是 `bug report/step/`。
