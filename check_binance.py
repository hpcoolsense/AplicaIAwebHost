import os
import json
from dotenv import load_dotenv

load_dotenv("/home/tomas/arb-panel-5tp5sl/.env")

from exchanges.binance_adapter import BinanceAdapter

try:
    b = BinanceAdapter()
    symbol = os.getenv("SYMBOL", "ETH-USDT")  # Read from env if present
    print(f"--- DIAGNOSTICS FOR SYMBOL: {symbol} ---")
    msym = b._get_market_symbol(symbol)
    
    print("--- POSITION MODE ---")
    try:
        dual = b.client.fapiPrivateGetPositionSideDual()
        print(dual)
    except Exception as e:
        print("Error checking dual side:", e)

    print("--- RECENT ORDERS ---")
    orders = b.client.fetch_orders(msym, limit=10)
    for o in orders:
        reduce_flag = o.get('reduceOnly', o.get('info', {}).get('reduceOnly'))
        print(f"ID: {o['id']} | SIDE: {o['side']} | TYPE: {o['type']} | STATUS: {o['status']} | AMT: {o['amount']} | REDUCE: {reduce_flag}")
        if str(o['status']).lower() in ('rejected', 'failed', 'canceled'):
            print("REJECT/CANCEL REASON:", o.get('info'))
            
    print("--- TARGET POSITION ---")
    pos = b.get_open_position(symbol)
    print(pos)

    print("--- RAW OPEN ORDERS ---")
    open_orders = b.get_open_orders(symbol)
    print([{"id": o.get('id'), "side": o.get('side'), "type": o.get('type')} for o in open_orders])

except Exception as e:
    import traceback
    traceback.print_exc()
