# tools/ws_quick_test.py
import asyncio, os, json, sys, time
import websockets

# Puedes sobreescribir con LIGHTER_WS_URL en tu .env si quieres
WS_URL = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")

async def main(channel="order_book/0", seconds=20):
    """
    Test mínimo para verificar que el WS de Lighter responde.
    - channel: por defecto 'order_book/0' (ETH = 0 según tu hallazgo)
    - seconds: tiempo de escucha
    Imprime en consola los mensajes crudos (recortados).
    """
    print(f"[i] Conectando {WS_URL}, suscribiendo '{channel}', escuchando {seconds}s")
    try:
        async with websockets.connect(
            WS_URL,
            ping_interval=15,  # ping cada 15s
            ping_timeout=10,   # tiempo máximo esperando pong
            close_timeout=5,
            max_size=2**22
        ) as ws:
            # Suscripción al canal indicado
            sub = {"type": "subscribe", "channel": channel}
            await ws.send(json.dumps(sub))

            t0 = time.perf_counter()
            got_any = False
            while time.perf_counter() - t0 < seconds:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=6)
                    got_any = True
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode("utf-8", "ignore")
                    print(raw[:1200])  # mostramos hasta 1200 chars
                except asyncio.TimeoutError:
                    # Mantener viva la conexión
                    try:
                        await ws.ping()
                    except Exception:
                        pass
            print("\n[i] Fin. ¿Recibimos algo? ->", got_any)
    except Exception as e:
        print("[!] Excepción:", repr(e))

if __name__ == "__main__":
    # Puedes pasar canal y segundos por línea de comandos:
    #   python tools\ws_quick_test.py order_book/0 20
    ch = sys.argv[1] if len(sys.argv) > 1 else "order_book/0"
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    asyncio.run(main(ch, secs))
