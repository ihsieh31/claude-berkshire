# M0 介面契約（各模組必須照此簽名實作；已依實作更新）

已完成、不得修改：`trader/paths.py`、`trader/models.py`、`trader/llm/base.py`、`tests/conftest.py`、`pyproject.toml`。先讀這幾個檔。
規則來源（唯讀）：`../routing/model_routing.json`、`../routing/output_schemas.json`、`../routing/prompts/*.md`、`../plan.md`。

通用：Python 3.11+，`from __future__ import annotations`；只用標準庫＋pyyaml、httpx、pandas、jsonschema、fastapi、uvicorn、openpyxl、pytest。
註解與訊息用繁體中文。測試不得連網、不得呼叫真的 `claude`；用 `tmp_path`、monkeypatch。
**安全**：不得把任何 email 或金鑰寫死在程式；秘密只從環境變數讀；設定檔只存環境變數名稱。

---

## trader/config.py（代理 A）

```python
class ConfigError(Exception): ...          # 訊息列出所有問題（不是只第一個）

@dataclass AccountCfg: initial_cash: float = 100000; slippage_bps: float = 10; commission_per_order: float = 0; benchmark: str = "SPY"
@dataclass BudgetCfg: max_llm_calls_per_run: int = 200; max_cost_usd_per_run: float = 100.0; max_input_tokens_per_run: int = 0（0＝不限）
@dataclass DataCfg: sec_user_agent_env="SEC_USER_AGENT"; alpaca_key_env="ALPACA_API_KEY"; alpaca_secret_env="ALPACA_SECRET_KEY"; alpaca_feed="iex"; fred_key_env="FRED_API_KEY"
@dataclass DashboardCfg: host="127.0.0.1"; port=8787
@dataclass Config:
    account: AccountCfg; universe: str; schedule: dict; models: dict[str, TierConfig]
    job_tier_overrides: dict[str, str]; budget: BudgetCfg; data: DataCfg; dashboard: DashboardCfg
    source_path: Path | None
    def secret(self, env_name: str | None) -> str | None   # os.environ.get；None/空字串 → None

def load_dotenv(path: Path) -> dict[str, str]   # 簡單 KEY=VALUE 解析（# 註解、引號），不覆蓋已存在的環境變數；回傳實際設定的鍵
def load_config(path: str | Path | None = None, env_file: str | Path | None = None) -> Config
```
- `path` 預設：環境變數 `TRADER_CONFIG` → `{PROJECT_DIR}/config.yaml`。檔案不存在 → ConfigError（提示 `cp config.example.yaml config.yaml`）。
- `env_file` 預設 `{PROJECT_DIR}/.env`，存在才讀。
- 驗證：`models` 必須有 strong／medium／weak；backend ∈ BACKENDS；openai_compat 需 base_url、api_key_env、model；claude_code、fake 需 model；任一 tier 出現 `api_key` 鍵（明文金鑰）→ 錯誤；`job_tier_overrides` 的鍵 ∈ JOB_TYPES、值 ∈ TIERS；未知頂層鍵 → 錯誤。
- `schedule` 預設：`{"timezone":"America/New_York","daily_close":"17:30","review_watch":"biweekly","quarterly_screen":["03-20","05-20","08-20","11-20"],"monthly_freeze":"01"}`，使用者設定覆蓋同名鍵；載入時驗證（timezone 為有效 IANA 時區、daily_close 為 `"HH:MM"` 字串（未加引號被 YAML 讀成整數時提示加引號）、quarterly_screen 為 `"MM-DD"` 清單、monthly_freeze 1–28、review_watch ∈ biweekly｜weekly、未知鍵 → 錯誤）。持股固定每日隨 daily 檢查：`review_holdings` 只接受 daily（其他值報錯）。
- `universe` 只接受 `sp500`（其他值 → 錯誤；自訂名單用 `state/universe_override.txt`）。設定檔路徑一律解析為絕對路徑（`source_path`）。

## trader/db.py（代理 A）

