# Multi-AI-Agent for Crypto Trading

本專案是一個本地 AI Agent 驅動的虛擬貨幣短線交易原型，先以 `Bybit Demo` / `mock` 驗證多代理決策流程，再逐步往可落地的多標的系統演進。

目前重點是：

- 本地模型推理，預設支援 `Ollama`
- 多代理分工決策與風控
- `Bybit Demo` / `mock` 執行
- Web 控制台、Notion status、Daily Review
- 持續運作的 background runner
- 目前策略方向已收斂為 `USDT perpetual intraday long/short` 優先，而不是中長線配置系統
- 會持續累積資金曲線（equity curve），產生本地 SVG 折線圖，並同步摘要到 Notion
- `Grid / Alpha Arena / 其他外部策略候選` 現在先走 research-only benchmark，不直接覆蓋 live trading
- external benchmark research 現在已能比較：
  - `Donchian + ADX` 的 `10 / 14 / 20` 週期版本
  - `Keltner-filtered Donchian`
  - `ATR + Donchian midline exit` 版本
- daily report 會額外產出 `Shadow Benchmark Watch`，把目前 live baseline 和指定 shadow candidate 做 head-to-head 對照，方便決定是否值得升級研究候選
- `Shadow Benchmark Watch` 目前會以近期 benchmark 連續快照計算 promotion streak，不會因單次領先就直接建議升級
- full cycle 會額外接入基礎 `order flow / market microstructure` 特徵，而不再只看 15m K 線與 sentiment
- daily report / Notion `Daily Review` 會自動產出 `Symbol Postmortem`，優先回顧單一重點標的的走勢、決策分布與主要卡點
- 每筆決策現在都會記錄 `Decision Source`，區分 `base_strategy / fallback / fallback_guard / policy_exit`
- `TradePulse` 的 learning controls 現在會看多日資金曲線，而不只看最近 12 小時；若 equity 持續低於設定基準且連續多日虧損，fallback 會維持受限，benchmark watch 也會強制對齊目前 live symbol
- 每天中午之後會把 `Strategy Review` 併入當天 daily report 與 Notion Daily Review，從 strategist / risk / benchmark / execution 四個角度對同一天表現做複盤
- Daily report 與 Notion `Daily Review` 的主窗口現在是 **台灣時間前一天 12:00 -> 當天 12:00**，不再是曆日 `00:00 -> 24:00`
- daily report 現在也會區分 `carry-in` 倉位關閉與「本窗口新交易」，避免把前一窗口留下來的部位管理和今天的新進場混在一起
- 若設定外部模型 API key，Daily 也可以額外產出 `External AI Review`，把同一天的摘要送去給外部模型做第二視角審稿；這層只用於研究與檢討，不直接影響 live 下單

版本更新與里程碑請看 `CHANGELOG.md`，不再把歷史更新內容全部塞進 README。
若要看 `Alpha Arena` 如何作為 benchmark / research 來源接進目前架構，請看 `ALPHA_ARENA_INTEGRATION_PLAN.md`。
若你已經有一份公開 Alpha Arena / 類 Alpha Arena 訊號 JSON 匯出，可以直接用 `scripts/alpha_arena_import_and_backtest.py` 做第一階段 research benchmark。
若你要一次重跑目前所有外部 benchmark 候選，請用 `scripts/run_external_strategy_benchmarks.py`。
若你要對同一個標的做成本感知的候選策略排名，請用 `scripts/run_strategy_tournament.py`；它現在也支援 `--include-alpha`，並會把 candidate-specific 成本假設與 skipped candidates 一起寫進報告。
目前 `funding rate` 仍未接進 live 或 benchmark，暫時只列為下一階段 research item。

## Repo 目標

這個 repo 已整理成可 Git clone 後再部署的形態：

- 專案路徑不再綁死 `/Users/koshino/...`
- 預設執行資料寫到 repo 內的 `./runtime`
- 提供 `.env.example`
- 提供一鍵 setup script
- 提供 macOS 桌面啟動器

如果你要在另一台電腦上使用，建議流程就是：

```bash
git clone https://github.com/koshino17/Multi-AI-Agent-for-Crypto-Trading.git
cd Multi-AI-Agent-for-Crypto-Trading
./scripts/setup_local_env.sh
```

