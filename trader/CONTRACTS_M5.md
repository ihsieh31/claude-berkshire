# M5 介面契約：組合層、紙上帳本、公司行動、每日收盤

## 規則來源
- `plan.md` §7、§8
- `berkshire-deep-merged/SKILL.md` §5、§6
- `berkshire-deep-merged/references/procedure/06_certainty_position.md`、`12_auto_buy_gates.md`
- `routing/prompts/CORP_ACTION.md`、`routing/output_schemas.json#CORP_ACTION`、`routing/model_routing.json` 的 `jobs.CORP_ACTION`

資料表已在 `trader/trader/db.py` 定義：orders、fills、positions、cash_ledger、corporate_actions、nav_daily、anomalies。不要改 schema。

> **2026-10 稽核修正後同步（P49，M7-D2-08）**：本契約原為 M5 子代理的開發規格；撮合價、公司行動去重、daily 決策窗口等已依
> 設計決策 D03–D07、D15、D50 與修正 B6／M6a／H1（P16、P22、P23、P27、P28）改過，下文已改為現行規則。委託與撮合口徑另見
> `CONTRACTS_M7.md`「委託與撮合口徑」；更細的行為以各模組說明（broker.py、corp_actions.py、daily.py、sizing.py）為準。

## 共用約定
- 代號一律經過 `data.universe.normalize_ticker`。
- 金額單位為美元。股數為整數，不允許零股持有。
- 帳本寫入要在單一交易內完成，可沿用 `analysis.writeback._write_rows`，或自行 `BEGIN IMMEDIATE`。
- 測試一律不連網：Alpaca、SEC 用假物件，模型用 FakeRunner。
- **暫停交易**：anomalies 中有 `status='open'` 且 `pause_trading=1` 的代號，不得撮合、不得下新單。
  - 判斷用 `book.paused(conn) -> set[str]`。
  - 例外（D06）：公司行動等系統暫停只擋買進與部分減碼，SELL_ALL（論點證偽全清）照常下單撮合；人工暫停（manual_pause，`trader ledger pause`）買進與 TRIM 拒絕下單，SELL_ALL 照建委託、撮合時擋，解除後次日自動撮合（不要另補賣單，A3-M8C2-04）。

---

## A. trader/ledger/book.py、trader/ledger/broker.py（子代理 C）

### book.py

```python
class LedgerError(Exception): ...

def ensure_initial_deposit(conn, initial_cash: float, date: str) -> None
    # cash_ledger 沒有任何 deposit 時，寫一筆 deposit
def cash(conn) -> float
    # kind != 'dividend_receivable' 的 amount 總和
def receivables(conn) -> float
    # kind == 'dividend_receivable' 的 amount 總和
def positions(conn) -> dict[str, dict]
    # {T: {qty, avg_cost, first_buy_date}}
def open_orders(conn, ticker: str | None = None) -> list[dict]
def paused(conn) -> set[str]
def committed_buy_value(conn) -> float
    # 未成交買單的 qty × limit_price 總和
def equity(conn, closes: dict[str, float]) -> float
    # 現金＋應收股利＋Σ qty × close
    # 缺收盤價時用 avg_cost，並把該代號列入回傳的警示（見 nav）
def record_nav(conn, date: str, closes: dict[str, float], benchmark_close: float | None) -> dict
    # upsert nav_daily；data 內含 {weights, missing_close: [...], receivables}
def show(conn, closes: dict | None = None) -> dict
    # 現金、應收、持股（含權重）、未成交委託、未結異常
def adjust(conn, *, date, cash_delta=None, ticker=None, set_qty=None, set_avg_cost=None,
           resolve_anomaly: int | None = None, unpause: str | None = None, note: str) -> dict
    # 人工調整：
    # - cash_delta 寫一筆 kind='adjust'
    # - set_qty 為 0 時刪除該持股
    # - resolve_anomaly：該筆 status='resolved'，寫入 resolved_at、resolution=note
    # - unpause：該代號所有未結且 pause_trading=1 的異常，一律 resolved
    # - note 必填
```

**股利現金流：**
- 除息日：寫一筆 `dividend_receivable`，金額 `+X`。
- 發放日：寫兩筆，`dividend +X` 與 `dividend_receivable −X`。
- `ref` 寫成 `CA:{T}:{type}:{effective_date}`。

### broker.py

