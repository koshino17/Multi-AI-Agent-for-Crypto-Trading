# Multi-AI-Agent for Crypto Trading

本專案是一個本地 AI Agent 驅動的虛擬貨幣短線交易原型，先以 `Bybit Demo` / `mock` 驗證多代理決策流程，再逐步往可落地的多標的系統演進。

目前重點是：

- 本地模型推理，預設支援 `Ollama`
- 多代理分工決策與風控
- `Bybit Demo` / `mock` 執行
- Web 控制台、Notion status、Daily Review
- 持續運作的 background runner

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
```

如果你要接 `Bybit Demo`：

```bash
TRADING_MODE=bybit-demo
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

## 預設策略節奏

目前預設是：

- `MONITOR_INTERVAL_SECONDS=30`
- `TIMEFRAME=15m`
- `RUN_INTERVAL_SECONDS=900`

意思是：

- 每 `30` 秒監控一次市場 / 帳戶
- 完整 decision cycle 主要由新 candle、帳戶變化或價格觸發
- 不會每 30 秒都跑完整 LLM 重分析

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

服務相關檔案預設都在：

```text
./runtime/service
```

重要狀態檔：

- `runner_supervisor.pid`
- `runner.pid`
- `runner_supervisor.log`
- `runner.log`

## 重要設定

這幾個設定最常調：

```bash
OBSERVATION_POOL=SOL/USDT,LINK/USDT,AVAX/USDT
TIMEFRAME=15m
MIN_SIGNAL_SCORE=0.55
MAX_POSITION_PCT=0.20
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
BUY_BALANCE_BUFFER_PCT=0.95
FEE_HURDLE_MULTIPLIER=1.15
FAST_CYCLE_SIGNAL_BOOST=0.08
LLM_TIMEOUT_SECONDS=18
LLM_FULL_CYCLE_ONLY=true
```

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