```python
MIGRATIONS: list[str]                 # 第 i 個字串 = 版本 i+1 的 SQL（逐敘述執行）
def connect(path: Path) -> sqlite3.Connection   # row_factory=sqlite3.Row；PRAGMA journal_mode=WAL、foreign_keys=ON、busy_timeout=5000；自動建父目錄
def migrate(conn) -> int              # 套用尚未套用的版本，回傳目前版本；冪等。BEGIN IMMEDIATE 取寫鎖後在交易內重讀版本（多行程同時 migrate 安全）；DB 版本比程式新 → RuntimeError
def now_iso() -> str                  # UTC ISO8601，秒精度，結尾 Z
def insert(conn, table: str, row: dict) -> int        # 回傳 lastrowid；dict/list 值自動 json.dumps
def upsert(conn, table: str, row: dict, key: list[str]) -> None   # INSERT ... ON CONFLICT(key) DO UPDATE
BACKUP_KEEP = 14
def backup(db_path: Path, note: str | None = None, keep: int = BACKUP_KEEP) -> Path
    # 本機快照（`trader db backup`；排程 daily 成功後自動呼叫）：PRAGMA integrity_check 非 ok → 寫 db_integrity 異常並 RuntimeError（不寫、不刪舊快照）；
    # 否則以 sqlite backup API 寫 {db 目錄}/backups/trader-YYYYMMDD-HHMMSS[-note].db（本機時間），保留最近 keep 份＋每月最早一份；回傳快照路徑
```
版本 1 的表（欄位照抄；JSON 欄位存 TEXT）：
```sql
schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)
registry(ticker TEXT PRIMARY KEY, status TEXT, first_buy_date TEXT, data TEXT NOT NULL, updated_at TEXT NOT NULL)
decisions(decision_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, decision_date TEXT NOT NULL, run_id TEXT, job_id TEXT, signal TEXT, reason_code TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL)
signals(decision_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, signal TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL)
runs(run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, asof TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, finished_at TEXT, summary TEXT)
jobs(job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, job_type TEXT NOT NULL, ticker TEXT NOT NULL, asof TEXT NOT NULL, tier TEXT, model TEXT,
     status TEXT NOT NULL, deps TEXT NOT NULL DEFAULT '[]', params TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
     input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL, latency_s REAL,
     sandbox TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)
     + INDEX jobs(run_id, status)
llm_calls(call_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, run_id TEXT, tier TEXT, backend TEXT, model TEXT, attempt INTEGER,
     prompt_sha256 TEXT, input_path TEXT, output_path TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, latency_s REAL,
     ok INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL)  + INDEX llm_calls(run_id)
orders(order_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), qty INTEGER NOT NULL,
     limit_price REAL, status TEXT NOT NULL CHECK(status IN ('open','filled','cancelled','expired')), decision_id TEXT,
     created_date TEXT NOT NULL, cancel_reason TEXT, updated_at TEXT NOT NULL)
fills(fill_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL REFERENCES orders(order_id), ticker TEXT NOT NULL, side TEXT NOT NULL,
     qty INTEGER NOT NULL, price REAL NOT NULL, commission REAL NOT NULL DEFAULT 0, fill_date TEXT NOT NULL)
positions(ticker TEXT PRIMARY KEY, qty INTEGER NOT NULL, avg_cost REAL NOT NULL, first_buy_date TEXT, updated_at TEXT NOT NULL)
cash_ledger(entry_id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, amount REAL NOT NULL,
     kind TEXT NOT NULL CHECK(kind IN ('deposit','buy','sell','dividend','dividend_receivable','corp_action_cash','commission','adjust')), ref TEXT, note TEXT)
corporate_actions(ca_id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, action_type TEXT NOT NULL, effective_date TEXT NOT NULL,
     ratio_new_per_old REAL, cash_per_share REAL, new_ticker TEXT, status TEXT NOT NULL CHECK(status IN ('applied','anomaly','pending_human')),
     source TEXT, data TEXT, created_at TEXT NOT NULL, UNIQUE(ticker, action_type, effective_date))
nav_daily(date TEXT PRIMARY KEY, nav REAL NOT NULL, cash REAL NOT NULL, positions_value REAL NOT NULL, n_positions INTEGER NOT NULL, benchmark_close REAL, data TEXT)
anomalies(anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, kind TEXT NOT NULL, detail TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
     pause_trading INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT)
universe_versions(version_id INTEGER PRIMARY KEY AUTOINCREMENT, asof TEXT NOT NULL, source TEXT, tickers TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL)
```
版本 2（M6，opus 同意閘門；同一 run 同一檔至多一列）：
```sql
approvals(approval_id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, run_id TEXT NOT NULL, asof TEXT NOT NULL,
     job_id TEXT NOT NULL, job_type TEXT NOT NULL, tier TEXT, holding INTEGER NOT NULL DEFAULT 0, reason TEXT,
     status TEXT NOT NULL CHECK(status IN ('pending','deferred','approved','rejected','superseded','done')),
     created_at TEXT NOT NULL, decided_at TEXT, decided_via TEXT, note TEXT, UNIQUE(run_id, ticker))  + INDEX approvals_status ON approvals(status)
```
版本 3（D51，決策來源欄）：
```sql
ALTER TABLE decisions ADD COLUMN provenance TEXT   -- JSON（例 {"origin":..,"backend":..,"effort":..,"price_source":..}），NULL＝未標記；由產生／匯出資料的一方填寫，框架不解讀
```