```python
class BrokerError(Exception): ...

def place_order(conn, ticker, side, qty, limit_price, decision_id, created_date) -> str
```
- `order_id` 格式：`{T}-{side}-{YYYYMMDD}-{n}`。
- 檢查：qty 為正整數；BUY 必須有 `limit_price > 0`；SELL 的 `limit_price` 可以是 None，代表市價（隔日典型價成交）。
- 暫停代號拒單，丟 `BrokerError`（SELL_ALL 例外見共用約定）。

```python
def cancel_order(conn, order_id, reason, status="cancelled") -> None   # status 可為 cancelled 或 expired

def recheck_pending(conn, date, closes: dict[str, float], slippage_bps: float) -> list[dict]
    # D05：撮合前以前一交易日收盤重核冷靜單（PRICE 觸發的 BUY／ADD：收盤×(1＋滑價) < 現行上限）
    #      與估值 TRIM（收盤 ≥ 觸發線）；所有未成交買單另跑 sizing.revalidate_open_buys；缺前一日收盤即取消；
    #      不符即取消並寫理由。人工下單（MANUAL）不重核。

def match_orders(conn, date, bars: dict[str, dict], slippage_bps: float, commission: float) -> list[dict]
```
- `bars` 格式：`{T: {open, high, low, close}}`，只含當日；撮合只用 high、low、close。
- 只撮合 `created_date < date` 的 open 委託，也就是「下一交易日撮合」。暫停代號不撮合（SELL_ALL 例外見共用約定）。缺高／低／收的代號不撮合。
- 成交價（D07，紙上交易近似；SKILL §5 避開開盤）：典型價 `TP = (high + low + close) / 3`，滑價 `bps/1e4`。
- **BUY**（一律限價）：
  - 成交條件：`low <= limit`。
  - 成交價：`min(TP×(1＋滑價), 限價)`——不劣於限價（B6 起已非 `min(開盤, 限價)×(1＋滑價)`）。
  - 現金不足時，股數降為 `floor((cash − commission) / 成交價)`；降到 0 股就不成交，委託保留；降量成交後委託標 filled，其餘股數不再撮合（cancel_reason 記部分成交）。
- **SELL**：
  - 有限價（估值類 TRIM 限價＝觸發線，D03；人工 `trade sell --limit`）：`high >= limit` 時成交，成交價 `max(TP×(1−滑價), 限價)`；
    未達即作廢（status＝expired，當日有效、回落即停），並把登錄表同日 `last_trim` 標 unfilled。
  - 無限價（市價）：成交價 `TP×(1−滑價)`。
  - 股數 `min(委託股數, 持股數)`；無持股 → 取消委託。
- **全有或全無**：除了上述現金／持股不足的情況，不做部分成交。
- **入帳**（同一交易內完成）：
  - fills 寫一列。
  - cash_ledger 依方向寫 `buy`（負數）或 `sell`（正數）；commission 大於 0 時另寫一筆 `commission`（負數）。
  - positions：
    - 買進：avg_cost 用加權平均，不含手續費；第一次買進時寫 first_buy_date。
    - 賣光：刪除該持股。
  - registry：若有該代號，`data.cost` 改為新的 avg_cost；買進後 status 改為 HOLDING；賣光後改為 WATCH。
    - 必須合併既有 data，不得清掉其他欄位。
  - 委託狀態改為 filled。
- 回傳本日所有成交。

### 測試：`tests/test_ledger.py`
至少涵蓋：
- 撮合：買、賣、無限價賣
- 只在下一日撮合
- 現金不足減量
- 暫停代號不撮合
- avg_cost 與 first_buy_date
- 賣光刪除持股
- 股利應收轉入帳
- record_nav
- adjust 各分支

---

## B. trader/ledger/corp_actions.py＋CorpActionHandler（子代理 E）

### 偵測

```python
def detect(conn, date, prev_date, tickers: list[str], alpaca_actions: list[dict],
           filings_8k: dict[str, list[dict]], bars_prev: dict, bars_today: dict) -> list[dict]
```

事件來源有三種：
1. Alpaca 公司行動：格式見 `data/alpaca.py` 的 `_normalize_action`。只取 `ex_date`（若無則用 `effective_date`），日期須落在 `(min(prev_date, date − 14 天), date]`
   （回看 `ALPACA_LOOKBACK_DAYS=14` 曆日，晚登錄的事件也抓，M7-D9-02）。晚到事件（事件日 ≤ prev_date）的 price_before／price_after 設 None（比例類因此轉人工）。
