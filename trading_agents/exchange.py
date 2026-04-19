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


def _build_microstructure_features(
    *,
    bids: list,
    asks: list,
    trades: list[dict],
    last_price: float,
) -> dict:
    normalized_bids = [(float(item[0]), float(item[1])) for item in bids if len(item) >= 2]
    normalized_asks = [(float(item[0]), float(item[1])) for item in asks if len(item) >= 2]
    best_bid = normalized_bids[0][0] if normalized_bids else 0.0
    best_ask = normalized_asks[0][0] if normalized_asks else 0.0
    mid_price = ((best_bid + best_ask) / 2.0) if (best_bid > 0 and best_ask > 0) else max(float(last_price), 0.0)
    spread_bps = ((best_ask - best_bid) / mid_price) * 10000.0 if best_bid > 0 and best_ask > 0 and mid_price > 0 else 0.0

    top_bid_size = normalized_bids[0][1] if normalized_bids else 0.0
    top_ask_size = normalized_asks[0][1] if normalized_asks else 0.0
    top_bid_notional = best_bid * top_bid_size
    top_ask_notional = best_ask * top_ask_size
    top_book_imbalance = (
        (top_bid_notional - top_ask_notional) / (top_bid_notional + top_ask_notional)
        if (top_bid_notional + top_ask_notional) > 0
        else 0.0
    )

    depth_bid_notional = sum(price * size for price, size in normalized_bids)
    depth_ask_notional = sum(price * size for price, size in normalized_asks)
    depth_imbalance = (
        (depth_bid_notional - depth_ask_notional) / (depth_bid_notional + depth_ask_notional)
        if (depth_bid_notional + depth_ask_notional) > 0
        else 0.0
    )

    bid_wall_price = 0.0
    bid_wall_notional = 0.0
    if normalized_bids:
        bid_wall_price, bid_wall_size = max(normalized_bids, key=lambda item: item[0] * item[1])
        bid_wall_notional = bid_wall_price * bid_wall_size
    ask_wall_price = 0.0
    ask_wall_notional = 0.0
    if normalized_asks:
        ask_wall_price, ask_wall_size = max(normalized_asks, key=lambda item: item[0] * item[1])
        ask_wall_notional = ask_wall_price * ask_wall_size

    bid_wall_distance_bps = ((mid_price - bid_wall_price) / mid_price) * 10000.0 if bid_wall_price > 0 and mid_price > 0 else 0.0
    ask_wall_distance_bps = ((ask_wall_price - mid_price) / mid_price) * 10000.0 if ask_wall_price > 0 and mid_price > 0 else 0.0

    trade_buy_notional = 0.0
    trade_sell_notional = 0.0
    trade_notionals: list[tuple[str, float]] = []
    for item in trades:
        price = float(item.get("price", 0.0) or 0.0)
        size = float(item.get("size", 0.0) or 0.0)
        if price <= 0 or size <= 0:
            continue
        side = str(item.get("side", "") or "")
        notional = price * size
        trade_notionals.append((side, notional))
        if side == "Buy":
            trade_buy_notional += notional
        elif side == "Sell":
            trade_sell_notional += notional
    trade_delta_notional = trade_buy_notional - trade_sell_notional
    total_trade_notional = trade_buy_notional + trade_sell_notional
    trade_delta_ratio = trade_delta_notional / total_trade_notional if total_trade_notional > 0 else 0.0
    avg_trade_notional = total_trade_notional / len(trade_notionals) if trade_notionals else 0.0
    large_threshold = avg_trade_notional * 2.5 if avg_trade_notional > 0 else float("inf")
    large_buy_count = sum(1 for side, notional in trade_notionals if side == "Buy" and notional >= large_threshold)
    large_sell_count = sum(1 for side, notional in trade_notionals if side == "Sell" and notional >= large_threshold)

    return {
        "best_bid_price": round(best_bid, 6),
        "best_ask_price": round(best_ask, 6),
        "spread_bps": round(max(spread_bps, 0.0), 4),
        "top_bid_size": round(top_bid_size, 6),
        "top_ask_size": round(top_ask_size, 6),
        "top_book_imbalance": round(top_book_imbalance, 4),
        "depth_bid_notional": round(depth_bid_notional, 4),
        "depth_ask_notional": round(depth_ask_notional, 4),
        "depth_imbalance": round(depth_imbalance, 4),
        "bid_wall_price": round(bid_wall_price, 6),
        "ask_wall_price": round(ask_wall_price, 6),
        "bid_wall_notional": round(bid_wall_notional, 4),
        "ask_wall_notional": round(ask_wall_notional, 4),
        "bid_wall_distance_bps": round(max(bid_wall_distance_bps, 0.0), 4),
        "ask_wall_distance_bps": round(max(ask_wall_distance_bps, 0.0), 4),
        "trade_buy_notional": round(trade_buy_notional, 4),
        "trade_sell_notional": round(trade_sell_notional, 4),
        "trade_delta_notional": round(trade_delta_notional, 4),
        "trade_delta_ratio": round(trade_delta_ratio, 4),
        "aggressive_buy_ratio": round(trade_buy_notional / total_trade_notional if total_trade_notional > 0 else 0.0, 4),
        "aggressive_sell_ratio": round(trade_sell_notional / total_trade_notional if total_trade_notional > 0 else 0.0, 4),
        "recent_trade_count": len(trade_notionals),
        "large_buy_count": large_buy_count,
        "large_sell_count": large_sell_count,
        "orderbook_levels": min(len(normalized_bids), len(normalized_asks)),
    }


