from core.book import OrderBook

def compute_edges(book_a: OrderBook, book_b: OrderBook):
    bid_a = book_a.best_bid()
    ask_a = book_a.best_ask()
    bid_b = book_b.best_bid()
    ask_b = book_b.best_ask()

    signals = []
    if None in (bid_a, ask_a, bid_b, ask_b):
        return signals

    signals.append(("A_LONG_B_SHORT", ask_a, bid_b, (bid_b - ask_a)/ask_a if ask_a else 0.0))
    signals.append(("B_LONG_A_SHORT", ask_b, bid_a, (bid_a - ask_b)/ask_b if ask_b else 0.0))
    return signals

def decide(signals, threshold_frac: float):
    for tag, cheap_ask, rich_bid, edge in signals:
        if edge >= threshold_frac:
            return tag, edge
    return None, 0.0
