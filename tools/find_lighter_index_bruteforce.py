# tools/find_lighter_index_bruteforce.py
import asyncio, json, os, sys, time, statistics
import websockets

WS_URL = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")

async def try_index(idx: int, timeout: float = 3.0):
    channel = f"order_book/{idx}"
    sub = {"type":"subscribe","channel":channel}
    try:
        async with websockets.connect(
            WS_URL,
            ping_interval=15, ping_timeout=10, close_timeout=5, max_size=2**22
        ) as ws:
            await ws.send(json.dumps(sub))
            t0 = time.perf_counter()
            best_bid = None
            best_ask = None
            while time.perf_counter() - t0 < timeout:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8","ignore")
                try:
                    msg = json.loads(raw)
                except:
                    continue
                ch = (msg.get("channel") or msg.get("topic") or "").lower()
                if ch != channel.lower():
                    continue
                data = msg.get("data") or {}
                bids = data.get("bids") or msg.get("bids") or []
                asks = data.get("asks") or msg.get("asks") or []
                # bids/asks pueden venir como [[p,q], ...] o [{price, size},...]
                def coerce(side):
                    out=[]
                    for it in side:
                        if isinstance(it,(list,tuple)) and len(it)>=2:
                            try:
                                out.append((float(it[0]), float(it[1])))
                            except: pass
                        elif isinstance(it,dict):
                            p = it.get("price") or it.get("p") or it.get(0)
                            q = it.get("size") or it.get("q") or it.get(1)
                            try:
                                if p is not None and q is not None:
                                    out.append((float(p), float(q)))
                            except: pass
                    return out
                bids = coerce(bids)
                asks = coerce(asks)
                if bids:
                    best_bid = max(p for p,_ in bids)
                if asks:
                    best_ask = min(p for p,_ in asks)
                if best_bid is not None or best_ask is not None:
                    return (True, best_bid, best_ask)
    except Exception:
        return (False, None, None)
    return (False, None, None)

async def main():
    start = int(sys.argv[1]) if len(sys.argv)>1 else 0
    end   = int(sys.argv[2]) if len(sys.argv)>2 else 200
    print(f"[i] Brute-force order_book/{{index}} en rango [{start}, {end})")
    found = []
    for idx in range(start, end):
        ok, bb, ba = await try_index(idx)
        if ok:
            print(f"  - index {idx:<4} -> best_bid={bb}, best_ask={ba}")
            found.append((idx, bb, ba))
    if not found:
        print("[!] No se encontró ningún libro en ese rango. Amplía el rango.")
        return 1

    # Heurística: si buscabas ETH-USD, su precio rondará miles; eliges el índice cuyo mid-price ~ precio ETH
    mids = []
    for idx, bb, ba in found:
        if bb is not None and ba is not None:
            mids.append((idx, (bb+ba)/2))
    if mids:
        # muestra los candidatos ordenados por mid-price (ayuda a identificar ETH vs otros)
        mids.sort(key=lambda x: x[1])
        print("\n[i] Candidatos (ordenados por mid):")
        for idx, mid in mids[:10]:
            print(f"    index {idx:<4} mid≈{mid}")
    print("\n[i] Elige el index que corresponda al nivel de precio de ETH y agrégalo a tu .env como LIGHTER_MARKET_INDEX=<index>")
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
