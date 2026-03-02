# feeds/pacifica.py
import os
import asyncio
import json
import logging
from typing import Any, Iterable, List, Tuple, Optional
import contextlib
import websockets

from core.book import OrderBook

PACIFICA_WS_URL = os.getenv("PACIFICA_WS_URL", "wss://ws.pacifica.fi/ws")
DEFAULT_AGG_LEVEL = int(os.getenv("PACIFICA_AGG_LEVEL", "1"))

logger = logging.getLogger("pacifica")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def _ensure_sorted_top(entries: Iterable[Tuple[float, float]], reverse: bool, top_n: int) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for p, q in entries or []:
        try:
            fp = float(p); fq = float(q)
            if fq > 0:
                out.append((fp, fq))
        except Exception:
            pass
    out.sort(key=lambda x: x[0], reverse=reverse)
    return out[:top_n]


def _looks_like_orderbook_update(msg: Any) -> bool:
    if not isinstance(msg, dict):
        return False
    if str(msg.get("channel", "")).lower() != "book":
        return False
    data = msg.get("data")
    if not isinstance(data, dict):
        return False
    l = data.get("l")
    return isinstance(l, list) and len(l) == 2


def _extract_levels_from_msg(msg: dict) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    data = msg.get("data", {})
    l = data.get("l", [[], []])

    def coerce(side_levels: Iterable[Any]) -> List[Tuple[float, float]]:
        out: List[Tuple[float, float]] = []
        for it in side_levels:
            if isinstance(it, dict):
                p = it.get("p"); a = it.get("a")
                try:
                    if p is not None and a is not None:
                        out.append((float(p), float(a)))
                except Exception:
                    pass
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                try:
                    out.append((float(it[0]), float(it[1])))
                except Exception:
                    pass
        return out

    bids = coerce(l[0] if len(l) > 0 else [])
    asks = coerce(l[1] if len(l) > 1 else [])
    return bids, asks


class PacificaFeed:
    def __init__(self, symbol: str, quote: str, levels: int = 10, agg_level: Optional[int] = None):
        self.symbol = (symbol or "").upper().strip()
        self.quote = (quote or "").upper().strip()
        self.levels = int(levels)
        self.agg_level = int(agg_level if agg_level is not None else DEFAULT_AGG_LEVEL)

        self.book = OrderBook()

        # ✅ BLOQUE 2: top-of-book directo
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None

        # ✅ opcional: no construir libro (menos CPU/allocs)
        self.top_only = os.getenv("PACIFICA_TOP_ONLY", "1") == "1"

        self._stop = asyncio.Event()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._heartbeat_interval = 15
        self._reconnect_backoff = (2, 30)

        # ✅ para detectar congelamientos
        self.last_update_ms: int = 0

    async def connect(self):
        backoff = self._reconnect_backoff[0]
        while not self._stop.is_set():
            try:
                await self._run_orderbook_stream()
                backoff = self._reconnect_backoff[0]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"PacificaFeed: desconectado por error: {e!r}. Reintentando en {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_backoff[1])

    def stop(self):
        self._stop.set()

    async def _run_orderbook_stream(self):
        sub_msg = {
            "method": "subscribe",
            "params": {
                "source": "book",
                "symbol": self.symbol,
                "agg_level": self.agg_level,
            },
        }

        logger.info(f"PacificaFeed: conectando y suscribiendo libro de {self.symbol} (agg_level={self.agg_level})")
        async with websockets.connect(
            PACIFICA_WS_URL,
            ping_interval=15,
            ping_timeout=10,
            close_timeout=5
        ) as ws:
            self._ws = ws
            await ws.send(json.dumps(sub_msg))
            hb = asyncio.create_task(self._heartbeat(ws))

            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    msg = _safe_json(raw)
                    if not _looks_like_orderbook_update(msg):
                        continue

                    bids, asks = _extract_levels_from_msg(msg)

                    # ✅ BLOQUE 2: calcular BEST sin ordenar
                    bb = None
                    for p, q in bids:
                        if q > 0 and (bb is None or p > bb):
                            bb = p

                    ba = None
                    for p, q in asks:
                        if q > 0 and (ba is None or p < ba):
                            ba = p

                    self.best_bid = bb
                    self.best_ask = ba

                    # opcional: mantener libro solo si top_only=0
                    if not self.top_only:
                        self.book.bids = _ensure_sorted_top(bids, reverse=True, top_n=self.levels)
                        self.book.asks = _ensure_sorted_top(asks, reverse=False, top_n=self.levels)

                    self.last_update_ms = int(asyncio.get_running_loop().time() * 1000)

            finally:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await hb
                self._ws = None

    async def _heartbeat(self, ws: websockets.WebSocketClientProtocol):
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._heartbeat_interval)
                pong = await ws.ping()
                await asyncio.wait_for(pong, timeout=10)
            except Exception:
                break