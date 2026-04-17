from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from random import Random
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from trading_agents.models import MarketSnapshot

try:
    import ccxt  # type: ignore
except ImportError:  # pragma: no cover
    ccxt = None


@dataclass
class AccountState:
    free_usdt: float
    base_asset: float = 0.0
    market_type: str = "spot"
    position_side: str = "flat"
    net_position: float = 0.0
    entry_price: float = 0.0
    mark_price: float = 0.0
    position_notional_usdt: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    cum_realized_pnl_usdt: float = 0.0
    total_equity_usdt: float = 0.0
    available_balance_usdt: float = 0.0
    leverage: float = 0.0
    liq_price: float = 0.0
    position_im_usdt: float = 0.0
    position_mm_usdt: float = 0.0
    take_profit_price: float = 0.0
    stop_loss_price: float = 0.0
    trailing_stop_distance: float = 0.0
    position_status: str = "Normal"
    is_reduce_only: bool = False


class MockExchangeClient:
    def __init__(self, initial_balance_usdt: float, seed: int = 7) -> None:
        self.account = AccountState(
            free_usdt=initial_balance_usdt,
            total_equity_usdt=initial_balance_usdt,
            available_balance_usdt=initial_balance_usdt,
        )
        self._rng = Random(seed)

    def fetch_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot:
        base_price = 87000.0
        closes: list[float] = []
        volumes: list[float] = []
        price = base_price
        for _ in range(48):
            price += self._rng.uniform(-220, 220)
            closes.append(price)
            volumes.append(self._rng.uniform(10, 80))
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            opens=closes[:],
            highs=closes[:],
            lows=closes[:],
            closes=closes,
            volumes=volumes,
            last_price=closes[-1],
        )

    def execute_order(self, order: dict) -> dict:
        if order["side"] == "buy":
            self.account.free_usdt -= order["notional_usdt"]
            self.account.base_asset += order["quantity"]
        elif order["side"] == "sell":
            self.account.free_usdt += order["notional_usdt"]
            self.account.base_asset = max(0.0, self.account.base_asset - order["quantity"])
        self.account.available_balance_usdt = self.account.free_usdt
        self.account.total_equity_usdt = self.account.free_usdt + self.account.base_asset * order["price"]
        return {
            "status": "filled",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "account_free_usdt": round(self.account.free_usdt, 2),
            "account_base_asset": round(self.account.base_asset, 6),
            "order": order,
        }

    def fetch_account_state(self, symbol: str | None = None) -> AccountState:
        return self.account

    def minimum_order_value_usdt(self, symbol: str) -> float:
        return 0.0

    def set_leverage(self, symbol: str, leverage: float) -> dict:
        return {"status": "unsupported", "reason": "mock exchange has no leverage"}

    def set_position_protection(
        self,
        symbol: str,
        *,
        take_profit: float = 0.0,
        stop_loss: float = 0.0,
        trailing_stop: float = 0.0,
    ) -> dict:
        return {"status": "unsupported", "reason": "mock exchange has no position protection"}


