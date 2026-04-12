# Changelog

本文件用來記錄這個框架的重要版本演進，重點不只放「改了什麼」，也保留「為什麼要改」。

註：

- `v0.x` 版本為專案在正式上 GitHub 前的回溯式整理版本
- 早期版本內容是依據實際開發歷程、功能落地順序與系統現況回推整理
- 從這個檔案開始，後續版本建議持續沿用同樣格式維護

## Versioning Rule

- `major`: 架構層重大變更或不相容調整
- `minor`: 明顯的新能力、模組、流程升級
- `patch`: 修 bug、修文件、修部署與可用性

---

## v0.11.0 - Bybit perp prototype and directional reporting

### Why

- 原始專案目標不是只有現貨短線，而是希望在高波動的幣圈市場同時具備做多與做空能力
- 近半個月的回顧顯示，系統不只 long entry 品質仍待改善，也明顯受限於 spot-only 架構，無法在下跌行情中參與獲利
- 既然 runner、reporting、Notion、service layer 都已經穩定，下一步就應該把原型正式往 `perp long/short` 推進，並把多空拆分統計納入報表

### What Changed

- 新增 `bybit-demo-perp` 交易模式，接入 Bybit Demo USDT perpetual / linear 交易路徑
- `AccountState` 擴充為支援合約語意，新增：
  - `market_type`
  - `position_side`
  - `net_position`
  - `entry_price`
  - `mark_price`
  - `position_notional_usdt`
  - `unrealized_pnl_usdt`
  - `cum_realized_pnl_usdt`
  - `total_equity_usdt`
  - `available_balance_usdt`
- `strategist`、`risk_supervisor`、`executor` 全部理解 perp 語意：
  - `buy` 可代表開多或回補空單
  - `sell` 可代表開空或平掉多單
  - 平倉時會正確使用 `reduceOnly`
- 修正 cooldown key，改為以 `mode:symbol` 區分，避免舊 spot 冷卻狀態污染 perp 模式
- Daily report、Live Status、Notion 狀態頁新增方向性統計：
  - `Realized PnL Split`
  - `Directional Exposure`
  - `Long vs Short`
  - `long / short proposals`
  - `long / short accepted`
- 日報重建後會直接顯示目前多空曝險與方向別損益，讓隔天開始的 daily report 能直接觀察 perp 版本表現
- `.env.example` 與 `README.md` 補上 `bybit-demo-perp` 的設定與使用說明

---

## v0.10.0 - LLM wake gate and early-exit filtering

### Why

- 4/10 的討論確認 full cycle 最大壓力不是市場資料抓取，而是本地 LLM 在多標的流程中被過度喚醒
- 即使 sentiment cache 與 latency breakdown 已經降低外部資料成本，系統仍需要在喚醒 strategist / risk LLM 前做一層輕量判斷
- 需要避免「安靜盤勢」也耗費完整 LLM 推理，同時又不能漏掉已有持倉時的出場與風控訊號

### What Changed

- 新增 `wake_score` early-exit 流程，每個候選標的會先用輕量 market/account 訊號判斷是否值得喚醒 LLM
- `wake_score` 目前使用：
  - recent volatility
  - MA5 / MA20 momentum spread
  - recent volume expansion
  - 20-candle high / low breakout proximity
  - held-position price move
- 空倉標的預設 `wake_score >= 2` 才喚醒 LLM
- 有持倉標的預設 `wake_score >= 1` 就可喚醒 LLM，避免錯過退場或風控判斷
- 沒達標的候選仍會保留 market / sentiment / backtest / deterministic strategy 結果，但不花本地 LLM 推理成本
- Daily report、Web UI、Notion Live Status / Daily Review 新增 `LLM Wake Rate`
- 新增 wake gate 設定：
  - `LLM_WAKE_GATE_ENABLED`
  - `LLM_WAKE_MIN_SCORE`
  - `LLM_WAKE_POSITION_MIN_SCORE`
  - `LLM_WAKE_VOLATILITY_PCT`
  - `LLM_WAKE_MOMENTUM_PCT`
  - `LLM_WAKE_VOLUME_RATIO`
  - `LLM_WAKE_BREAKOUT_PROXIMITY_PCT`
  - `LLM_WAKE_POSITION_MOVE_PCT`
- 修正 wake rate 報表口徑：舊紀錄若沒有 `llm_wake` 欄位，不再被誤算成「未喚醒」

