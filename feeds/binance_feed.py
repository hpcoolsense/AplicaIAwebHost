import ccxt.pro as ccxtpro
import asyncio
from dataclasses import dataclass

@dataclass
class Book:
    bids: list
    asks: list

class BinanceFeed:
    def __init__(self, symbol, quote, levels=1):
        self.base = symbol.upper()
        self.quote = quote.upper()
        self.levels = levels
        self.book = Book([], [])
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.exchange = ccxtpro.binanceusdm()
        # El símbolo se resolverá en connect()
        self.symbol = f"{self.base}/{self.quote}" 

    async def connect(self):
        try:
            print(f"[BINANCE] Buscando mercado para {self.base}/{self.quote}...")
            await self.exchange.load_markets()
            
            # 1. Intentar formato estándar
            target = f"{self.base}/{self.quote}"
            
            # 2. Si es USDC, intentar formato específico de CCXT para perps USDC
            if target not in self.exchange.markets and self.quote == 'USDC':
                target = f"{self.base}/{self.quote}:USDC"
            
            # 3. Validación final
            if target in self.exchange.markets:
                self.symbol = target
                print(f"[BINANCE] Mercado encontrado: {self.symbol} (ID: {self.exchange.markets[self.symbol]['id']})")
            else:
                print(f"[BINANCE ERROR] No se encontró el par {self.base}/{self.quote}. Mercados disponibles similares: {[m for m in self.exchange.markets if self.base in m and self.quote in m]}")
                return

        except Exception as e:
            print(f"[BINANCE INIT ERROR] {e}")

        while True:
            try:
                orderbook = await self.exchange.watch_order_book(self.symbol, limit=5)
                self.book.bids = orderbook['bids'][:self.levels]
                self.book.asks = orderbook['asks'][:self.levels]
                self.best_bid = self.book.bids[0][0] if self.book.bids else 0.0
                self.best_ask = self.book.asks[0][0] if self.book.asks else 0.0
            except Exception as e:
                print(f"[BINANCE FEED ERROR] {e}")
                await asyncio.sleep(2)
