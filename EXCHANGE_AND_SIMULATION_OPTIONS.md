# 交易所與模擬平台選項

這份文件整理目前較適合本專案的交易所與模擬環境。

## 先講結論

若目標是「先讓 AI 代理在很像真的環境裡練習，再決定是否進入實盤」，建議：

1. 第一個交易所優先評估 `Bybit`
2. 第二個備選 `OKX`
3. 若重視最典型的程式化 Spot Testnet，再保留 `Binance Spot Testnet`

## 為什麼這樣排

### Bybit

優點：

- 官方提供 `Demo Trading`
- 官方 API 文件明確列出 Demo Trading 專用網域
- 公共行情與主站一致，私有交易可走 demo 帳戶
- 適合做接近真實流程的代理交易測試

較適合：

- 想先在現成模擬盤練流程
- 想較快接 API
- 想測 Spot 與 Derivatives，但仍先用虛擬資金

### OKX

優點：

- 官方文件直接支援 `Demo Trading` API
- 有專門的 demo header 與流程
- 適合做 API 導向的模擬交易

較適合：

- 想測交易機器人
- 想用官方 demo trading API

### Binance Spot Testnet

優點：

- 對很多量化與交易工具鏈來說很常見
- 很適合練標準化 Spot API 流程

限制：

- 與現成主站 demo 介面相比，體驗上比較偏工程測試
- 官方頁面顯示 Testnet 可能會有維護狀態

較適合：

- 想先把 bot 和交易 API 對接跑通
- 更在意程式接口而不是使用者介面

## 對本專案的建議

### 帳號選擇

如果你現在只打算先開一個：

- 若重視「模擬盤真實感」與「現成可用性」：優先看 `Bybit`
- 若重視「官方 demo API 純度」：優先看 `OKX`
- 若重視「標準 Spot Testnet 練 bot」：選 `Binance`

### 模擬順序

建議順序：

1. `Bybit Demo Trading` 或 `OKX Demo Trading`
2. `Binance Spot Testnet`
3. 極小額實盤

這樣可以同時驗證：

- UI 與實際交易流程
- API 與 bot 執行流程
- 風控與訂單生命週期

## 地區與合規提醒

交易所可用性、KYC、衍生品權限與 API 功能會依地區而變。實際開戶前，仍要以你註冊時顯示的地區資格與官方條款為準。