## trader/cli.py（代理 A）

argparse 子命令；`main(argv: list[str] | None = None) -> int`；共用參數 `--config`、`--state-dir`。
- `init`：`paths.ensure()`；DB migrate；印出版本；設定檔不存在 → 提示複製範例並回 1；列出缺少的環境變數（各 tier 的 api_key_env、SEC／Alpaca／FRED）為「警告」不失敗；SEC_USER_AGENT 有值但不含 `@` → 警告；任一 tier backend=claude_code 時用 `shutil.which(tier.claude_bin)` 檢查。全部 OK 回 0。
- `doctor [--skip-models] [--timeout 10] [--state]`：逐項印 `[OK]/[FAIL]/[SKIP] 名稱 說明`。網路項：SEC（GET `https://www.sec.gov/files/company_tickers.json`，header User-Agent=環境變數值；沒設 → SKIP）、Alpaca（GET `https://paper-api.alpaca.markets/v2/clock`，header APCA-API-KEY-ID／APCA-API-SECRET-KEY；沒設 → SKIP）、FRED（GET `https://api.stlouisfed.org/fred/series?series_id=DGS30&api_key=..&file_type=json`；沒設 → SKIP）。模型項：除非 `--skip-models`，對每個 tier 建 `JobSpec(job_type="DOSSIER", ticker="DOCTOR", ...)`，`prompt_text='只回覆 JSON：{"ok": true}'`，用 `trader.llm.router.build_runner(tier_cfg)` 跑一次（`call_with_retry(retries=0)`），抽得 `{"ok": true}` → OK。代理式後端（claude_code、hark_queue）的沙箱建在隔離根（`sandbox.root` 或平台預設）下的 `doctor-*` 子目錄、用完刪除，`build_runner(tier, sandbox.extra_deny, state_dir)`。HTTP 用 httpx；可注入 `http_get` 以便測試。任一 FAIL 回 1。
- `doctor --state [--state-dir D]`：只檢查本機 state（唯讀、不需設定檔、略過網路與模型）：integrity_check、schema 版本 ≤ 程式、待執行工作／待同意＝0、failed 分析工作未寫回＝0（CORP_ACTION 與已補寫回的歷史列 WARN）、決策／訊號卡一致、論點檔與 cap_inputs 存在、scheduler.json 的 last_screen（缺 → WARN）與 gave_up（非空 → FAIL）。換機程序 §2.2／§3.5 使用。
- `run-job JOB_TYPE TICKER --asof DATE [--fake FIXTURE_DIR]`：建一個 run（`run_id=f"manual-{asof}"`）並以 engine 跑單一工作（M0 只需支援 `--fake`；未給 → 印「M3 才支援真實工作」回 2）。engine 由代理 D 實作；cli 以延遲匯入 `from trader.jobs.engine import Engine`。
- 其他（`bootstrap screen review daily scheduler dashboard export ledger validate-report`）：先註冊子命令與 plan.md §11 列的參數，執行時印「尚未實作（里程碑 Mx）」回 2。

## trader/llm/*（代理 B）

