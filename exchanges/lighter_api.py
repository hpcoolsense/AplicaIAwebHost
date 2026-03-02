from __future__ import annotations

import os
import time
import asyncio
import threading
import inspect
import itertools
import lighter
from typing import Any, Callable, Dict, Optional, Tuple, List

# ==========================================================
# 1. HELPERS GLOBALES
# ==========================================================

async def _maybe_call(fn, *args, **kwargs):
    res = fn(*args, **kwargs)
    if asyncio.iscoroutine(res): return await res
    return res

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()

def _env_int(name: str, default: int) -> int:
    try: return int(_env(name, str(default)) or str(default))
    except: return int(default)

def _env_float(name: str, default: float) -> float:
    try: return float(_env(name, str(default)) or str(default))
    except: return float(default)

def _require(v: str, name: str) -> str:
    if not v: raise RuntimeError(f"Lighter: falta {name} en .env")
    return v

def _build_token_index_map() -> Dict[str, int]:
    m: Dict[str, int] = {}
    for i in range(1, 51):
        sym = _env(f"T{i}_SYMBOL", "").upper()
        if sym: m[sym] = i
    return m

def _token_cfg(symbol: str) -> Dict[str, Any]:
    tok = (symbol or "").strip().upper()
    token_map = _build_token_index_map()
    idx = token_map.get(tok)
    g_market = _env_int("LIGHTER_MARKET_INDEX", 0)
    g_base_dec = _env_int("LIGHTER_BASE_DECIMALS", 6)
    g_price_dec = _env_int("LIGHTER_PRICE_DECIMALS", 2)
    if not idx:
        return {"market_index": g_market, "base_decimals": g_base_dec, "price_decimals": g_price_dec}
    return {
        "market_index": _env_int(f"T{idx}_LIGHTER_MARKET_INDEX", g_market),
        "base_decimals": _env_int(f"T{idx}_LIGHTER_BASE_DECIMALS", g_base_dec),
        "price_decimals": _env_int(f"T{idx}_LIGHTER_PRICE_DECIMALS", g_price_dec),
    }

def _load_sdk():
    sdk_module = _env("LIGHTER_SDK_MODULE", "")
    candidates = [sdk_module] if sdk_module else []
    candidates += ["lighter", "lighter_sdk"]
    
    for name in candidates:
        if not name: continue
        try:
            mod = __import__(name, fromlist=["SignerClient", "OrderType", "TimeInForce", "AccountApi", "OrderApi", "ApiClient"])
            SignerClient = getattr(mod, "SignerClient")
            OrderType = getattr(mod, "OrderType", None)
            TimeInForce = getattr(mod, "TimeInForce", None)
            AccountApi = getattr(mod, "AccountApi", None)
            OrderApi = getattr(mod, "OrderApi", None)
            ApiClient = getattr(mod, "ApiClient", None)
            return mod, SignerClient, OrderType, TimeInForce, AccountApi, OrderApi, ApiClient
        except Exception: continue
    raise ModuleNotFoundError("No se encontró librería lighter/SignerClient.")

# ==========================================================
# 2. ASYNC RUNNER
# ==========================================================
class _AsyncLoopRunner:
    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thr: Optional[threading.Thread] = None
        self._ready = threading.Event()
    def start(self):
        if self._thr and self._thr.is_alive(): return
        def _worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            loop.run_forever()
        self._thr = threading.Thread(target=_worker, daemon=True, name="lighter-async-loop")
        self._thr.start()
        self._ready.wait(timeout=5)
    def run(self, coro, timeout: Optional[float] = None):
        self.start()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

_ASYNC = _AsyncLoopRunner()

