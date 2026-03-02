import os
from pprint import pprint
from dotenv import load_dotenv

load_dotenv()

from exchanges.pacifica_api import PacificaAPI

def main():
    client = PacificaAPI()
    orders = client.get_open_orders()
    print("OPEN ORDERS:")
    pprint(orders)
    
    if orders:
        oid = orders[0]['order_id']
        sym = orders[0]['symbol']
        print(f"Borrando {oid} ...")
        
        # Test 1: POST /orders/cancel
        try:
            op_data = {"symbol": sym, "order_id": oid}
            body = client._build_signature("cancel_order", op_data)
            import json
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            
            headers = {"Content-Type": "application/json", "type": "cancel_order"}
            r = client._session.post(client.base + "/orders/cancel", data=payload, headers=headers)
            print("Response /orders/cancel:", r.status_code, r.text)
        except Exception as e:
            print("Error 1:", e)

if __name__ == "__main__":
    main()