class BinanceTestnetExchangeClient:
    def __init__(self, api_key: str, secret: str) -> None:
        if not api_key or not secret:
            raise ValueError("Missing Binance Testnet API credentials.")
        if ccxt is None:
            raise RuntimeError("ccxt is not installed. Run `pip install -r requirements.txt`.")

        self.client = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        self.client.set_sandbox_mode(True)

    def fetch_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot:
        ohlcv = self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=48)
        opens = [row[1] for row in ohlcv]
        highs = [row[2] for row in ohlcv]
        lows = [row[3] for row in ohlcv]
        closes = [row[4] for row in ohlcv]
        volumes = [row[5] for row in ohlcv]
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            last_price=closes[-1],
        )

    def fetch_free_usdt(self) -> float:
        balance = self.client.fetch_balance()
        return float(balance["free"].get("USDT", 0.0))

    def fetch_account_state(self, symbol: str) -> AccountState:
        balance = self.client.fetch_balance()
        base_asset = symbol.split("/")[0]
        return AccountState(
            free_usdt=float(balance["free"].get("USDT", 0.0)),
            base_asset=float(balance["free"].get(base_asset, 0.0)),
            total_equity_usdt=float(balance["total"].get("USDT", balance["free"].get("USDT", 0.0))),
            available_balance_usdt=float(balance["free"].get("USDT", 0.0)),
        )

    def execute_order(self, order: dict) -> dict:
        created = self.client.create_market_order(
            symbol=order["symbol"],
            side=order["side"],
            amount=order["quantity"],
        )
        return created

    def minimum_order_value_usdt(self, symbol: str) -> float:
        return 0.0

    def set_leverage(self, symbol: str, leverage: float) -> dict:
        return {"status": "unsupported", "reason": "spot testnet has no leverage"}

    def set_position_protection(
        self,
        symbol: str,
        *,
        take_profit: float = 0.0,
        stop_loss: float = 0.0,
        trailing_stop: float = 0.0,
    ) -> dict:
        return {"status": "unsupported", "reason": "spot testnet has no position protection"}