---

## v0.9.0 - Latency instrumentation and sentiment caching

### Why

- 4/9 的報表已經明確暴露出決策延遲重新升高，但原本只有單一 `Avg Decision Latency`，很難知道瓶頸卡在哪一段
- full cycle 內每個標的都做完整辯論，讓本地 LLM 在多標的模式下容易把整輪耗時拉長
- sentiment 外部資料在同一輪與重啟後都可能重抓相同來源，造成不必要的網路等待
- dust 部位雖然不致命，但會持續污染決策上下文，讓策略與風控浪費注意力在不可操作倉位上

### What Changed

- 新增 stage-level latency metrics，將延遲拆成：
  - `Market`
  - `Sentiment`
  - `Backtest`
  - `Research`
  - `Strategist`
  - `Risk`
  - `Selector`
  - `Executor`
  - `Evaluator`
- Daily report、Web UI、Notion Live Status / Daily Review 全部新增：
  - `Latency Breakdown Avg`
  - `Latency Breakdown P95`
- full cycle 的重型 LLM 辯論改為「先跑完整個觀察池，再只對 selector 最後選中的候選做 selected-candidate debate」
- 新增 `LLM_SELECTED_CANDIDATE_ONLY` 設定，預設減少每輪不必要的多標的辯論成本
- 新增 dust normalization：低於交易所最小下單額的殘餘部位會保留在帳戶資料中，但不再作為可執行持倉參與決策
- sentiment 加入兩層快取：
  - 同進程 TTL 快取
  - 跨重啟檔案快取 `sentiment_http_cache.json`
- 新增：
  - `SENTIMENT_REQUEST_TIMEOUT_SECONDS`
  - `SENTIMENT_CACHE_TTL_SECONDS`
  - `DUST_POSITION_MULTIPLIER`
- Daily Review 的改善建議收斂，不再直接鼓勵因資金利用率低就放寬主策略進場門檻

---

## v0.8.0 - GitHub-ready portable repo

### Why

- 專案已經從本機實驗腳本，變成可持續維護的框架
- 需要能在其他電腦上 `clone` 後快速部署
- 需要把文件與啟動方式從「只適合原開發機」整理成「可交接、可重建」

### What Changed

- 初始化 Git repository 並推到 GitHub
- 新增獨立 `CHANGELOG.md`
- 重寫 `README.md`，改成部署與操作導向
- 補 `scripts/setup_local_env.sh`，讓 clone 後可快速建立環境
- 將多個啟動腳本改為相對路徑，不再綁死本機目錄
- 調整 `.gitignore`，避免把 `.env`、runtime 與 log 一起提交
- 把 `DATA_ROOT` 預設收斂成 `./runtime`

---

## v0.7.0 - Service hardening and operations visibility

### Why

- 系統曾發生 runner 停掉但沒有立即被發現的狀況
- Daily report 缺失時，不容易快速判斷是策略沒觸發，還是背景服務早已停止
- UI 需要直接呈現 service health，而不是只能靠終端機檢查 PID

### What Changed

- 將 runner 從脆弱的單次背景啟動改為 `supervisor + runner` 結構
- 補 `runner_supervisor.pid`、`runner.pid`、對應 log 與 stale lock 清理
- Web UI 顯示 `Supervisor PID / Runner PID / Uptime`
- 修正 `2026-04-05` 無日報時的補報與服務重啟流程
- 強化 background service 與 UI 解耦，UI 關掉不影響 runner 持續運作

---

## v0.6.0 - Financial reporting and review system

### Why

- 只看 decision / blocked / executed 數量，無法回答「到底有沒有賺錢」
- 需要把系統從工程 debug log，升級成交易績效報表
- 需要把每日回顧與即時狀態分開，避免 Live Status 和 Daily Review 混在一起

### What Changed

- Daily report 增加 `Financial Snapshot`
- 新增：
  - `Total Portfolio Value`
  - `Daily PnL`
  - `Realized / Unrealized PnL`
  - `Daily / Cumulative Fees`
  - `Capital Utilization`
- Daily Review 改成台灣時間中午後發布一次，不再被小 cycle 持續覆蓋
- Live Status 改回精簡版，專注於即時監控資訊
- Daily report / UI / Notion 的營運資訊與財務資訊同步對齊

---

## v0.5.0 - Debate workflow and 12-hour strategy memory

