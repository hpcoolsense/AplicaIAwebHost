import ccxt
import os
import json
import uuid
import time
import threading
import hmac
import hashlib
import urllib.parse
from pathlib import Path
import requests
import websockets
import asyncio

class BinanceAdapter:
    def __init__(self):
        self._quiet = os.getenv("BINANCE_QUIET", "true").lower() == "true"
        def qprint(*args, **kwargs):
            if not self._quiet:
                print(*args, **kwargs)
        self._qprint = qprint

        api_key = os.getenv("BINANCE_API_KEY")
        secret = os.getenv("BINANCE_SECRET")
        if not api_key or not secret:
            self._qprint("[BINANCE WARN] BINANCE_API_KEY/BINANCE_SECRET faltan o están vacías")
        self.client = ccxt.binanceusdm({
            'apiKey': api_key,
            'secret': secret,
            'timeout': 3000,
            'enableRateLimit': False,
            'headers': {
                'Connection': 'keep-alive'
            },
            'options': {
                'defaultType': 'future',
                'warnOnFetchOpenOrdersWithoutSymbol': False 
            }
        })
        self.quote = os.getenv("QUOTE", "USDT").upper()
        self.client.load_markets()
        
        # HFT TLS Warmup
        try:
            # Pings the server to establish an early SSL/TCP handshake
            self.client.fapiPublicGetPing()
            self._qprint("[BINANCE HFT] TLS Connection Pre-warmed.")
        except Exception as e:
            self._qprint(f"[BINANCE HFT] TLS Warmup failed: {e}")
        self._id_prefix = os.getenv("BINANCE_CLIENT_ID_PREFIX", "ARB")
        self._id_store = Path(os.getenv("BINANCE_ORDER_ID_STORE", "/tmp/binance_order_ids.json"))
        self._stream_enabled = os.getenv("BINANCE_USER_STREAM", "false").lower() == "true"
        self._listen_key = None
        self._stream_orders = {}
        self._stream_lock = threading.Lock()
        if self._stream_enabled:
            self._qprint("[BINANCE STREAM] habilitado")
            threading.Thread(target=self._start_user_stream, daemon=True).start()

    def _record_client_order_id(self, symbol: str, oid: str):
        try:
            data = {}
            if self._id_store.exists():
                data = json.loads(self._id_store.read_text())
            sym = symbol.upper()
            ids = data.get(sym, [])
            if oid not in ids:
                ids.append(oid)
            data[sym] = ids[-200:]
            self._id_store.write_text(json.dumps(data))
        except Exception:
            pass

    def _start_user_stream(self):
        try:
            self._listen_key = self._get_listen_key()
            if not self._listen_key:
                self._qprint("[BINANCE STREAM] no se pudo obtener listenKey")
                return
            self._qprint("[BINANCE STREAM] listenKey ok")
            threading.Thread(target=self._keepalive_listen_key, daemon=True).start()
            asyncio.run(self._user_stream_loop(self._listen_key))
        except Exception as e:
            self._qprint(f"[BINANCE STREAM] error al iniciar: {e}")
            pass

    def _get_listen_key(self):
        try:
            base = "https://fapi.binance.com"
            resp = requests.post(f"{base}/fapi/v1/listenKey", headers={"X-MBX-APIKEY": self.client.apiKey})
            if resp.ok:
                return resp.json().get("listenKey")
            else:
                self._qprint(f"[BINANCE STREAM] listenKey status={resp.status_code} body={resp.text}")
        except Exception:
            pass
        return None

    def _keepalive_listen_key(self):
        while True:
            try:
                if self._listen_key:
                    base = "https://fapi.binance.com"
                    requests.put(f"{base}/fapi/v1/listenKey", headers={"X-MBX-APIKEY": self.client.apiKey}, params={"listenKey": self._listen_key})
            except Exception:
                pass
            time.sleep(60 * 25)

    def _fapi_symbol(self, market_symbol: str):
        # Convert "BNB/USDT:USDT" -> "BNBUSDT"
        return market_symbol.split(":")[0].replace("/", "")

    def _fapi_signed_request(self, method: str, path: str, params: dict):
        """
        Signed request to fapi private endpoints (manual, to avoid ccxt URL issues).
        """
        api_key = self.client.apiKey
        secret = self.client.secret
        if not api_key or not secret:
            raise Exception("missing apiKey/secret")
        base = "https://fapi.binance.com"
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params, doseq=True)
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{base}/fapi/v1/{path}?{query}&signature={signature}"
        resp = requests.request(method, url, headers={"X-MBX-APIKEY": api_key})
        if resp.ok:
            return resp.json()
        raise Exception(f"status={resp.status_code} body={resp.text}")

    def _fapi_signed_get(self, path: str, params: dict):
        return self._fapi_signed_request("GET", path, params)

    async def _user_stream_loop(self, listen_key: str):
        ws_url = f"wss://fstream.binance.com/ws/{listen_key}"
        while True:
            try:
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if msg.get("e") == "ORDER_TRADE_UPDATE":
                            o = msg.get("o", {})
                            status = o.get("X")  # NEW/CANCELED/FILLED/EXPIRED
                            oid = o.get("i")
                            client_id = o.get("c")
                            sym = o.get("s")
                            if not oid:
                                continue
                            with self._stream_lock:
                                if status in ("NEW", "PARTIALLY_FILLED"):
                                    self._stream_orders[str(oid)] = {"symbol": sym, "clientOrderId": client_id, "status": status}
                                elif status in ("CANCELED", "FILLED", "EXPIRED", "REJECTED"):
                                    self._stream_orders.pop(str(oid), None)
                            self._qprint(f"[BINANCE STREAM] {sym} {status} {client_id or oid}")
            except Exception:
                await asyncio.sleep(3)

    def get_stream_open_orders(self, symbol: str):
        if not self._stream_enabled:
            return []
        with self._stream_lock:
            market_symbol = self._get_market_symbol(symbol).replace("/", "").replace(":","")
            return [o for o in self._stream_orders.values() if o.get("symbol") == market_symbol]

    def _get_market_symbol(self, base_symbol):
        base = base_symbol.upper()
        s1 = f"{base}/{self.quote}"
        if s1 in self.client.markets: return s1
        s2 = f"{base}/{self.quote}:{self.quote}"
        if s2 in self.client.markets: return s2
        return s1

    # --- EJECUCIÓN ---
    def place_market(self, symbol, side, base_qty, is_close=False, **kwargs):
        market_symbol = self._get_market_symbol(symbol)
        amt = self.client.amount_to_precision(market_symbol, base_qty)
        params = {}
        if is_close:
            params['reduceOnly'] = True
        return self.client.create_order(market_symbol, 'market', side, amt, params=params)

    def place_limit(self, symbol, side, base_qty, price):
        market_symbol = self._get_market_symbol(symbol)
        amt = self.client.amount_to_precision(market_symbol, base_qty)
        px = self.client.price_to_precision(market_symbol, price)
        client_id = f"{self._id_prefix}_TP_{uuid.uuid4().hex[:20]}"
        self._record_client_order_id(symbol, client_id)
        return self.client.create_order(
            market_symbol,
            'LIMIT',
            side,
            amt,
            px,
            params={'reduceOnly': True, 'timeInForce': 'GTC', 'newClientOrderId': client_id}
        )

    def place_stop(self, symbol, side, base_qty, stop_price):
        market_symbol = self._get_market_symbol(symbol)
        amt = self.client.amount_to_precision(market_symbol, base_qty)
        sp = self.client.price_to_precision(market_symbol, stop_price)
        client_id = f"{self._id_prefix}_SL_{uuid.uuid4().hex[:20]}"
        self._record_client_order_id(symbol, client_id)
        return self.client.create_order(
            market_symbol,
            'STOP_MARKET',
            side,
            amt,
            None,
            params={'stopPrice': sp, 'reduceOnly': True, 'newClientOrderId': client_id}
        )

    def set_position_tpsl(self, symbol, position_side, tp_stop, sl_stop, **kwargs):
        market_symbol = self._get_market_symbol(symbol)
        ps = position_side.lower()
        side = 'SELL' if ps in ['ask', 'long', 'buy'] else 'BUY'
        
        try:
            sl_price = self.client.price_to_precision(market_symbol, sl_stop) if sl_stop else None
            tp_price = self.client.price_to_precision(market_symbol, tp_stop) if tp_stop else None
            
            # SL
            if sl_price:
                self.client.create_order(market_symbol, 'STOP_MARKET', side, 0, params={'stopPrice': sl_price, 'closePosition': True})
            # TP
            if tp_price:
                self.client.create_order(market_symbol, 'TAKE_PROFIT_MARKET', side, 0, params={'stopPrice': tp_price, 'closePosition': True})  
            return {"accepted": True}
        except Exception as e:
            print(f"[BINANCE ADAPTER ERROR] TPSL Rechazado: {e}")
            return {"accepted": False}

    # --- LECTURA (Status.py) ---
    def get_open_position(self, symbol):
        market_symbol = self._get_market_symbol(symbol)
        try:
            positions = self.client.fetch_positions([market_symbol])
            for p in positions:
                amt = float(p.get('contracts', 0) or p.get('info', {}).get('positionAmt', 0))
                if amt != 0:
                    return {
                        "size": abs(amt),
                        "side": "BUY" if amt > 0 else "SELL",
                        "unrealized_pnl": float(p.get('unrealizedPnl', 0) or 0)
                    }
            return None 
        except Exception as e:
            print(f"[BINANCE READ ERROR] get_position: {e}")
            return None

    def get_open_orders(self, symbol):
        """
        DETECTA ÓRDENES DE ACTIVACIÓN (STOP/CONDICIONALES)
        Binance Futures requiere llamar a fetch_open_orders con parámetros de filtrado
        para que devuelva las órdenes que no están en el libro (Trigger Orders).
        """
        market_symbol = self._get_market_symbol(symbol)
        try:
            seen = {}
            def add_orders(lst):
                for o in lst or []:
                    oid = o.get("id") or o.get("orderId")
                    if oid is not None:
                        seen[str(oid)] = o

            # 1) Órdenes abiertas normales
            add_orders(self.client.fetch_open_orders(market_symbol))

            # 1b) Open orders raw (futures endpoint, signed)
            try:
                raw_open = self._fapi_signed_get("openOrders", {
                    "symbol": self._fapi_symbol(market_symbol)
                })
                add_orders(raw_open)
            except Exception as e:
                print(f"[BINANCE READ ERROR] openOrders: {e}")

            # 2) Órdenes condicionales por tipo
            for typ in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
                try:
                    add_orders(self.client.fetch_open_orders(market_symbol, params={"type": typ}))
                except Exception as e:
                    self._qprint(f"[BINANCE READ ERROR] fetch_open_orders type={typ}: {e}")

            # 3) Órdenes condicionales (algo orders)
            try:
                algo = self._fapi_signed_get("algoOpenOrders", {
                    "symbol": self._fapi_symbol(market_symbol)
                })
                add_orders(algo)
            except Exception as e:
                self._qprint(f"[BINANCE READ ERROR] algoOpenOrders: {e}")

            # 4) Fallback: allAlgoOrders (algunas órdenes condicionales sólo aparecen aquí)
            try:
                all_algo = self._fapi_signed_get("allAlgoOrders", {
                    "symbol": self._fapi_symbol(market_symbol),
                    "limit": 200,
                })
                # Filtrar abiertas/activas
                add_orders([o for o in all_algo if str(o.get("algoStatus", "")).upper() in ("NEW", "WORKING", "TRIGGERED", "ACTIVE")])
            except Exception as e:
                self._qprint(f"[BINANCE READ ERROR] allAlgoOrders: {e}")

            # 3) Fallback general
            if not seen:
                all_raw = self.client.fetch_orders(market_symbol)
                add_orders([o for o in all_raw if o.get("status") == "open"])

            return list(seen.values())
        except Exception as e:
            self._qprint(f"[BINANCE READ ERROR] get_open_orders: {e}")
            return []

    def get_all_open_orders_debug(self, symbol):
        market_symbol = self._get_market_symbol(symbol)
        out = {"normal": [], "raw_open": [], "cond": {}, "algo": [], "all_algo": [], "all_open": [], "unique": []}
        try:
            out["normal"] = self.client.fetch_open_orders(market_symbol)
        except Exception as e:
            print(f"[BINANCE DEBUG] fetch_open_orders error: {e}")

        try:
            out["raw_open"] = self._fapi_signed_get("openOrders", {
                "symbol": self._fapi_symbol(market_symbol)
            })
        except Exception as e:
            out["raw_open"] = []
            self._qprint(f"[BINANCE DEBUG] openOrders error: {e}")

        for typ in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
            try:
                out["cond"][typ] = self.client.fetch_open_orders(market_symbol, params={"type": typ})
            except Exception as e:
                out["cond"][typ] = []

        try:
            all_raw = self.client.fetch_orders(market_symbol)
            out["all_open"] = [o for o in all_raw if o.get("status") == "open"]
        except Exception as e:
            print(f"[BINANCE DEBUG] fetch_orders error: {e}")

        try:
            out["algo"] = self._fapi_signed_get("algoOpenOrders", {
                "symbol": self._fapi_symbol(market_symbol)
            })
        except Exception:
            out["algo"] = []
        try:
            out["all_algo"] = self._fapi_signed_get("allAlgoOrders", {
                "symbol": self._fapi_symbol(market_symbol),
                "limit": 200,
            })
        except Exception:
            out["all_algo"] = []

        # Resumen
        try:
            seen = {}
            for o in out["normal"]:
                oid = o.get("id") or o.get("orderId")
                if oid is not None:
                    seen[str(oid)] = o
            for o in out["raw_open"]:
                oid = o.get("id") or o.get("orderId")
                if oid is not None:
                    seen[str(oid)] = o
            for lst in out["cond"].values():
                for o in lst:
                    oid = o.get("id") or o.get("orderId")
                    if oid is not None:
                        seen[str(oid)] = o
            for o in out["algo"]:
                oid = o.get("id") or o.get("orderId") or o.get("algoId")
                if oid is not None:
                    seen[str(oid)] = o
            for o in out["all_algo"]:
                if str(o.get("algoStatus", "")).upper() not in ("NEW", "WORKING", "TRIGGERED", "ACTIVE"):
                    continue
                oid = o.get("id") or o.get("orderId") or o.get("algoId")
                if oid is not None:
                    seen[str(oid)] = o
            out["unique"] = list(seen.values())

            # Conteo por estado algo
            status_counts = {}
            for o in out["all_algo"]:
                st = str(o.get("algoStatus", "")).upper() or "UNKNOWN"
                status_counts[st] = status_counts.get(st, 0) + 1
            self._qprint(f"[BINANCE DEBUG] open orders summary: normal={len(out['normal'])} raw_open={len(out['raw_open'])} cond_queries={sum(len(v) for v in out['cond'].values())} algo={len(out['algo'])} all_algo={len(out['all_algo'])} unique={len(out['unique'])} all_open={len(out['all_open'])} algo_status={status_counts}")
            for typ, lst in out["cond"].items():
                if lst:
                    print(f"  - {typ}: {len(lst)}")
        except Exception:
            pass

        return out

    def get_open_orders_by_client_ids(self, symbol):
        market_symbol = self._get_market_symbol(symbol)
        try:
            if not self._id_store.exists():
                return []
            data = json.loads(self._id_store.read_text())
            ids = data.get(symbol.upper(), [])
            out = []
            for oid in ids:
                try:
                    res = self.client.fapiPrivate_get_openOrder({
                        "symbol": market_symbol.replace("/", ""),
                        "origClientOrderId": oid,
                    })
                    if res:
                        out.append(res)
                except Exception:
                    pass
            return out
        except Exception:
            return []
    
    def cancel_all_orders(self, symbol):
        market_symbol = self._get_market_symbol(symbol)
        try:
            # Cancelación directa vía endpoint futures (más fiable con condicionales)
            try:
                self._fapi_signed_request("POST", "countdownCancelAll", {
                    "symbol": self._fapi_symbol(market_symbol),
                    "countdownTime": 0,
                })
            except Exception:
                pass

            try:
                self._fapi_signed_request("DELETE", "allOpenOrders", {
                    "symbol": self._fapi_symbol(market_symbol),
                })
            except Exception:
                pass

            # Cancelar algo orders (condicionales)
            try:
                self._fapi_signed_request("DELETE", "algoOpenOrders", {
                    "symbol": self._fapi_symbol(market_symbol)
                })
            except Exception:
                pass

            # Intento nativo
            try:
                self.client.cancel_all_orders(market_symbol)
            except Exception:
                pass

            # Intentos específicos para órdenes condicionales en futures
            for typ in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
                try:
                    self.client.cancel_all_orders(market_symbol, params={"type": typ})
                except Exception:
                    pass

            # Cancelar órdenes abiertas normales
            try:
                for o in self.client.fetch_open_orders(market_symbol):
                    oid = o.get('id')
                    if oid:
                        self.client.cancel_order(oid, market_symbol)
            except Exception:
                pass

            # Cancelar por clientOrderId (si hay registros)
            try:
                if self._id_store.exists():
                    data = json.loads(self._id_store.read_text())
                    ids = data.get(symbol.upper(), [])[:10]
                    if ids:
                        self._fapi_signed_request("DELETE", "batchOrders", {
                            "symbol": self._fapi_symbol(market_symbol),
                            "origClientOrderIdList": json.dumps(ids),
                        })
            except Exception:
                pass

            # Cancelar órdenes condicionales (a veces no aparecen en open_orders)
            for typ in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
                try:
                    cond = self.client.fetch_open_orders(market_symbol, params={"type": typ})
                    for o in cond:
                        oid = o.get('id')
                        if oid:
                            self.client.cancel_order(oid, market_symbol)
                except Exception:
                    pass

            # Fallback: inspeccionar todas
            try:
                all_raw = self.client.fetch_orders(market_symbol)
                for o in all_raw:
                    if o.get('status') == 'open':
                        oid = o.get('id')
                        if oid:
                            self.client.cancel_order(oid, market_symbol)
            except Exception:
                pass

            return []
        except Exception:
            return []
