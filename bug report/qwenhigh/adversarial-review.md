# 對抗式審查報告（Qoder／qwenhigh）

審查對象：`/Users/zongen/Downloads/claude code/berkshire`
日期：2026-10-17
邊界：**未改動專案任何檔案**。本目錄（`bug report/qwenhigh/`）以外的所有操作都是唯讀（Read／Grep／Glob、`sqlite3` 唯讀查詢、import 專案原始碼到行程記憶體中計算）。佐證程式只有三支，都在本目錄：`verify_money_logic.py`、`verify_money_logic2.py`、`verify_isolation.py`。

本報告的主張分三級可信度，逐條標註：

- `[已證]`：我親自讀過引用的那幾行，且用可重現的數字或實際資料檔覆核過。
- `[部分]`：程式碼我讀過了，但實際運作條件（例如某個呼叫路徑是否真被走到）未全部追完。
- `[未覆核]`：由 subagent 提出、給出行號但我本輪未逐行覆核，列在 §9 供抽查。

高嚴謹度的理由：這套系統的輸出會被拿去花真錢，而使用者自己定的規矩（`handoff.md` §2）是「沒問題就說沒有」。所以我先講結論，再把**我不背書的指控**單獨列在 §7——那一段和缺陷清單同等重要。

---

## 1. 一句話結論

**這套系統最重要的幾個宣稱，沒有一個對應到「會失敗的測試」。** 買進上限與賣出全清線是同一個數字（`auto_buy_cap ≡ IV_base = sell_review_above`），兩者之間沒有安全邊際；把折現率的 10% 下限寫錯方向，範例標的的上限漲 50.8%，而專案對這個數字的三處斷言在結構上不可能發現它（G16）。「模型作答前看不到價格」是整套方法論的獨立性前提，但隔離用的是全部 run 共用的同一個沙箱根，而那個根刻意不在 deny 清單內——我從 FULL_A 沙箱的相對路徑直接把同 run 的 `price=70.39` 讀了出來（Z8）。賣出側因此同時具備「一買進就該考慮全清」與「跌了什麼都不做」兩種相反指令，而輸出枚舉裡根本沒有 `SELL_ALL` 的通道。

再疊上四件事：基準報酬把 SPY 的股利吃掉了卻標成「含股利」（超額報酬系統性虛增，自我複核後定為「高」而非「致命」——它動的是證據不是部位）、回填歷史日期會用今天的帳本覆寫那天的淨值且校驗看不出來、閘門 3 的時點查核讀得到「未來」的問卷結果、以及唯一會擋住前面幾項的稽核報告有兩個查核恆等於 0。另有一條提示／schema 矛盾（照規則回答即凍結該檔交易）和一條拆股後買進上限不換算的帳務缺口。

**本報告經過一輪自我 adversarial 複核**（使用者要求的第二問）。結果：42 條裡 1 條整案收回（M10）、6 條降級或改因（Z5、G9、G10、G13、M4、Z7 的文件半段）、3 條在查證中變強（Z3、M8、G14），而**過度工程化的地方在我的修法建議裡、不在缺陷清單裡**——我砍掉了自己原先的四條建議，逐條理由記在 §11。先讀 §11 再讀 §2 可以省下不少誤導。

**關於現況的重要背景**（影響修補優先序，實查 `state/trader.db`）：`nav_daily`、`orders`、`fills` 三張表都是 0 列，registry 6 列的 status 全為 `MISSING_DATA`／`REJECT`／`TOO_HARD`（無 HOLDING）。所以 §4 裡所有撮合與帳本類缺陷目前都是**潛伏**的——它們要在第一次真實建倉之後才發作，而不是現在正在發生。反之，Z1／Z2／Z3／Z7／G13 這一類「分析與估值階段就會錯」的項，已經在這 6 筆決策裡發生過了；Z8、G17、G19 也已經發生過——`/Users/Shared/berkshire-sandbox-zongen/` 下實際有 50 個工作沙箱（21 個 DOSSIER、8 個 FULL_A、3 個 TRIAGE、3 個 THESIS，即 35 個「應當看不到價格」的工作；另有 10 個 CORP_ACTION、3 個 FULL_B、1 個 PRICE、1 個 HOLD_REVIEW），它們是真實執行過的結果，不是範例；而其中 3 份 `*_price.json` 就放在那 35 個沙箱的兄弟目錄裡，我打開了其中一份。



---

## 2. 缺陷分級表

