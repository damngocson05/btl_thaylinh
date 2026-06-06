from __future__ import annotations
import hashlib
import hmac
import time
from datetime import datetime
import requests
import pandas as pd
from typing import Optional, List, Dict
from urllib.parse import urlencode
import yfinance as yf

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds
BINANCE_BASE = "https://api.binance.com"


class PriceApiClient:
    def __init__(self, timeout: int = 10, api_key: str = "", api_secret: str = "") -> None:
        self.session = requests.Session()
        self.timeout = timeout
        self.api_key = api_key
        self.api_secret = api_secret
        if api_key:
            self.session.headers["X-MBX-APIKEY"] = api_key

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request_with_retry(self, url: str, params: dict, signed: bool = False) -> dict:
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                p = dict(params)
                if signed:
                    p = self._sign(p)
                response = self.session.get(url, params=p, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def _require_auth(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("Cần cấu hình BINANCE_API_KEY và BINANCE_API_SECRET trong config.py")

    # --- Public API ---

    def get_price(self, symbol: str, asset_type: str = "crypto") -> float:
        asset_type = asset_type.lower()
        if asset_type == "crypto":
            return self.get_crypto_price(symbol)
        if asset_type == "stock":
            return self.get_stock_price(symbol)
        raise ValueError(f"Loại tài sản không hỗ trợ: {asset_type}")

    def get_crypto_price(self, symbol: str) -> float:
        symbol_key = symbol.strip().upper()
        trading_pair = f"{symbol_key}USDT"
        url = f"{BINANCE_BASE}/api/v3/ticker/price"
        params = {"symbol": trading_pair}
        data = self._request_with_retry(url, params)
        if "price" not in data:
            raise ValueError(f"Không lấy được giá cho {symbol} từ Binance")
        return float(data["price"])

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> pd.DataFrame:
        symbol_key = symbol.strip().upper()
        trading_pair = f"{symbol_key}USDT"
        url = f"{BINANCE_BASE}/api/v3/klines"
        params = {"symbol": trading_pair, "interval": interval, "limit": limit}
        data = self._request_with_retry(url, params)
        rows = []
        for k in data:
            rows.append({
                "time": datetime.fromtimestamp(k[0] / 1000),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        df = pd.DataFrame(rows)
        df.set_index("time", inplace=True)
        return df

    def get_stock_price(self, symbol: str) -> float:
        ticker = yf.Ticker(symbol.strip().upper())
        history = ticker.history(period="1d", interval="1m")
        if history.empty:
            history = ticker.history(period="5d")
        if history.empty:
            raise ValueError(f"Không lấy được giá cho cổ phiếu {symbol}")
        return float(history["Close"].iloc[-1])

    # --- Authenticated API ---

    def get_account_balance(self) -> List[Dict]:
        self._require_auth()
        url = f"{BINANCE_BASE}/api/v3/account"
        data = self._request_with_retry(url, {}, signed=True)
        balances = []
        for b in data.get("balances", []):
            free = float(b["free"])
            locked = float(b["locked"])
            if free > 0 or locked > 0:
                balances.append({
                    "asset": b["asset"],
                    "free": free,
                    "locked": locked,
                    "total": free + locked,
                })
        return balances

    def get_my_trades(self, symbol: str, limit: int = 50) -> List[Dict]:
        self._require_auth()
        symbol_key = symbol.strip().upper()
        trading_pair = f"{symbol_key}USDT"
        url = f"{BINANCE_BASE}/api/v3/myTrades"
        params = {"symbol": trading_pair, "limit": limit}
        data = self._request_with_retry(url, params, signed=True)
        trades = []
        for t in data:
            trades.append({
                "id": t["id"],
                "price": float(t["price"]),
                "qty": float(t["qty"]),
                "quote_qty": float(t["quoteQty"]),
                "commission": float(t["commission"]),
                "commission_asset": t["commissionAsset"],
                "time": t["time"],
                "is_buyer": t["isBuyer"],
            })
        return trades

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        self._require_auth()
        url = f"{BINANCE_BASE}/api/v3/openOrders"
        params = {}
        if symbol:
            params["symbol"] = symbol.strip().upper() + "USDT"
        data = self._request_with_retry(url, params, signed=True)
        orders = []
        for o in data:
            orders.append({
                "symbol": o["symbol"],
                "side": o["side"],
                "type": o["type"],
                "price": float(o["price"]),
                "qty": float(o["origQty"]),
                "executed_qty": float(o["executedQty"]),
                "status": o["status"],
                "time": o["time"],
            })
        return orders

