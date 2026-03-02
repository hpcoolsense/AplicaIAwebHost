from dataclasses import dataclass, field
from typing import List, Tuple

PriceSize = Tuple[float, float]  # (price, size)

@dataclass
class OrderBook:
    bids: List[PriceSize] = field(default_factory=list)  # sorted desc by price
    asks: List[PriceSize] = field(default_factory=list)  # sorted asc by price

    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    def snapshot_top(self, n: int) -> tuple[list[PriceSize], list[PriceSize]]:
        return self.bids[:n], self.asks[:n]