| 編號 | severity | 一句話 | 主要證據 |
|---|---|---|---|
| Z1 | 致命 | 規則書寫了三遍的「歷史成長 ≤0 → 上限只能用 IV0×等級係數」在 `cap_calc.py` 完全沒實作，框架還用這個偏高的數字**覆寫模型自己更保守的數字** | `cap_calc.py:133-134`、`checks.py:156-191`、`deep.py:350-355` |
| Z2 | 致命 | `auto_buy_cap ≡ IV_base`，而 `IV_base` 的成長假設就是「最好的那個歷史數字」；等級係數（0.85／0.75）從不約束買價，A 級與 B 級同一條線 | `cap_calc.py:87,96,123,161`、`verify_money_logic2.py` V3 |
| Z3 | 致命 | CORP_ACTION 提示要模型把「無公司行動」的數字欄填 null，schema 卻把 `effective_date` 定義成必填日期 → 照規則答就驗證失敗 → `reject()` 無條件寫 `pause_trading=1`，該檔交易被凍結到人工解異常（接線已追完：`engine.py:310` → `corp_jobs.py:91-92` → `corp_actions.py:526`，且 `tests/test_engine.py:476-484` 把 `pause_trading=1` 寫成斷言） | `routing/prompts/CORP_ACTION.md:15`、`routing/output_schemas.json:167`、`jobs/validate.py:164-170`、`ledger/corp_actions.py:512-530,537-541` |
| Z4 | 致命 | 公司行動的「市值守恆」覆核用自己推出來的假想股價，**算出來恆等於 1**；比例容差 15% 又寬到能放過 4 拆 1 寫成 3.5 | `corp_actions.py:36,306,315-328` |
| Z5 | 高（自我複核後由「致命」降級） | SPY 基準股利**恆為 0**：每日流程讀 `a.get("cash")`，而 Alpaca 轉接層輸出的鍵是 `cash_dividend` 歸一化後的 `cash_per_share`；但報酬公式與儀表板／檢討報告都宣稱「SPY（含股利）」 | `pipelines/daily.py:294`、`data/alpaca.py:106-117`、`report/metrics.py:7,60`、`report/validation.py` render「SPY（含股利）」 |
| Z6 | 致命（潛伏） | 淨值回填用**今天的帳本**計算**過去的日期**再 upsert 回該日；寫出來的列仍滿足 `nav = cash + 應收 + 部位`，所以現有校驗永遠看不出來 | `ledger/book.py:137-159`、`scheduler.py:185-189`、`pipelines/daily.py:291-299` |
| Z7 | 致命（代碼半段）／規則自我矛盾（經濟半段） | **買進上限就是賣出檢視線同一個數**（`auto_buy_cap = min(P2′,IV_base)` 且實證 `≡ IV_base`，`sell_review_above = IV_base`），而規則對此給出三種相反處方（全清／要有更好用途才減碼／持有不加碼）——這一半是文件矛盾，不是 bug；另一半是 bug：輸出枚舉裡**沒有 `SELL_ALL` 通道**，PRICE 通道的 `SELL_REVIEW` 被框架硬編成 HOLD | Z2＋`checks.py:178`、`SKILL.md:112,130` vs `rulebook.md:370,376`、`routing/output_schemas.json:43`、`review_jobs.py:145-146` |
| Z8 | 致命 | **價格隔離在實作上是空的**：沙箱佈局是 `{root}/{run_id}/{ticker}/_{JOB}_{n}`，同一檔的所有工作（含被允許看價格的 FULL_B）是同層兄弟，而那個根刻意不在 deny 清單內（`check_sandbox` 要求沙箱不得落在封鎖目錄之下）。我從 FULL_A 沙箱用 `../_FULL_B_1/dossier/OTIS_price.json` 直接讀到現價（`price=70.39`）。deny 規則字串本身還是畸形的（`Read(//Users/…` 雙斜線），測試把畸形式寫成期望值，等於從未對著真正的匹配器驗過 | `jobs/isolation.py:18-52,76-79`、`jobs/sandbox.py:72-85`、`jobs/engine.py:59-68`、`tests/test_isolation.py:32`、`verify_isolation.py` W1／W2 |
| G1 | 高 | 價格隔離被自家資料包洩漏：`平均回購價 = 回購金額 ÷ 回購股數` 寫進標榜「本檔不含任何價格」的檔案，而這份檔案是閘門 3 問卷的作答上下文 | `build_dossier.py:47,94,96` |
| G2 | 高 | 閘門 2 只檢查隱含成長的 a 題；規則書的 b／c 題（不得高於歷史、不得高於保守 g0）由 `cap_calc` 算出後**在 `trader/` 內零讀者** | `cap_calc.py:181-185`、`review_jobs.py:296-300`、`writeback.py:531-535` |
| G3 | 高 | 金融股的「硬上限」就是規則書 P-19 明文排除的軟上限 P4′；P2′ 與 P4′ 都缺時退回「只供報告」的 P1 | `cap_calc.py:150-154`、`checks.py:177`、V2 |
| G4 | 高 | `absurd_price_line = 50 × (norm_eps or oe)`：E 為 0 時無聲退回未正常化的 oe，E ≤ 0 時線變 0 或負數 → 任何正股價都觸發 SELL_HALF；且這條線與 `sell_review_above` **只由 PRICE 快速通道寫入 registry**，完整分析重跑不更新 | `review_jobs.py:253-254`、`writeback.py:534-535,561-562`、V4 |
| G5 | 高 | 前瞻檢討報告的兩項核心查核（決策日後申報資料、skill 凍結雜湊）讀的欄位**從不寫入 decisions**，所以永遠報 0 筆／0 種——而原始資料（`fy_last_filed`、`max_filed_used`）其實就在同日的 `handoff_noprice.json` 裡 | `validation.py:70-71,75`、實測 `state/screens/2026-10-15/` |
| G6 | 高 | 同一欄位三種真值標準：`gate2_price_below_cap` 存「通過」字串，下單端認得、檢討報告端認不得 → 每一筆合法買進都被報成流程違規；值為「無上限」時兩邊都不報 | `writeback.py:545`、`sizing.py:43-49`、`validation.py:23,46-47,64`、V1 |
| G7 | 高（潛伏到第一次 PRICE 回寫） | 拆股後 registry 的每股上限有換算（`_scale_registry`）、`state/cap_inputs/{T}.json` 有換算（`_scale_cap`，含 `oe`/`norm_eps`），**訊號卡（`signals.data`）沒有**——`corp_actions.py` 內對 `signals` 零引用——而買進額度讀的是訊號卡那一份。附註：實查 6 張訊號卡的 `auto_buy_cap` 目前全是 null（尚無 PRICE 回寫），所以這條要在第一張有數字的卡之後才發作 | `corp_actions.py:32,349,362`、`sizing.py:305`（讀卡）vs `:477,528`（讀 registry）、實查 state DB |
| G8 | 高（僅在重跑／回填時發作） | 月凍結大型股本益比中位數只按「asof 所在月」取檔，**不看檔內 asof** → 只要凍結檔是在較晚的日子建立的，较早的決策日就會拿到未來的樣本。實測 `state/cache/pe_median/2026-10.json` 內 `asof=2026-10-16`，而同月已有 `state/screens/2026-10-15/`；當日首跑不受影響（`freeze_monthly` 首呼叫即以當日建立），會發作的是重跑與回填 | `data/pe_median.py:94-101,111-118`、實測 cache 與 screens |
| G9 | 中（由「高」降級；見 §11） | 觸發複查的價位來自 `adjustment="raw"` 的未換算收盤。自我複核後**修正了原因**：registry 那幾條比對（`review_due.py:111,117,119,122,125`）是**正確的**，因為 `_scale_registry` 把門檻一起除以 r，與拆股後的 raw 現價同口徑；錯的只有同一條序列自己跨拆股日比較的兩條——`:109`「單季自高點跌 ≥25%」與 `:114`「單日 ±4%」，2 拆 1 的次日必然各誤觸一次（−50% 的假跳空）。後果是額外的 THESIS 工作與訊號污染，不是直接金流 | `pipelines/review.py:132`、`review_due.py:105-115`、`corp_actions.py:32`（`PER_SHARE_REGISTRY`） |
| G10 | 中（由「高」降級；見 §11） | 賣方撮合無任何參與率／成交量上限，開盤觸價即全額出清；買方也是觸價全成。這是**紙上**撮合器，所以它不動真錢，但它產出的報酬數字是「何時可以上真錢」的唯一證據，把流動性約束抹掉等於把那個證據美化 | `ledger/broker.py:186-196,199-212` |
| G11 | 高 | LLM 開銷（實測 30 次呼叫 $12.80）不進 `cash_ledger` 也不進 `nav_daily` → 淨值、報酬、每筆決策的成本收益比較全部偏高 | `[部分]` 兩張表無 llm 相關 kind；實查 state DB |
| G12 | 高 | 文件偏離實況：`handoff.md` §0 稱正式 state「沒有持股或決策」，實際 `registry=6、decisions=6、approvals=15`，且日誌顯示 opus 已被放行執行；§0 的時间戳與 DB 相差約 9 小時 | 實查 `state/trader.db` |
| G13 | 中（由「高」降級；見 §11） | FULL_A 提示要求步 8「產業檢查」不過即停並填停止碼，但該工作 schema 的 `stopped_at_gate` 枚舉到 `G6`／`null` 為止、沒有產業檢查碼；另一支工作 FULL_B 卻有 `G7..G11`。自我複核後把後果講準：`null` 是合法值（`validate.py:191`），所以老實回答的代價是**停止原因記不下來、稽核報告把它印成「未停止」**（稽核缺口）；模型若自己發明 `G7`，才會走驗證失敗→重試→`on_invalid`，而 FULL_A 的 `on_invalid` 是 `_missing(..., "STEP0")`＝寫一筆 NO_ACTION／MISSING_DATA，**不凍結交易**（這點與 Z3 不同，我在原稿把兩者混為同族） | `routing/prompts/FULL_A.md:10`、`routing/output_schemas.json`（FULL_A vs FULL_B）、`validate.py:191`、`deep.py:290` |
| G14 | 高（潛伏） | registry 的 `weight`／`target_weight`／`cost`／`first_buy_date` 是**死欄位**：`cost`/`first_buy_date`/`weight` 只做「從舊列保留」（`writeback.py:249`）而舊列本身從未獲赋值，`target_weight` 在 `trader/` 內**沒有任何寫入端**（`daily.py:118` 寫的是 `build_plan.target_weight`，名稱與位置都不同）。實測 registry 6 列與 6 張訊號卡這幾欄全為 null。四個讀者因此失效：`review_due.py:123` 的加碼觸發 `(weight or 0) < (target_weight or 0)` **恆為 False → 持股低權重且低於上限時永遠不會機械性地觸發加碼檢查**、`review_jobs.py:305` 的同款條件、`已達目標權重，不加碼` 分支、`_held_under_year(None,…)` 恒回 True（保守當作未滿一年）→ SELL_HALF 永遠變 HOLD。**自我複核修正**：原把 `auto_buy_cap` 也列為死欄位是錯的——它在 `writeback.py:561`（registry）與 `:531`（訊號卡）都有寫入，只是只由 PRICE 通道寫 | `writeback.py:249,531,561`、`review_due.py:123`、`review_jobs.py:305`、`daily.py:116-118`、實查 state DB |
| G15 | 高 | 決策動作枚舉有四套不相容定義：`SKILL.md:113`（只用 BUY／WATCH／NO_ACTION）、`rulebook.md:432`（七種，含 SELL_ALL／TRIM／ADD／HOLD）、`decision_log_schema.json`（七值）、`signal_card_schema.json`（三值）；而 `output_schemas.json:43` 的 `proposed_action` 只有 `SELL_REVIEW／SELL_HALF`，沒有 SELL_ALL | 括號內各行 |
| G16 | 高 | **花錢那個數字沒有一個獨立期望值**：全部測試對 `auto_buy_cap`／`IV_base` 的斷言都是「拿 cap_calc 的輸出比 cap_calc 的輸出」。我實測變體：把 `abc = min(cap, iv_base)` 改成 `max` → 上限 89.53→101.07；把折現率下限 10% 拿掉 → 上限 89.53→**135.00（+50.8%）**、保守情境 67.06→89.06，而三處斷言全部繼續成立 | `verify_isolation.py` W3、`tests/test_checks.py:113,225-226`、`tests/test_review_jobs.py:101,243` |
| G17 | 高 | 價格隔離只有**入口**稽核：`assert_price_free` 全專案僅在 `stage_inputs` 被呼叫一次（模型跑之前）；模型寫進沙箱的檔案從不回首掃。`claude_code.py:107` 已經算出 `files_written`，但它在 `trader/` 內**沒有任何讀取端**。反證：我對實際跑完的 FULL_A 沙箱事後補跑這支掃描，它直接 `PriceLeakError`（被 `price_seen_before_answering=false` 這種布林欄触發）——接得上去的話每次必死 | `sandbox.py:126`、`claude_code.py:107`、`verify_isolation.py` W4 |
| G18 | 高 | 閘門 3 的狀態查核**沒有時點上界**：`_gate3_status` 的 SELECT 未限定 `decision_date <= asof`，還用 `ORDER BY decision_date DESC` 取最新 → 回填舊交易日時讀到「未來」的問卷結果。實測 asof=2026-10-05、唯一 asof 前的紀錄是 `gate3_pass=False`，函式回傳「通過」。同族：`writeback.latest_signal`（`:379-387`）與 `metrics.py:152` 都以 `created_at DESC` 取最新而不設界 | `verify_isolation.py` W5、`review_jobs.py:94-97` vs `sizing.py:221,239`、`validation.py:35` |
| G19 | 高 | 驗證重試**繞過預算與併發**：`_on_done` 在輸出非法時直接 `_submit(spec, tier)`，既不過 `_budget_exhausted`，也不過 `_busy[tier] >= concurrency`（這兩道都只在 `_launch` 裡）。預算判斷同時是「本次 run 已花的呼叫數 ≥ 上限」，所以重試那一次是先花再算 | `engine.py:299-308` vs `:110-118,211-218,252-253` |
| M1 | 中 | 保守 g0 = `min(hist) × 0.75`，hist 為負時這個乘法把衰退**變小**（−3% → −2.25%），方向與「保守」相反 | `cap_calc.py:81-83`、F3 |
| M2 | 中 | `ivc_at(r, oe_)` 在呼叫端給了 `iv_cons` 時直接丟掉 `oe_` 參數 → 金融股結論測試的「E×0.9」那條腿恆等於 P1 報告欄，測了個不存在的東西 | `cap_calc.py:116-122,170-172` |
| M3 | 中 | 結論測試的**觸發**用 `auto_buy_cap`（可能被 IV_base 夾過），**內容**測 P2′ → 兩者分叉時會「該測的不測、不該測的亂測」；且只在現價低於上限 5% 內才跑 | `cap_calc.py:161,168-175` |
| M4 | 低（由「中」降級；見 §11） | 閘門 1 結構上是恆真式：`screen.py:178` 把交接檔每列的 `screen_decision` 重寫成「PASS-困境 或 PASS」，`screen_quant.py:99` 算出的 `DELETE`／`PASS-資料不足` 到不了下游；而 `_is_delete(None)` 也回傳 False。我刻意去找**更糟的版本**（DELETE 被改寫成 PASS 而混進短名單）——查無：`screen.py:106` 在排名前就 `& ~deleted`，`:143-145` 只給 core 成員編號、其餘 `core_rank` 為 NaN → `shortlist` 為 False，所以刪除類根本不會出現在 `core_shortlist`。所以這條只是一個「永遠不會擋住任何东西的防線」被當成一道閘門陳述，屬文件與測試誠實度問題，不是漏洞 | `screen.py:106,143-145,178`、`screen_quant.py:99`、`checks.py:196-200`、實測 `screen_handoff.json`（15 筆全 PASS） |
| M5 | 中 | 股票池 503 列但只有 500 個 CIK（GOOG/GOOGL、FOX/FOXA、NWS/NWSA 各佔兩列）；FOX 25 名、FOXA 28 名同落核心道，凍結樣本 `n_norm=478` 重複計同一公司 | 實測 `state/cache/universe/sp500.csv`、`state/screens/2026-10-15/screen.csv` |
| M6 | 中 | 缺收盤價時以平均成本估 NAV（已在 `missing_close` 記名，但虧損當日會被藏起來，且 `equity` 分母偏高會放大加碼額度） | `ledger/book.py:117-134` |
| M7 | 中 | 同日同一公司的兩種公司行動（拆股＋現金股利）會被程式端去重鍵吞掉一筆：DB 的 UNIQUE 含 `action_type`（`db.py:46`），而 `_ca_exists(conn, T, eff)`（`:70-73`，逐行讀過）只用 (ticker, effective_date)，`:447` 與 `:529` 兩處都用它早退 | `db.py:44-46`、`corp_actions.py:70-73,447,529` |
| M8 | 中 | 被拒收的公司行動列（`anomaly`／`pending_human`）仍佔用去重鍵：`reject()` 在 `:529` 以 `_ca_exists` 判斷後插入 status=anomaly 的列，之後人工修正重跑時 `:447` 會因同 (ticker, effective_date) 已存在而回 `{"status": "duplicate"}` 早退 → 部位永遠停在拆股前股數。`_ca_exists` 定義已逐行讀過，`[部分]` 標記解除 | `corp_actions.py:70-73,447,512-530` |
| M9 | 中 | `SELL_REVIEW` 這條賣出路徑在框架裡被寫死成 HOLD（「賣出檢視待組合比較（M5）」）——四條賣出路徑中唯一由價格驅動的那條，目前沒有實作 | `review_jobs.py:145-146` |
| M10 | **收回**（移到 §7） | 原寫法：「`gate3_check.py` 對空值是跳過而非拒絕」暗示自證時間戳可以被留空繞過。逐行重讀後這是錯的：`gate3_check.py:26-28` 用 `ans.get('price_seen_before_answering') is not False` → 未填、填 true、填字串 `"false"` 一律判失敗；`:29-31` 只在 `price_file_opened_at` **非空**時才比較時序。真正的殘留觀察只有一條：提示（`FULL_A.md:11`）指示該欄留空，所以那個時間戳永遠不存在，比對形同虛設——但這是「自證欄位沒有外部證據」的觀察，已折進 Z8，不是獨立缺陷 | `scripts/gate3_check.py:26-31`、`routing/prompts/FULL_A.md:11` |
| M11 | 中 | 首批權重 50% 與 70%、隱含成長上限 8% 與 10% 在規則檔的不同章節各寫一套，`G_IMPL_MAX=0.10` 寫死不可輸入 | `[未覆核]`（subagent SK-9） |
| M12 | 中 | CSV 匯出未做公式注入跳脫，模型產出的自由文字欄位匯入關聯式試圖表軟體可觸發公式執行 | `[未覆核]`（subagent OP-5） |
| M13 | 中（潛伏） | 同日重跑會重複下單：`orders`／`fills`／`cash_ledger` 無 UNIQUE，`plan_buys` 有「已有未成交買單即跳過」守衛（`:294-295`），`plan_tranches` 與 `plan_sells` 沒有；而 `_consume` 在下單當下就扣 `tranches_left` | `db.py`（無 UNIQUE）、`sizing.py:294-295,453-516` `[部分]`（未逐行讀完整 `plan_tranches`） |
| M14 | 中 | deny 清單被**兩處各自重建**、內容不同：`engine._sandbox_root` 用 `deny_roots(extra_deny, paths.state)`，而 `router.build_runner` 用 `deny_roots(extra_deny)`——不帶 state 目錄。state 只在預設落於家目錄／專案內、或 `TRADER_STATE_DIR` 有設時才被封；用 `--state-dir` 指到家目錄外（外接磁碟、`/Users/Shared`）就不封，而那是承載 `trader.db`（持股、成本、現價）的目錄 | `engine.py:68`、`router.py:22`、`isolation.py:33-52`、`paths.py:77` |
| M15 | 中 | opus 同意閘門的 `PASS` 含 `done`：同意是記在 (run_id, ticker) 這一列而不是記在每個工作上，所以 `close_finished` 把列收 `done` 之後，同 run 同檔若再冒出新的 strong 工作（新的 `job_id` 序號），閘門直接放行而不重新徵求同意。今天被 `add_job` 的 job_id 去重擋住，所以是**潛伏**；但它把「一次同意＝該 run 該檔全部強模型花費」寫成了語意 | `approvals.py:19,56-61,194-203`、`engine.py:79-97,246-248` |

---

## 3. 致命項詳述

（編號 Z1–Z8 是穩定 ID，跨輪修訂不改號；自我複核後 Z5 的分級已降為「高」、Z7 拆成代碼半段與規則半段，但它們仍留在本節，因為讀者是用編號在 §2 與 §4／§7／§11 之間跳的。）

### Z1 規則要求的 IV0 上限從未被實作，而框架用它覆寫模型 `[已證]`

規則把這件事講了四遍，措辭一致：

- `berkshire-deep-merged/SKILL.md:101`：「歷史成長 ≤0 → 上限只能用 IV0（E ÷ r_d）× 等級係數」
- `references/rulebook.md:271,276`、`references/procedure/05_valuation_caps.md:38`、`references/rule_index/02_valuation.md:250`

程式碼只做了這一句（`scripts/cap_calc.py:133-134`）：

```python
if hist and min(hist) <= 0:
    notes.append("歷史成長 ≤0：反方必答（b）（c），答不出 → 觀察（references/05 §4）")
```

`iv0 = oe / r_d` 在 `:89` 算出、`:99` 輸出，之後**全檔不再被任何上限運算引用**。硬上限仍然是 `min(P2′, IV_base)`，而 `IV_base` 的終值成長被硬寫成 `0.03`（`:96`）——一家連續兩期衰退的公司，在買進上限裡被假設第 11 年起永久 3% 成長。

可重現數字（`verify_money_logic.py` F1；oe=10、r_d=10%、B 級、A 級同值）：

| g10 | g5 | 規則書應有上限 | 程式 auto_buy_cap | 偏高 |
|---|---|---|---|---|
| −2% | +1% | 75.00 | 127.09 | +69% |
| 0% | 0% | 75.00 | 118.18 | +58% |
| −2% | −5% | 75.00 | 102.29 | +36% |
| −4% | −1% | 75.00 | 158.39 | +111% |