2. 8-K：項目含 3.03、5.03 或 8.01，且 filingDate 在 `(prev_date, date]`。
3. 價格跳空：`|close_today / close_prev − 1| >= 0.40`，且同代號在 `(prev_date, date]` 內沒有前兩種事件（回看窗口內的舊事件不壓掉今日跳空）。

每筆事件的格式：
```
{event_id, ticker, source: alpaca|8k|gap, date, text, alpaca: dict|None, url: str|None,
 price_before, date_before, price_after, date_after}
```

**防重複（B6／P28 後）：**
- 偵測：Alpaca 事件以 `event_id` 比對 corporate_actions.data.event.event_id 與既有 `corp_action%` 異常的 detail.event.event_id（同日可有多筆不同類型）；
  8-K 與跳空：corporate_actions 已有同 `(代號, 事件日)` 的列即視為已解釋 → 略過。同一次偵測中相同 `event_id` → 略過。
- 入帳：去重鍵與資料表唯一鍵一致，為 `(代號, 類型, 生效日)`（同日拆股＋股利是兩筆）。同鍵已有列：同一 Alpaca 事件或既有列來自 8-K → `duplicate`；
  本次事件也來自 Alpaca 且既有列是另一筆 Alpaca 事件 → `conflict`，轉人工（M7-D1-02）；8-K／跳空事件遇既有列一律 `duplicate`。
  例外（M3-R8-01）：既有列來自 8-K／跳空且未真正入帳（`pending_human`／`anomaly`，或人工結案時沒動帳）→ Alpaca 事件照常入帳、就地改寫該列（原內容存 `data.superseded`，該列的未結異常結案）；建列後有人工調帳 → `conflict`。
- `event_id` 格式：`{T}:{source}:{date}:{序號或 alpaca id}`。

### 覆核

```python
def verify(result: dict, event: dict, position: dict | None) -> list[str]
```
回傳錯誤清單；空清單代表通過。

1. **需要人工**：`needs_human` 為 true，或 action_type 屬於分拆、併購換股、其他 → 回傳「需要人工」。
   - 1b 來源旁證（P28）：現金股利、代號變更只認 Alpaca 事件；Alpaca 事件須帶類型且與 action_type 對得上（缺類型或不符 → 轉人工）；
     代號變更的新代號須合格式、等於 Alpaca 新代號，生效日等於事件日。「無公司行動」只限 8-K 事件。
2. **比例與 Alpaca 一致**：有 Alpaca 比例時，`|ratio / alpaca_ratio − 1| <= 0.001`。
   - Alpaca 比例 = `new_rate / old_rate`，欄位名請照 `_normalize_action` 的實際輸出。
3. **比例與價格比相符**：只在拆股、合股、股票股利時檢查。
   - 條件：`|(price_before / price_after) / ratio − 1| <= 0.15`。
   - 價格缺漏 → 回錯誤「缺前後價格，無法覆核」。
4. **市值守恆**：用同一價格基準計算。
   - 調整前市值 = `qty × price_before`。
   - 調整後市值 = `floor(qty × ratio) × (price_before / ratio)` ＋ 零股現金 ＋ `qty × cash_per_share`（現金對價）。
   - 相對差須 ≤ 0.02。
   - 沒有持股時略過此項。
5. **現金股利**：須有 `cash_per_share > 0`，且 `effective_date` 存在。

### 入帳

```python
def apply(conn, paths, result: dict, event: dict, date: str) -> dict
```
全部在同一交易內完成：

**拆股、合股、股票股利**（比例 r）：
- 持股：`qty' = floor(qty × r)`；`avg_cost' = avg_cost × qty / qty'`，但零股以現金了結時 `avg_cost' = avg_cost / r`（零股成本已以現金收回，M7-D1-07）。
- 零股：以 `price_after` 現金了結，寫 `corp_action_cash`。
- 未成交委託：`qty' = floor(qty × r)`，`limit' = limit / r`。股數變成 0 時委託改為 cancelled。
- 登錄表：`auto_buy_cap`、`sell_review_above`、`absurd_price_line`、`cost`、`review_price` 這幾個每股欄位都除以 r，必須合併 data。
- `state/cap_inputs/{T}.json`：每股欄位除以 r，包括 `oe`、`norm_eps`、`iv_cons`、`iv_base`、`iv_opt`、`price`、`insurance.tbv_ps`、`insurance.float_ps`。檔案存在才處理。

