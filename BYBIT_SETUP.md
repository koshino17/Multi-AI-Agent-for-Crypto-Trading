# Bybit Demo Trading 設定清單

這份清單是給你在 Bybit 端要手動完成的最少操作。

## 你現在需要做的事

1. 登入你已建立好的 Bybit 主帳號
2. 切換到 `Demo Trading`
3. 在 Demo Trading 模式下建立新的 API key
4. 權限只勾：
   - `Read`
   - `Spot Trade`
5. 不要開提現相關權限
6. 若 Bybit 提供 IP 綁定選項，之後可再補綁你實際執行 bot 的出口 IP
7. 申請 Demo 資金，至少先準備：
   - `USDT`

## 很重要

- Demo Trading 是獨立帳戶，有自己的帳戶 ID
- Demo API key 不能拿主站正式 API domain 亂接
- 連線要用：
  - `https://api-demo.bybit.com`

## 放進本專案的設定

把你拿到的 key 填進 `.env`：

```bash
TRADING_MODE=bybit-demo
BYBIT_DEMO_API_KEY=
BYBIT_DEMO_SECRET=
DATA_ROOT=./runtime
```

## 本專案目前已支援

- 讀取 Bybit Demo 市場 K 線
- 查詢 Demo 帳戶 USDT 餘額
- 建立 Spot Market Order

## 建議先測的交易對

- `BTC/USDT`
- `ETH/USDT`

先不要碰低流動性幣種。