惡化它的第二件事：`trader/trader/analysis/checks.py:156-191` 的 `apply_cap_override()`，由 `jobs/deep.py:350-355` 在比對出差異後呼叫，把訊號卡的 `auto_buy_cap`／`buy_price_cap`／`sell_review_above`／`value_per_share.*` 一律換成 `cap_calc` 的數字。也就是**模型的數字即使較保守也會被這個偏高的值蓋掉**。設計動機（「不信模型的數字」）是對的，但在這個規則缺失之下，它把唯一殘留的保守性也移除了。

最小修正方向（不在本輪執行）：`calc()` 內於 `hist and min(hist) <= 0` 時把 `cap` 與 `abc` 夾到 `k * iv0`；並讓 `iv_base is None` 時走 MISSING_DATA 而不是靜默退回 `cap`（`:161` 的 `if iv_base else cap`）。

### Z2 買進上限就是公平價本身，沒有安全邊際 `[已證]`

這是 Z1 的姊姊項，但獨立成立，必須單列。

- `cap_calc.py:87`：`g0b = x.get("base_g0", min(max(hist), 0.10) ...)` — 基準情境的成長取**兩期歷史裡最好的那個**（上限 10%）。
- `cap_calc.py:96`：`iv_base = pv(oe, g0b, r_d, 0.03)`。
- `cap_calc.py:123`：`P1 = k * iv_cons`，其中 `k` 是等級係數 0.85／0.75／0.40，而 `:149` 把 P1、P3 的 key 直接命名為 `P1_report_only`／`P3_report_only`；`:153` 的 `min` 只看 P2′（或 P4′／P1 fallback）。
- `cap_calc.py:161`：`abc = min(cap, iv_base)`。

於是有一條可以證明的恆等式：當 `g_term_i`（`:126`，= min(3%, 30 年期 −1pp)）與 IV_base 的終值 0.03 相同，且 `g0b = min(max(hist),10%) ≤ G_IMPL_MAX = 0.10 = P2′ 的 g0`，而 `pv` 對 g0 單調 → **P2′ ≥ IV_base 恆成立 → `auto_buy_cap ≡ IV_base`**。V3 的網格逐行證實（每一列的 auto_buy_cap 都精確等於 IV_base）：

```
 g10     g5    基準g0   IV_base  auto_buy_cap
+0.02  +0.02   +0.020    136.73     136.73
+0.06  +0.06   +0.060    171.91     171.91
+0.12  +0.12   +0.100    202.13     202.13     ← 與 +0.10 同值，因 10% 是 cap
```

三個後果：

1. **A 級與 B 級的買進上限完全相同**（實測同為 179.05）。確定性等級只在報告欄出现，不在任何會下單的數字裡出现。
2. 買在上限 = 以 r_d = 10% 折現、假設未來 10 年維持**公司歷史上最好的一段成長**、第 11 年起永久 3% 的公平價。安全邊際 = 0（`sizing.py:313` 的 `buy_limit()` 只再扣一個 `$0.0001`，`cap*0.9999`）。而這條閘門在 `procedure/12` 裡被叫做「安全邊際」。
3. 上限對「最好的歷史數字」是 1:1 線性反應：歷史越漂亮，能付的價越高，這方向本身沒有錯；錯的是**沒有任何一題問「我付的價格低於公平價多少」**。

注意 `handoff.md` 把「即使 B 級，上限仍是 IV_base，結論穩定」寫成合併版的刻意設計，而 `rulebook.md:278` 同一節又明寫「**達到上限即可買，不必等更便宜（EVID-011）**」。兩句合起來看，這不是漏乘 `k`，而是**刻意把買進點放在公平價上**。那問題就從「實作漏掉」升級為規則層面的自我矛盾：同一份規則書 `:263` 說上限是 P2′（「唯一硬上限」）、`:265` 把 P1 標為「安全邊際欄…不進上限」、`:278` 又說 P4′ 寬鬆而 P1–P3 不過就不買（把「不進上限」的欄位變成否決條件），而 `procedure/12_auto_buy_gates.md` 的閘門 2 那一列，左邊名字寫「安全邊際」、右邊理由寫「堅持買價要有安全邊際」，中間的定義卻是 `現價 < min(P2′, IV_base)`。讀規則的人會以為有 15%／25% 的邊際，實際是 0.01%（`$0.0001` 級別的 `cap×0.9999`）。

我對「該不該有邊際」沒有意見——那是投資哲學選擇，而且「不必等更便宜」確實是巴菲特式口徑之一。我有意見的是**命名**：一個把安全邊際設為零的閘門不該叫安全邊際，因為它會讓後續所有的「已有安全邊項保護」論證失效，包括 `verify` 報告與儀表板上那些讀起來像風控控制的欄位。

### Z3 照提示回答就會被凍結交易 `[已證]`

`routing/prompts/CORP_ACTION.md:15` 指示：

> 事件來自 8-K，且原文沒有改變股數、每股現金、代號的公司行動（例如新聞稿、章程修改、一般公告）→ action_type＝無公司行動，needs_human＝false，**其餘數字欄位為 null**。

`routing/output_schemas.json:167`：`"effective_date": "YYYY-MM-DD（除權／生效日）"`。`jobs/validate.py:164-170` 把括號剝掉後 base = `YYYY-MM-DD`，`val is None` 只有在 `base.endswith("或 null")` 時才放行 → **null 直接報錯**。

失敗的下游不是「重答一次」而已：重試用盡後 `on_invalid` → `ledger/corp_actions.py:537-541` → `reject()`，而 `reject()` 在 `:526-529` **无條件**插入 `anomalies(..., pause_trading, 1)`。`book.paused()` 讓 `broker.place_order`（`:60`）拒單、`match_orders`（`:232,239-240`）停撮合。

後果：一篇普通的新聞稿 8-K，只要模型老實回答「無公司行動」，這檔持股就開始不能買賣，直到人工解異常。模型若學會亂填日期以避免報錯，就會污染公司行動帳——這是提示與 schema 的責任歸屬錯誤，模型會被迫在兩個壞答案之間選。

測試為什麼擋不住：`tests/test_corp_actions.py` 用 `result()` helper 恆填 `effective_date=ASOF`，直接呼叫 verify／apply，**繞過 validator**。

最小修正方向：schema 改成 `"YYYY-MM-DD 或 null"`（`validate.py:164` 已支援這個寫法），或提示改為「無公司行動時 `effective_date` 填事件申報日」。前者一行、且不需要同步改驗證器。

### Z4 「市值守恆」覆核恆等於通過 `[已證]`

`ledger/corp_actions.py:315-328` 聲稱檢查市值守恆：

```python
pre = qty * pb
post = _whole(qty * r) * (pb / r) + cash_frac + qty * (cps or 0.0)
if pre > 0 and abs(post / pre - 1.0) > TOL_MV:   # TOL_MV = 0.02
```

`post` 里的「調整後股價」用的是 `pb / r`——**由待驗的比例自己推出來的假想價**，不是實際觀察到的 `pa`。代回去：`_whole(qty*r)*(pb/r) ≈ qty*pb = pre`，所以 `post/pre ≈ 1` 恆成立，容差 2% 永遠過。這是同義反覆，不是覆核。它甚至讓 `pa` 只在零股現金那一項出現。

同一區塊的另一個覆核 `:306`：

```python
elif abs((pb / pa) / r - 1.0) > TOL_PRICE_RATIO:   # TOL_PRICE_RATIO = 0.15
```

15% 的容差對「比例」這種離散、整數、可精確查證的量過於寬鬆：真實 4 拆 1（pb/pa = 4）若模型抄成 3.5，`|4/3.5 − 1| = 14.29% < 15%` → 過關。入帳後：1,000 股變 3,500 股（應為 4,000）、`avg_cost` 被除以 3.5、registry 與 cap_inputs 的每股欄位（`PER_SHARE_REGISTRY`、`PER_SHARE_CAP`）全部除以 3.5 → 市值憑空少 12.5%，而**每股買進上限被高估 14.3%**，之後的買進閘門用的就是這個錯的上限。

注意：與 Alpaca 比對那一關（`:293-296`，容差 0.1%）是能擋住的，但 `:294` 是 `if ar is not None and ar > 0` ——Alpaca 沒回比例時整關跳過，此時 Z4 是唯一防線，而它是壞的。

最小修正方向：`post` 改用觀察值 `qty*r*pa`（真的市值守恆）；`r` 加整數／常見比例白名單或把 TOL_PRICE_RATIO 降到 1%。

### Z5 大盤基準把股利吃掉了，卻標成「含股利」 `[已證]`

`pipelines/daily.py:294`：

```python
bench_div = sum(a.get("cash") or 0.0 for a in alpaca_actions ...)
```

`data/alpaca.py:106-117` 歸一化後的字典，鍵是 `cash_per_share`（還有 `ratio_new_per_old`、`payable_date` 等），**沒有 `cash` 這個鍵**。所以每一筆都是 `None or 0.0`，`bench_div` 恆為 `0.0`，寫進 `nav_daily.data.benchmark_dividend`。

下游：`report/metrics.py:7` 宣稱「股利取自 nav_daily.data.benchmark_dividend」，`:60` 把它讀進 `I_t = I_{t-1}×(close_t+div_t)/close_{t-1}` → `spy_return` 退化成**純價格指數報酬**。而 `report/validation.py` 的輸出標題寫「SPY（含股利）」、儀表板同口徑。

這是方向單一的偏誤：自己的組合把股利計進現金（`dividend` 進 `cash_ledger`），基準不算股利。SPY 長期殖利率約 1.1–1.3%，所以**任何「跑贏大盤」的結論都被灌水約每年 1 個百分點**，而且口徑不一致這件事被標籤主動掩蓋了。這一項不需要任何極端情境就會發生，且它正好落在使用者最依賴的那個數字（超額報酬）上。

**自我複核後的分級理由（致命 → 高）**：這條改的是**證據**不是**部位**——它不會多買一股，也不會多賣一股；它讓「超額報酬」這個數字系統性偏高約每年 1 個百分點，而那個數字是使用者判斷「這套方法有没有效、要不要加大」的唯一依據。以「會直接花錯錢」的標準它不夠致命，以「會讓你相信一個錯的結論」的標準它是最高級別的非交易缺陷。我把它留在 Z 區而不是搬到 G 區，是因為它的修法只有一個鍵名，而它的錯誤方向單一且無聲。

最小修正方向：`:294` 改 `a.get("cash_per_share")`；並在 `metrics.py` 加一條「宣稱含股利時，檢查基準股利欄位是否曾有非零值」的自我檢查，避免同類鍵名漂移再次無聲發生。

### Z6 淨值回填：用今天的帳本寫歷史日期 `[已證]`（現況潛伏）

`ledger/book.py:137-159` 的 `record_nav()` 從**目前**的 `cash(conn)`、`receivables(conn)`、`positions(conn)` 算出 nav，再 `write_row(conn, "nav_daily", row, ["date"])`（依 date upsert）。它接受任意歷史 `date` 作為標籤，但沒有任何機制把帳本還原到那天的狀態。

`pipelines/daily.py:291` 的註解只想到「同日重跑為 upsert」（那是對的），但 `scheduler.py:185-189` 的排程邏輯是：

```python
for d in cal.trading_days(start, last):
    if not self._daily_done(conn, d):
        tasks.append((f"daily:{d}", ["daily", "--date", d]))
```

——**無條件補跑所有沒標記完成的過去交易日**。於是補一個錯過的日期，就會把「今天的持股 × 那天的收盤」寫成那天的淨值。`cli.py` 也容許手動指定過去日期。

危險之處在於它不會被任何現有的自檢抓到：那筆列仍然滿足 `nav = cash + receivables + positions_value`，`benchmark_close` 也真的是那天的收盤，只有部位組成的時間戳是錯的。淨值曲線、TWR、`return`、`excess_pp`、以及 `validation.py` 對大盤的整節比較，會一起走樣而表面無瑕。

這一項現在還沒造成損失（`nav_daily` 0 列），但它決定了「這套系統的歷史績效能不能被相信」。**只要開過一次自動排程、且中間有錯過的日子，之後所有的回看就都不可用了。**

最小修正方向：`run_daily` 在 `date != 今日` 時改走「重放」路徑（依該日之前的 fills 重建現金與部位），或在無法重建時拒絕寫 NAV 並在 `runs` 表留一個明確的 `backfill_unsupported` 記號。前者工作量大，我建議先做後者。

### Z7 買進上限＝賣出檢視線，且賣出側沒有輸出通道 `[已證]`

把 Z2 的恆等式和賣出規則接起來看：

- `cap_calc.py:161` → `auto_buy_cap ≡ IV_base`（Z2 已用網格證明）
- `checks.py:178` → `sell_review_above = cap["IV_base"]`
- `SKILL.md:112` → 「`sell_review_above`＝`IV_base`（只觸發賣出檢視）」

所以**買進允許的最高價，正好是把持股放進「賣出檢視」区間的那個價**。設計動機（`cap_calc.py:157-158` 的註解）其實是為了解決這件事——「買在 IV_base 之上等於一買進就觸發賣出檢視」——但它解決的是「超過」，沒有處理「等於」：`buy_limit()` 讓成交價可以貼到 `cap × 0.9999`，也就是貼著 `IV_base` 的下緣。每一次成功建倉，都剛好把部位放在需要立刻開始考慮賣出的位置上。