- `fake.py`：`class FakeRunner`，`backend="fake"`。`__init__(responses: dict | None = None, fixtures_dir: Path | None = None, model="fake")`。查找順序：`responses[(job_type, ticker)]` → `responses[job_type]` → fixtures `{JOB_TYPE}__{TICKER}.json` → `{JOB_TYPE}.json` → `responses["*"]`；找不到拋 `FatalLLMError`。值若是 `list`：依 `job.attempt` 取第 i 個（超過取最後一個）。值若是 callable：`value(job) -> dict | str`。值若為 `TransportError` 實例 → 直接 raise（測試重試）。dict 轉成 JSON 文字當 `text`；再 `extract_json` → `materialize_files(parsed, job.sandbox)`。token：`input_tokens=len(job.prompt_text)//4`、`output_tokens=len(text)//4`。`self.calls: list[JobSpec]` 記錄每次呼叫（存複本）。用 `write_raw` 存 prompt 與 response。
- `claude_code.py`：`class ClaudeCodeRunner(tier: TierConfig, runner_fn=subprocess.run)`，`backend="claude_code"`。指令：`[tier.claude_bin, "-p", "--model", tier.model, "--output-format", "json", "--allowedTools", "Read,Write,Glob,Grep", "--disallowedTools", "Bash,WebFetch,WebSearch", "--permission-mode", "acceptEdits"]`；提示由 **stdin** 傳入（`input=job.prompt_text`），`cwd=job.sandbox`，`timeout=tier.timeout_s`，`capture_output=True, text=True`。`job.sandbox` 為 None → FatalLLMError。`FileNotFoundError` → FatalLLMError；`TimeoutExpired` → TransportError；returncode≠0 → stderr 含 `rate limit`／`429`／`overloaded` → TransportError，否則 TransportError（訊息含 stderr 前 500 字）。stdout 解析 JSON：`is_error` 為真 → TransportError；`text = data["result"]`；`input_tokens = usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens`（缺則 0）；`output_tokens = usage.output_tokens`；`cost_usd = data.get("total_cost_usd")`；`parsed = extract_json(text)`；Claude Code 自己寫檔，`files_written` = 執行後沙箱內新增或修改的檔案（執行前後比對 mtime）。存 prompt 與 stdout 原文。
- `openai_compat.py`：`class OpenAICompatRunner(tier: TierConfig, client: httpx.Client | None = None, env=os.environ)`。金鑰 `env[tier.api_key_env]`，缺 → FatalLLMError。`POST {base_url.rstrip('/')}/chat/completions`，body：`{"model", "messages":[{"role":"user","content": job.prompt_text}], "temperature", "max_tokens": tier.max_output_tokens}`，`json_mode` 時加 `"response_format":{"type":"json_object"}`；header `Authorization: Bearer`。429 → TransportError(retry_after=Retry-After 秒數或 None)；5xx、連線錯誤、逾時 → TransportError；401/403/404/400 → FatalLLMError（訊息不得含金鑰）。`text = choices[0].message.content`；usage.prompt_tokens／completion_tokens；`parsed = extract_json(text)`；`files_written = materialize_files(parsed, job.sandbox)`。存 prompt 與 response 原文（不得含金鑰與 Authorization header）。
- `router.py`：
  ```python
  def build_runner(tier: TierConfig) -> Runner          # 依 backend；fake → FakeRunner()
  class Router:
      def __init__(self, config: Config, routing_path: Path, dry_run: bool = False, fake: FakeRunner | None = None)
      def tier_for(self, job: JobSpec) -> str           # override > routing jobs[job_type].model；THESIS 為 dict → params.get("holding") 真 → "HOLDING" 否則 "其他"；值 "none" → ValueError
      def runner_for(self, tier: str) -> Runner         # 快取實例；dry_run → 全部回 fake（未給則新建 FakeRunner）
      def tier_config(self, tier: str) -> TierConfig
      def max_context_tokens(self, job_type: str, tier: str | None = None) -> int  # min(routing jobs[jt].max_context_tokens 或 defaults, tier 上限)；未給 tier 取可能 tier 的最小值
  ```

## trader/jobs/validate.py、sandbox.py、context.py（代理 C）

`output_schemas.json` 不是 JSON Schema，是「範例形狀」；`validate.py` 把它編譯成檢查規則：
- dict → 物件：必填鍵＝所有不以 `_` 開頭的鍵；額外鍵允許；遞迴。
- list：空 → 任意 list；首元素 dict → 每個元素依該 dict 檢查；首元素字串 → 每個元素依該字串規則檢查。
- bool → bool；int/float（非 bool）→ 數字（int 或 float，非 bool）。
- 字串 s：先去掉全形括號註解 `（…）` 得 base（strip）。
  1. base 以 `true | false` 開頭 → bool；接受字串 `"true"/"false"`，正規化成 bool。
  2. base 含 `數字或 null` → 數字或 None；base 為 `小數或 0` → 數字。
  3. base 為 `YYYY-MM-DD` → 符合 `^\d{4}-\d{2}-\d{2}$` 且是有效日期；base 為 `YYYY-MM-DD 或 null` → 同上或 None。
  4. base 為 `字串或 null` → str 或 None。
  5. base 含 `{YYYYMMDD}` → 符合 `^[A-Z0-9.\-]+-\d{8}-\d+$`。
  6. base 含 ` | `：選項＝以 `|` 切開並 strip。任一選項含 `…` 或 `..` 或 `：` → 自由字串（選項含 `null` 時也接受 None）；否則嚴格列舉：值必須完全等於某選項；選項 `null` 代表接受 None。
  7. 其他 → 任意 str。
