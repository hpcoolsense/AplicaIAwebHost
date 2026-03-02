# tools/ws_sniff.py
import asyncio, os, json, sys, time
import websockets

WS_URL = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")

async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    print(f"[i] Conectando a {WS_URL} y escuchando {n} mensajes...")
    async with websockets.connect(
        WS_URL,
        ping_interval=15, ping_timeout=10, close_timeout=5, max_size=2**22
    ) as ws:
        # Si el server necesita una suscripción “global” para empezar a emitir algo, ponla aquí.
        # Ej: await ws.send(json.dumps({"type":"subscribe","channel":"market_stats/all"}))

        for i in range(n):
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            print(f"\n[{i+1}] {raw[:1200]}")  # recorta por si es largo
    print("\n[i] Fin del sniff.")

if __name__ == "__main__":
    asyncio.run(main())
