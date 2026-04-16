# Alpha Arena Integration Plan

這份文件用來定義：如何把 `Alpha Arena` 這類公開 AI 交易競賽 / leaderboard / reasoning / trade stream，安全地接進目前的 `Multi-AI-Agent-for-Crypto-Trading` 架構。

重點不是「直接抄單」，而是把它當成：

- 外部 benchmark
- 策略研究資料來源
- evaluator 的對照組
- market regime 與 exit quality 的校正參考

## 為什麼值得接

目前這個專案已經具備：

- 本地多代理流程
- spot / perp 雙模式
- Notion 與日報
- safety rails 與 profit lock ladder

但還缺少一個穩定的「外部對照物」，用來回答：

- 我們現在做多做空的方向，和公開市場上的強模型是否一致？
- 我們的持倉時間是否過長？
- 我們的 exit 是否偏慢？
- 哪些 market regime 下，我們常和表現較好的模型站在相反方向？

`Alpha Arena` 類型的系統，剛好可以提供這種外部 benchmark。

## 不直接照抄的原因

不建議把 Alpha Arena 當成即時交易訊號直接餵進 executor，原因是：

1. 市場與交易所不同

- Alpha Arena 公開說明目前主要在 `Hyperliquid` perp 市場運行
- 我們現在主跑的是 `Bybit Demo Perp`
- 交易成本、流動性、撮合、可交易標的與 API 行為都不同

2. 公開資料不一定完整

- 可能有 leaderboard
- 可能有 positions / trades / reasoning
- 但未必保證每個時間點都有乾淨、可重建、可訓練的完整資料集

3. 市場是非平穩的

- 直接模仿某段時間表現好的模型，很容易學到已經過時的行為

所以正確使用方式應該是：

- `不直接複製交易`
- `先做研究、比對、再回饋策略`

## 與 Grid / Oscillation 想法是否衝突

不衝突。

兩者屬於不同層級：

- `Grid` 是交易執行風格
- `Alpha Arena` 是外部 benchmark / meta-research 來源

建議架構：

```text
market_regime detector
  -> oscillating    -> Grid / range execution
  -> trending       -> directional strategist
  -> volatile/chaos -> risk-off / smaller size / no-trade

alpha arena benchmark
  -> 只影響 research / evaluator / regime calibration
  -> 不直接變成下單訊號
```

真正會衝突的情況只有一種：

- 我們一邊用 grid 做規則化震盪交易
- 一邊又讓外部模型的方向性主觀看法直接覆蓋 grid

這是應該避免的。

## 目標用途

### 1. Benchmark

回答：

- 同一段行情下，外部強模型偏多、偏空、還是空手？
- 它們平均持倉多久？
- 它們在什麼情況下快速翻向？

### 2. Research

回答：

- 哪些 regime 下，公開模型偏向 breakout？
- 哪些 regime 下，公開模型偏向 mean reversion？
- 哪些 regime 下，它們的實際表現優於我們？

### 3. Evaluator Reference

回答：

- 我們每次決策是否和公開表現較好的模型方向一致？
- 若不一致，差在：
  - regime 判斷
  - entry timing
  - exit timing
  - 風險大小

### 4. Prompt / Strategy Memory Calibration

不是把外部決策原樣灌進 strategist，而是把它整理成：

- 近期市場偏向 range 還是 trend
- 近期外部強模型更常用哪種持倉節奏
- 哪些 exit 行為較成功

## 建議導入階段

### Phase 1: Offline Benchmark Dataset

先不要碰 live trading。

新增目標：

- 收集公開可取得的：
  - leaderboard 快照
  - model positions
  - trades
  - reasoning / commentary（若有）
- 存到本地 research dataset

建議存放：

```text
<DATA_ROOT>/
  data/
    alpha_arena/
      raw/
      normalized/
```

建議新增檔案：

- `trading_agents/alpha_arena.py`
- `scripts/fetch_alpha_arena_snapshot.py`

輸出格式建議：