```python
def load_schemas(path: Path) -> dict
def check(job_type: str, obj: Any, schemas: dict) -> tuple[Any, list[str]]   # (正規化後物件, 錯誤清單；空＝合格)；錯誤訊息含 JSON 路徑如 "gates.gate1_screen"
def error_feedback(errors: list[str]) -> str   # 給重試用的繁中說明（列出錯誤，要求只輸出合格 JSON）
```
`sandbox.py`：
```python
class PriceLeakError(Exception); class PriceGateError(Exception)
PRICE_KEYS = frozenset({"price","price_date","market_cap","earn_yield","fcf_yield","norm_earn_yield","pe","pe_ratio","core_rank","research_priority","vol_36m","drawdown_3y","price_context","review_price","auto_buy_cap","sell_review_above","absurd_price_line"})
def is_price_key(k: str) -> bool        # k.lower() in PRICE_KEYS 或含 "price" 或含 "market_cap"
def scrub_prices(obj) -> Any            # 遞迴刪除價格鍵（回傳新物件）
def create_sandbox(work_dir: Path, job: JobSpec) -> Path   # {work_dir}/{run_id 安全化}/{ticker}/_{job_type}_{n}，n＝job_id 末段；已存在則清空重建；設定 job.sandbox
def ticker_dir(work_dir: Path, job: JobSpec) -> Path       # {work_dir}/{run_id 安全化}/{ticker}；同一檔各工作的共用輸出區
def stage_inputs(sandbox: Path, files: list[Path | tuple[Path, str]], job_type: str) -> list[str]
    # 複製進沙箱（tuple 第二項為目的相對名稱）；回傳相對名稱清單。job_type ∈ PRICE_ISOLATED_JOBS 時：
    # 檔名符合 *_price.json → PriceLeakError；.json 檔先 scrub_prices 再寫入；最後呼叫 assert_price_free
def assert_price_free(sandbox: Path) -> None   # 掃描所有檔：*_price.json 存在、或任一 .json 含價格鍵（遞迴）→ PriceLeakError
def open_price_file(sandbox: Path, price_file: Path, gate3_answers: Path, now: float | None = None) -> str
    # gate3_answers 不存在 → PriceGateError；其 mtime >= now → PriceGateError；
    # 否則複製價格檔進沙箱，寫 sandbox/price_file_opened_at.txt（ISO UTC），回傳該時間字串
def collect_outputs(sandbox: Path, dest: Path, names: list[str]) -> list[str]   # 把模型輸出檔複製到 ticker_dir，回傳已複製者
```
`context.py`：
```python
def estimate_tokens(text: str) -> int    # CJK（　-鿿、＀-￯）每字 1；其他字元每 4 字 1；向上取整
@dataclass class Section: name: str; text: str; droppable: int = 0   # 0＝不可刪；數字越大越先刪（knowledge=2、references=1）
def render_template(text: str, values: dict[str, str]) -> str   # 只替換 {鍵} 有在 values 的；其他大括號原樣保留（JSON 範例不能壞）
def build_prompt(job: JobSpec, backend: str, max_tokens: int, pkg_root: Path, sections: list[Section] | None = None,
                 values: dict[str, str] | None = None) -> str
    # 組成：_COMMON.md、prompts/{job_type}.md（都經 render_template；values 預設含 pkg、asof、ticker、work=沙箱路徑）、
    #       再接 output_schemas.json 該工作那一節（json 縮排）、再接 sections。
    # backend == "claude_code"：sections 只列「## 輸入檔」＋沙箱內檔案清單（不內嵌內容）；
    #       其他 backend：內嵌 sections 文字，並加說明「要寫的檔案放在輸出 JSON 的 files：{檔名: 內容}」。
    # 超過 max_tokens：依 droppable 由大到小整段刪除；仍超過 → raise ContextOverflowError（不截斷不可刪段）
class ContextOverflowError(Exception)
```

## trader/jobs/engine.py、handlers.py（代理 D）