然後規則對「價格 ≥ IV_base」給了三種互斥處方，沒有宣告優先序：

| 出處 | 對同一狀態（價格 ≥ IV_base，普通／良好企業）的指令 |
|---|---|
| `SKILL.md:130` 賣出路徑 (2) | 「**且有明顯更好的用途（G8 比較）** → 減碼或換股」 |
| `rulebook.md:370`（3b） | 「只有 V，普通／良好企業，價格 ≥ IV_base → **全清**」 |
| `rulebook.md:376`（3c 末行） | 「IV_base ≤ 價格 < IV_base×1.3 → **持有、不加碼**」 |

模型三份都要讀（`SKILL.md:10` 要模型去 grep `rule_index`；`procedure/12` 指定用閘門表）。這是部位從 0 到 15% 的差別，不是一個可以用「酌情」收攏的措辭差異。

最後，即使模型選了最激進的那個答案也發不出去：`routing/output_schemas.json:43` 的 `proposed_action` 枚舉只有 `BUY_CANDIDATE | HOLD | SELL_REVIEW | SELL_HALF | NO_CHANGE`，**沒有 `SELL_ALL`**；而 `review_jobs.py:145-146` 把 `SELL_REVIEW` 映射成 `HOLD`（理由欄寫「賣出檢視待組合比較（M5）」）。論點證偽的全清另有路徑（`ThesisHandler.next_jobs` 升級 DOSSIER），所以 Z7 的準確說法是：**四條賣出路徑中由價格驅動的那兩條 (2) 和 (3)，目前一條沒有輸出格式、一條被框架改寫成 HOLD。**

---

### Z8 「看不到價格」這件事在實作上並不存在 `[已證]`

整套 skill 的地基是一個可檢驗的宣稱（`jobs/sandbox.py:3`）：「除 FULL_B 外，沙箱內不得出現任何價格」，以及它背後的設計意圖——G1–G6、閘門 3 問卷、能力圈判斷都必須在不知道現價的前提下完成，否則「這家公司值多少」會被「現在市場標多少」反向污染。這是一個**關於模型能看到什麼的斷言**，所以它的強度取決於唯一的執行者：`--settings` 的 deny 規則。

三道防線實際上只有兩道半，而且都靠同一個假設：

1. `stage_inputs`（`sandbox.py:88-127`）：只複製白名單檔、把 `*_price.json` 檔名直接拒絕、JSON 先 `scrub_prices` 再落地，最後 `assert_price_free` 掃沙箱。這道防線管的是**框架自己塞進去的東西**。
2. `isolation.check_sandbox`（`isolation.py:59-64`）：**要求沙箱不得位於任何封鎖目錄之下**——否則模型連自己的輸入都讀不到。這條正當的要求產生了後果：沙箱根 `/Users/Shared/berkshire-sandbox-{user}`（`:26-30`）天生不在 deny 清單裡。
3. `settings_json`（`:76-79`）對 `deny_roots()` 的每一項產生 `Read/Edit/Write` 三條規則。清單內容是家目錄、專案根、`/private`、`/tmp`、`/var`、`/Volumes`、`/opt`（W1 實測 9 個根、27 條規則）。

於是：`{root}/{run_id}/{ticker}/_{job}_{n}` 是同一檔股票的兄弟目錄。我對實際沙箱樹做了枚舉（唯讀）：根下 50 個工作沙箱，`*_price.json` 只有 3 份，全部在 `analyze-2026-10-16/OTIS/_FULL_B_{1,2,9}/dossier/` 裡——也就是「被允許看價格」的那支工作的沙箱。洩漏面不是有人把價格檔放錯地方，而是**同層的兄弟沙箱彼此可達**。對真實沙箱補跑檢查（W2）：

```
FULL_A 沙箱內的價格檔（stage_inputs 的保證）：無
以 cwd=…/analyze-2026-10-16/OTIS/_FULL_A_1 相對路徑 ../_FULL_B_1/dossier/OTIS_price.json 打開 → 成功
內容：price=70.39 price_date=2026-10-15 t30=0.0552
該檔自註：價格隔離：閘門 3 問卷與 G1–G6 存檔後才可開啟本檔
```

被 deny 覆蓋的情形：`False`（該路徑不在任何封鎖目錄之下）。這正是 `isolation.py:3` 作者自己寫的實測結論——「`claude -p` 的 Read／Glob／Grep 預設可讀沙箱外任何檔案（實測）」——他們因為知道這件事才建了 deny 清單，而清單少了自己的根。跨 run 也一樣：`review-2026-10-16-watch/OTIS/_PRICE_1`、任何別的股票的同名檔案，全在同一個未封鎖的樹裡。

第二條獨立缺陷在規則字串的成形方式：

```python
rules = [f"{t}(/{d.as_posix()}/**)" for d in denies for t in _TOOLS]
```

`d` 來自 `deny_roots()`，已是解析後的**絕對路徑**，`as_posix()` 自帶前導 `/`，f-string 又補了一個 → 實測輸出 `Read(//Users/zongen/**)`。`tests/test_isolation.py:32` 把這個畸形式原樣寫進期望值（`assert "Read(//Users/someone/**)" in rules`），而該檔標題註明「不呼叫 claude」。也就是說：**這套隔離唯一的驗證方式是拿自己的字串比自己的字串**，雙斜線是否會被真實匹配器正規化，專案內沒有人知道，我也沒有跑真實 CLI（會花訂閱額度，且不在審查範圍）。若匹配器不做正規化，後果不是「FULL_A 偷看到價格」，而是**整個檔案隔離層是空轉的**——包含「不得讀 `.env`、不得讀 `state/trader.db`」在內的所有約束。

我不據此宣稱模型「一定」讀過價格；我宣稱的是：**這條邊界目前沒有被任何機制守住，且框架裡唯一的偵測器（`assert_price_free`）只看自己那一格、只在輸入端跑一次（見 G17）**。以一個要用真錢的系統來說，「提示詞裡要求模型不去看」與「模型看不到」不是同一件事；而 `price_seen_before_answering` 那個布林欄位正是把前者偽裝成後者的裝置——它是模型自己填的自證宣告，`gate3_check.py` 只檢查「它等於 false」，不檢查任何外部證據（自我複核後我把原本的 M10 收回，見 §11；但「自證而非他證」這個性質本身仍在）。

修法方向（自我複核後簡化過；我不再建議原先那套）：最小且足夠的一步是在 `create_sandbox` 之後，把**同層兄弟沙箱目錄**逐一加進該工作的 deny 清單（`deny_roots(extra=[...])` 已經支援，且不觸犯 `check_sandbox`「沙箱不得在封鎖根之下」——被 deny 的是兄弟，不是自己）。這一步就把「同檔跨工作可達」關掉。剩下的「跨 run／跨股票可達」要真正關掉必須改根目錄佈局（每 run 一個根，或每工作一個根），那是結構調整、不是補規則；我會把它排在兄弟目錄之後、但承認它才是完整解。

我**撤銷**原先另兩條建議：（a）「把沙箱根也納入 deny＋對自己開 allow 例外」——Claude Code 的 deny 優先於 allow 與否我在本專案內無法驗證，拿一個未驗證的語意去設計修法本身就是過度工程；（b）「跑完再掃一次沙箱數值」——W4 實測證明這條會誤報：模型在 `gate3_answers.json` 寫的 `price_seen_before_answering=false`、`price_file_opened_at=""` 這類布林／空值就會讓掃描器 `PriceLeakError`，接上去等於每次必死（也正是 G17 那條觀察的來源）。

---

## 4. 高嚴重度項（逐條要點）

**G1 價格隔離從資料包內部漏掉** `[已證]`
`build_dossier.py:94` 以粗體宣告「**本檔不含任何價格、市值、本益比。**」，但 `:47` 計算 `R["平均回購價"] = T.buyback / T.buyback_shares`，`:96` 把整張衍生比率表 `fmt(R)` 寫進同一檔。回購金額 ÷ 回購股數就是該檔自己的股票 10 年真實成交均價。這份 dossier 正是 DOSSIER／FULL_A 的輸入，也就是 11 題能力圈問卷（含「你願不願意以此價格買進並持有」這一類）的作答上下文。另外 `PaymentsForRepurchaseOfCommonStock`（含優先股與 ASR 預付）除以 `StockRepurchasedDuringPeriodShares` 未做拆股追溯調整，跨拆股即是垃圾數字。隔離機制（`jobs/sandbox.py` 的 `is_price_key`／`scrub_prices`／`assert_price_free`）是**依鍵名與檔名**判斷的，對「用別的欄名表達同一個價格」沒有任何防禦——這條就是那個缺口。

**G2 三題只剩一題有程式把關** `[已證]`
`cap_calc.py:181-185` 輸出 `g_impl_checks = {a_le_10pct_gate, b_le_hist5y(_half), c_le_cons_g0}`。`grep -rn g_impl_checks trader/` 在框架碼中**零參考點**（只出现在 `bug report/` 底下別人的 prompt fixture）。閘門 2 的條件只有 `review_jobs.py:298`：`price < auto and grade_ok and not flips`。實測反例（V5）：g10=−2%／g5=+1%、現價 118 → `auto_buy_cap=127.09`、`c_le_cons_g0 = False`（隱含成長 −0.02% 高於保守 g0 −1.50%），框架 gate2 仍然判「通過」。規則書把 b／c 當做「付了歷史沒給過的成長」的最後防線，這條線目前靠模型自律。

**G3 軟上限變成硬上限** `[已證]`
`cap_calc.py:150` 註解自己寫「P4′ 為軟上限（規則書 P-19）——不進硬上限」，但 `:153` `capc = [P2] or [P4] or [P1]` 讓金融股（P2′ 因 `fin` 而為 None）的硬上限正好等於 P4′。實測（V2）：financial=true → cap = auto_buy_cap = 165.0 = P4′；再拿掉所有 P/E 中位 → cap = 109.85 = **P1 報告欄**。`checks.py:177` 接著把它寫進 `buy_price_cap`，註解還是「P2′ 硬上限」。金融股另外還少了 `rule_index/02_valuation.md` 要求的 P/B 帶檢查（cap_calc 內無對應程式）。

**G4 賣出線可為 0／負，且只在一條通道上更新** `[已證]`
`review_jobs.py:253-254`：`e = x.get("norm_eps") or x["oe"]`，`cap["absurd_price_line"] = round(50 * float(e), 4)`，無正數檢查。norm_eps 恰為 0.0 時 falsy 退回 oe；oe ≤ 0 → 線 ≤ 0 → 任何正股價都在線之上 → `_map_action("SELL_HALF", ...)` → TRIM（建倉未滿一年則 HOLD）。另外，`absurd_price_line` 與 `sell_review_above` **只在 `write_price_result()` 裡寫進 registry**（`writeback.py:534-535,561-562`）；完整分析（`write_full_result`）、論點檢查（`write_thesis_result`）、持股複查（`write_hold_review_result`）都不更新它們。所以一份讓 E 下修 30% 的 10-Q，只要那次走的是完整分析而不是價格快速通道，登錄表上的兩條賣出線就還是舊的，而 `review_due.py:121-122` 讀的正是登錄表舊值。

**G5 兩項稽核是空殼** `[已證]`
`report/validation.py:70-71` 與 `:75` 從決策日誌讀 `fy_last_filed`、`skill_hashes`。`grep` 全框架：這兩個鍵只有讀取端，沒有任何寫入端（`writeback.py` 的 `_blank_from_fields()` 會把它們填成 schema 預設的空字串）。因此報告 §1 無條件印出「使用決策日之後申報的資料：0 筆」與「skill 凍結雜湊：0 種」。諷刺處：真正的時點證據就在同一次執行的 `state/screens/2026-10-15/handoff_noprice.json`（實測含 `fy_last_filed`、`max_filed_used`、`dq_note`、`pit_checked`），而且 `jobs/sandbox.py` 的 `scrub_prices` 會把它打包進 FULL_A 的提示（`tests/test_engine.py:410` 斷言 `screen_decision` 在提示裡）——**模型看得到、框架記得到，唯獨稽核者看不到**。一條會讓人誤以為「已驗證無前視」的 0，比不報更壞。

**G6 真值標準三分** `[已證]`
見 V1 輸出。`writeback.py:545` 存字串、`sizing.py:48` 認 `("true","通過","yes","1")`、`validation.py:47` 只認 `True`／`"true"`。另外「無上限」在兩邊都是假，但報告把它和「缺欄位」印成同一件事，違反該檔自己宣告的「固定格式、可比較」目的。

**G7 拆股後兩份上限不一致，錢讀錯的那份** `[已證]`
`corp_actions.py:349 _scale_registry` 與 `:362 _scale_cap` 會把 registry 的 `auto_buy_cap`／`sell_review_above`／`absurd_price_line`／`cost`／`review_price` 和 cap_inputs 的 `oe`／`norm_eps`／`iv_*`／`price` 除以 r；**`signals` 表（訊號卡）不在任何換算清單裡**（`grep -n signals ledger/corp_actions.py` 無命中）。而 `sizing.py:305` 的買進額度取的是 `card.get("auto_buy_cap")`，`:311` 也拿它比價。4 拆 1 之後，卡上的 cap 仍是拆股前的 4 倍，收盤價已除以 4 → `quote*(1+slip) < cap` 輕鬆成立 → 在真實估值上限的 4 倍處一次買滿首批。暴露窗口是「拆股後到該檔下一次 PRICE 跑完之前」，而 PRICE 只在 `review_due` 判定有觸發時才跑，所以這個窗口沒有上界。**現況補充（唯讀實查）**：目前 6 張訊號卡的 `auto_buy_cap` 全是 null（还没有一次 PRICE 回寫把數字寫進去），所以這條要等第一張有數字的卡之後才發作——是潛伏，不是正在發生；但 `sizing.py:308` 對 null 是 `skip("訊號卡缺 auto_buy_cap")`，所以它不會以「缺欄位」的形式被永久擋住。