**現金股利：**
- 有持股時，寫 `dividend_receivable`，金額 `qty × cash_per_share`。
- `pay_date` 存進 corporate_actions 的 data，供 `settle_dividends` 使用。

**代號變更：**
- 改 positions、orders、registry 的 ticker，並把 cap_inputs 檔改名。
- 新代號在 registry 已存在 → 改為不入帳，寫 anomaly。

**不入帳、回 `conflict`（由 post 改走 reject）：** 代號變更的新代號已存在；本次 Alpaca 事件遇同日同類型另一筆 Alpaca 事件已入帳；
該檔有待人工的暫停時的比例／代號類行動；比例類或現金股利在事件日（含）之後該檔已有成交。

**最後：** 寫一列 corporate_actions（status='applied'），source 為事件的 source，data 存入 `{result, event}` 與改前改後紀錄（持股、委託，M7-D5-07）。

```python
def settle_dividends(conn, date) -> list[dict]
```
- 處理 corporate_actions 中現金股利、`data.pay_date <= date`、且尚未發放的列（以 `data.paid` 標記）。
- 每列寫入 `dividend +X` 與 `dividend_receivable −X`，再把 `data.paid` 設為 true。

```python
def reject(conn, result: dict | None, event: dict, errors: list[str]) -> None
```
- 寫 anomalies：`kind='corp_action_check_failed'`，`pause_trading=1`，detail 存入 `{event, result, errors}`。
- 寫 corporate_actions 一列：status 為 `anomaly`；屬於需要人工時為 `pending_human`。
- 有 `result.effective_date` 時用它，否則用 `event.date`；防重複鍵為 `(代號, 類型, 生效日)`，同鍵已有列時不另寫 corporate_actions。
- **需要人工者（含分拆、併購換股的現金對價）一律不入帳**；現金對價記在異常 detail，由人工處理。

### CorpActionHandler

位於 `trader/trader/jobs/handlers.py`，把現有類別改到 `trader/trader/jobs/corp_jobs.py`，再由 handlers.py 匯入。
- `params`：`{event, position, open_orders}`。
- **prompt_values**：`event_text`、`shares`、`cost`、`open_orders`、`price_before`、`date_before`、`price_after`、`date_after`。CORP_ACTION 不受價格隔離限制。
- **post**：
  1. `errs = verify(result, event, position)`。
  2. errs 為空 → `apply`；否則 → `reject`。
  3. 結果寫入 ticker_dir 的 `corp_action.json`。
- **on_invalid**：沿用現有寫 anomaly 的邏輯，另外補寫 corporate_actions（status='anomaly'，鍵用 `event.date`）。

### 測試：`tests/test_corp_actions.py`
至少涵蓋：
- 拆股 1→4
- 合股 10→1（含零股現金）
- 比例和 Alpaca 不符
- 價格比不符
- 需要人工
- 分拆只入帳現金
- 現金股利的應收與發放
- 代號變更
- 防重複
- 跳空偵測
- 委託與登錄表、cap_inputs 同步調整
- 用 FakeRunner 跑 handler 整條流程

---

## C. trader/portfolio/sizing.py＋trader/pipelines/daily.py＋cli.py（子代理 D）

C、E 的函式還沒寫好時，依本契約的簽名呼叫。測試可以等另外兩個子代理完成後再跑；若跑不起來，用 xfail 標記。

### sizing.py：純函式，回傳計畫，不直接下單

```python
GRADE_CAP = {"A": 0.15, "B": 0.10}
MIN_T = 0.03
MAX_HOLDINGS = 10        # 最多 12，保留 2 空名額
MAX_NEW_PER_DAY = 4
CASH_FLOOR = 0.05
ADV_FRAC = 0.01
BUILD_DAYS = 15
NO_ADD_ABOVE = 0.15
CANCEL_ABOVE_CAP_DAYS = 10

def target_weight(card: dict, user_cap: float | None) -> tuple[float, list[str]]
    # T = min(訊號卡 target_weight_pct/100（或 position_plan.target_weight_pct）,
    #         等級上限（certainty_grade；C、D 視為 0）,
    #         card.max_weight_pct 中的非空值/100, user_cap)
    # T < 0.03 → 回傳 0 與理由

def plan_buys(conn, asof, decisions: list[dict], quotes: dict[str, float],
              adv: dict[str, float], slippage_bps: float) -> list[dict]
```