class MockExchangeClient:
    def __init__(
        self,
        initial_balance_usdt: float,
        seed: int = 7,
        *,
        microstructure_enabled: bool = True,
    ) -> None:
        self.account = AccountState(
            free_usdt=initial_balance_usdt,
            total_equity_usdt=initial_balance_usdt,
            available_balance_usdt=initial_balance_usdt,
        )
        self._rng = Random(seed)
        self.microstructure_enabled = microstructure_enabled

    def fetch_snapshot(self, symbol: str, timeframe: str, include_microstructure: bool = True) -> MarketSnapshot:
        base_price = 87000.0
        closes: list[float] = []
        volumes: list[float] = []
        price = base_price
        for _ in range(48):
            price += self._rng.uniform(-220, 220)
            closes.append(price)
            volumes.append(self._rng.uniform(10, 80))
        microstructure = self._mock_microstructure(closes[-1], volumes[-1]) if (include_microstructure and self.microstructure_enabled) else {}
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            opens=closes[:],
            highs=closes[:],
            lows=closes[:],
            closes=closes,
            volumes=volumes,
            last_price=closes[-1],
            **microstructure,
        )

    def _mock_microstructure(self, last_price: float, last_volume: float) -> dict:
        spread_bps = 4.0 + self._rng.uniform(-1.5, 1.5)
        spread = last_price * (spread_bps / 10000.0)
        best_bid = max(last_price - spread / 2.0, 0.0)
        best_ask = last_price + spread / 2.0
        top_bid_size = max(last_volume * self._rng.uniform(0.08, 0.16), 0.01)
        top_ask_size = max(last_volume * self._rng.uniform(0.08, 0.16), 0.01)
        top_bid_notional = best_bid * top_bid_size
        top_ask_notional = best_ask * top_ask_size
        top_imbalance = (
            (top_bid_notional - top_ask_notional) / (top_bid_notional + top_ask_notional)
            if (top_bid_notional + top_ask_notional) > 0
            else 0.0
        )
        depth_bid_notional = top_bid_notional * self._rng.uniform(5.0, 9.0)
        depth_ask_notional = top_ask_notional * self._rng.uniform(5.0, 9.0)
        depth_imbalance = (
            (depth_bid_notional - depth_ask_notional) / (depth_bid_notional + depth_ask_notional)
            if (depth_bid_notional + depth_ask_notional) > 0
            else 0.0
        )
        trade_buy_notional = last_price * max(last_volume * self._rng.uniform(0.8, 1.3), 0.01)
        trade_sell_notional = last_price * max(last_volume * self._rng.uniform(0.8, 1.3), 0.01)
        trade_delta_notional = trade_buy_notional - trade_sell_notional
        total_trade_notional = trade_buy_notional + trade_sell_notional
        trade_delta_ratio = trade_delta_notional / total_trade_notional if total_trade_notional > 0 else 0.0
        return {
            "best_bid_price": round(best_bid, 6),
            "best_ask_price": round(best_ask, 6),
            "spread_bps": round(max(spread_bps, 0.0), 4),
            "top_bid_size": round(top_bid_size, 6),
            "top_ask_size": round(top_ask_size, 6),
            "top_book_imbalance": round(top_imbalance, 4),
            "depth_bid_notional": round(depth_bid_notional, 4),
            "depth_ask_notional": round(depth_ask_notional, 4),
            "depth_imbalance": round(depth_imbalance, 4),
            "bid_wall_price": round(best_bid * (1.0 - self._rng.uniform(0.0005, 0.0030)), 6),
            "ask_wall_price": round(best_ask * (1.0 + self._rng.uniform(0.0005, 0.0030)), 6),
            "bid_wall_notional": round(depth_bid_notional * self._rng.uniform(0.12, 0.20), 4),
            "ask_wall_notional": round(depth_ask_notional * self._rng.uniform(0.12, 0.20), 4),
            "bid_wall_distance_bps": round(self._rng.uniform(3.0, 18.0), 4),
            "ask_wall_distance_bps": round(self._rng.uniform(3.0, 18.0), 4),
            "trade_buy_notional": round(trade_buy_notional, 4),
            "trade_sell_notional": round(trade_sell_notional, 4),
            "trade_delta_notional": round(trade_delta_notional, 4),
            "trade_delta_ratio": round(trade_delta_ratio, 4),
            "aggressive_buy_ratio": round(trade_buy_notional / total_trade_notional if total_trade_notional > 0 else 0.0, 4),
            "aggressive_sell_ratio": round(trade_sell_notional / total_trade_notional if total_trade_notional > 0 else 0.0, 4),
            "recent_trade_count": 24,
            "large_buy_count": 1 if trade_buy_notional > trade_sell_notional * 1.2 else 0,
            "large_sell_count": 1 if trade_sell_notional > trade_buy_notional * 1.2 else 0,
            "orderbook_levels": 10,
        }

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
    def __init__(
        self,
        api_key: str,
        secret: str,
        *,
        microstructure_enabled: bool = True,
        orderbook_depth_limit: int = 25,
        recent_trade_limit: int = 60,
    ) -> None:
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
        self.microstructure_enabled = microstructure_enabled
        self.orderbook_depth_limit = max(orderbook_depth_limit, 1)
        self.recent_trade_limit = max(recent_trade_limit, 1)

    def fetch_snapshot(self, symbol: str, timeframe: str, include_microstructure: bool = True) -> MarketSnapshot:
        ohlcv = self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=48)
        opens = [row[1] for row in ohlcv]
        highs = [row[2] for row in ohlcv]
        lows = [row[3] for row in ohlcv]
        closes = [row[4] for row in ohlcv]
        volumes = [row[5] for row in ohlcv]
        microstructure = {}
        if include_microstructure and self.microstructure_enabled:
            try:
                orderbook = self.client.fetch_order_book(symbol, limit=self.orderbook_depth_limit)
                trades = self.client.fetch_trades(symbol, limit=min(self.recent_trade_limit, 100))
                microstructure = _build_microstructure_features(
                    bids=orderbook.get("bids", []),
                    asks=orderbook.get("asks", []),
                    trades=[
                        {
                            "price": item.get("price", 0.0),
                            "size": item.get("amount", 0.0),
                            "side": "Buy" if str(item.get("side", "")).lower() == "buy" else "Sell",
                        }
                        for item in trades
                    ],
                    last_price=closes[-1],
                )
            except Exception:
                microstructure = {}
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            last_price=closes[-1],
            **microstructure,
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

    def __init__(
        self,
        api_key: str,
        secret: str,
        *,
        microstructure_enabled: bool = True,
        orderbook_depth_limit: int = 25,
        recent_trade_limit: int = 60,
        microstructure_cache_ttl_seconds: float = 5.0,
    ) -> None:
        if not api_key or not secret:
            raise ValueError("Missing Bybit Demo API credentials.")
        self.api_key = api_key
        self.secret = secret
        self._instrument_cache: dict[str, dict] = {}
        self.microstructure_enabled = microstructure_enabled
        self.orderbook_depth_limit = max(orderbook_depth_limit, 1)
        self.recent_trade_limit = max(recent_trade_limit, 1)
        self.microstructure_cache_ttl_seconds = max(microstructure_cache_ttl_seconds, 0.0)
        self._microstructure_cache: dict[tuple[str, str], tuple[float, dict]] = {}

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

    def fetch_snapshot(self, symbol: str, timeframe: str, include_microstructure: bool = True) -> MarketSnapshot:
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
        microstructure = self._market_microstructure(symbol, "spot", closes[-1]) if include_microstructure else {}
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            last_price=closes[-1],
            **microstructure,
        )

    def _market_microstructure(self, symbol: str, category: str, last_price: float) -> dict:
        if not self.microstructure_enabled:
            return {}
        cache_key = (category, self._symbol(symbol))
        now = time.time()
        cached = self._microstructure_cache.get(cache_key)
        if cached and now - cached[0] <= self.microstructure_cache_ttl_seconds:
            return cached[1]

        orderbook_limit = self.orderbook_depth_limit
        if category == "spot":
            orderbook_limit = min(orderbook_limit, 200)
        else:
            orderbook_limit = min(orderbook_limit, 500)
        trade_limit = min(self.recent_trade_limit, 60 if category == "spot" else 1000)

        try:
            orderbook_response = self._request(
                "GET",
                "/v5/market/orderbook",
                {
                    "category": category,
                    "symbol": self._symbol(symbol),
                    "limit": orderbook_limit,
                },
            )
            orderbook_result = orderbook_response.get("result", {})
            bids = orderbook_result.get("b", [])
            asks = orderbook_result.get("a", [])
        except Exception:
            bids = []
            asks = []

        try:
            trades_response = self._request(
                "GET",
                "/v5/market/recent-trade",
                {
                    "category": category,
                    "symbol": self._symbol(symbol),
                    "limit": trade_limit,
                },
            )
            trades = trades_response.get("result", {}).get("list", [])
        except Exception:
            trades = []

        features = _build_microstructure_features(
            bids=bids,
            asks=asks,
            trades=trades,
            last_price=last_price,
        )
        self._microstructure_cache[cache_key] = (now, features)
        return features

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
        constraints = self._lot_constraints(symbol)
        step = format(constraints["step"], "f")
        min_qty = constraints["min_qty"]
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
        min_order_amt = Decimal(str(self.executable_min_order_value_usdt(symbol, price)))
        if min_order_amt <= 0:
            return
        notional = Decimal(quantity) * Decimal(str(price))
        if notional < min_order_amt:
            raise RuntimeError(
                f"Order value {notional.quantize(Decimal('0.0001'))} is below Bybit minimum "
                f"{min_order_amt.normalize()} for {symbol}."
            )

    def _lot_constraints(self, symbol: str) -> dict[str, Decimal]:
        info = self._instrument_info(symbol)
        lot = info.get("lotSizeFilter", {}) if isinstance(info, dict) else {}
        step = Decimal(str(lot.get("qtyStep") or lot.get("basePrecision") or lot.get("minOrderQty") or "0.000001"))
        if step <= 0:
            step = Decimal("0.000001")
        min_qty = Decimal(str(lot.get("minOrderQty") or "0"))
        min_notional = Decimal(
            str(
                lot.get("minOrderAmt")
                or lot.get("minNotionalValue")
                or 0
            )
        )
        return {
            "step": step,
            "min_qty": min_qty,
            "min_notional": min_notional,
        }

    def minimum_order_value_usdt(self, symbol: str) -> float:
        try:
            return float(self._lot_constraints(symbol)["min_notional"])
        except (TypeError, ValueError):
            return 0.0

    def executable_min_order_value_usdt(self, symbol: str, price: float) -> float:
        constraints = self._lot_constraints(symbol)
        min_notional = constraints["min_notional"]
        min_qty = constraints["min_qty"]
        step = constraints["step"]
        executable_qty = max(min_qty, step)
        price_decimal = Decimal(str(price if price > 0 else 0.0))
        qty_floor_notional = executable_qty * price_decimal if price_decimal > 0 else Decimal("0")
        executable_min = max(min_notional, qty_floor_notional)
        return float(executable_min)

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
    def fetch_snapshot(self, symbol: str, timeframe: str, include_microstructure: bool = True) -> MarketSnapshot:
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
        microstructure = self._market_microstructure(symbol, "linear", closes[-1]) if include_microstructure else {}
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            last_price=closes[-1],
            **microstructure,
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
        try:
            return float(self._lot_constraints(symbol)["min_notional"])
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
        try:
            response = self._request(
                "POST",
                "/v5/position/trading-stop",
                payload,
                private=True,
            )
        except RuntimeError as exc:
            if "not modified" in str(exc).lower():
                return {
                    "status": "unchanged",
                    "symbol": symbol,
                    "take_profit": take_profit,
                    "stop_loss": stop_loss,
                    "trailing_stop": trailing_stop,
                    "reason": str(exc),
                }
            raise
        return {
            "status": "ok",
            "symbol": symbol,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "trailing_stop": trailing_stop,
            "response": response.get("result", {}),
        }