**G8 月凍結不檢查自己的時間戳** `[已證]`（發作條件：重跑／回填）
`data/pe_median.py:111-118`：`for m in (month, _prev_month(month))` 找到檔就 `json.loads(...)` 回傳，從不比對檔內 `asof` 與請求 `asof`。實測：唯一的凍結檔 `2026-10.json` 內 `asof = 2026-10-16`，而同月存在 `asof = 2026-10-15` 的初選。**自我複核後把觸發條件講窄**：`freeze_monthly`（`:94-101`）是「檔已存在就直接讀」，所以正常順跑（第一次遇到這個月就是當天）不會拿到未來樣本；會發作的是**事後用較早的 asof 重跑或回填**，那時當月檔已含晚幾天的樣本。差一天是這兩件事今天實際差了一天；機制上可以是 30 天。這個中位數直接進 `cap_calc.py:138-145` 的 P4′，而 G3 已證明 P4′ 對金融股就是硬上限。凍結的**原意**是「同一個月內大家都用同一個值，避免逐日翻騰」，但那不等於「月中的任意 asof 都能看到月末的樣本」。修法就一行：`get_frozen` 讀到檔後比對 `data["asof"] <= asof`，不合規就回 None（讓 P4′ 變成「不適用」而不是用錯值）。

**G9 同一條價格序列跨拆股日自己比自己** `[已證]`（自我複核後：原因修正、severity 降為中）
`pipelines/review.py:132` 用 `adjustment="raw"` 取未換算收盤寫進 `daily.csv`，交給 skill 的 `review_due.py`。**我原本把這條写成「raw 現價 vs 拆股後口徑的 registry 門檻」，逐行重讀後那個原因是錯的，而且錯得有价值**：`review_due.py:111,117,119,122,125` 那幾條比對（自上次分析價跌 ≥20%、較成本 −12%、現價 ≥ `sell_review_above`／≥ 荒謬級線、現價 < `auto_buy_cap`）都是**正確的**——因為 `corp_actions.py:32 PER_SHARE_REGISTRY` 把這些門檻一起除以 r，與拆股後的 raw 現價同口徑。真正跨不過去的只有同一序列「自己跟自己比」的兩條：`:109` `p / q["close"].max() - 1 <= -0.25`（92 日內高點）與 `:114` `(recent["close"].pct_change().abs() >= 0.04).any()`。2 拆 1 的那個交易日，raw 序列就是一根 −50% 的假跳空，兩條各誤觸一次。合成案例（F10）與重讀後一致：回 `action=THESIS`，理由是「單季自高點跌 ≥25%」與「上次分析後出現單日 ±4%」。方向是誤觸發（多跑一次 THESIS／可能多一次 opus），不是漏觸發，也不直接產生訂單，所以降為中；但它是**每一次拆股都會發生**的確定性錯誤，而且會讓人對「跌了 25%」寫出一個根本沒發生的論點破裂檢查。修法：這兩條改用依已知公司行動換算後的報酬序列，或在窗口內含拆股列時跳過該次比較（後者更省，且不需要新的資料源）。

**G10 賣方零參與率** `[已證]`（自我複核後：severity 降為中，理由 changed）
`broker.py:212`：`_fill(conn, o, min(o["qty"], held), price, comm, date)` ——開盤觸價即全額出清，沒有成交量上限。買方同構（`:188-196`）。下單端的 1% ADV 上限（`sizing.py:29,355,507`）只在**建新單**時限制數量，對已掛出的單、以及所有賣單（TRIM／SELL_ALL 不是經 `plan_buys` 產生）沒有約束。**這條不動真錢**——它是紙上撮合器，所以我原先給的「高」是把它當成交系統看了，降一級。降級的同時我把另一句話講重：它是「何時可以拿真錢去跑」這個決策的**唯一證據來源**，把流動性約束抹掉等於把那份證據往好看的方向系統性偏差（流動性差的標的真實少收 1–3%）。所以「中」是severity，不是優先序——修它只要一個 `min(qty, adv_shares × 參與率)`。

**G11 LLM 成本不在帳** `[部分]`
`grep` 過 `cash_ledger` 的 kind 與 `nav_daily` 的寫入路徑，沒有任何 llm／api 成本項；實查 state DB 的 `llm_calls` 約 30 次、累計 $12.80。我沒有追完 scheduler 是否另有出口，所以標 `[部分]`。第一性原理上這是獲利邏輯問題：這套系統對每檔標的每兩週做一次觸發檢查、深度分析用 opus 級模型，決策次數的邊際成本不是零；當報酬率裡不含它，「小標的＋高頻率複查」的壞處就不會出現在任何報表上，而它恰恰是這套設計最大的成本不確定性。

**G12 文件與實況不同步** `[已證]`
實查 `state/trader.db`：registry 6 列、decisions 6 筆、approvals 15 筆、runs 3 筆；`state/logs/run_approved.log` 顯示 `analyze-2026-10-15` 的 done 計數 16→17→18→19 與 awaiting_approval 遞減，即已對正式 state 放行並執行。`handoff.md` §0 宣稱「正式 state 目前沒有任何持股或決策」。另外 §0 記錄首批 4 筆同意時間為「2026-10-17 12:25」，DB 實際是 `created_at=2026-10-16T15:57:10Z`、`decided_at=2026-10-17T01:05:39Z`（差約 9 小時）。測試數量兩處不一致（656 vs 643）。
這一條本身不讓錢虧損，但它讓「我現在看到的系統狀態是真的」這個前提不可靠——而這個系統整個設計就是把可靠性當賣點。

**G13 提示要求的枚舉值不在 schema** `[已證]`（自我複核後：severity 高→中，後果重寫）
`routing/prompts/FULL_A.md:10` 要求走完 STEP0…G6、**產業檢查**，並「非通過即停：填停止碼」；`routing/output_schemas.json` 的 FULL_A 枚舉是 `STEP0|G0|G1|G2|G3|4A|G4|G5|G6|null`，沒有產業檢查的碼。同一檔 FULL_B 卻有 `G7..G11|null`。**我把三條下場重排了**：(1) 最可能的是模型填 `null`——`validate.py:191` 明確允許「枚舉含 null 時 null 過」，所以這**不會**報錯，代價是停止原因永遠記不下來（`validation.py:169` 會把它印成「未停止」，而它其實是被產業條件停止的：這是一條會出現在稽核報告裡的錯位，屬稽核缺口不是交易風險）；(2) 模型把它塞進 `G6`／`4A` → 紀錄不實但流程正常；(3) 模型發明 `G7` → 驗證失敗 → 重試 → `on_invalid`，而 FULL_A 的 `on_invalid` 是 `deep.py:290 _missing(job, ctx, "FULL_A 輸出不合格…", "STEP0")` → 寫一筆 NO_ACTION／MISSING_DATA、白燒一次 opus 級分析。**第三條不是凍結交易**；我原先把它跟 Z3 當同族寫是誇大，Z3 那條才是真的會 `pause_trading=1`。修法一個 token：把 FULL_A 的枚舉補成與提示一致的產業檢查碼（或直接沿用 FULL_B 的 `G7..G11`）。

**G16 花錢的那個數字沒有 golden value** `[已證]`
這是 Z1／Z2 能一路活到正式 state 的原因。專案有 650+ 支測試，但跟買進上限有關的斷言全部是**同一份算式的自我一致性**：`test_checks.py:113` 比對「import 進來的 cap_calc」與「`python cap_calc.py` 的 stdout」（同一支腳本的兩種啟動方式）；`:225-226` 比對 `out["value_per_share"]["base"] == cap["IV_base"]`（兩個都是產物）；`test_review_jobs.py:101,243` 的 `_auto_cap()` 乾脆就是 `run_cap_calc()`。我拿 skill 自己附的範例輸入跑變體（W3）：

```
原始：cap 101.0664　auto_buy_cap 89.5258　IV_base 89.5258　IV_cons 67.0574
變體[1] min(cap, iv_base) → max ：auto_buy_cap 89.5258 → 101.0664
變體[2] 保守 g0 的 ×0.75 折減拿掉：IV_cons 67.0574 → 71.1527
變體[3] 折現率 10% 下限拿掉　　：cap → 153.2643、auto_buy_cap → 135.0029、IV_base → 135.0029、IV_cons → 89.0627
```

三個變體都讓上述斷言**繼續成立**。變體[3] 的含義具體一點：把規則書的 `r_d = max(10%, 30y+3pp)` 寫錯方向，買進上限在範例標的多 50.8%，而上述三處斷言照樣通過。第一性原理上該有的防線是：對一組**手工算好的**（輸入 → IV_cons／IV_base／P2′／auto_buy_cap）金標值做斷言，且金標值不得由被測程式產生。順帶一提，W3 也再次確認 Z2 的恆等式在 skill 自己附的範例裡就成立：`auto_buy_cap == IV_base == 89.5258`。

**G17 入口掃一次，出口不掃** `[已證]`
`assert_price_free` 在全專案只有一個呼叫點（`sandbox.py:126`，在 `stage_inputs` 尾端，也就是模型啟動之前）。`claude_code.py:107` 已經用前後快照算出 `files_written`（沙箱內新增或被改的檔名），但 `grep -rn files_written trader/` 顯示它只有賦值與 dataclass 宣告，**沒有任何讀者**。我對實際執行完的 FULL_A 沙箱事後補跑那支掃描，它立刻 `PriceLeakError：沙箱內的 OTIS/gate3_answers.json 含價格鍵`——觸發者是 `price_seen_before_answering=false` 與 `price_file_opened_at=""` 這兩個根本沒有價格值的鍵名。這個假阳性正好反證出口從沒接上稽核（接上去 FULL_A 每次必死）。合 Z8 一起讀：入口防線管得到框架塞進去的東西，出口沒有任何防線，而 `is_price_key` 是**依鍵名**判斷的（G1 的 `平均回購價` 就是那個缺口的實例）。

**G18 閘門 3 的查核沒有時點上界** `[已證]`
`review_jobs.py:94-97`：`SELECT decision_date, data FROM decisions WHERE ticker = ? ORDER BY decision_date DESC, created_at DESC`——沒有 `decision_date <= asof`。同一專案其他三處都有上界（`sizing.py:221,239`、`validation.py:35`），所以這不是風格而是漏寫。實測（W5，tmp DB）：asof=2026-10-05，唯一在 asof 之前的決定是 `gate3_pass=False`，asof 之後（2026-10-16）那份是 `True`，`_gate3_status()` 回傳「通過」。這條路徑與 Z6／scheduler 的回填（`scheduler.py:185-189` 無條件補跑錯過的交易日）是相乘的：回填的那天會用一個「還沒發生」的問卷結果去過閘門 2 的三閘門檢查，而回填出來的決策日誌看起來完全合法。同族還有 `writeback.latest_signal`（`:379-387`，按 `created_at DESC` 取最近一張卡，不設日期界）與 `metrics.py:152`（同樣取最新、不設界）——後者是報告端，影響評述不影響下單。

**G19 重試繞過預算與併發** `[已證]`
`engine.py:299-308`：輸出驗證失敗且 `spec.attempt + 1 < MAX_ATTEMPTS` 時，直接 `self._submit(spec, tier)`。預算檢查（`_budget_exhausted`，`:211-218`）與等級併發檢查（`_busy[tier] >= concurrency`，`:252-253`）都只存在於 `_launch` 路徑上。後果兩條：(a) 一個 run 可以超出 `max_llm_calls_per_run`／`max_cost_usd_per_run` 繼續花錢——而且是**先花再算**，因為預算用的是 `llm_calls` 已累計值；(b) 併發上限是 per-tier 的資源約束（每個 claude 行程都是子程序），重試那一次不受它管，實際併發可以超過設定值。這條不是邏輯錯誤而是約束漏接，但「花錢有上限」是這個系統對外的一條承諾。

---

## 5. 獲利邏輯：從第一性原理拆

把這套系統「怎麼賺錢」寫成它自己隱含的式子：

```
買進 ⇔ gate1(初篩未刪) ∧ gate2(現價 < min(P2′, IV_base)) ∧ gate3(問卷)
賣出 ⇔ 論點證偽 ∨ (現價 ≥ sell_review_above ∧ 有更好的用途) ∨ 現價 ≥ 50×E ∨ 權重超限
```

逐項檢查它需要的東西是否存在：