然後編輯 `.env`，最後啟動：

```bash
./Launch\ Trading\ Agents.command
```

或直接用命令列：

```bash
source .venv/bin/activate
python trading_agents_web.py
```

## 系統需求

最低建議：

- Python `3.9+`
- macOS / Linux
- `Ollama`，若你要跑本地 LLM
- 穩定網路，若你要抓新聞 / CoinGecko / Alternative.me / Bybit API

目前 `requirements.txt` 很輕：

- `ccxt`
- `python-dotenv`

## 快速開始

### 1. Clone repo

```bash
git clone https://github.com/koshino17/Multi-AI-Agent-for-Crypto-Trading.git
cd Multi-AI-Agent-for-Crypto-Trading
```

### 2. 建立環境

最簡單：

```bash
./scripts/setup_local_env.sh
```

手動也可以：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

### 3. 設定 `.env`

最重要的欄位：

```bash
MODEL_BACKEND=ollama
MODEL_NAME=qwen2.5:7b-instruct
OLLAMA_HOST=http://127.0.0.1:11434

TRADING_MODE=mock
OBSERVATION_POOL=SOL/USDT,LINK/USDT,AVAX/USDT
TIMEFRAME=15m
DATA_ROOT=./runtime
INITIAL_BALANCE_USDT=500
```

如果你要接 `Bybit Demo` 現貨：

```bash
TRADING_MODE=bybit-demo
BYBIT_DEMO_API_KEY=...
BYBIT_DEMO_SECRET=...
```

如果你要接 `Bybit Demo` 永續合約（可做多做空）：

```bash
TRADING_MODE=bybit-demo-perp

# 若要先聚焦單一幣種做 intraday attribution，可先收斂成單一 observation pool
OBSERVATION_POOL=SOL/USDT
SYMBOL=SOL/USDT
BYBIT_DEMO_API_KEY=...
BYBIT_DEMO_SECRET=...
```

如果你要同步 Notion：

```bash
NOTION_API_TOKEN=secret_xxx
NOTION_STATUS_PAGE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
NOTION_STATUS_PAGE_TITLE=Trading Agents Live Status
NOTION_DAILY_REVIEW_PARENT_PAGE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
NOTION_DAILY_REVIEW_TITLE_PREFIX=Trading Agents Daily Review
NOTION_DAILY_REVIEW_HOUR=12
EXTERNAL_AI_REVIEW_ENABLED=true
EXTERNAL_AI_REVIEW_PROVIDER=gemini
EXTERNAL_AI_REVIEW_MODEL=gemini-2.5-flash
EXTERNAL_AI_REVIEW_API_KEY=your_api_key_here
EXTERNAL_AI_REVIEW_TIMEOUT_SECONDS=20
```

### 4. 啟動 Ollama

若你使用本地模型，先確定 Ollama 可用，例如：

```bash
ollama serve
ollama pull qwen2.5:7b-instruct
```

### 5. 啟動控制台

macOS：

```bash
./Launch\ Trading\ Agents.command
```

跨平台命令列：

```bash
source .venv/bin/activate
python trading_agents_web.py
```

打開：

```text
http://127.0.0.1:8765
```

## 執行模式

### `mock`

最適合第一次 clone 下來時先驗證流程。

### `bybit-demo`

最接近實際下單邏輯，但仍是模擬資金環境。

### `bybit-demo-perp`

使用 Bybit Demo 的 USDT 永續合約路徑。

- 支援 `buy/sell/hold`
- `buy` 在 perp 模式下代表看多或回補空單
- `sell` 在 perp 模式下代表看空或平掉多單
- 日報 / Live Status 會顯示 `equity / available balance / long-short position / unrealized pnl`
- 新開倉位會自動嘗試設定交易所保護：
  - `PERP_MAX_LEVERAGE`
  - `PERP_MIN_LIQUIDATION_BUFFER_PCT`
  - `PERP_HARD_STOP_LOSS_PCT`
  - `PERP_TAKE_PROFIT_PCT`
  - `PERP_TRAILING_STOP_PCT`