**plan_buys 的輸入與流程：**
- `decisions`：當日 action 為 BUY 或 ADD 的決策，包含 decision 列與該檔最新訊號卡。
- 下單前自檢，全部通過才產生計畫：
  - 決策日誌的 gate1、gate2、gate3 都為真。
  - `quote × (1 + slip) < auto_buy_cap`。
  - 代號未暫停。
  - 持股數＋新部位數 ≤ MAX_HOLDINGS。
  - 當日新部位 ≤ 4。
- 持股已 ≥ 10 檔時不建新部位，理由記「需勝最弱持股（G8），留待人工」。
- 可動用現金 = 現金 − 計畫水位（5% × `account.cash_plan_buffer`，預設 1.2＝6%，D15）× 權益 − 已承諾買單；5% 為硬下限。
- 每筆計畫：
  - 金額 = `first_tranche_pct_of_target`（預設 50）% × T × 權益，ADD 則依 build_plan 決定。
  - 再限制在 ≤ ADV × 1%、≤ 可動用現金。
  - `limit = round_down(auto_buy_cap × 0.9999, 2)`，嚴格低於上限（D04；不再取 min(…, quote×(1＋滑價))）。
  - `qty = floor(金額 / (quote × (1 + slip)))`（收盤只用來算股數）；為 0 時略過並記理由。
- 名額或現金不足時要排序：
  - 用訊號卡的 G8 調整後預期報酬欄位（請在 schema 與報告格式中尋找；找不到就說明）。
  - 沒有該欄位時，退回用 `price_to_conservative` 升冪排序，並在計畫的 notes 標「推論：缺 G8 欄位」。
- 計畫格式：`{ticker, side: "BUY", qty, limit_price, decision_id, reason, notes}`。

```python
def plan_tranches(conn, asof, quotes, adv, slippage_bps) -> tuple[list[dict], list[dict]]
```
- 回傳 `(加碼計畫, 取消清單)`。
- 處理對象：登錄表 `data.build_plan` 存在，且 `first_buy_date` 起算 ≤ 15 個交易日（用曆日 21 天近似，註明）的持股。
- 加碼許可，四項全過才產生下一批：
  - 最近一次 THESIS 不是疑似或證偽。
  - 最近一次有 gate 紀錄的決策三閘門全過，且在 365 天內。
  - 報價 < 上限。
  - 權重 ≤ 15%。
- 每批金額 = (T × 權益 − 目前市值) ÷ 剩餘批數，`build_plan.tranches_left` 預設 3。
- `build_plan.days_above_cap` 連續 10 天以上 → 計畫取消，並取消該代號的未成交買單。

```python
def plan_sells(conn, asof, decisions: list[dict], quotes: dict) -> list[dict]
```
- `SELL_ALL`：賣出全部持股，`limit_price` 為 None。這是論點證偽，不受一年限制。
- `TRIM`：賣出 `floor(qty × trim_fraction)`（HOLD_REVIEW 帶比例；其他來源減半）；估值類 TRIM 掛限價＝觸發線（當日有效，D03），其他 `limit_price` 為 None（市價）。
- **權重超限**（D01）：權重 > `account.user_hard_cap`（制度類）→ 只減到上限；因上漲超過等級上限 → 不賣，記 `weight_over_cap` 異常（不暫停）並停止加碼。
- 賣出前先取消該代號的未成交買單。

**冷靜期：**
- 由 PRICE 觸發（決策 data 的 `review_type == 'PRICE'`）的 BUY、ADD、TRIM，委託的 `created_date` 設為下一個交易日，等於延後一天撮合；撮合前由 `recheck_pending` 重核（D05）。
- SELL_ALL 與建倉批次（review_type＝TRANCHE）不受冷靜期限制。

### pipelines/daily.py

```python
def run_daily(cfg, paths, conn, date, clients=None, router=None, skip_review=False, dry_run=False) -> dict
```

**流程：**
1. 用 `alpaca.calendar(date, date)` 判斷是否為交易日；不是 → 回傳 `{"skipped": "非交易日"}`。
   - 前一交易日 = calendar(date − 10 天, date) 中，date 之前的最後一天。
2. `ensure_initial_deposit(cfg.account.initial_cash, date)`。
3. 代號範圍：持股 ∪ 未成交委託 ∪ 登錄表 WATCH ∪ benchmark。
   - 抓 date − 40 天到 date 的日線（raw），用來算 bars_prev、bars_today 和 20 日平均成交額（ADV = close × volume）。