class BybitDemoExchangeClient:
    base_url = "https://api-demo.bybit.com"
    recv_window = "5000"
    interval_map = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "4h": "240",
        "1d": "D",
    }

    def __init__(self, api_key: str, secret: str) -> None:
        if not api_key or not secret:
            raise ValueError("Missing Bybit Demo API credentials.")
        self.api_key = api_key
        self.secret = secret
        self._instrument_cache: dict[str, dict] = {}

    def _symbol(self, unified_symbol: str) -> str:
        return unified_symbol.replace("/", "")

    def _request(self, method: str, path: str, params: dict | None = None, private: bool = False) -> dict:
        params = params or {}
        query = urlencode(params)
        body = json.dumps(params, separators=(",", ":")) if method == "POST" else ""
        url = f"{self.base_url}{path}"
        if method == "GET" and query:
            url = f"{url}?{query}"

        headers = {"Content-Type": "application/json"}
        if private:
            timestamp = str(int(time.time() * 1000))
            payload = query if method == "GET" else body
            raw = f"{timestamp}{self.api_key}{self.recv_window}{payload}"
            signature = hmac.new(
                self.secret.encode("utf-8"),
                raw.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers.update(
                {
                    "X-BAPI-API-KEY": self.api_key,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": self.recv_window,
                    "X-BAPI-SIGN": signature,
                }
            )

        request = Request(
            url=url,
            data=body.encode("utf-8") if method == "POST" else None,
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("retCode") not in (0, None):
            raise RuntimeError(f"Bybit API error: {payload.get('retMsg', 'unknown error')}")
        return payload

    def fetch_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot:
        interval = self.interval_map.get(timeframe, "5")
        response = self._request(
            "GET",
            "/v5/market/kline",
            {
                "category": "spot",
                "symbol": self._symbol(symbol),
                "interval": interval,
                "limit": 48,
            },
        )
        raw_rows = response["result"]["list"]
        rows = list(reversed(raw_rows))
        opens = [float(row[1]) for row in rows]
        highs = [float(row[2]) for row in rows]
        lows = [float(row[3]) for row in rows]
        closes = [float(row[4]) for row in rows]
        volumes = [float(row[5]) for row in rows]
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            last_price=closes[-1],
        )

    def fetch_free_usdt(self) -> float:
        return self.fetch_account_state("BTC/USDT").free_usdt

    def fetch_account_state(self, symbol: str) -> AccountState:
        response = self._request(
            "GET",
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED"},
            private=True,
        )
        accounts = response["result"]["list"]
        if not accounts:
            return AccountState(free_usdt=0.0, base_asset=0.0)
        base_asset_code = symbol.split("/")[0]
        coins = accounts[0].get("coin", [])
        free_usdt = 0.0
        free_base_asset = 0.0
        total_equity = float(accounts[0].get("totalEquity", 0.0) or 0.0)
        available_balance = float(accounts[0].get("totalAvailableBalance", 0.0) or 0.0)
        for coin in coins:
            if coin.get("coin") == "USDT":
                free_usdt = float(coin.get("walletBalance", 0.0))
            if coin.get("coin") == base_asset_code:
                free_base_asset = float(coin.get("walletBalance", 0.0))
        return AccountState(
            free_usdt=free_usdt,
            base_asset=free_base_asset,
            total_equity_usdt=total_equity or free_usdt,
            available_balance_usdt=available_balance or free_usdt,
        )

    def _instrument_info(self, symbol: str) -> dict:
        exchange_symbol = self._symbol(symbol)
        cached = self._instrument_cache.get(exchange_symbol)
        if cached is not None:
            return cached
        response = self._request(
            "GET",
            "/v5/market/instruments-info",
            {"category": "spot", "symbol": exchange_symbol},
        )
        instruments = response.get("result", {}).get("list", [])
        if not instruments:
            return {}
        info = instruments[0]
        self._instrument_cache[exchange_symbol] = info
        return info

    def _normalize_quantity(self, symbol: str, quantity: float) -> str:
        info = self._instrument_info(symbol)
        lot = info.get("lotSizeFilter", {}) if isinstance(info, dict) else {}
        step = str(lot.get("qtyStep") or lot.get("basePrecision") or lot.get("minOrderQty") or "0.000001")
        min_qty = Decimal(str(lot.get("minOrderQty") or "0"))
        qty_decimal = Decimal(str(quantity))
        step_decimal = Decimal(step)
        if step_decimal <= 0:
            step_decimal = Decimal("0.000001")

        normalized = (qty_decimal / step_decimal).to_integral_value(rounding=ROUND_DOWN) * step_decimal
        if normalized <= 0:
            raise RuntimeError(f"Order quantity rounds to zero for {symbol}.")
        if min_qty > 0 and normalized < min_qty:
            raise RuntimeError(
                f"Order quantity {normalized.normalize()} is below Bybit minimum {min_qty.normalize()} for {symbol}."
            )

        normalized = normalized.normalize()
        return format(normalized, "f")

    def _validate_order_value(self, symbol: str, quantity: str, price: float) -> None:
        min_order_amt = Decimal(str(self.minimum_order_value_usdt(symbol)))
        if min_order_amt <= 0:
            return
        notional = Decimal(quantity) * Decimal(str(price))
        if notional < min_order_amt:
            raise RuntimeError(
                f"Order value {notional.quantize(Decimal('0.0001'))} is below Bybit minimum "
                f"{min_order_amt.normalize()} for {symbol}."
            )

    def minimum_order_value_usdt(self, symbol: str) -> float:
        info = self._instrument_info(symbol)
        lot = info.get("lotSizeFilter", {}) if isinstance(info, dict) else {}
        try:
            return float(lot.get("minOrderAmt") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def execute_order(self, order: dict) -> dict:
        qty = self._normalize_quantity(order["symbol"], float(order["quantity"]))
        self._validate_order_value(order["symbol"], qty, float(order["price"]))
        response = self._request(
            "POST",
            "/v5/order/create",
            {
                "category": "spot",
                "symbol": self._symbol(order["symbol"]),
                "side": "Buy" if order["side"] == "buy" else "Sell",
                "orderType": "Market",
                "qty": qty,
                "marketUnit": "baseCoin",
                "orderLinkId": f"codex-{int(time.time() * 1000)}",
            },
            private=True,
        )
        return {
            "status": "accepted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exchange": "bybit-demo",
            "response": response["result"],
            "submitted_qty": qty,
            "order": order,
        }

    def set_leverage(self, symbol: str, leverage: float) -> dict:
        return {"status": "unsupported", "reason": "spot mode has no leverage"}

    def set_position_protection(
        self,
        symbol: str,
        *,
        take_profit: float = 0.0,
        stop_loss: float = 0.0,
        trailing_stop: float = 0.0,
    ) -> dict:
        return {"status": "unsupported", "reason": "spot mode has no position protection"}


class BybitDemoPerpExchangeClient(BybitDemoExchangeClient):
    def fetch_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot:
        interval = self.interval_map.get(timeframe, "5")
        response = self._request(
            "GET",
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": self._symbol(symbol),
                "interval": interval,
                "limit": 48,
            },
        )
        raw_rows = response["result"]["list"]
        rows = list(reversed(raw_rows))
        opens = [float(row[1]) for row in rows]
        highs = [float(row[2]) for row in rows]
        lows = [float(row[3]) for row in rows]
        closes = [float(row[4]) for row in rows]
        volumes = [float(row[5]) for row in rows]
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            last_price=closes[-1],
        )

    def fetch_account_state(self, symbol: str) -> AccountState:
        wallet = self._request(
            "GET",
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED"},
            private=True,
        )
        accounts = wallet.get("result", {}).get("list", [])
        if not accounts:
            return AccountState(free_usdt=0.0, market_type="perp")
        account = accounts[0]
        total_equity = float(account.get("totalEquity", 0.0) or 0.0)
        available_balance = float(account.get("totalAvailableBalance", 0.0) or 0.0)
        coins = account.get("coin", [])
        wallet_usdt = 0.0
        for coin in coins:
            if coin.get("coin") == "USDT":
                wallet_usdt = float(coin.get("walletBalance", 0.0) or 0.0)
                break

        position_response = self._request(
            "GET",
            "/v5/position/list",
            {
                "category": "linear",
                "symbol": self._symbol(symbol),
            },
            private=True,
        )
        positions = position_response.get("result", {}).get("list", [])
        if not positions:
            return AccountState(
                free_usdt=available_balance or wallet_usdt,
                market_type="perp",
                total_equity_usdt=total_equity or available_balance or wallet_usdt,
                available_balance_usdt=available_balance or wallet_usdt,
            )

        position = positions[0]
        side = str(position.get("side", "") or "")
        size = float(position.get("size", 0.0) or 0.0)
        if not side or size <= 0:
            return AccountState(
                free_usdt=available_balance or wallet_usdt,
                market_type="perp",
                total_equity_usdt=total_equity or available_balance or wallet_usdt,
                available_balance_usdt=available_balance or wallet_usdt,
            )
        position_side = "long" if side == "Buy" else "short"
        net_position = size if position_side == "long" else -size
        mark_price = float(position.get("markPrice", 0.0) or 0.0)
        position_value = float(position.get("positionValue", 0.0) or 0.0)
        return AccountState(
            free_usdt=available_balance or wallet_usdt,
            base_asset=size,
            market_type="perp",
            position_side=position_side,
            net_position=net_position,
            entry_price=float(position.get("avgPrice", 0.0) or 0.0),
            mark_price=mark_price,
            position_notional_usdt=position_value or abs(net_position) * mark_price,
            unrealized_pnl_usdt=float(position.get("unrealisedPnl", 0.0) or 0.0),
            cum_realized_pnl_usdt=float(position.get("cumRealisedPnl", 0.0) or 0.0),
            total_equity_usdt=total_equity or (available_balance or wallet_usdt),
            available_balance_usdt=available_balance or wallet_usdt,
            leverage=float(position.get("leverage", 0.0) or 0.0),
            liq_price=float(position.get("liqPrice", 0.0) or 0.0),
            position_im_usdt=float(position.get("positionIM", 0.0) or 0.0),
            position_mm_usdt=float(position.get("positionMM", 0.0) or 0.0),
            take_profit_price=float(position.get("takeProfit", 0.0) or 0.0),
            stop_loss_price=float(position.get("stopLoss", 0.0) or 0.0),
            trailing_stop_distance=float(position.get("trailingStop", 0.0) or 0.0),
            position_status=str(position.get("positionStatus", "Normal") or "Normal"),
            is_reduce_only=bool(position.get("isReduceOnly", False)),
        )

    def _instrument_info(self, symbol: str) -> dict:
        exchange_symbol = self._symbol(symbol)
        cached = self._instrument_cache.get(exchange_symbol)
        if cached is not None:
            return cached
        response = self._request(
            "GET",
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": exchange_symbol},
        )
        instruments = response.get("result", {}).get("list", [])
        if not instruments:
            return {}
        info = instruments[0]
        self._instrument_cache[exchange_symbol] = info
        return info

    def minimum_order_value_usdt(self, symbol: str) -> float:
        info = self._instrument_info(symbol)
        lot = info.get("lotSizeFilter", {}) if isinstance(info, dict) else {}
        try:
            return float(lot.get("minNotionalValue") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def execute_order(self, order: dict) -> dict:
        target_leverage = float(order.get("target_leverage", 0.0) or 0.0)
        if target_leverage > 0 and not bool(order.get("reduce_only")):
            try:
                self.set_leverage(order["symbol"], target_leverage)
            except RuntimeError as exc:
                message = str(exc).lower()
                if "not modified" not in message and "same to" not in message and "leverage not modified" not in message:
                    raise
        qty = self._normalize_quantity(order["symbol"], float(order["quantity"]))
        self._validate_order_value(order["symbol"], qty, float(order["price"]))
        payload = {
            "category": "linear",
            "symbol": self._symbol(order["symbol"]),
            "side": "Buy" if order["side"] == "buy" else "Sell",
            "orderType": "Market",
            "qty": qty,
            "positionIdx": 0,
            "orderLinkId": f"codex-perp-{int(time.time() * 1000)}",
        }
        if bool(order.get("reduce_only")):
            payload["reduceOnly"] = True
        response = self._request(
            "POST",
            "/v5/order/create",
            payload,
            private=True,
        )
        return {
            "status": "accepted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exchange": "bybit-demo-perp",
            "response": response["result"],
            "submitted_qty": qty,
            "order": order,
        }

    def set_leverage(self, symbol: str, leverage: float) -> dict:
        normalized_leverage = max(float(leverage), 1.0)
        payload = {
            "category": "linear",
            "symbol": self._symbol(symbol),
            "buyLeverage": format(Decimal(str(normalized_leverage)).normalize(), "f"),
            "sellLeverage": format(Decimal(str(normalized_leverage)).normalize(), "f"),
        }
        response = self._request(
            "POST",
            "/v5/position/set-leverage",
            payload,
            private=True,
        )
        return {
            "status": "ok",
            "symbol": symbol,
            "leverage": normalized_leverage,
            "response": response.get("result", {}),
        }

    def set_position_protection(
        self,
        symbol: str,
        *,
        take_profit: float = 0.0,
        stop_loss: float = 0.0,
        trailing_stop: float = 0.0,
    ) -> dict:
        payload: dict[str, str | int] = {
            "category": "linear",
            "symbol": self._symbol(symbol),
            "tpslMode": "Full",
            "positionIdx": 0,
            "tpTriggerBy": "MarkPrice",
            "slTriggerBy": "MarkPrice",
        }
        if take_profit > 0:
            payload["takeProfit"] = format(Decimal(str(take_profit)).normalize(), "f")
        if stop_loss > 0:
            payload["stopLoss"] = format(Decimal(str(stop_loss)).normalize(), "f")
        if trailing_stop > 0:
            payload["trailingStop"] = format(Decimal(str(trailing_stop)).normalize(), "f")
        if len(payload) == 6:
            return {"status": "skipped", "reason": "no protection prices provided"}
        response = self._request(
            "POST",
            "/v5/position/trading-stop",
            payload,
            private=True,
        )
        return {
            "status": "ok",
            "symbol": symbol,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "trailing_stop": trailing_stop,
            "response": response.get("result", {}),
        }