- 已有浮盈的 perp 倉位會在後續 cycle 自動同步保護單，支援簡單的 profit lock ladder：
  - `PERP_PROFIT_LOCK_TRIGGER_PCT`
  - `PERP_PROFIT_LOCK_BREAKEVEN_OFFSET_PCT`
  - `PERP_PROFIT_LOCK_TRIGGER_2_PCT`
  - `PERP_PROFIT_LOCK_STOP_2_PCT`
- intraday 政策預設偏日內，但不是一翻兩瞪眼硬平倉：
  - 若部位久抱後沒有 follow-through，會走 stagnation exit
  - 若持有超過 intraday 規劃窗口，且優勢已弱化，會走 policy exit
  - 若趨勢仍健康、保護單已收緊，系統允許繼續持有
  - 可選擇是否啟用接近日切時段的 de-risk / flatten
  - 若 base strategy 已明確處於 `hold + 低 ADX` 的中性盤，TradePulse 也會抑制缺乏 tape / volume 跟進的 fallback 新開倉，避免 neutral day 被過度方向化

## 預設策略節奏

目前預設是：

- `MONITOR_INTERVAL_SECONDS=30`
- `TIMEFRAME=15m`
- `RUN_INTERVAL_SECONDS=900`
- `LLM_WAKE_MIN_SCORE=4`
- `LLM_WAKE_QUIET_VOLATILITY_PCT=0.15`
- `LLM_WAKE_QUIET_VOLUME_RATIO=1.05`

意思是：

- 每 `30` 秒跑一次 monitor loop
- monitor loop 會對觀察池中的每個標的抓一次「不含 microstructure 的 K 線快照」與一次帳戶狀態，不是只看單一 ticker price
- 完整 decision cycle 主要由新 candle、帳戶變化或價格觸發
- 不會每 30 秒都跑完整 LLM 重分析
- 無持倉、低波動、低量能、且沒有明確區間邊緣的盤面，會先走 Python short-circuit，不喚醒 LLM
- 高頻 monitor 不會抓較重的 `orderbook / recent trades` 盤面特徵；這些只在完整 decision cycle 載入

## Order Flow / Market Microstructure

目前在 full cycle 內，Bybit 公開市場資料會被轉成可用的盤面特徵，接到：

- `MarketSnapshot`
- `market summary`
- `LLM wake gate`
- `StrategistAgent` prompt
- fallback decision bias

目前接入的特徵包含：

- `spread_bps`
- `top_book_imbalance`
- `depth_imbalance`
- `bid/ask wall notional`
- `trade_buy_notional`
- `trade_sell_notional`
- `trade_delta_ratio`
- `large_buy_count / large_sell_count`

這一層不是把整本 order book 原樣丟給 LLM，而是先用 deterministic 特徵把盤面壓縮成可決策語意，讓系統真正具備一點基本的 microstructure awareness。

## Decision Attribution

TradePulse 現在會為每筆決策標記來源：

- `base_strategy`
- `fallback`
- `fallback_guard`
- `policy_exit`

Daily Summary 與 `Symbol Postmortem` 也會顯示 attribution。這能幫我們分清楚：

- 是主策略本身在犯錯
- 還是 fallback override 過度干擾
- 或是 policy exit 太早把單關掉

## 外部策略 Benchmark

目前框架把外部策略分成兩層：

- `live strategy`
  - 目前收斂成單一主策略 `donchian_adx_perp_v1`
- `research-only benchmarks`
  - 目前統一 benchmark：
    - `donchian_adx_perp_v1`
    - `grid_range_reversion_v1`
    - `bollinger_rsi_mean_reversion_v1`
    - `alpha_arena_public_imports`

相關檔案：

- `config/external_benchmark_library.json`
- `trading_agents/external_benchmarks.py`
- `scripts/run_external_strategy_benchmarks.py`
- `scripts/run_strategy_tournament.py`

手動強制跑一輪 benchmark：

```bash
source .venv/bin/activate
python scripts/run_external_strategy_benchmarks.py --force
```

手動對單一標的跑 strategy tournament：

```bash
source .venv/bin/activate
python scripts/run_strategy_tournament.py --symbol SOL/USDT --include-alpha
```

輸出位置：

- normalized benchmark signals:
  - `./runtime/data/external_benchmarks/normalized`
- latest benchmark snapshot:
  - `./runtime/service/external_benchmark_latest.json`
