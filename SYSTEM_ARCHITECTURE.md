# 虛擬貨幣多代理交易系統架構

這份文件把目前專案正式定義成適合本地模型與 Testnet 起步的多代理架構。

## 核心原則

- LLM 負責推理、摘要、質疑與產生結構化提案
- Python 負責數學、風控、倉位、費用與實際下單參數
- 市場資料與社群資料分流，避免把社群雜訊直接當交易訊號
- 每次交易後都必須留下可回頭檢討的紀錄

## 角色分工

### 1. `market_collector`

只負責市場資料，不做交易結論。

- K 線
- 成交量
- 簡單技術指標
- 後續可擴充 order book、funding、open interest

輸出：

- 結構化市場快照

### 2. `sentiment_collector`

只負責外部資訊，不做交易結論。

- X / Twitter
- 新聞
- 專案公告
- 白名單社群來源

輸出：

- 情緒摘要
- 訊號來源清單
- 資料可信度分數

### 3. `strategist`

只根據 collector 輸入提出交易假說。

- `buy / sell / hold`
- 置信度
- 主要依據
- 失效條件
- 預估持有時間

不直接給下單數量。

### 4. `risk_supervisor`

不是第二個 strategist，而是反偏誤與風控關卡。

- 檢查是否過度依賴單一來源
- 檢查社群訊號是否與價格矛盾
- 檢查訊號強度是否高於最低門檻
- 檢查單筆風險、日損失上限、手續費
- 決定最大可下單名目金額

### 5. `executor`

只接收結構化指令。

- 計算下單數量
- 對接 mock exchange 或 Binance Testnet
- 回寫成交結果

### 6. `post_trade_evaluator`

不參與當下決策，只負責事後評估。

- 比較策略提案與實際結果
- 判斷這次表現是策略有效還是運氣
- 找出哪個資料來源最有用
- 找出哪個角色最常產生偏差

## 為什麼不是只有三個代理

若把「蒐集資訊」和「分析結論」混在同一個角色，最容易發生：

- 社群雜訊被誤認為訊號
- 模型用自己剛整理的資料說服自己
- 監督者沒有足夠獨立性

所以至少要把 `collector` 與 `strategist` 分開。

## 建議資料流

1. `market_collector` 取得市場資料
2. `sentiment_collector` 取得外部事件與社群資料
3. `strategist` 輸出交易假說
4. `risk_supervisor` 審核與限制名目金額
5. `executor` 執行 mock / Testnet 訂單
6. `post_trade_evaluator` 寫入回顧紀錄

## 外部 benchmark 的位置

若後續導入像 `Alpha Arena` 這種公開 AI 交易競賽 / leaderboard / reasoning 資料，建議角色定位是：

- benchmark
- research source
- evaluator reference

不建議直接當成 live 下單訊號。

比較合理的接法是：

1. 先進 research，整理成 external context summary
2. 再進 evaluator，做事後對照
3. 最後才作為 regime / prompt 的軟性校正

這樣可以保留外部參考價值，同時不破壞本地風控與倉位管理。

## 建議資料夾

大資料可以放在 repo 內的 `./runtime`，也可以另外指定到外接碟。

```text
<DATA_ROOT>/
  data/
    market/
    sentiment/
    backtests/
  logs/
    runs/
    trades/
    evaluations/
  db/
  reports/
  service/
```

## MVP 階段哪些能力不要做

- 自動槓桿交易
- 自動切換大量小幣
- 讓 LLM 自己決定倉位大小
- 讓單一社群來源直接觸發交易

## 接下來的優先順序

1. 穩定 mock 模式下的多代理流程
2. 補上情緒資料介面與白名單來源
3. 接 Binance Spot Testnet
4. 補交易評估與回測