1. **買進側需要「便宜」的定義。它沒有。** `min(P2′, IV_base)` 是公平價，不是便宜價。唯一表達便宜量的 P1（= 等級係數 × IV_cons）被 `:149` 的命名與 `:153` 的 min 明確排除在決策之外，只在報告欄出现。Z2 已用網格證明 `auto_buy_cap ≡ IV_base`。所以整個系統能講的道理只剩「這家公司值得 10% 回報的價格」，而「用多少錢買值得 10% 回報的東西」必須低於那個價格才有剩餘。這是第一層、也是最致命的一層：**它不是沒有安全邊際的設計，而是把「沒有邊際」寫成了規則然後又聲稱有**。
2. **買進側需要「我付的成長假設不能高於公司證明過的成長」。只剩一題有程式把關。** `g_impl` 三題裡的 a 題（≤10%）被折進 P2′ 的定義、b／c 題無讀者（G2）。a 題的 10% 是個對「任何公司」都成立的寬鬆假設；b／c 才是個別公司的約束。
3. **賣出側需要兩個線在決策當下是新的。它們不是。** `sell_review_above`／`absurd_price_line` 只由 PRICE 通道更新（G4），E 下修走完整分析時不更新；`absurd_price_line` 允許 ≤0（F5）；`_is_delete(None)=False`、`iv_base=None` 靜默退讓；四條賣出路徑中由價格驅動的那條在框架裡被硬編成 HOLD（M9：`review_jobs.py:145-146`）。把這些疊起來的實際行為是：**「無價格停損」從紀律退化為「跌了什麼都不做、且舊上限仍持續發出加碼許可」**。`review_due.py:119-124` 用登錄表的舊 `auto_buy_cap` 判「未達目標且低於上限」→ PRICE，`_map_action` 對 BUY_CANDIDATE 且未達目標權重回 ADD。這條鏈在盈餘下修情境下會穩定產出「越跌越買」，直到 `weight > NO_ADD_ABOVE(15%)` 才停——而 `weight` 這欄目前根本沒有寫入者（G14，實測 registry 6 列的 `weight`／`target_weight`／`cost`／`first_buy_date`／`auto_buy_cap` 全為 null），所以連這最後一道煞車也不在。
4. **買進與賣出需要分開的兩條線。它們是同一條。** Z7：`auto_buy_cap ≡ IV_base = sell_review_above`，所以系統在結構上沒有「持有區間」——買進允許的最高價就是開始考慮賣出的價，而每一次成功建倉都落在 `cap × 0.9999`，即這條線的下緣。
5. **賣出指令需要能被表達。它沒有輸出通道。** `routing/output_schemas.json:43` 的 `proposed_action` 沒有 `SELL_ALL`；`review_jobs.py:145-146` 把 `SELL_REVIEW` 映射成 HOLD。四條賣出路徑中由價格驅動的那兩條（`SKILL.md:130` 的 (2) 與 (3)），一條沒有格式、一條被框架改寫。
6. **決策成本需要進報酬。它沒進。** G11。
7. **基準需要被同樣地計量。它被少了股利。** Z5：組合含股利、SPY 不含，標籤卻寫「含股利」，每年約 1 個百分點的「超額報酬」是口徑差而不是選股。
8. **稽核需要能證明「沒有用未來資料」。它的兩個主要查核是空殼，而淨值曲線本身可以被無痕污染。** G5、G8、G9、Z6。
9. **口徑需要一致。它有五套真相。** 同一個 `gate2_price_below_cap` 有字串／布林兩種寫法（G6）；同一個 `auto_buy_cap` 在 registry 與訊號卡兩張表可以差一個拆股倍數（G7）；同一個「硬上限」在 cap_calc 可能是 P4′ 或 P1（G3）；同一個「決策動作」有四套枚舉（G15）；同一個閘門 2 在 `cap_calc` 有 IV_base 夾制、在 `deep_quant.py:148` 只剩 `g_impl < 0.10`（`[未覆核]`，subagent SK-6）。
10. **edge 的唯一來源是「判斷比市場早且獨立」，而獨立性是這個系統聲稱的核心紀律——它目前不是被強制出來的。** 這套方法論能講出第 1–5 點的道理，全靠「G1–G6 與閘門 3 是在不知道現價的前提下完成的」這個前提：如果估值與能力圈判斷是先看價格再倒推的，那 `IV_base` 就只是市場標價的另一種寫法，整套 min(P2′, IV_base) 的邊際討論失去參照物。Z8 顯示這個前提是提示詞層的自我約束，不是沙箱層的約束；G1 顯示框架自己還會從 dossier 遞一個真實均價進去。所以**獲利邏輯的地基是一條無法被事後驗證的宣稱**（`price_seen_before_answering` 是模型自己填的布林值，且空值被跳過）。就算所有 deny 規則都正常運作，缺的還是「觀測得到模型有沒有讀過那個路徑」這一層，而不是「擋得住」這一層。
11. **花錢的節奏需要被預算約束。重試那一路沒有。** G19：驗證失敗的重試直接 `_submit`，繞過 `_budget_exhausted` 與 per-tier 併發。與 G13／Z3 疊起來看更糟：schema 與提示矛盾會**系統性地製造重試**（照規則回答 → 驗證失敗 → 重試一次 opus），而重試這一路徑恰好是不受預算上限計的那條。也就是說規則檔的缺陷会被轉換成花費，而且是不被承諾的那種花費。

一個正面結論，我確實查了：**折現率紀律是真的**。`r_d = max(10%, 30 年期 + 3pp)`（`cap_calc.py:71`）、`r_stress`、`r_sens` 全部有下限，不因低利率環境放水；`g_term_impl = min(3%, 30 年期 − 1pp)` 也是收緊方向；W-PE（`:177-179`）、P-21 寬度降級（`:106-110`）、結論測試翻轉 WATCH（`:169-175`）都是往「不買」方向偏的真實把關。問題不在這些紀律缺失，而在**這些紀律作用在一個沒有邊際的數字的旁邊**。

---

## 6. skill 撰寫：從第一性原理拆

skill 在本專案裡等於規格書（框架照 `routing/*` 把規則餵給模型），所以「寫得好不好」的判準是**規格品質**，不是文筆。

1. **同一數量在多處各自定義，且沒有一處被指定為權威。** `auto_buy_cap` 在 `SKILL.md:93`、`rulebook.md:263`（「唯一硬上限」）、`rulebook.md:269`（`min(P2′, IV_base)`）、`procedure/05_valuation_caps.md`、`rule_index/02_valuation.md:250,276-278` 五處出现，措辭與範圍不同；P1 在 `procedure/05:44` 被標為「報告欄，不進 min」，但同檔 `:38` 又要求「歷史成長≤0 時上限只能用 IV0×等級係數」——那個「上限」既不是 P1 也不是 P2′，讀者的合理推論只能是 P1，而程式兩者都不是。這直接解釋了 Z1/Z2 為什麼能存在而不被發現：**沒有可證偽的單一權威定義，就沒有辦法寫出會失敗的測試**。
2. **能被程式算的判斷不該交給模型；必須由模型判斷的該有程式覆核。專案在兩個方向都做了反。** 前者：`g_impl_checks` b／c（G2）。後者：`apply_cap_override`（Z1）把模型的保守值蓋掉——覆核的方向對了，但覆核者本身算錯時，它就把系統唯一的保護拆了。
3. **規則把算術藏在散文裡。** 「歷史成長 ≤0 → 上限只能用 IV0（E ÷ r_d）× 等級係數」這句被寫了四遍，程式端只有一行 `notes.append`（`cap_calc.py:133-134`）。凡是「上限／倍數／減幾趴」類的句子，都應該在規則檔裡自帶一個可貼進 `cap_calc.py` 的表示式；現在是反過來：規則用中文描述運算，實作決定要不要聽。
4. **枚舉／欄位規格與提示詞不同步，導致「越遵守越容易失敗」。** Z3（`effective_date` 不接受 null）與 G13（產業檢查沒有停止碼）是同一類錯誤的兩例，而且是**互為因果**的那類：提示把行為講清楚、schema 沒有跟上。這類缺陥的共同測試盲區是 `tests/test_corp_actions.py` 用 helper 繞過 validator——測試測的是「我 construct 出來的物件過不過」，不是「模型依提示產出的物件過不過」。
5. **「推論」標記等於承認規則未實作。** `cap_calc.py` 內部有 4 處 `（推論）` 註解（`:118`、`:189`、`:161` 的「合併版一致性條款，推論」、`:107` 的 P-21），`inferred_thresholds.md` 另有一份推論門檻清單，而 `SKILL.md:183` 要求模型逐項申報推論。一份需要靠「推論」補齊的規格，無法用「模型有沒有遵守規則」來稽核它自己。
6. **正面控制：沒有拿具名歷史案例當例子。** 我 grep 過 `kraft|see's|coca.cola|geico|dexter|aircall|apple` 等（含中英對照）在 `berkshire-deep-merged`、`berkshire-screen-merged`、`routing` 三個目錄，**0 命中**。使用者在 `handoff.md` §2 定的這條規矩被確實遵守了。同理，「標的代號只出現在日誌、不出現在 skill 文字」也成立（`assets/decision_log_schema.json` 的說明欄與 skill 正文無代號）。這兩點我原本預期會抓到，沒有；就照實說。
7. **skill 自己把同一條規則寫成兩個互斥的權威，且用「等價」掩蓋。** `[已證]` 四條我逐字讀過的：
   - `SKILL.md:199` 宣告 `deep_quant.py` 「**逐檔流程不使用**，其輸出不得當作任何閘門的答案」；`procedure/12_auto_buy_gates.md:9` 的閘門 1「判定方式」欄卻寫「讀交接檔；**腳本 `deep_quant.auto_buy_gates` 的 `gate1_not_deleted`**」。同一份 skill 裡，被禁止當閘門來源的腳本正是該閘門被指定的實作。模型遇到衝突時沒有優先序可依，只能挑一個——這就是 `handoff.md` §2 說的「skill 文字要恢復選擇邏輯」的反面：它給的是**兩套選擇邏輯**。
   - `procedure/12:10` 的閘門 2「內容」欄是 `現價 < min(P2′, IV_base)`，「判定方式」欄是 `gate2_price_below_cap`（價格與 `g_impl` 同向，所以等價於 `g_impl` < 上限）。這個「等價」是錯的：`g_impl < 10%` 只等價於 `價格 < P2′`，它**不含 IV_base 那一腿**。而 Z2 已用網格證明真正綁住的恰好是 IV_base（`auto_buy_cap ≡ IV_base`）。換言之規格把決定性的一半描述成「不必檢查的另一半」，G2 的 b／c 題零讀者是同一個動機的延伸。
   - `SKILL.md:113` 「決策枚舉只用 `BUY`／`WATCH`／`NO_ACTION`」對上 `rulebook.md:432` 「**七種訊號，單選**：…／`ADD`／`HOLD`／`TRIM`／`SELL_ALL`／…」，而 `assets/signal_card_schema.json` 是三值、`decision_log_schema.json` 是七值、`routing/output_schemas.json:43` 的 `proposed_action` 是第四套（含 `SELL_REVIEW`、無 `SELL_ALL`）。這是 G15 的根源，不是措辭問題：`SELL_ALL` 在規則書是指令、在 skill 摘要被取消、在框架輸出格式不存在。
   - `procedure/12:13` 是全 skill 語氣最重的一條：「作答前不得看價格…**問卷必須聲明 `price_seen_before_answering: false`，否則閘門 3 不過**」。它的實作鏈是：提示詞 `FULL_A.md:11` 叫模型**留空**該欄 → `gate3_check.py` 對空值是跳過而非拒絕（M10）→ 而 Z8 顯示價格檔就在沙箱兄弟目錄、可直接讀。一條被反复强调的流程紀律，落地後是一個可留空的布林欄，外加一道沒有關上的門。

---

## 7. 我推翻的指控（不背書清單）

subagent 產出的東西裡，有以下幾項我查過之後**判定不是 bug**，或已把範圍縮小。列出來是因為若不列，報告的其餘部分可信度也要打折。