- historical benchmark reports:
  - `./runtime/reports/benchmarks`

這條 benchmark 管線目前不直接碰 executor，但會進入：

- daily report
- Notion live status / daily review 摘要
- 12 小時 strategy reflection

## 目前架構

角色包含：

- `market_collector`
- `sentiment_collector`
- `backtester`
- `strategy_researcher`
- `strategist`
- `risk_supervisor`
- `selector`
- `executor`
- `post_trade_evaluator`

另外還有：

- `runner supervisor`：確保 background runner 持續運作
- `web console`：觀察與 debug
- `Notion sync`：live status / daily review

## 背景服務

目前 runner 不依賴 UI。

也就是：

- 關掉網頁控制台，runner 仍會繼續跑
- runner 若意外退出，supervisor 會把它再拉起來

若你是直接從終端或前景手動跑，服務相關檔案預設都在：

```text
./runtime/service
```

重要狀態檔：

- `runner_supervisor.pid`
- `runner.pid`
- `runner_supervisor.log`
- `runner.log`

若你使用的是 **macOS 開機自動啟動 / `launchd` 常駐版本**，TradePulse 會把自己的 runtime 與 service state 移到：

```text
~/Library/Application Support/TradePulse/runtime
~/Library/Application Support/TradePulse/state
```

這樣做的原因是：

- 避免 `launchd` 直接從 `Documents` 或外接磁碟啟動時遇到 TCC/權限限制
- 讓 runner 的 pid / lock / log / daily report 能由系統級服務穩定寫入
- 確保即使 Codex 或終端關閉，TradePulse 仍能由 macOS 自己接管

macOS 常駐版的關鍵特性：

- 開機後會自動啟動
- runner 被 kill 後，`launchd KeepAlive` 會自動拉回
- 網路暫時中斷時，runner 會在 loop 裡持續重試；若進程真的退出，`launchd` 也會重啟它

## 重要設定

這幾個設定最常調：

```bash
OBSERVATION_POOL=SOL/USDT,LINK/USDT,AVAX/USDT
TIMEFRAME=15m
MIN_SIGNAL_SCORE=0.55
MAX_POSITION_PCT=0.40
MONITOR_INTERVAL_SECONDS=30
RUN_INTERVAL_SECONDS=900
PRICE_TRIGGER_PCT=0.0075
```

若要更偏訓練模式：

