# Current Live Strategy Truth Table

這份文件只回答一件事：

> 現在 live trading 真正用的是什麼策略？

---

## Short Answer

截至目前版本，live trading 已經不再使用原本那 3 個本地內部 intraday 策略：

- `intraday_breakout_perp_v1`
- `intraday_pullback_perp_v1`
- `intraday_reversal_scalp_v1`

現在已收斂成單一主策略：

- `donchian_adx_perp_v1`

來源：
- [config/strategy_library.json](/Users/koshino/Documents/Playground/config/strategy_library.json)

定位：

- `source = public_classic`
- `credibility = external_public`

也就是說，現在 live trading 的主策略已經改成：

**公開經典規則策略：Donchian breakout + Wilder ADX trend-strength filter**

---

## Why This Reset Happened

原先系統雖然名義上是 intraday-perp，但實際上是：

- 本地 hand-crafted 策略池
- strategist / risk / policy exit 混合決策

結果造成兩個問題：

1. 策略來源不夠有公信力
2. 決策鏈太混合，難以驗證到底是哪一層在虧錢

因此本輪 reset 的原則是：

- 先停止把 live trading 建立在內部自造策略池上
- 先收斂成一個公開、經典、可審計、可回測的單一主策略
- 保留框架、風控、報表、Notion、runner，不重寫基礎設施

---

## The Active Strategy

### `donchian_adx_perp_v1`

策略來源概念：

- Richard Donchian breakout / channel breakout
- Welles Wilder ADX trend-strength confirmation

目前在 repo 的實作位置：

- [trading_agents/research.py](/Users/koshino/Documents/Playground/trading_agents/research.py)
- [trading_agents/backtest.py](/Users/koshino/Documents/Playground/trading_agents/backtest.py)

---

## Entry Logic

當前實作用的是：

### Long

- 價格突破過去 `20` 根 bar 的高點
- `ADX >= 20`
- `+DI >= -DI`
- 最近量能比率達標

### Short

- 價格跌破過去 `20` 根 bar 的低點
- `ADX >= 20`
- `-DI >= +DI`
- 最近量能比率達標

### Hold

若不滿足以上條件，就不產生方向性進場。

---

## Replay / Backtest Logic

目前回測不再是三個內部策略互相比較，而是直接回放同一條外部主策略。

回測邏輯：

- 用 Donchian + ADX 規則產生進場
- 持有最多 `6` 根 bar
- 使用 ATR 風格的動態 TP / SL 近似
  - `stop_loss_pct = max(ATR * 1.0, 0.45%)`
  - `take_profit_pct = max(ATR * 1.8, stop_loss * 1.6)`

來源：
- [trading_agents/backtest.py](/Users/koshino/Documents/Playground/trading_agents/backtest.py)

---

## What Still Exists Around It

雖然主策略已經變成單一外部規則策略，但系統周邊框架還在：

1. `market_collector`
2. `sentiment_collector`
3. `backtester`
4. `strategy_researcher`
5. `strategist`
6. `risk_supervisor`
7. `selector`
8. `executor`
9. `post_trade_evaluator`

但要注意：

- `strategy_researcher` 現在只會選這一個主策略
- 不再是多個內部策略池競爭

因此，現在 strategist/risk 的角色比較像：

- 對外部主策略做包裝、風控、與執行調節

而不是：

- 在多個自創策略中自由切換

---

## What Is NOT Driving Live Trading

### Alpha Arena

目前仍然只是：

- benchmark
- research source
- evaluator/reference candidate

不直接下單。

來源：
- [trading_agents/alpha_arena.py](/Users/koshino/Documents/Playground/trading_agents/alpha_arena.py)
- [ALPHA_ARENA_INTEGRATION_PLAN.md](/Users/koshino/Documents/Playground/ALPHA_ARENA_INTEGRATION_PLAN.md)

### Grid

目前仍未接進 live trading。

也就是說：

- 現在不是 grid 在跑
- 也不是 regime router 在切換 grid / trend

---

## What This Means Practically

現在的 live trading，應該被理解成：

**「以單一外部公開規則策略為主腦的 intraday-perp 系統，外圍再包一層 sentiment / strategist / risk / execution framework。」**

這比之前更清楚，也更容易檢驗：

- 如果績效不好
- 我們先檢查這條外部主策略在你這個市場 / 週期是否真的有 edge
- 而不是先被內部自造策略池與混合選擇搞亂

---

## Current Caveat

雖然主策略已經 reset，但系統還不是「完全純規則化」。

因為現在仍然保留：

- strategist
- risk supervisor
- intraday policy exit
- perp protection / profit-lock

所以這一版比之前更收斂，但還不是最終的「單策略純 execution engine」。

這是刻意保留的，因為：

- 先讓外部主策略接管方向來源
- 再逐步檢查周邊模組是否幫忙，還是在干擾

---

## Current Best Next Step

下一步最值得做的不是再加新策略，而是：

### 做 strategy attribution

拆清楚最近一段交易：

- 進場是否符合 `donchian_adx_perp_v1`
- strategist 有沒有覆蓋策略方向
- 哪些單是 policy exit 關掉
- 哪些單是 TP/SL 關掉
- 哪些單是 risk 擋掉

這樣我們才能真正知道：

- 問題在主策略
- 還是周邊框架