# ==========================================================
# 3. CLASE PRINCIPAL LIGHTER API
# ==========================================================
class LighterAPI:
    def __init__(self):
        self.base_url      = (_env("LIGHTER_REST_URL", "https://mainnet.zklighter.elliot.ai")).rstrip("/")
        self.api_key_priv = _require(_env("LIGHTER_API_KEY_PRIVATE_KEY", ""), "LIGHTER_API_KEY_PRIVATE_KEY")
        self.api_key_idx  = int(_require(_env("LIGHTER_API_KEY_INDEX", ""), "LIGHTER_API_KEY_INDEX"))
        self.account_idx  = int(_require(_env("LIGHTER_ACCOUNT_INDEX", ""), "LIGHTER_ACCOUNT_INDEX"))
        self.auth_exp_s   = _env_int("LIGHTER_AUTH_EXPIRY_S", 600)
        self.slippage_pct = _env_float("LIGHTER_SLIPPAGE_PCT", 0.01)
        
        self._sdk_mod, self._SignerClient, self._OrderType, self._TIF, self._AccountApi, self._OrderApi, self._ApiClient = _load_sdk()
        
        self._client: Any = None
        self._token_expires_at: float = 0.0
        self._client_lock = threading.Lock()
        self._signer_ctor_kwargs = self._prepare_signer_ctor_kwargs()
        self._order_idx_lock = threading.Lock()
        self._order_idx = itertools.count(int(time.time() * 1000) % 1000000)
        self._diag = _env("LIGHTER_DIAG", "false").lower() == "true"
        self._diag_once = False
        self._api_client = None
        self._order_api = None
        self._market_cache: Dict[int, Dict[str, Any]] = {}

        # HFT TLS Warmup & Async Pool initialization
        try:
            _ASYNC.run(self._ensure_client_and_token())
            # Force pre-fetch market info to cache decimals and warm up HTTP connection
            cfg = _token_cfg("ETH-USDT") # generic token fetch to warm up
            _ASYNC.run(self._get_market_info(cfg["market_index"]))
            print("[LIGHTER HFT] Async EventLoop & TLS Connection Pre-warmed.")
        except Exception as e:
            print(f"[LIGHTER HFT] Warmup failed: {e}")

    async def _get_market_info(self, market_index: int) -> Dict[str, Any]:
        if market_index in self._market_cache:
            return self._market_cache[market_index]
        if self._api_client is None:
            self._api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.base_url))
        if self._order_api is None:
            self._order_api = lighter.OrderApi(self._api_client)
        resp = await self._order_api.order_book_details(market_id=int(market_index))
        details = []
        if hasattr(resp, "order_book_details") and resp.order_book_details:
            details = resp.order_book_details
        elif hasattr(resp, "spot_order_book_details") and resp.spot_order_book_details:
            details = resp.spot_order_book_details
        info = {}
        for item in details:
            try:
                if int(getattr(item, "market_id", -1)) != int(market_index):
                    continue
                info = {
                    "size_decimals": int(getattr(item, "size_decimals")),
                    "price_decimals": int(getattr(item, "price_decimals")),
                    "min_base_amount": float(getattr(item, "min_base_amount")),
                    "min_quote_amount": float(getattr(item, "min_quote_amount")),
                }
                break
            except Exception:
                continue
        self._market_cache[market_index] = info
        return info

    def _next_client_order_index(self) -> int:
        with self._order_idx_lock:
            return next(self._order_idx) % 1000000

    def _prepare_signer_ctor_kwargs(self) -> Dict[str, Any]:
        params = inspect.signature(self._SignerClient.__init__).parameters
        kwargs = {}
        if "url" in params: kwargs["url"] = self.base_url
        if "account_index" in params: kwargs["account_index"] = self.account_idx
        if "api_private_keys" in params: kwargs["api_private_keys"] = {int(self.api_key_idx): self.api_key_priv}
        return kwargs

    async def _ensure_client_and_token(self):
        if self._client is None:
            self._client = self._SignerClient(**self._signer_ctor_kwargs)
        if hasattr(self._client, "create_auth_token_with_expiry") and time.time() > self._token_expires_at:
            await _maybe_call(self._client.create_auth_token_with_expiry, int(self.auth_exp_s))
            self._token_expires_at = time.time() + self.auth_exp_s

    def _normalize_response(self, resp: Any) -> Dict[str, Any]:
        txt = str(resp).lower()
        if any(n in txt for n in ("error", "fail", "reject", "insufficient")):
            return {"accepted": False, "status": "rejected", "raw": resp}
        return {"accepted": True, "status": "accepted", "raw": resp}

    def _build_create_order_req(self, *, cfg: Dict[str, Any], qty_base: float, price: float, is_ask: bool,
                                order_type: Any, trigger_price: Optional[float] = None) -> Any:
        from lighter.signer_client import CreateOrderTxReq

        q_i = int(round(qty_base * (10**cfg["base_decimals"])))
        p_i = int(round(price * (10**cfg["price_decimals"])))
        trig_i = 0
        if trigger_price is not None:
            trig_i = int(round(trigger_price * (10**cfg["price_decimals"])))

        # Algunos builds del SDK exponen CreateOrderTxReq como struct ctypes.
        # Si _fields_ está presente, construimos por posición para evitar kwargs.
        if self._diag and not self._diag_once:
            sig = getattr(CreateOrderTxReq, "__text_signature__", None)
            fields = getattr(CreateOrderTxReq, "_fields_", None)
            print("[LIGHTER_DIAG] CreateOrderTxReq __text_signature__:", sig)
            print("[LIGHTER_DIAG] CreateOrderTxReq type:", type(CreateOrderTxReq))
            print("[LIGHTER_DIAG] CreateOrderTxReq _fields_:", fields)
            self._diag_once = True

        fields = getattr(CreateOrderTxReq, "_fields_", None)
        if fields:
            by_name = {
                "MarketIndex": int(cfg["market_index"]),
                "ClientOrderIndex": self._next_client_order_index(),
                "BaseAmount": q_i,
                "Price": p_i,
                "IsAsk": 1 if is_ask else 0,
                "Type": order_type,
                "TimeInForce": self._client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                "ReduceOnly": 1,
                "TriggerPrice": trig_i,
                "OrderExpiry": -1,
            }
            req = CreateOrderTxReq()
            for name, _ in fields:
                setattr(req, name, by_name.get(name, 0))
            return req

        # Fallback kwargs (si no hay _fields_)
        try:
            return CreateOrderTxReq(
                MarketIndex=int(cfg["market_index"]),
                ClientOrderIndex=self._next_client_order_index(),
                BaseAmount=q_i,
                Price=p_i,
                IsAsk=1 if is_ask else 0,
                Type=order_type,
                TimeInForce=self._client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                ReduceOnly=1,
                TriggerPrice=trig_i,
                OrderExpiry=-1,
            )
        except Exception:
            return CreateOrderTxReq(
                market_index=int(cfg["market_index"]),
                client_order_index=self._next_client_order_index(),
                base_amount=q_i,
                price=p_i,
                is_ask=1 if is_ask else 0,
                order_type=order_type,
                time_in_force=self._client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=True,
                trigger_price=trig_i,
                order_expiry=-1,
            )

    # --- Market Order Standard ---
    def place_market(self, symbol: str, side: str, qty_base: float, *, avg_exec_px: float, is_close: bool = False, **kwargs) -> Dict[str, Any]:
        lock = self._client_lock
        if lock: lock.acquire()
        try:
            async def _do():
                await self._ensure_client_and_token()
                cfg = _token_cfg(symbol)
                q_i = int(round(qty_base * (10**cfg["base_decimals"])))
                p_i = int(round(avg_exec_px * (10**cfg["price_decimals"])))
                return await _maybe_call(self._client.create_market_order_if_slippage, 
                                       market_index=int(cfg["market_index"]), 
                                       client_order_index=0, 
                                       base_amount=q_i, 
                                       ideal_price=p_i,
                                       max_slippage=float(self.slippage_pct),
                                       is_ask=bool(side.upper() in ["SELL", "SHORT"]))
            raw = _ASYNC.run(_do())
            return self._normalize_response(raw)
        finally:
            if lock: lock.release()

    def place_limit(self, symbol: str, side: str, qty_base: float, price: float) -> Dict[str, Any]:
        try:
            async def _do():
                await self._ensure_client_and_token()
                cfg = _token_cfg(symbol)
                info = await self._get_market_info(cfg["market_index"])
                size_decimals = info.get("size_decimals", cfg["base_decimals"])
                price_decimals = info.get("price_decimals", cfg["price_decimals"])
                min_base = info.get("min_base_amount")
                min_quote = info.get("min_quote_amount")
                if min_base and qty_base < min_base:
                    raise ValueError(f"Lighter min_base_amount {min_base} > qty {qty_base}")
                if min_quote and (qty_base * price) < min_quote:
                    raise ValueError(f"Lighter min_quote_amount {min_quote} > notional {qty_base * price}")
                is_ask = bool(side.upper() in ["SELL", "SHORT"])
                res = await self._client.create_order(
                    int(cfg["market_index"]),
                    self._next_client_order_index(),
                    int(round(qty_base * (10**size_decimals))),
                    int(round(price * (10**price_decimals))),
                    1 if is_ask else 0,
                    self._client.ORDER_TYPE_LIMIT,
                    self._client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    0,  # <-- reduce_only = FALSE para Spot
                    0,
                    -1,
                )
                if self._diag:
                    print("[LIGHTER_DIAG] create_order(limit) result:", res)
                return res
            return {"accepted": True, "raw": _ASYNC.run(_do())}
        except Exception as e:
            print(f"[LIGHTER LIMIT ERROR]: {e}")
            return {"accepted": False}

    def place_stop(self, symbol: str, side: str, qty_base: float, trigger_price: float) -> Dict[str, Any]:
        try:
            async def _do():
                await self._ensure_client_and_token()
                cfg = _token_cfg(symbol)
                info = await self._get_market_info(cfg["market_index"])
                size_decimals = info.get("size_decimals", cfg["base_decimals"])
                price_decimals = info.get("price_decimals", cfg["price_decimals"])
                min_base = info.get("min_base_amount")
                min_quote = info.get("min_quote_amount")
                is_ask = bool(side.upper() in ["SELL", "SHORT"])
                # Ejecuta a mercado con un colchón mucho más apretado (0.5% en lugar de 5%)
                exec_price = trigger_price * (0.995 if is_ask else 1.005)
                if min_base and qty_base < min_base:
                    raise ValueError(f"Lighter min_base_amount {min_base} > qty {qty_base}")
                if min_quote and (qty_base * exec_price) < min_quote:
                    raise ValueError(f"Lighter min_quote_amount {min_quote} > notional {qty_base * exec_price}")
                res = await self._client.create_order(
                    int(cfg["market_index"]),
                    self._next_client_order_index(),
                    int(round(qty_base * (10**size_decimals))),
                    int(round(exec_price * (10**price_decimals))),
                    1 if is_ask else 0,
                    self._client.ORDER_TYPE_STOP_LOSS_LIMIT,
                    self._client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    0,  # <-- reduce_only = FALSE para Spot
                    int(round(trigger_price * (10**price_decimals))),
                    -1,
                )
                if self._diag:
                    print("[LIGHTER_DIAG] create_order(stop) result:", res)
                return res
            return {"accepted": True, "raw": _ASYNC.run(_do())}
        except Exception as e:
            print(f"[LIGHTER STOP ERROR]: {e}")
            return {"accepted": False}

    # --- Stop Loss Individual (Marketable Limit) ---
    def create_sl_order(self, symbol: str, side: str, qty: float, trigger_price: float) -> Dict[str, Any]:
        try:
            async def _do():
                await self._ensure_client_and_token()
                cfg = _token_cfg(symbol)
                q_i = int(round(qty * (10**cfg["base_decimals"])))
                trig_i = int(round(trigger_price * (10**cfg["price_decimals"])))
                
                # side = Dirección de APERTURA.
                # Si side="BUY" (Long), el cierre es una VENTA.
                is_close_sell = (side.upper() in ["BUY", "LONG"])
                
                # LÓGICA DE CIERRE GARANTIZADO:
                # Si vendemos (Close Long), ponemos precio límite -5% para que ejecute YA.
                # Si compramos (Close Short), ponemos precio límite +5% para que ejecute YA.
                if is_close_sell:
                    exec_price = trigger_price * 0.95 
                else:
                    exec_price = trigger_price * 1.05
                    
                p_i = int(round(exec_price * (10**cfg["price_decimals"])))

                return await _maybe_call(self._client.create_sl_order, 
                                         market_index=int(cfg["market_index"]),
                                         client_order_index=0,
                                         base_amount=q_i,
                                         trigger_price=trig_i,
                                         price=p_i,
                                         is_ask=is_close_sell,
                                         reduce_only=True)
            return self._normalize_response(_ASYNC.run(_do()))
        except Exception as e:
            print(f"[LIGHTER SL ERROR]: {e}")
            return {"accepted": False}

    # --- OCO Grouped (TP Limit + SL Marketable) ---
    def set_tpsl_grouped(self, symbol: str, side: str, tp_trigger: float, sl_trigger: float) -> Dict[str, Any]:
        try:
            async def _do():
                await self._ensure_client_and_token()
                cfg = _token_cfg(symbol)
                
                is_open_long = (side.upper() in ["BUY", "LONG"])
                is_close_ask = is_open_long # Si abrí Long, cierro con Ask (Venta)
                
                dec_p = 10**cfg["price_decimals"]
                
                tp_trig_i = int(round(tp_trigger * dec_p))
                sl_trig_i = int(round(sl_trigger * dec_p))
                
                # Configuramos TP como Limit estricto (queremos ganar eso o más)
                tp_limit_i = int(round(tp_trigger * dec_p))
                
                # Configuramos SL como Marketable Limit (queremos salir SÍ o SÍ)
                if is_close_ask: 
                    # Cerrar Long -> Vender. Precio límite bajo para asegurar venta.
                    sl_exec_price = sl_trigger * 0.95
                else: 
                    # Cerrar Short -> Comprar. Precio límite alto para asegurar compra.
                    sl_exec_price = sl_trigger * 1.05

                sl_limit_i = int(round(sl_exec_price * dec_p))

                # Usamos los tipos del SDK
                from lighter.signer_client import CreateOrderTxReq

                # 1. Take Profit (Limit Standard)
                tp_order = CreateOrderTxReq(
                    MarketIndex=int(cfg["market_index"]),
                    ClientOrderIndex=0,
                    BaseAmount=0, 
                    Price=tp_limit_i,
                    IsAsk=is_close_ask,
                    Type=self._client.ORDER_TYPE_TAKE_PROFIT_LIMIT,
                    TimeInForce=self._client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    ReduceOnly=1,
                    TriggerPrice=tp_trig_i,
                    OrderExpiry=-1, 
                )

                # 2. Stop Loss (Marketable Limit)
                sl_order = CreateOrderTxReq(
                    MarketIndex=int(cfg["market_index"]),
                    ClientOrderIndex=0,
                    BaseAmount=0, 
                    Price=sl_limit_i, # <--- PRECIO FORZADO PARA EJECUCIÓN INMEDIATA
                    IsAsk=is_close_ask,
                    Type=self._client.ORDER_TYPE_STOP_LOSS_LIMIT,
                    TimeInForce=self._client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME, 
                    ReduceOnly=1,
                    TriggerPrice=sl_trig_i,
                    OrderExpiry=-1,
                )

                return await self._client.create_grouped_orders(
                    grouping_type=self._client.GROUPING_TYPE_ONE_CANCELS_THE_OTHER,
                    orders=[tp_order, sl_order],
                )

            return {"accepted": True, "raw": _ASYNC.run(_do())}
        except Exception as e:
            print(f"[LIGHTER OCO ERROR]: {e}")
            return {"accepted": False}

    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        try:
            async def _do():
                await self._ensure_client_and_token()
                cfg = _token_cfg(symbol)
                return await _maybe_call(self._client.cancel_all_orders, market_index=int(cfg["market_index"]))
            return {"accepted": True, "raw": _ASYNC.run(_do())}
        except Exception as e:
            return {"accepted": False}

    # --- Consultas de Estado ---
    def get_account_info(self):
        if not self._AccountApi: return None
        try:
            async def _do():
                await self._ensure_client_and_token()
                api_client = self._client.api_client
                acc_api = self._AccountApi(api_client)
                return await _maybe_call(acc_api.account, by="index", value=str(self.account_idx))
            return _ASYNC.run(_do())
        except Exception as e:
            print(f"🔥 ERROR OCULTO EN LIGHTER: {e}")
            import traceback
            traceback.print_exc()
            return None

    def get_active_orders(self, market_index: int = None):
        if not self._OrderApi: return None
        try:
            async def _do():
                await self._ensure_client_and_token()
                return await _maybe_call(self._client.get_orders) # Intento genérico
            return _ASYNC.run(_do())
        except: return []
    
    def close(self):
        try:
            async def _do():
                if self._client and hasattr(self._client, 'api_client'):
                    await self._client.api_client.close()
            _ASYNC.run(_do())
        except: pass

LighterClient = LighterAPI
