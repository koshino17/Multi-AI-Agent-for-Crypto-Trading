# Current Live Strategy Truth Table

這份文件只回答一件事：

> 現在 live trading 真正用的是什麼策略？

---

## Short Answer

截至目前版本，`TradePulse` 的 live baseline 已經不是早期的本地 intraday 策略池，也不是後來那版 `donchian_adx_perp_v1`。

現在 live baseline 是：

- `grid_range_reversion_maker_v1`

來源：

- `config/strategy_library.json`

定位：

- `source = research_execution_variant`
- `credibility = experimental_live_candidate`

也就是說，現在的 live baseline 是：

**在 maker-style execution 假設下運作的區間均值回歸策略。**

---

## Why This Changed

前一階段我們先把主線收斂到單一可審計策略，避免內部策略池、fallback、policy exit 混在一起很難歸因。

但之後的 cost-aware benchmark / tournament 持續顯示：

- `donchian_adx_perp_v1` 在近期 `SOL/USDT` 環境下沒有證明自己具備正期望值
- 一般 `taker` 版 grid 也沒有證明自己可行
- 真正比較有希望的是：
  - `grid_range_reversion_maker_v1`
  - 也就是「策略邏輯 + maker-style execution 假設」的組合

所以目前的 live baseline 已經切到這條線，而 Donchian 家族退回 benchmark / research 軌。

---

## The Active Baseline

### `grid_range_reversion_maker_v1`

策略概念：

- 區間均值回歸 / grid-style oscillation fade
- Bollinger Band 區間邊緣回補
- 低 ADX / 受限區間環境下的反轉入場
- 假設使用 `limit + maker` 執行，降低 fee drag

主要位置：

- `trading_agents/research.py`
- `trading_agents/backtest.py`
- `config/strategy_library.json`

---

## Entry Logic

當前 baseline 大致是：

### Long

- 價格接近 Bollinger 下緣
- `ADX` 不高，盤勢仍被視為 range / contained regime
- `RSI` 偏弱，符合回補條件
- signal cooldown 已過

### Short

- 價格接近 Bollinger 上緣
- `ADX` 不高，盤勢仍被視為 range / contained regime
- `RSI` 偏強，符合回落條件
- signal cooldown 已過

### Hold

若價格沒有靠近區間邊緣、或 `ADX` 顯示盤勢不適合均值回歸，則維持觀察。

---

## Cost Assumption

這條 baseline 的關鍵不是只有「grid」兩個字，而是：

- `entry_order_type = limit`
- `entry_liquidity = maker`

也就是說，它成立的前提包含較低交易摩擦。  
目前 repo 裡的成本假設是：

- round-trip fee `0.04%`
- round-trip slippage `0.01%`

因此它不能被理解成「普通 taker grid 直接升級 live」，而是：

**maker-style grid candidate 被拉成新的 live baseline。**

---

## Replay / Backtest Logic

目前這條 baseline 的 replay / benchmark 會：

- 用 `grid_range_reversion_maker_v1` 規則產生進場
- 預設持有最多 `5` 根 bar
- 以較短 TP/SL 模型模擬均值回歸段
- 用 candidate-specific 成本模型估算預期報酬

來源：

- `trading_agents/backtest.py`
- `scripts/run_strategy_tournament.py`
- `scripts/run_strategy_research_cycle.py`

---

## What Still Exists Around It

雖然 live baseline 已切到單一策略，但系統周邊框架還在：

1. `market_collector`
2. `sentiment_collector`
3. `backtester`
4. `strategy_researcher`
5. `strategist`
6. `risk_supervisor`
7. `selector`
8. `executor`
9. `post_trade_evaluator`

也就是說，現在的 baseline 不是「裸策略直接下單」，而是：

- 一個單一 baseline
- 外圍再包一層 risk / learning controls / reporting / execution discipline

---

## What Is NOT Driving Live Trading

### Donchian Family

目前這些仍然存在，但已退回 benchmark / research：

- `donchian_adx_perp_v1`
- `donchian_adx_fast_14_v1`
- `donchian_adx_fast_10_v1`
- `donchian_adx_keltner_v1`
- `donchian_adx_atr_midline_exit_v1`

也就是說：

- 現在不是 Donchian 在跑 live
- 但 Donchian 家族仍會被拿來做 replay / benchmark / attribution

### Alpha Arena

目前不直接下單，但會作為：

- benchmark
- research source
- evaluator / reference candidate

主要檔案：

- `trading_agents/alpha_arena.py`
- `ALPHA_ARENA_INTEGRATION_PLAN.md`

---

## What This Means Practically

現在的 live trading，應該被理解成：

**「以 `grid_range_reversion_maker_v1` 為 live baseline 的 intraday-perp 系統，外圍再包一層 sentiment / strategist / risk / execution framework。」**

而研究支線則是：

**「用 external benchmark pipeline 持續比較 maker-grid、Donchian 家族、其他公開規則，以及 Alpha Arena imports。」**

---

## Current Caveat

雖然 baseline 已經切換，但目前仍不是「完全放開交易」。

因為現在還保留：

- `capital_preservation`
- `capital_preservation_pilot`
- carry-in de-risk
- fee hurdle
- maker execution gate

所以這個階段更像：

- baseline 已換
- 但 live exposure 仍受 learning controls 強限制

這是刻意保留的，因為我們目前在做的是：

- 先確認 baseline 是否有比較像樣的 edge
- 再決定是否放寬 pilot / 恢復更積極的 live entry

---

## Current Best Next Step

下一步最值得做的不是再把文件寫得更漂亮，而是：

1. 確認 `grid_range_reversion_maker_v1` 的 maker-style execution 在 live runtime 真的能形成可驗證的 pilot evidence
2. 繼續用 cost-aware tournament / strategy research 驗證它不是只在單一窗口看起來比較好
3. 用 daily attribution 拆清楚：
   - baseline 本身是否有 edge
   - risk / memory / carry-in 是否仍在拖累它
