# exchanges/binance_futures_api.py
from __future__ import annotations

import os
import time
import hmac
import hashlib
import requests
from typing import Dict, Any, Optional, List
from requests.adapters import HTTPAdapter


BASE_URL = os.getenv("BINANCE_FUT_REST_URL", "https://fapi.binance.com").rstrip("/")


def _mk_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"Content-Type": "application/x-www-form-urlencoded", "Connection": "keep-alive"})
    adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0, pool_block=False)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_SESSION = _mk_session()


def _to_binance_symbol(base: str, quote: str) -> str:
    return f"{(base or '').upper()}{(quote or '').upper()}"


class BinanceFuturesClient:
    def __init__(self) -> None:
        self.api_key = (os.getenv("BINANCE_API_KEY", "") or "").strip()
        self.api_secret = (os.getenv("BINANCE_API_SECRET", "") or "").strip()

        if not self.api_key or not self.api_secret:
            raise RuntimeError("BinanceFuturesClient: faltan BINANCE_API_KEY / BINANCE_API_SECRET en .env")

        self._session = _SESSION
        self._timeout = (float(os.getenv("BINANCE_TIMEOUT_CONNECT", "1.5")),
                         float(os.getenv("BINANCE_TIMEOUT_READ", "8.0")))

    # ---------- helpers ----------
    def _sign(self, qs: str) -> str:
        return hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        qs = "&".join([f"{k}={params[k]}" for k in params.keys()])
        sig = self._sign(qs)
        url = f"{BASE_URL}{path}?{qs}&signature={sig}"

        headers = {"X-MBX-APIKEY": self.api_key}
        r = self._session.request(method, url, headers=headers, timeout=self._timeout, allow_redirects=False)
        if not (200 <= r.status_code < 300):
            raise requests.HTTPError(f"Binance HTTP {r.status_code}: {r.text[:500]}")
        return r.json()

    # ---------- reads ----------
    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._request("GET", "/fapi/v1/openOrders", params)
        return data if isinstance(data, list) else []

    def get_positions(self) -> List[Dict]:
        data = self._request("GET", "/fapi/v2/positionRisk", {})
        return data if isinstance(data, list) else []

    # ---------- trading ----------
    def place_market(self, *, symbol: str, side: str, base_qty: float) -> Dict[str, Any]:
        if base_qty <= 0:
            raise ValueError("Binance.place_market: qty inválida")

        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError("Binance.place_market: side debe ser BUY/SELL")

        params = {
            "symbol": symbol,
            "side": s,
            "type": "MARKET",
            "quantity": f"{float(base_qty):.8f}".rstrip("0").rstrip("."),
        }
        return self._request("POST", "/fapi/v1/order", params)

    def set_position_tpsl(
        self,
        *,
        symbol: str,
        position_side: str,  # "bid"/"ask" (compat). Lo mapeamos a LONG/SHORT solo a nivel conceptual.
        tp_stop: float,
        sl_stop: float,
        tp_client_order_id: Optional[str] = None,
        sl_client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Crea 2 órdenes reduceOnly:
          - TAKE_PROFIT_MARKET
          - STOP_MARKET
        Ambas cierran la posición (reduceOnly).
        """
        # Si estás LONG, para cerrar es SELL. Si estás SHORT, para cerrar es BUY.
        side_close = "SELL" if (position_side or "").lower() == "ask" else "BUY"

        out: Dict[str, Any] = {"tp": None, "sl": None}

        # TP
        tp_params = {
            "symbol": symbol,
            "side": side_close,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": f"{float(tp_stop):.8f}".rstrip("0").rstrip("."),
            "closePosition": "true",
            "reduceOnly": "true",
            "workingType": os.getenv("BINANCE_WORKING_TYPE", "MARK_PRICE"),
        }
        if tp_client_order_id:
            tp_params["newClientOrderId"] = tp_client_order_id

        # SL
        sl_params = {
            "symbol": symbol,
            "side": side_close,
            "type": "STOP_MARKET",
            "stopPrice": f"{float(sl_stop):.8f}".rstrip("0").rstrip("."),
            "closePosition": "true",
            "reduceOnly": "true",
            "workingType": os.getenv("BINANCE_WORKING_TYPE", "MARK_PRICE"),
        }
        if sl_client_order_id:
            sl_params["newClientOrderId"] = sl_client_order_id

        out["tp"] = self._request("POST", "/fapi/v1/order", tp_params)
        out["sl"] = self._request("POST", "/fapi/v1/order", sl_params)
        return out


# Alias para tu patrón de ctor dinámico
BinanceFuturesAPI = BinanceFuturesClient
BinanceFuturesClient = BinanceFuturesClient