```bash
DEMO_AGGRESSIVE_MODE=true
EXPECTANCY_FLOOR_PCT=-0.03
MICRO_CYCLE_TRIGGER_PCT=0.0040
POSITION_MICRO_TRIGGER_PCT=0.0030
TRADE_COOLDOWN_SECONDS=900
TRADE_COOLDOWN_SINGLE_SYMBOL_CAP_SECONDS=300
TRADE_COOLDOWN_TREND_MULTIPLIER=0.35
TRADE_COOLDOWN_MIN_SECONDS=120
TRADE_COOLDOWN_REENTRY_MOMENTUM_PCT=0.25
TRADE_COOLDOWN_REENTRY_TRADE_DELTA_RATIO=0.35
TRADE_COOLDOWN_REENTRY_VOLUME_RATIO=1.20
FALLBACK_ENTRY_GUARD_ENABLED=true
FALLBACK_ENTRY_MIN_SCORE=0.72
FALLBACK_ENTRY_MIN_MOMENTUM_PCT=0.20
FALLBACK_ENTRY_MIN_VOLUME_RATIO=1.25
FALLBACK_ENTRY_MIN_TRADE_DELTA_RATIO=0.30
BUY_BALANCE_BUFFER_PCT=0.95
FEE_HURDLE_MULTIPLIER=1.15
FAST_CYCLE_SIGNAL_BOOST=0.08
LLM_TIMEOUT_SECONDS=18
SENTIMENT_REQUEST_TIMEOUT_SECONDS=6
SENTIMENT_CACHE_TTL_SECONDS=120
LLM_FULL_CYCLE_ONLY=true
LLM_SELECTED_CANDIDATE_ONLY=true
LLM_WAKE_GATE_ENABLED=true
LLM_WAKE_MIN_SCORE=4
LLM_WAKE_POSITION_MIN_SCORE=1
LLM_WAKE_VOLATILITY_PCT=0.30
LLM_WAKE_MOMENTUM_PCT=0.25
LLM_WAKE_VOLUME_RATIO=1.35
LLM_WAKE_BREAKOUT_PROXIMITY_PCT=0.12
LLM_WAKE_POSITION_MOVE_PCT=0.20
LLM_WAKE_DEPTH_IMBALANCE=0.22
LLM_WAKE_TRADE_DELTA_RATIO=0.35
LLM_WAKE_LARGE_TRADE_COUNT=4
LLM_WAKE_QUIET_VOLATILITY_PCT=0.15
LLM_WAKE_QUIET_VOLUME_RATIO=1.05
LLM_WAKE_QUIET_VOLUME_RATIO=0.95
MARKET_MICROSTRUCTURE_ENABLED=true
ORDERBOOK_DEPTH_LIMIT=25
RECENT_PUBLIC_TRADE_LIMIT=60
MICROSTRUCTURE_CACHE_TTL_SECONDS=5
DUST_POSITION_MULTIPLIER=1.0
PERP_MAX_LEVERAGE=2.0
PERP_MIN_AVAILABLE_BALANCE_RATIO_PCT=10.0
PERP_MIN_LIQUIDATION_BUFFER_PCT=8.0
PERP_HARD_STOP_LOSS_PCT=1.2
PERP_TAKE_PROFIT_PCT=2.4
PERP_TRAILING_STOP_PCT=0.0
PERP_ENABLE_PROTECTION_ORDERS=true
EXTERNAL_BENCHMARK_ENABLED=true
EXTERNAL_BENCHMARK_REFRESH_HOURS=4
EXTERNAL_BENCHMARK_LIMIT=320
EXTERNAL_BENCHMARK_MAX_ALPHA_SIGNALS=1000
EXTERNAL_AI_REVIEW_ENABLED=false
EXTERNAL_AI_REVIEW_PROVIDER=gemini
EXTERNAL_AI_REVIEW_MODEL=gemini-2.5-flash
EXTERNAL_AI_REVIEW_API_KEY=
EXTERNAL_AI_REVIEW_TIMEOUT_SECONDS=20
INTRADAY_MAX_ENTRIES_PER_EPISODE=3
```

其中：

- `LLM_SELECTED_CANDIDATE_ONLY=true` 代表 full cycle 先用規則與摘要跑完整個觀察池，再只對 selector 最後挑中的候選做 LLM 風控辯論，避免每輪每個標的都重跑重型辯論。
- `LLM_WAKE_GATE_ENABLED=true` 代表每個候選標的會先用 volatility、momentum、volume expansion、breakout proximity、depth imbalance、trade delta 和 large prints 計算 `wake_score`，沒達標就不喚醒重型 LLM。
- `MARKET_MICROSTRUCTURE_ENABLED=true` 代表 full cycle 會額外抓公開 `orderbook` 與 `recent public trades`，轉成盤面特徵再餵進主決策管線。
- `ORDERBOOK_DEPTH_LIMIT` / `RECENT_PUBLIC_TRADE_LIMIT` 用來控制每輪 order flow 特徵抽取的資料量。
- `MICROSTRUCTURE_CACHE_TTL_SECONDS=5` 會讓同一個 exchange client 在短時間內重用最近一次盤面特徵，避免高頻監控被重型市場資料拖慢。
- `DUST_POSITION_MULTIPLIER=1.0` 代表低於交易所最小下單額的殘餘倉位會被視為 dust，保留在帳戶中，但不再拿來當成可賣持倉參與決策。
- `SENTIMENT_REQUEST_TIMEOUT_SECONDS=6` 與 `SENTIMENT_CACHE_TTL_SECONDS=120` 用來壓低情緒資料抓取延遲；像 Fear & Greed、CoinGecko trending、共用 RSS 這些來源，會在短時間內重用快取而不是每個標的都重抓一次。
- `PERP_MAX_LEVERAGE=2.0` 會在風控審批時限制有效槓桿，避免合約曝險擴得太快。
- `PERP_MIN_LIQUIDATION_BUFFER_PCT=8.0` 會在現有倉位距離強平太近時擋下新的加碼。
- `PERP_HARD_STOP_LOSS_PCT` / `PERP_TAKE_PROFIT_PCT` 會在新開倉後嘗試設定交易所 stop loss / take profit。
- `INTRADAY_MAX_ENTRIES_PER_EPISODE=3` 會限制單一持倉 episode 的同方向加倉次數，避免像單邊小波段被切成太多筆、最後利潤被 taker fee 吃掉。
- `EXTERNAL_BENCHMARK_ENABLED=true` 代表 runner 會低頻刷新 research-only benchmark 快照。
- `EXTERNAL_BENCHMARK_REFRESH_HOURS=4` 代表 benchmark 預設每 4 小時重跑一次，不會每輪 cycle 都重算。
- `EXTERNAL_BENCHMARK_MAX_ALPHA_SIGNALS=1000` 用來控制 Alpha Arena normalized dataset 每次最多讀多少筆訊號，避免 research 支線無限制膨脹。
- `EXTERNAL_AI_REVIEW_ENABLED=true` 代表中午 daily review 產出後，會再呼叫外部模型幫 `TradePulse` 做一份第二視角審稿。
- 目前第一版支援 `EXTERNAL_AI_REVIEW_PROVIDER=gemini`，透過 Gemini `generateContent` API 回收結構化評論。
- `EXTERNAL_AI_REVIEW` 只會進 daily report / Notion `Daily Review`，不會直接影響 live strategist / risk / executor。