```json
{
  "timestamp": "...",
  "source": "alpha_arena",
  "market": "perp",
  "symbol": "BTC/USDT",
  "model": "gpt-4.x",
  "position_side": "long",
  "position_size": 0.25,
  "entry_price": 67200.0,
  "mark_price": 67410.0,
  "unrealized_pnl_pct": 0.31,
  "commentary": "...",
  "leaderboard_rank": 3
}
```

### Phase 2: Research-only Integration

把 Alpha Arena 接進 research，但還不碰 executor。

建議新增：

- `alpha_arena_summary` in `research.py`
- `alpha_arena_alignment` in post-trade evaluator

讓 daily report / Notion 多出：

- `External Benchmark Alignment`
- `Top external bias: long / short / flat`
- `Agreement with benchmark leaders`

### Phase 3: Regime Calibration

這一階段最值得和未來 `OscillationDetector` 合併。

目標：

- 比對 `Alpha Arena` 強模型在：
  - trend
  - oscillation
  - volatile
  三種 regime 下的實際行為

用法：

- 幫我們調整：
  - wake gate
  - strategist prompt
  - selected strategy ranking
  - exit patience / hold duration

### Phase 4: Soft Influence on Live Decision

這一階段仍然不直接複製交易，只做 soft influence。

例如：

- 若外部 benchmark 在過去 12 小時高度一致偏空
- 且我們本地 regime 也判為 trending down
- 那就讓 strategist prompt 額外收到一條：
  - `external benchmark bias currently leans short`

這層影響只能：

- 調整置信度
- 提醒風控
- 作為 selector 的 tie-breaker

不能直接：

- 繞過 risk
- 繞過 executor sizing
- 變成硬下單條件

## 建議新增模組

### `trading_agents/alpha_arena.py`

責任：

- 抓取公開快照
- 轉成標準化格式
- 寫入 raw / normalized dataset

### `trading_agents/benchmarking.py`

責任：

- 比對我們的決策和外部 benchmark
- 計算：
  - direction agreement
  - exit lag
  - hold duration gap

### `trading_agents/regime.py`

責任：

- 抽出 regime detection
- 給未來 `OscillationDetector` / `GridAgent` 共用
- 也讓 benchmark 分析可共用相同 regime 標籤

## 建議新增報表欄位

### Daily Report

- `Benchmark Bias`
- `Benchmark Agreement Rate`
- `Benchmark-vs-Us PnL Direction Check`
- `Average Hold Duration vs Benchmark`

### Notion Live Status

- `External Bias`
- `Agreement with Benchmark Leaders`

### Post Trade Evaluator

- `benchmark_alignment`
- `benchmark_disagreement_reason`

## 風險與限制

### 1. 不能把外部模型當權威

外部模型也會錯，而且可能在不同市場結構下失效。

### 2. 不能繞過本地風控

Alpha Arena 再強，也不能直接跳過：

- leverage cap
- liquidation buffer
- TP / SL / profit lock
- cooldown

### 3. 不應直接做模仿學習

至少在第一階段不要做：

- 直接 supervised imitation
- 直接把外部 trade 當 label

先做 benchmark 與分析，才不會把暫時有效的噪音學成永久規則。

## 和目前架構的接點

最適合接進現有專案的三個位置：

1. `research.py`

- 當作 external context summary

2. `post_trade_evaluator`

- 當作事後對照組

3. `future regime router`

- 當作 oscillating / trending calibration 來源

## 建議優先順序

### Step 1

先做 `Alpha Arena benchmark dataset`，不改 live trading。

### Step 2

接進 `research.py` 和 `post_trade_evaluator`。

### Step 3

做 `market_regime` 與 `OscillationDetector`。

### Step 4

再評估是否上 `GridAgent`。

## 一句話版本

`Alpha Arena` 最適合拿來幫我們「看懂自己缺什麼」，而不是直接替我們下單。

它和未來的 `Grid` 不衝突；最好的用法是：

- `Grid` 處理震盪盤執行
- `Alpha Arena` 幫我們做 benchmark、research、regime 校正與 exit 檢討
