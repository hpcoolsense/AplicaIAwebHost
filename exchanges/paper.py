# exchanges/paper.py
from __future__ import annotations
from typing import Optional, Dict, Any
from .base import ExchangeClient, OrderRequest, OrderAck

class PaperClient(ExchangeClient):
    """Simula ejecución inmediata al precio “visible” (mejor bid/ask) que le pases externamente."""
    def __init__(self, name: str):
        super().__init__(api_base="", api_key="", api_secret="", paper=True)
        self.name = name
        # Los precios los setea el motor desde los feeds:
        self.best_bid: Optional[float] = None  # precio al que vendería (short) inmediatamente
        self.best_ask: Optional[float] = None  # precio al que compraría (long) inmediatamente

    async def create_order(self, req: OrderRequest) -> OrderAck:
        # price de ejecución simulada
        if req.side == "buy":
            px = self.best_ask
        else:
            px = self.best_bid

        ok = px is not None and req.size_usd > 0
        return OrderAck(
            ok=ok,
            id=f"paper-{self.name}-{self._now_ms()}",
            exchange=self.name,
            side=req.side,
            price=px if ok else None,
            filled_usd=req.size_usd if ok else 0.0,
            ts_ms=self._now_ms(),
            raw={"paper": True, "note": "simulated instant fill"}
        )