- **「除息後賣股仍領股利 = 幽靈現金（致命）」→ 不成立。** 美股在 T+1 結算下 ex-date 即 record date；在除息日之後賣出，**股利本來就該歸賣方**（買方買的是除息股）。`corp_actions.py:422` 以 effective_date 當時的持倉鎖額、`:488-504` 到期入帳，對正常流程是正確的。殘留的真實風險只有一句：金額在偵測日鎖死、結算日不複核，若其間發生補跑或其他公司行動改變部位才會錯——這條我降列為 §9 待確認。
- **「gate3 作答檔的 mtime／順序可以被事後開價檔污染」→ 結構上已擋。** FULL_A 的沙箱裡根本沒有價格檔（Z8 證的是它可以*绕过去读兄弟目录*，不是框架把檔塞錯了地方），且 `checks.py:203-217 cap_signal_by_gates()` 會在 gate3 未過時把 BUY 降 WATCH。
- **「閘門 3 的自證時間戳可以留空繞過（我原来的 M10）」→ 我收回這條。** 逐行重讀 `scripts/gate3_check.py:26-31`：`price_seen_before_answering` 用的是 `is not False`，未填、填 true、甚至填字串 `"false"` 都直接判失敗；`price_file_opened_at` 只在**非空**時才做時序比對。所以「空值被當成通過」這個說法對我原先指控的那個欄位是錯的。殘留的只是一句較小的話：提示（`FULL_A.md:11`）指示該時間戳留空，於是那條時序比對在實際執行中永遠不被執行——它是「自證沒有外部證據」，已折進 Z8，不另立一條。
- **「decision_id 會撞號」→ 不成立。** `writeback._next_free_id` 會循現存 id 往後找空號。殘留問題是崩潰重試造成重複列，不是編號相撞。
- **「缺價時仍允許加碼」→ 不成立。** `sizing.py:309` 缺當日報價直接 `skip("缺當日報價")`。MN-11 真正剩下的只有「`equity` 分母用平均成本高估 → 加碼額度被放大」，我按這個較小的範圍列在 M6。
- **「Yahoo 月線前視（最多 30 天）是正式管線的缺陷」→ 只在 skill 獨立腳本成立。** `data/prices_adapter.py:44-45` 明確把**日線**餵給 skill 的 `fetch_prices.feats()`，實測 `screen.csv` 的 `price_src` 503 筆全是 alpaca、`price_date` 全是 2026-10-15（無一筆是月初）。所以 `feats()` 對日線輸入是安全的；只有走 `fetch_prices.py:27 interval=1mo` 的研究腳本才會把月末收盤標成月初日期。我不把它列進 Z/G，只在 §9 保留給回測使用情境。
- **「儀表板把寫入權杖嵌進無需認證的 `GET /`，等於任何人都能批准 opus 花費」→ 我查過，判定是可接受的設計取捨，不列缺陷。** `dashboard/server.py:264-268` 確實無認證地回傳含權杖的 HTML，但這是檔頭註明的設計（`:5-7`），且寫入端有四重獨立守門：`host_guard` 比對 Host（擋 DNS rebinding，`:102-106`）、`guard_write` 要求 Origin 屬於本機 origin 集合（`:109-111`）、Content-Type 必須 `application/json`（擋簡單表單 CSRF，`:112-113`）、`hmac.compare_digest` 比對權杖（`:114-115`）；`serve()` 硬性只綁 127.0.0.1（`:278-281`）。跨網來源讀不到響應體（無 CORS 標頭 + Host 檢查）。真實殘留只有兩句，我按小問題記：(a) 讀端（`/api/summary` 等）不需要任何憑證，本機任意行程可讀出持股、成本、現價；(b) 權杖同時是「同意花 opus 錢」的唯一憑證，而它存在 `state/.dashboard_token`（600）並被嵌進 HTML——本機任意行程也能讀那兩處，所以風險沒有因為嵌頁而實質增加。我不把它寫成漏洞。
- **「opus 同意閘門可以被繞過（致命）」→ 我降為中等、且標為潛伏（M15）。** `PASS = ("approved","done")`（`approvals.py:19`）的語意是「這一檔在這一次 run 已獲准」，不是「這一個工作已獲准」，所以 `close_finished` 收 `done` 之後若同 run 同檔再冒出新的 strong 工作，閘門會放行——這是真的。但我追不到今天可走到那一步的呼叫路徑：`analyze.py:87` 的 run_id 是 `analyze-{asof}`、`engine.add_job` 以 job_id 去重（`engine.py:81-82`），重跑同一天同一檔根本不會產生新工作。所以我把它記成「語意脆弱」而不是「正在漏錢」。設計上更穩的做法是把同意記在 (approval_id → job_id) 的對應上，而不是 (run_id, ticker) 一列。

---

## 8. 我認為該排第一的那一件事

Z1、Z2、Z4、G1、G7、G16 表面上是六個不相干的程式缺陷，但它們是同一個**架構決定**的六個症狀：框架對模型的態度是「覆寫它的數字」（`apply_cap_override`），對自己的態度是「用我的輸出測我的輸出」。於是系統裡最重要的一些宣稱——買進上限有安全邊際、閘門 2 檢查了成長假設、模型沒有看過價格、同意閘門管住了花費——**沒有一條對應到一個會失敗的測試**。G16 是這個判斷的決定性證據：把折現率下限拿掉，上限漲 50.8%；把 `min` 改 `max`，上限照樣過——因為那些斷言的期望值是被測程式自己算出來的。Z8 是同一件事在邊界層的版本：隔離的唯一驗證方式是把自家產生的字串跟自己寫的死期望值相比（`test_isolation.py:32`），而該檔註明「不呼叫 claude」。

若要只修一件事：**先建立「獨立可證偽的檢查」這件事本身**，具體兩個起手式——

1. 給 `cap_calc.py` 一組 golden values（衰退公司／零成長／金融股／E ≤ 0／利率極低各一例，期望值由人算或另一支獨立腳本算，來源不得是被測程式），並把規則書那四條實作出來（IV0 夾制、等級係數進 `min`、b／c 題、`norm_eps` 正數檢查）。修好它，Z1、Z2、G2、G3、G4 的一半、M1 的一半、M2、M3 同時消失。
2. 給沙箱邊界一次**真實呼叫**的紅線測試：在沙箱內對「兄弟沙箱的價格檔」「家目錄的 `.env`」「`state/trader.db`」各做一次 Read，斷言被拒；把 `settings_json` 的絕對路徑前綴修成單一 `/`；把同層兄弟沙箱目錄加進該工作的 deny 清單（`create_sandbox` 之後一行，見 Z8）。修好它，Z8、G1 的「聲稱」與「事實」才第一次對齊。**自我複核後我從這條刪掉兩件事**：「為自己的沙箱開 allow 例外」（依賴我無法在本專案內驗證的 deny／allow 優先序語意）與「把 `assert_price_free` 改成鍵名＋數值雙口徑接到出口去掃」（W4 實測它會被 `price_seen_before_answering=false` 這類布林／空值誤報，接上去 FULL_A 每次必死）。G17 我仍然照實記錄「出口沒有稽核」這個事實，但我不再把它包裝成「只要接上掃描器就修好了」——那是一個會引入新故障的建議，不是修正。

第 2 項我不建議跳過，理由是獲利邏輯層面的：整套方法的 edge 宣稱建立在「判斷獨立於市價」上（§5 第 10 點）。第一項決定你付多少錢，第二項決定那個數字是不是真的來自估值。

另外三條是「修法不需要設計決策」的：Z3（`effective_date` 容許 null，或提示改填日期——二選一即可，但必須選一個。觸發條件我要講準：只有當模型**照提示老實填 null** 時才會走到凍結；模型若自己編一個日期就不會觸發，但那會污染公司行動帳。今天 0 筆 `corporate_actions`／`anomalies`，所以這條尚未在正式 state 裡發作過，我沒有任何一次實際凍結的證據，只有讀完的呼叫鏈）、G18（`_gate3_status` 加 `decision_date <= ?`）、G6（`gate2_price_below_cap` 存布林並讓兩端讀同一個真值標準）。


---

## 9. 未確認／需要人判定的項目

以下是 subagent 提出、有行號但我本輪**未逐行覆核**的項，建議優先抽查（我刻意不把它們寫進上面的嚴重度清單）：

**先講已查證並升上 §2／§3／§4 的，以免重複抽查**：OP-3 → G19、OP-7／OP-8 → G18、OP-12 → Z8、OP-19 → G16、OP-2 → M15（降為中等＋潛伏）、OP-17 → §7（判定不是缺陷）、SK-5 → §7（只在獨立腳本成立）。這些我逐行讀過引用的那幾行並跑了數字。帳本／資料層那一批（Z4–Z7、G8、G14、M6–M8）同樣是我讀過原始碼與實查 state DB 之後才寫進清單的，但它們源自 subagent 的編號對應我不再重複列。

仍**未覆核**的：

- `berkshire-screen-merged/scripts/screen.py` 的缺值中性預設 `fillna(0.5)`（缺 3–4 項指標仍能進核心道）；百分位對 NaN 恆不觸發排除。
- `data/sec.py:312-317`：`since is None` 時只用 `filings.recent` → 高申報量公司的歷史清單被截成空值，與「真的沒有申報」不可區分，`review_due` 據此判「不需重審」。
- `scripts/fetch_edgar.py:144-145` 的前視 `RuntimeError` 被 `sec.py:363-364` 每檔 `except Exception` 吞成「抓取失敗」，`screen_quant.py:48-56` 只查 `fy_last_filed`。
- `data/universe.py:18,59`：股票池取「現行」成分並把 `universe_versions.asof` 寫成今天 → 歷史重跑有存活者偏差。
- `xbrl_shares.py:562,585-588`：有加權股數就跳過等值換算，雙類別市值口徑可能錯（實測 BF.B、FOX、NWS 等 6 檔走該分支）。
- `fetch_edgar.cover_shares:114-124` 以「申報日」而非封面「截至日」當拆股基準；與 `prices_adapter.py:36`／`screen.py:39` 的邊界（`<=` vs `>`）不一致。
- `deep_quant.py:79,105,119,129,137`：另一套閘門實作，`g0` 只取 5 年、負成長 `.clip(-0.05, 0.08)`、缺利率時 g_term 取上限 0.03。研究網格與交易規則口徑分叉。
- `broker.py:239-240` + `pipelines/daily.py:245-249`：暫停期間賣訊被跳過，而決策視窗 `(prev, date]` 只吃一次（訂單本身會留 open 重試，所以只有「從未成功下單」的那一小段會永久消失）。
- `metrics.py:51-66,135` 與 `daily.py:294-298`：SPY 起算日與含股利口徑、`validation.py` 標題仍取 `summary` 的超額報酬（單筆「不可比」只標在明細）。
- `report/export.py:51-58` CSV 公式注入；`jobs/validate.py` 未檢查 `gate3_answers.json` 存在（到 FullB 才 `FileNotFoundError`）。
- 規則檔之間的 50%／70% 首批、8%／10% 上限、P-21 寬度檢查不可達等枚舉級矛盾（subagent SK-9）。
- 待確認的資料問題：Alpaca 日線 `t` 是否恆為 `04:00:00Z`（若不是，`data/alpaca.py:31-36 _ny_date` 會把每根棒次往後挪一天，`price_date`、拆股基準、52 週回撤一起錯）；`state/cap_inputs/*.json` 無 asof 鍵，PRICE 快速通道在重跑時是否混用不同年代的盈餘輸入（`review_jobs.py:231-248` 只覆寫 price／利率／P/E 中位）。

---

## 10. 佐證與重現方式

```sh
cd "/Users/zongen/Downloads/claude code/berkshire"
python3 "bug report/qwenhigh/verify_money_logic.py"    # F1 F2 F3 F5 F10
python3 "bug report/qwenhigh/verify_money_logic2.py"   # V1 V2 V3 V4 V5
python3 "bug report/qwenhigh/verify_isolation.py"      # W1 W2 W3 W4 W5
```

三支都是唯讀：把專案原始碼 import 進自己的行程記憶體計算，**不寫入專案任何檔案、不開 `state/trader.db`、不發網路請求、不呼叫 claude**。`verify_money_logic.py` 的 F10 用合成 2:1 拆股餵 `review_due.check()`，證明誤觸發只取決於口徑，不需要真實資料。

`verify_isolation.py` 的額外讀取範圍（重現前請知悉）：
- W1／W2／W4 讀 `/Users/Shared/berkshire-sandbox-zongen/`（執行期沙箱，專案目錄以外）與 `berkshire-deep-merged/assets/cap_calc_example_input.json`；
- W3 把 `cap_calc.py` 的**原始碼字串**讀進記憶體、在 `bug report/qwenhigh/` 下的暫時目錄裡產生變體副本執行（專案原始碼未被改動，一個位元都沒動）；
- W5 在系統暫存建立暫時 SQLite、灌入 `db.migrate()` 的真實 schema 後呼叫 `review_jobs._gate3_status()`，跑完即刪。

沙箱抽查（唯讀）：`analyze-2026-10-16/OTIS/_FULL_A_1`（無價格檔）、`_FULL_B_1/dossier/OTIS_price.json`（`price=70.39`，同 run 兄弟沙箱）、`review-2026-10-16-watch/OTIS/_PRICE_1`。同一沙箱根下目前共有 3 個 `*_price.json`。

**我沒有執行專案的 pytest。** 跑測試會在工作區寫 `.pytest_cache`／`test_runs/`，超出「零改動」的邊界；所以 G16 的論證是結構性的——那些斷言的期望值由被測程式自己產生，因此變體在設計上就不可能讓它們變紅——不是我實測整套測試通過的結果。

DB 與資料檔抽查（唯讀）：`state/trader.db`（registry 6／decisions 6／approvals 15，所有 `fy_last_filed`、`skill_hashes` 皆 null）、`state/cache/pe_median/2026-10.json`（asof=2026-10-16）、`state/screens/2026-10-15/screen.csv`（503 列／500 CIK；FOX 25 與 FOXA 28 同為核心道；15 筆 shortlist 的 `screen_decision` 全為 PASS／PASS-困境）。

**另記一筆不是腳印的腳印**：本報告撰寫期間，工作區有另一個不是我啟動的程序在寫入（`.pytest_cache/`、`trader/.coverage`、`test_runs/probe_*`、`bug report/step/`）。我沒有執行任何會改動專案的命令；本報告裡所有「實查 state DB」的數字以 2026-10-17 約 11:30 的讀取為準。

---

## 11. 自我複核（第二問：問題是否真實存在、有沒有過度工程化）

方法：把 §2 每一條重新對著**原始碼本身**讀一次（不是對著我上一輪寫的摘要讀），能實測的就實測（三支 `verify_*.py` 重跑、`state/trader.db` 唯讀、`/Users/Shared/berkshire-sandbox-zongen/` 沙箱樹枚舉），並刻意往兩個方向找：「這條其實不成立」和「這條比我想的更糟」。下面只記差異，不重複結論。

