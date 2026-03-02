# feeds/binance_futures.py
import os
import asyncio
import json
import logging
import contextlib
from typing import Optional
import websockets

from core.book import OrderBook

BINANCE_FUT_WS = os.getenv("BINANCE_FUT_WS_URL", "wss://fstream.binance.com/ws")
logger = logging.getLogger("binance_futures")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


def _safe_json(raw):
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        with contextlib.suppress(Exception):
            return json.loads(raw)
    return raw


def _to_binance_symbol(base: str, quote: str) -> str:
    # ETH + USDT -> ETHUSDT
    return f"{(base or '').upper()}{(quote or '').upper()}"


class BinanceFuturesFeed:
    """
    Top-of-book (best_bid/best_ask) vía WS bookTicker.
    Mantiene interfaz compatible: best_bid, best_ask, book (opcional).
    """
    def __init__(self, symbol: str, quote: str, levels: int = 10):
        self.base = (symbol or "").upper().strip()
        self.quote = (quote or "").upper().strip()
        self.levels = int(levels)

        self.symbol = _to_binance_symbol(self.base, self.quote)

        self.book = OrderBook()
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None

        self.top_only = os.getenv("BINANCE_TOP_ONLY", "1") == "1"

        self._stop = asyncio.Event()
        self._ws = None

        self.last_update_ms: int = 0

    async def connect(self):
        stream = f"{self.symbol.lower()}@bookTicker"
        url = f"{BINANCE_FUT_WS}/{stream}"
        logger.info(f"BinanceFuturesFeed: conectando {url}")

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=15,
                    ping_timeout=10,
                    close_timeout=3,
                    max_size=2**20
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    while not self._stop.is_set():
                        raw = await ws.recv()
                        msg = _safe_json(raw)
                        if not isinstance(msg, dict):
                            continue
                        # bookTicker fields: b (best bid), a (best ask)
                        b = msg.get("b"); a = msg.get("a")
                        if b is None or a is None:
                            continue
                        with contextlib.suppress(Exception):
                            self.best_bid = float(b)
                            self.best_ask = float(a)
                            self.last_update_ms = int(asyncio.get_running_loop().time() * 1000)

                        # opcional: book mínimo (1 nivel) si algún código lo usa
                        if not self.top_only and self.best_bid and self.best_ask:
                            self.book.bids = [(self.best_bid, 1.0)]
                            self.book.asks = [(self.best_ask, 1.0)]

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"BinanceFuturesFeed: error {e!r}. Reintentando en {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 15.0)

    def stop(self):
        self._stop.set()