```python
# handlers.py
@dataclass class HandlerCtx: paths: Paths; schemas: dict; work_dir: Path; conn: sqlite3.Connection
class Handler:
    job_type: str
    def prepare(self, job: JobSpec, ctx: HandlerCtx) -> list[Section]   # 建沙箱、stage 輸入（job.params["inputs"]: list[str 路徑]）；FULL_B 呼叫 open_price_file
    def post(self, job: JobSpec, result: dict, ctx: HandlerCtx) -> list[JobSpec]   # 收輸出到 ticker_dir；回傳動態新增工作
    def on_invalid(self, job: JobSpec, errors: list[str], ctx: HandlerCtx) -> list[JobSpec]   # 預設寫 decisions NO_ACTION/MISSING_DATA；TRIAGE → 改建 THESIS（視同不確定）；CORP_ACTION → anomalies＋pause_trading，不寫 decisions
HANDLERS: dict[str, Handler]
def next_job(job: JobSpec, job_type: str, ctx: HandlerCtx, params: dict | None = None) -> JobSpec   # job_id=f"{run_id}:{ticker}:{job_type}:{n}"，n 為同 run/ticker/type 下一序號（由 ctx 查 jobs 表）
```
動態升級（post 回傳）：DOSSIER → FULL_A；FULL_A `stopped` 假 → FULL_B（真則不建）；FULL_B `signal=="BUY"` → RED_TEAM；RED_TEAM（`fatal` 真或 `number_errors` 非空）且 `params.redteam_round` 為 0 → FULL_B（params redteam_round=1）；round 1 仍不過 → 不再建，結果標 `final_signal="WATCH"`；TRIAGE `escalate` 真 → THESIS；THESIS verdict ∈ {疑似, 證偽} → DOSSIER；PRICE `needs_full` 真 → DOSSIER；CORP_ACTION → 無。
後置腳本（gate3_check、cap_calc、build_dossier）在 M3 實作：`Handler` 留 `pre_script(job, ctx)`、`post_script(job, ctx) -> list[str]`（回傳錯誤）兩個掛鉤，M0 預設不做事。

```python
class Engine:
    def __init__(self, conn, router: Router, paths: Paths, config: Config, handlers=HANDLERS, schemas=None, sleep=time.sleep)
    def start_run(self, run_id: str, kind: str, asof: str) -> None     # runs 表 upsert（已存在不覆蓋 started_at）
    def add_job(self, spec: JobSpec) -> bool                           # 已存在（同 job_id）→ False，不覆蓋
    def run(self, run_id: str) -> dict                                 # 回 {"done":n,"failed":n,"skipped":n,"pending":n,"status":...}
```
`run` 的行為：
- 主執行緒負責所有 DB 寫入；只有 `call_with_retry(runner, job)` 在 ThreadPoolExecutor 執行；各 tier 同時執行數 ≤ `tier_config.concurrency`。
- 可執行＝pending 且所有 deps 為 done；任一 dep 為 failed/skipped → 本工作 skipped（error 寫原因）。重跑同一 run_id：running 狀態的工作（上次中斷）改回 pending；done 不重跑。
- 每個工作：`tier = router.tier_for`；`handler.prepare` → `context.build_prompt(..., max_tokens=router.max_context_tokens(...))` → 呼叫 → `validate.check`；不合格 → `job.attempt += 1`，提示加 `error_feedback` 再跑一次；仍不合格 → status failed、error="invalid_output"、`handler.on_invalid`。合格 → `handler.post` → `add_job` 新工作 → status done，result 存正規化 JSON。
- 每次模型呼叫（含失敗）寫 `llm_calls`（prompt_sha256、raw 路徑、tokens、ok、error）。jobs 表累加 tokens、attempts、latency、cost。
- `PriceLeakError`／`PriceGateError`／`ContextOverflowError`／`FatalLLMError`／傳輸重試耗盡 → 該工作 failed（error 記類別與訊息），不影響其他工作。
- 預算：本 run 的 llm_calls 筆數 ≥ `budget.max_llm_calls_per_run`、或 cost_usd 總和 ≥ `max_cost_usd_per_run`（>0 時）、或 input_tokens 總和 ≥ `max_input_tokens_per_run`（>0 時）→ 不再啟動新工作（2026-10-17 改：token 含快取讀取，代理式 DOSSIER 每檔 2–8M，原 20M 上限 8 檔即停）；結束時 runs.status="paused_budget"，否則全部終態 → "done"（有 failed 也算 done，summary 記數）。
- `job.log_dir = paths.llm_calls / run_id安全化`。
