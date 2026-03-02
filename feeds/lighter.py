# feeds/lighter.py
import os
import asyncio
import json
import logging
import time
from typing import Any, Iterable, List, Tuple, Optional
import contextlib
import websockets
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

from core.book import OrderBook

WS_URL = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
ENV_MARKET_INDEX = os.getenv("LIGHTER_MARKET_INDEX")
SYMBOL_HINT = os.getenv("LIGHTER_SYMBOL_HINT")
DEBUG_DISCOVERY = os.getenv("LIGHTER_DEBUG_DISCOVERY", "0") == "1"

APP_PING_SECS = int(os.getenv("LIGHTER_APP_PING_SECS", "10"))
INACT_SECS    = float(os.getenv("LIGHTER_INACTIVITY_SECS", "35"))
RESUB_SECS    = float(os.getenv("LIGHTER_RESUB_SECS", "300"))
RECV_TIMEOUT  = float(os.getenv("LIGHTER_RECV_TIMEOUT", "10.0"))

RECONNECT_SLEEP_SECS = float(os.getenv("LIGHTER_RECONNECT_SLEEP_SECS", "0.5"))

logger = logging.getLogger("lighter")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        with contextlib.suppress(Exception):
            return json.loads(raw)
    return raw


def _coerce_price_size_list(raw_list: Iterable[Any]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for it in raw_list or []:
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            with contextlib.suppress(Exception):
                p = float(it[0]); q = float(it[1])
                if q > 0:
                    out.append((p, q))
        elif isinstance(it, dict):
            p = it.get("price") or it.get("p") or it.get(0)
            q = it.get("size")  or it.get("q") or it.get(1)
            with contextlib.suppress(Exception):
                if p is not None and q is not None:
                    p = float(p); q = float(q)
                    if q > 0:
                        out.append((p, q))
    return out


def _ensure_sorted_top(entries: Iterable[Tuple[float, float]], reverse: bool, top_n: int) -> List[Tuple[float, float]]:
    lst: List[Tuple[float, float]] = []
    for p, q in entries or []:
        with contextlib.suppress(Exception):
            fp = float(p); fq = float(q)
            if fq > 0:
                lst.append((fp, fq))
    lst.sort(key=lambda x: x[0], reverse=reverse)
    return lst[:top_n]


def _looks_like_orderbook_update(msg: Any, subscribed_channel: str) -> bool:
    if not isinstance(msg, dict):
        return False

    def norm(s: str) -> str:
        return (s or "").lower().replace(":", "/").strip()

    ch_msg = norm(msg.get("channel") or msg.get("topic") or msg.get("ch") or "")
    ch_sub = norm(subscribed_channel)

    if ch_msg == ch_sub:
        return True
    if isinstance(msg.get("order_book"), dict):
        return True
    data = msg.get("data") or msg.get("payload")
    if isinstance(data, dict) and isinstance(data.get("order_book"), dict):
        return True
    if isinstance(msg.get("bids"), list) or isinstance(msg.get("asks"), list):
        return True
    if isinstance(data, dict) and (isinstance(data.get("bids"), list) or isinstance(data.get("asks"), list)):
        return True
    return False


def _extract_sides(msg: Any) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    def coerce(side):
        out = []
        for it in side or []:
            if isinstance(it, (list, tuple)) and len(it) >= 2:
                try:
                    out.append((float(it[0]), float(it[1])))
                except Exception:
                    pass
            elif isinstance(it, dict):
                p = it.get("price") or it.get("p") or it.get(0)
                q = it.get("size")  or it.get("q") or it.get(1)
                try:
                    if p is not None and q is not None:
                        out.append((float(p), float(q)))
                except Exception:
                    pass
        return out

    if not isinstance(msg, dict):
        return [], []
    ob = msg.get("order_book")
    if isinstance(ob, dict):
        return coerce(ob.get("bids")), coerce(ob.get("asks"))
    data = msg.get("data") or msg.get("payload")
    if isinstance(data, dict):
        ob = data.get("order_book")
        if isinstance(ob, dict):
            return coerce(ob.get("bids")), coerce(ob.get("asks"))
        if isinstance(data.get("bids"), list) or isinstance(data.get("asks"), list):
            return coerce(data.get("bids")), coerce(data.get("asks"))
    return coerce(msg.get("bids")), coerce(msg.get("asks"))


def _extract_market_candidates(msg: Any) -> List[Tuple[str, int]]:
    out: List[Tuple[str, int]] = []

    def push(sym: Any, idx: Any):
        try:
            if sym is None or idx is None:
                return
            out.append((str(sym).upper(), int(idx)))
        except Exception:
            pass

    if not isinstance(msg, dict):
        return out

    payload = msg.get("data", msg.get("payload", msg.get("markets", msg)))
    if isinstance(payload, list):
        for it in payload:
            if isinstance(it, dict):
                sym = it.get("symbol") or it.get("name") or it.get("ticker") or it.get("market_name")
                idx = it.get("market_index") or it.get("marketIndex") or it.get("market_id") or it.get("id")
                push(sym, idx)
    elif isinstance(payload, dict):
        sym = payload.get("symbol") or payload.get("name") or payload.get("ticker") or payload.get("market_name")
        idx = payload.get("market_index") or payload.get("marketIndex") or payload.get("market_id") or payload.get("id")
        push(sym, idx)

    push(msg.get("symbol"), msg.get("market_index") or msg.get("marketIndex") or msg.get("market_id") or msg.get("id"))
    return out


def _extract_offset(msg: Any) -> Optional[int]:
    if not isinstance(msg, dict):
        return None
    try:
        if "offset" in msg and isinstance(msg["offset"], (int, float, str)):
            return int(float(msg["offset"]))
        ob = msg.get("order_book")
        if isinstance(ob, dict) and "offset" in ob:
            return int(float(ob["offset"]))
    except Exception:
        pass
    return None


def _extract_nonce_begin_nonce(msg: Any) -> Tuple[Optional[int], Optional[int]]:
    if not isinstance(msg, dict):
        return None, None
    ob = msg.get("order_book")
    if isinstance(ob, dict):
        n = ob.get("nonce")
        bn = ob.get("begin_nonce")
        with contextlib.suppress(Exception):
            n = int(n) if n is not None else None
        with contextlib.suppress(Exception):
            bn = int(bn) if bn is not None else None
        return n, bn
    return None, None


def _apply_delta_to_maps(bid_map: dict[float, float], ask_map: dict[float, float],
                         bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]):
    for p, q in bids or []:
        if q <= 0:
            bid_map.pop(p, None)
        else:
            bid_map[p] = q
    for p, q in asks or []:
        if q <= 0:
            ask_map.pop(p, None)
        else:
            ask_map[p] = q