4. 抓 Alpaca 公司行動（查詢區間 date − 14 天到 date + 60 天：端點以處理日過濾，CA_PROCESS_LAG_DAYS；再由 `detect` 依事件日過濾，見 §B）＋ SEC 8-K（只抓持股，since=prev_date）→ `detect` → 建 CORP_ACTION 工作，交給引擎執行（引擎 run_id 為 `daily-{date}:ca`；`runs['daily-{date}']` 只由 run_daily 寫）。
5. `settle_dividends(date)`。
6. `recheck_pending`（D05）→ `match_orders(date, bars_today)`。
7. `skip_review` 為否時，執行 `run_review(scope="holdings", run_id=f"review-{date}-holdings")`。
8. 讀決策：`load_decisions(since=prev_date, late_after=上次 daily 讀決策的時刻)`——決策日 ∈ `(prev_date, date]`，另加決策日 ≤ prev_date 但在上次 daily 讀完之後才寫入者
   （等待同意數日後才完成的分析，M6a／M7-D7-04）；首次 daily 回看 30 曆日（M8-D7-01）。略過已作廢、MANUAL 與框架自發（PORTFOLIO）決策；買進每檔只看最後一筆，賣出 SELL_ALL 優先（D02）。依序：
   - plan_sells → place_order
   - plan_tranches → place_order 或 cancel
   - plan_buys → place_order
   - 冷靜期依上述規則設定 created_date。
   - 觸發檢查失敗（D50）：只擋失敗檔的新買與加碼；觸發檢查整體出錯、CORP_ACTION 工作失敗、無法歸屬單檔或當日失敗 ≥ 3 檔 → 停全組合。
9. 過期處理：超過 15 個交易日仍未成交的買單（以曆日 21 天近似）→ 標為 expired。
10. `record_nav(date, closes, benchmark_close)`。
11. 寫 runs 表（kind 為 `daily_close`）與 `paths.work/f"daily-{date}"/summary.json`。

寫入模式另有（P16）：紐約 D 須已過 `schedule.daily_close`（`--force` 略過）；非交易日寫 kind＝daily_skip 的 done 列；已有較晚日期完成時不可回頭重跑；
當日完全沒有日線 → 失敗不標 done。寫入指令取 `state/.pipeline.lock`（D10）。

`dry_run=True`：不寫入任何資料，只依現有資料算出賣出與買進計畫（placed=False）。原規格的 FakeRunner＋SAVEPOINT 回滾不可行（db.insert／writeback 會自行 commit），未採用。

### cli.py（只有 D 可以改 cli.py）
```
trader daily --date D [--skip-review] [--dry-run]
trader ledger show
trader ledger adjust --note TEXT [--cash X] [--deposit X] [--ticker T --qty N --avg-cost X] [--resolve ID] [--unpause T]
                     [--unpause-all] [--resolve-corp-action CA_ID]
```
- `ledger show` 不接受 `--date`（P46 刪除）：以最後一筆 nav_daily 的估值價估值，缺價用成本，輸出 `valuation`／`valuation_date`。
- P46 新增的人工操作指令（細節見各指令 `--help` 與《換機匯出與封存程序.md》§4.1）：
```
trader ledger cancel ORDER_ID --note TEXT          # 人工撤單（記已結案的 manual_cancel 稽核異常）
trader ledger pause (T | --all) --note TEXT        # 人工暫停：不撮合；買進與 TRIM 拒絕下單，SELL_ALL 照建委託、解除後次日撮合；解除 adjust --unpause T／--unpause-all
trader trade buy|sell --ticker T --qty N [--limit X] --note TEXT [--anomaly ID] [--date D]   # MANUAL 決策＋委託（買單限價必填）
trader decisions void ID... --note TEXT [--superseded-by ID]   # 作廢決策（不刪列），重跑用新的 --run-id
trader registry retire T --note TEXT               # 改 RETIRED（無持股、無未成交委託才可）
trader jobs list [--run-id R] [--status S] [--all] # 列 run 與未完成工作（唯讀）
trader db backup [--note TEXT]                     # integrity_check 後快照到 state/backups/
```
- `ledger` 子命令呼叫 `book.show` 與 `book.adjust`。
- 從 NOT_IMPLEMENTED 移除 daily 與 ledger。

### 測試：`tests/test_portfolio.py`、`tests/test_daily_pipeline.py`
- 用合成情境跑完整流程：BUY 決策 → 隔日撮合 → 拆股 → 現金股利 → 證偽全清 → 每日淨值正確。
- 另外涵蓋各項限制與冷靜期。