### 11.1 逐條查證結果（有變化的才列）

| 編號 | 原主張 | 本輪查證結果 | 動作 |
|---|---|---|---|
| Z3 | 照提示回答即凍結交易 | 呼叫鏈補完了：`engine.py:310` → `corp_jobs.py:91-92` → `corp_actions.py:537-541 → reject():526`，`pause_trading` 是寫死的字面 `1`；而 `tests/test_engine.py:476-484` 直接把 `("corp_action_invalid_output", 1, "open")` 寫成斷言——專案自己的測試**鎖住了這個行為**，不是我一方面的推論。但「今天就會發作」講得太滿：要模型老實填 null 才會走到，而目前 `corporate_actions`／`anomalies` 都是 0 列 | 維持致命；頻率宣稱改成條件式（§8 已改） |
| Z4 | 市值守恆覆核恆等通過 | `:326` 的 `post` 用 `pb / r` 造出假想股價，`post/pre` 代數化簡後只剩 `_whole(qty*r)/(qty*r)`，與公司行動的經濟內容無關；`:306` 的價格比是唯一獨立交叉檢查，容差 `TOL_PRICE_RATIO=0.15`（`:36`）放得過 4 拆 1 寫成 3.5（實差 14.3%） | 維持致命，無變動 |
| Z5 | 基準股利恆為 0 | 確認：`daily.py:294` 讀 `a.get("cash")`，`alpaca.py:114` 輸出 `cash_per_share`，字面不符。但它動的是**報酬證據**不是**部位** | 致命 → **高**，把「動證據／動部位」的分辨寫進 §3 |
| Z6 | 回填用今天的帳本寫歷史 | 路徑確認且無護欄：`cli.py:692` 的 `--date` 必填但接受任意過去日期、`run_daily(cfg,paths,conn,date,...)`（`daily.py:125`）不驗證 `date`、`book.record_nav`（`:137-155`）從**當下的** `cash/receivables/positions` 算完再依 date upsert；`scheduler.py:185-189` 無條件補跑錯過的日子 | 維持致命（潛伏）；修法只保留「拒絕寫＋留記號」，不建議做重放 |
| Z7 | 買進上限＝賣出檢視線 ＋ 賣出無通道 | 前半（規則給三種相反處方）是**文件互相矛盾**，不是程式缺陷；後半（`output_schemas.json` 沒有 `SELL_ALL`、`review_jobs.py:145-146` 把 `SELL_REVIEW` 硬編成 HOLD）是可定位的代碼事實 | 一條拆兩半，標籤分開（§2 已改） |
| Z8 | 價格隔離在實作上是空的 | 沙箱樹枚舉推翻我寫的一個細節：根下 50 個沙箱，`*_price.json` **只有 3 份、全在被允許看價格的 FULL_B 沙箱內**（`analyze-2026-10-16/OTIS/_FULL_B_{1,2,9}/dossier/`），沒有一個「共享上層 dossier」把價格檔留在隔離工作旁邊。真正的洩漏面比我原稿小、但一樣致命：**同層兄弟可達**——`{root}/{run}/{T}/_{JOB}_{n}` 的佈局讓 FULL_A 用 `../_FULL_B_1/dossier/...` 就读到了 | 維持致命；機制描述改成實測佈局，修法縮成一行（11.2） |
| G7 | 拆股後訊號卡不換算 | `grep signals ledger/corp_actions.py` 零命中確認；`_scale_registry`（`:349`）與 `_scale_cap`（`:362`，含 `oe`/`norm_eps`）都換算，唯獨 `signals.data` 不換算，而 `sizing.py:305` 讀卡。**附加條件**：現有 6 張卡的 `auto_buy_cap` 全是 null → 尚未發作 | 維持高，加「潛伏到第一次 PRICE 回寫」 |
| G8 | 月凍結不看檔內 asof | 讀到 `freeze_monthly`（`:94-101`）「檔已存在就直接讀」之後，觸發條件比我原本寫的窄：**順跑時第一次遇到這個月就用當天建檔**，无前視；會拿到未來樣本的是「檔建立較晚、事後用較早 asof 重跑或回填」。實測檔內 `asof=2026-10-16`、同月已有 `screens/2026-10-15/` | 維持高，標題加「僅在重跑／回填時發作」 |
| G9 | raw 收盤與 registry 門檻口徑不同 | **原因寫錯了，而錯的這部分有價值**：`review_due.py:111,117,119,122,125` 那五條對 registry 的比都是**對的**（`PER_SHARE_REGISTRY` 把門檻一起除以 r，與拆股後的 raw 現價同口徑）。只有 `:109`（92 日內高點）與 `:114`（`pct_change`）是同一條序列跨拆股日自己跟自己比，那兩條才會誤觸 | 高 → **中**，並在 §4 把原因反過來寫清楚 |
| G10 | 賣方零參與率 | 代碼確認（`broker.py:212` `min(o["qty"], held)`，買方 `:188-196` 同構）。但這是**紙上**撮合器，不碰真錢；傷害是把「可以上真錢」的那份證據美化 | 高 → **中**，同時把「它是唯一證據來源」講重 |
| G13 | 提示要求的枚舉不在 schema → 模型只能誤標或填非法值 | `validate.py:191` 讓 `null` 成為合法值，所以最可能的下場是「停止原因記不下來、稽核報告 `validation.py:169` 把它印成『未停止』」，不是交易風險；模型若發明 `G7` 才會 `on_invalid`，而 `deep.py:290` 是 `_missing(..., "STEP0")`＝一筆 NO_ACTION／MISSING_DATA，**不凍結交易**。我上一輪把它和 Z3 當同族，那是誇大 | 高 → **中**；三種下場重新排序 |
| G14 | registry 死欄位（含 `auto_buy_cap`） | **欄位清單有錯**：`auto_buy_cap`／`sell_review_above`／`absurd_price_line` 在 `writeback.py:531`（卡）與 `:561`（registry）都有寫入，只是只由 PRICE 通道寫。真正沒有寫入端的是 `cost`／`first_buy_date`／`weight`（只 carry-forward）與 `target_weight`（**全專案零寫入端**；`daily.py:118` 寫的是 `build_plan.target_weight`，名稱與位置都不同）。順帶查出一條比我原稿更具體的後果：`review_due.py:123` 的 `(weight or 0) < (target_weight or 0)` 恆為 False → 持股低於目標權重且低於上限時，**加碼檢查永遠不會機械性觸發** | 收回錯誤欄位；補上更強的後果；維持高（潛伏） |
| M4 | 閘門 1 恆真 | 我去找「更糟的版本」（DELETE 被 `screen.py:178` 改寫成 PASS 而混進短名單）——**查無**：`screen.py:106` 在排名前 `& ~deleted`、`:143-145` 非 core 成員 `core_rank` 為 NaN → `shortlist` 為 False。所以它只是一條永遠不會擋住東西的防線被當成一道閘門陳述 | 中 → **低**，並把「我試圖升級它但失敗」寫進證據 |
| M7／M8 | 去重鍵吞列 | `_ca_exists`（`corp_actions.py:70-73`）逐行讀過：只用 (ticker, effective_date)；DB 的 UNIQUE 含 `action_type`（`db.py:46`）。`:447` 的 duplicate 早退與 `:529` 的插入共用同一個鍵 → 兩條都成立 | `[部分]` 解除 → 已證 |
| M10 | 自證時間戳可留空繞過 | **不成立**：`gate3_check.py:26` 用 `is not False`（未填／填 true／填字串 `"false"` 全判失敗），`:29-31` 只在非空時才比時序 | **整條收回**，移到 §7；殘留觀察折進 Z8 |
| G16／G17／G18 | 金標值缺失／出口不掃／查核無時點上界 | 本輪重跑 `verify_isolation.py`：W3（三個變體都讓斷言繼續成立）、W4（事後掃描被布林鍵誤報、`files_written` 零讀者）、W5（asof=2026-10-05 拿到未來的 `gate3_pass=True`）輸出與報告數字一致 | 維持 |

**本輪沒有重新開啟的條目**（沿用上一輪讀過的原始碼，不是新證據）：Z1、G1–G6、G11、G12、G15、G19、M1–M3、M5、M6、M9、M11–M15。其中 M11、M12 仍然只是 subagent 的行號轉述，我沒有替它們背書；M13 的 `plan_tranches` 我仍未逐行讀完，`[部分]` 保留。

### 11.2 過度工程化：砍掉我自己的 4 條建議

第二問照實答：**有過度工程化，位置在「我的修法建議」裡，不在缺陷清單裡。**

| 我原本的建議 | 判定 | 砍掉之後的替代 |
|---|---|---|
| Z8：把沙箱根納入 deny，再為該工作自己的沙箱開 allow 例外 | **過度工程，而且是危險的工程**：它依賴 Claude Code 的 deny／allow 優先序，而那件事在本專案內我無法驗證（雙斜線規則字串本身就還沒有人對真實匹配器驗過）。拿不確定的語意當修法地基，等於建議一個我不知道會不會生效的修補 | 只在 `create_sandbox` 之後把**同層兄弟沙箱目錄**逐一加進該工作的 deny 清單——被 deny 的是兄弟，不是自己，所以不觸犯 `check_sandbox`「沙箱不得落在封鎖根之下」。跨 run／跨股票那一層要關掉必須改根目錄佈局，我把它寫成「結構調整、另案」，不混進這一步 |
| Z8／G17：跑完再以「鍵名＋數值」雙口徑掃沙箱 | **過度工程，且實證會壞事**：W4 我親自補跑了這條，`PriceLeakError` 立刻被 `price_seen_before_answering=false`、`price_file_opened_at=""` 這兩個根本沒有價格值的鍵名觸發。接上去＝FULL_A 每次必死。我原先寫成「只要接上掃描器就修好了」是錯的 | 不建議接。G17 只保留「出口沒有稽核、`files_written` 零讀者」這個**事實記述**，不附帶一個我沒驗證過的修法 |
| Z6：`run_daily` 走「依該日之前的 fills 重建現金與部位」的重放路徑 | **工程量大於問題本身**（等於半個 event-sourcing 重構），而現況 `nav_daily` 是 0 列 | 保留原稿的「先做後者」：不能重放就**拒絕寫 NAV**，並在 `runs` 留一個明確的 `backfill_unsupported` 記號。一句 guard 的事 |
| G13：把 FULL_A 的枚舉與提示「重新設計成一致」 | 措辭太大；缺的就是一個代碼值 | 一個 token：枚舉補上產業檢查碼（或直接沿用 FULL_B 的 `G7..G11`） |
| Z5：改鍵名之外，再加「宣稱含股利時檢查基準股利是否曾有非零值」 | 這條我**維持**，不算過度工程：防的是同類鍵名漂移再次無聲發生，成本是一個布林檢查，而且放在會誤導人的那個輸出模組裡 | — |
| G16：金標值（golden values）測試 | **維持**，但把範圍講小以免被當成重構：不需要新框架，只要 5 個手工算例（衰退／零成長／金融股／E ≤ 0／利率極低）寫成 fixture，期望值不得由被測程式產生。這是「最重要那幾個宣稱沒有對應到會失敗的測試」的直接對治，不是可選加分項 | — |

第一問的答案則是：**42 條裡沒有虛構的缺陷。**本輪查證讓 1 條整案收回（M10）、2 條改了原因或欄位（G9 的原因、G14 的清單）、6 條降級（Z5、G9、G10、G13、M4，以及把 Z7 的文件半段從「缺陷」改成「矛盾」），沒有出現「根本查不到這個程式碼」的條目。反之有 3 條在查證中變強：Z3（專案自己的測試把 `pause_trading=1` 寫成斷言）、M8（找到了 `:447` 的 duplicate 早退這條完整閉路）、G14（從「死欄位」升級成「加碼觸發恆不發火」）。

### 11.3 仍然無法自己驗證的（不因寫了修法就假設已解決）

1. `claude -p` 的 `--settings` deny 規則能否匹配雙斜線路徑——需要一次真實呼叫（花訂閱額度、且會在工作區留下東西），我沒有做。因此 Z8 第二段「整個檔案隔離層可能是空轉的」到現在仍是**條件式**陳述，不是事實。
2. 模型實際執行時會不會真的對 `effective_date` 填 null（決定 Z3 的發生率）。沙箱根下有 10 個 CORP_ACTION 工作，我沒有逐個讀它們的實際輸出；要判定需要讀那 10 份 response，本輪沒做完。
3. 規則書的「安全邊際」是不是刻意設為零——投資哲學，不是 bug。我記的是 `handoff.md`「B 級上限仍是 IV_base，結論穩定」與 `rulebook.md:263/265/278` 的字面衝突，加上 `r_d = max(10%, 30y+3pp)`、`g0b = min(max(hist), 10%)` 這兩個**真實存在的抵銷邊際**（Z2 的「沒有安全邊際」應讀作「相對於公平價為零」，不是「沒有任何保守假設」；原稿 §3 已把 10% 折現率寫在句子裡，但我在這裡把它標成讀者最容易誤解的一點）。
4. M13（同日重跑重複下單）：`plan_tranches`／`plan_sells` 未逐行讀完，`[部分]` 不解除。
5. 11.0 提到的並行寫入程序若在今天 11:30 之後又動過 `state/`，本報告所有「實查 state DB」的數字以 11:30 那個時間點為準，需要重取。