### Why

- 單純的 pipeline 決策還不夠接近「多個 AI 互相質疑與收斂」的原始目標
- 若每個大 cycle 都立即調整策略，容易過度擬合短線波動
- 需要有節制地保留反思結果，讓 agent 有一致的中期記憶

### What Changed

- `strategy_researcher`、`strategist`、`risk_supervisor`、`selector` 增加辯論式流程
- 新增 `risk_feedback` 與決策辯論紀錄
- 新增 `strategy_memory.json`
- 策略反思改為每 `12` 小時更新一次，而不是每輪都改
- Web UI / Notion / report 顯示最新辯論內容與 strategy memory slot

---

## v0.4.0 - Expectancy-driven aggressive demo mode

### Why

- 早期策略過於保守，幾乎只會 `hold`，不利於在 demo 環境中訓練系統
- 單看勝率不夠，應該要納入期望值與盈虧比
- 使用者希望在模擬盤中更重視「整體營收為正」而不是單次虧損次數

### What Changed

- 回測與策略研究新增：
  - `avg_win_pct`
  - `avg_loss_pct`
  - `expectancy_pct`
  - `profit_factor`
- `strategist` / `risk_supervisor` 開始使用 expectancy 與 reward/risk 作為核心依據
- 加入 `DEMO_AGGRESSIVE_MODE`
- 放寬 demo 模式風控，使其更適合訓練與策略探索
- 微調 `buy` / `hold` / `sell` 判準，降低完全不出手的情況

---

## v0.3.0 - Continuous monitoring and execution-aware control

### Why

- 原本較像「每 15 分鐘批次重跑」的系統，不符合持續監控市場的設想
- 需要把大 cycle 與小 cycle 的任務分開
- 需要讓系統更偏向可執行訊號，而不是只挑分數最高但做不了的候選

### What Changed

- runner 改成 `30s` 監控輪詢 + 條件觸發 full cycle
- 新增：
  - `MONITOR_INTERVAL_SECONDS`
  - `RUN_INTERVAL_SECONDS`
  - `PRICE_TRIGGER_PCT`
  - `MICRO_CYCLE_TRIGGER_PCT`
  - `POSITION_MICRO_TRIGGER_PCT`
- 盤中 monitor 不再只是 idle watching，會因價格與持倉變化觸發決策
- `selector` 更偏向可執行候選
- UI 明確區分 continuous mode 與 debug control

---

## v0.2.0 - Multi-symbol flow, reporting clarity, and execution safety

### Why

- 早期流程以單標的視角為主，不夠貼近多標的觀察池設計
- 報表曾出現 UTC/台北日期混亂與舊 runner 覆寫問題
- 系統常產生「無持倉卻賣出」或「小於交易所最小單額」的不可執行提案

### What Changed

- Web UI 改為顯示多標的觀察池流程
- 新增完整流程階段可視化：
  - Setup
  - Market
  - Sentiment
  - Backtest
  - Research
  - Strategist
  - Risk
  - Selector
  - Executor
  - Evaluator
  - Reporting
- 修正 Daily Summary 時區與統計欄位重疊問題
- 新增 `Why Blocked` 統計
- 新增交易所最小單額檢查，避免把不可成交小單送出
- 修正 `sell` 在沒有 base asset 或倉位太小時的處理

---

## v0.1.0 - Initial local multi-agent trading MVP

### Why

- 需要一個本地 AI agent 原型，先在模擬資金環境驗證整套流程
- 目標是先打通資料流、代理分工、風控與執行，不急著直接上實盤

### What Changed

- 建立多代理框架
- 初步角色包括：
  - `market_collector`
  - `sentiment_collector`
  - `strategist`
  - `risk_supervisor`
  - `executor`
  - `post_trade_evaluator`
- 接上 `Ollama` 本地模型
- 支援 `mock` 與 `Bybit Demo`
- 建立市場資料、情緒資料、交易 log、日報與簡易 Web UI 的基本骨架

---

## Next Documentation Step

後續建議每次版本更新都維持這個格式：

```md
## vX.Y.Z - Title

### Why
- 為什麼要改

### What Changed
- 改了哪些行為、架構、設定、文件
```

如果需要，我可以下一步再幫你補：

- GitHub `Releases` 用的 release notes 模板
- Notion 版的同內容版本紀錄頁
- 每次 release 前自動提醒更新 changelog 的流程