class LighterFeed:
    def __init__(self, symbol: str, quote: str, levels: int = 10, market_index: Optional[int] = None):
        self.symbol = (symbol or "").upper().strip()
        self.quote  = (quote  or "").upper().strip()
        self.levels = int(levels)

        if market_index is not None:
            self.market_index = int(market_index)
        elif ENV_MARKET_INDEX not in (None, ""):
            self.market_index = int(ENV_MARKET_INDEX)
        else:
            self.market_index = None

        self.book = OrderBook()

        # ✅ BLOQUE 2: top-of-book directo
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None

        # ✅ opcional: no construir libro (menos CPU/allocs)
        self.top_only = os.getenv("LIGHTER_TOP_ONLY", "1") == "1"

        self._stop = asyncio.Event()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None

        self._reconnect_sleep = RECONNECT_SLEEP_SECS

        self._bid_map: dict[float, float] = {}
        self._ask_map: dict[float, float] = {}
        self._last_offset: int = -1

        self._last_nonce: Optional[int] = None

    async def connect(self):
        while not self._stop.is_set():
            try:
                if self.market_index is None:
                    await self._discover_market_index()
                    if self.market_index is None:
                        raise RuntimeError("No se pudo descubrir MARKET_INDEX. Configura LIGHTER_MARKET_INDEX en .env.")
                await self._run_orderbook_stream()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"LighterFeed: error '{e!r}'. Reintentando en {self._reconnect_sleep:.1f}s...")
                await asyncio.sleep(self._reconnect_sleep)

    def stop(self):
        self._stop.set()

    async def _discover_market_index(self, timeout: float = 8.0):
        wanted = {s for s in (self.symbol, f"{self.symbol}-{self.quote}", SYMBOL_HINT or "") if s}
        logger.info(f"LighterFeed: descubriendo MARKET_INDEX. Candidatos: {sorted(wanted)}")
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=2,
                max_size=2**22,
            ) as ws:
                self._ws = ws
                await ws.send(json.dumps({"type": "subscribe", "channel": "market_stats/all"}))
                end_at = asyncio.get_event_loop().time() + timeout

                while asyncio.get_event_loop().time() < end_at:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                    except asyncio.TimeoutError:
                        continue

                    msg = _safe_json(raw)
                    for sym, idx in _extract_market_candidates(msg):
                        if sym in wanted:
                            self.market_index = int(idx)
                            logger.info(f"LighterFeed: MARKET_INDEX={self.market_index} para '{sym}'")
                            return
        except Exception as e:
            logger.warning(f"LighterFeed: fallo descubrimiento: {e!r}")
        finally:
            self._ws = None

        if self.market_index is None:
            logger.error("LighterFeed: no se pudo obtener MARKET_INDEX. Setea LIGHTER_MARKET_INDEX en .env.")

    async def _run_orderbook_stream(self):
        assert self.market_index is not None, "market_index requerido"
        channel = f"order_book/{self.market_index}"
        sub = {"type": "subscribe", "channel": channel}

        logger.info(f"LighterFeed: suscribiendo canal {channel} en {WS_URL}")

        async with websockets.connect(
            WS_URL,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=2,
            max_size=2**22,
        ) as ws:
            self._ws = ws

            self._bid_map.clear()
            self._ask_map.clear()
            self._last_offset = -1
            self._last_nonce = None

            # ✅ reset best para coherencia
            self.best_bid = None
            self.best_ask = None

            await ws.send(json.dumps(sub))

            last_sub_ts = time.time()
            last_book_update_ts = time.time()

            try:
                while not self._stop.is_set():
                    now = time.time()

                    if now - last_book_update_ts > INACT_SECS:
                        raise RuntimeError("LighterFeed: watchdog (sin updates de order_book)")

                    if now - last_sub_ts > RESUB_SECS:
                        with contextlib.suppress(Exception):
                            await ws.send(json.dumps(sub))
                        last_sub_ts = now

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                    except asyncio.TimeoutError:
                        continue

                    msg = _safe_json(raw)
                    if not _looks_like_orderbook_update(msg, channel):
                        continue

                    off = _extract_offset(msg)
                    if off is not None and off <= self._last_offset:
                        continue

                    nonce, begin_nonce = _extract_nonce_begin_nonce(msg)
                    if nonce is not None:
                        if self._last_nonce is not None and begin_nonce is not None:
                            if begin_nonce > (self._last_nonce + 1):
                                raise RuntimeError(f"LighterFeed: nonce gap (begin_nonce={begin_nonce} last_nonce={self._last_nonce})")
                        self._last_nonce = nonce

                    bids, asks = _extract_sides(msg)
                    if bids or asks:
                        _apply_delta_to_maps(self._bid_map, self._ask_map, bids, asks)

                        # ✅ BLOQUE 2: mantener best_bid/best_ask sin ordenar maps cada vez
                        touched_best_bid = False
                        for p, q in bids:
                            if self.best_bid is not None and p == self.best_bid and q <= 0:
                                touched_best_bid = True
                            if q > 0:
                                if self.best_bid is None or p > self.best_bid:
                                    self.best_bid = p
                        if touched_best_bid:
                            self.best_bid = max(self._bid_map.keys(), default=None)

                        touched_best_ask = False
                        for p, q in asks:
                            if self.best_ask is not None and p == self.best_ask and q <= 0:
                                touched_best_ask = True
                            if q > 0:
                                if self.best_ask is None or p < self.best_ask:
                                    self.best_ask = p
                        if touched_best_ask:
                            self.best_ask = min(self._ask_map.keys(), default=None)

                        # opcional: mantener book solo si top_only=0
                        if not self.top_only:
                            best_bids = sorted(self._bid_map.items(), key=lambda x: x[0], reverse=True)[: self.levels]
                            best_asks = sorted(self._ask_map.items(), key=lambda x: x[0])[: self.levels]
                            self.book.bids = [(float(p), float(q)) for p, q in best_bids]
                            self.book.asks = [(float(p), float(q)) for p, q in best_asks]

                    if off is not None:
                        self._last_offset = off

                    last_book_update_ts = now

            except (ConnectionClosedOK, ConnectionClosedError):
                raise
            finally:
                self._ws = None