## Alpha Arena 第一階段 Benchmark

目前 repo 已經支援：

- 匯入公開 Alpha Arena / 類 Alpha Arena 訊號 JSON
- 轉成標準化 `jsonl`
- 用 Bybit 公開 K 線做一版基礎 benchmark replay

範例：

```bash
source .venv/bin/activate
python scripts/alpha_arena_import_and_backtest.py \
  --input /path/to/alpha_arena_export.json \
  --symbol BTC/USDT \
  --model alpha_arena_public \
  --source-url https://alpha-arena.io/ \
  --timeframe 15m \
  --hold-bars 4
```

輸出會寫到：

- `./runtime/data/alpha_arena/normalized/`
- `./runtime/reports/alpha-arena-benchmark-*.json`

注意：

- 這一階段是 `research / benchmark`，不是 live 下單
- 最適合拿來比較方向、節奏與公開模型行為
- 不建議直接繞過本地 risk / executor，把公開訊號原樣下單

## Equity Curve

系統現在會持續記錄總資產變化，並生成一張最新的資金曲線圖：

- History: `<DATA_ROOT>/service/equity_curve_history.jsonl`
- Chart: `<DATA_ROOT>/reports/charts/equity-curve-latest.svg`

這條曲線會在每次 reporting 階段後持續更新，用來追蹤：

- 總資產變化
- 日內回撤
- 長短期資金趨勢

Notion 目前也會同步：

- 一行 sparkline 版 `Equity Curve`
- 最近區間的 min / max 範圍

這樣你即使不開 UI，也能在 Notion 看出資金曲線是往上、盤整、還是往下。

## 專案結構

```text
config/
  sentiment_sources.json
  strategy_library.json
scripts/
  setup_local_env.sh
  launch_trading_runner.sh
  run_trading_supervisor.sh
trading_agents/
  agents.py
  backtest.py
  config.py
  exchange.py
  llm.py
  main.py
  notion_sync.py
  reporting.py
  research.py
  runner.py
  service_manager.py
  storage.py
trading_agents_web.py
Launch Trading Agents.command
```

## 相關文件

- `SYSTEM_ARCHITECTURE.md`
- `BYBIT_SETUP.md`
- `EXCHANGE_AND_SIMULATION_OPTIONS.md`

## 已知限制

- 目前主要以 macOS 開發與驗證
- `Launch Trading Agents.command` 是 macOS 友善入口
- 若你換到 Linux / Windows，建議直接用命令列啟動
- `Ollama`、Bybit API、Notion 都屬於外部相依，需要自己先準備

## 建議的 clone 後驗證順序

1. `mock` 模式先確認流程能跑
2. Web UI 可打開
3. `runner` / `supervisor` PID 正常生成
4. 再切 `bybit-demo`
5. 最後才接 Notion
