# exchanges/base.py
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Literal, Optional, Dict, Any, Tuple

Side = Literal["buy", "sell"]  # buy=long, sell=short en perps a mercado
OrderType = Literal["market"]

@dataclass
class OrderRequest:
    symbol: str          # ej. "ETH"
    quote: str           # ej. "USDT"
    side: Side           # "buy" o "sell"
    size_usd: float      # monto en USD nocional
    leverage: float = 1.0
    type: OrderType = "market"

@dataclass
class OrderAck:
    ok: bool
    id: Optional[str]
    exchange: str
    side: Side
    price: Optional[float]
    filled_usd: float
    ts_ms: int
    raw: Dict[str, Any]

class ExchangeClient:
    """Interfaz mínima para DEX perps."""
    name: str

    def __init__(self, api_base: str, api_key: str | None, api_secret: str | None, paper: bool = True):
        self.api_base = api_base.rstrip("/") if api_base else ""
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.paper = paper

    async def create_order(self, req: OrderRequest) -> OrderAck:
        """Implementar en subclase. En paper, devolver simulado."""
        raise NotImplementedError

    # util:
    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)
