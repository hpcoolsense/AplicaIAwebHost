import asyncio
import threading
import time
import os
import sys
import platform

# Módulos existentes
from feeds.pacifica import PacificaFeed
from feeds.lighter import LighterFeed
from trader.engine import ArbEngine

# NUEVO: Importamos el feed de Binance (asegúrate de haber creado feeds/binance_feed.py)
try:
    from feeds.binance_feed import BinanceFeed
except ImportError:
    pass # Ignoramos si no existe aún para no romper el IDE, pero fallará en runtime si usas BINANCE

# ========= CONFIG =========
SYMBOL = os.getenv("SYMBOL", "HYPE")
QUOTE = os.getenv("QUOTE", "USDT")
LEVELS = int(os.getenv("LEVELS", "1"))
LIGHTER_MARKET_INDEX = int(os.getenv("LIGHTER_MARKET_INDEX", "0"))
HEADLESS_EDGE_NO_SLEEP = os.getenv("HEADLESS_EDGE_NO_SLEEP", "true").lower() == "true"

# NUEVO: Selector de modo (PACIFICA o BINANCE)
TARGET_MODE = os.getenv("TARGET_MODE", "PACIFICA").upper()
OPEN_EDGE_TARGET = float(os.getenv("EDGE_THRESHOLD", "0.0005")) * 100
CLOSE_EDGE_TARGET = float(os.getenv("CLOSE_EDGE_TARGET", "0.0003")) * 100

# ========= ASYNC RUNNER =========
def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(coro)

def main():
    # CPU affinity: fija el proceso a un core para reducir jitter
    core_env = os.getenv("HEADLESS_CPU_CORE", "").strip()
    if core_env:
        try:
            core_id = int(core_env)
            if hasattr(os, "sched_setaffinity"):
                os.sched_setaffinity(0, {core_id})
                print(f"[HEADLESS] CPU affinity -> core {core_id}")
            elif platform.system().lower() == "windows":
                # En Windows no aplicamos afinidad desde aquí
                print("[HEADLESS] CPU affinity no soportado en Windows desde este script")
        except Exception as e:
            print(f"[HEADLESS] CPU affinity fallo: {e}")

    print(f"[HEADLESS] Iniciando vigilancia {TARGET_MODE} -> Lighter para {SYMBOL}...")

    # SELECCIÓN DE FEED (Polimorfismo: BinanceFeed se disfraza de PacificaFeed)
    if TARGET_MODE == "BINANCE":
        # Usamos Binance como fuente 'pac' (target principal)
        pac = BinanceFeed(SYMBOL, QUOTE, LEVELS)
    else:
        pac = PacificaFeed(SYMBOL, QUOTE, LEVELS)

    lig = LighterFeed(SYMBOL, QUOTE, LEVELS, market_index=LIGHTER_MARKET_INDEX)

    threading.Thread(target=run_async, args=(pac.connect(),), daemon=True).start()
    threading.Thread(target=run_async, args=(lig.connect(),), daemon=True).start()

    # Pasamos el modo al engine
    engine = ArbEngine(pac=pac, lig=lig, symbol=SYMBOL, quote=QUOTE, mode=TARGET_MODE)
    engine.start()

    print(f"[HEADLESS] Engine corriendo en modo {TARGET_MODE}. Esperando entrada...")

    # --- VARIABLES DE ESTADO ---
    in_position = False
    pac_done = False
    lig_done = False
    
    # Precios MAESTROS
    master_tp = 0.0
    master_sl = 0.0
    side_pac = "" 
    resynced = False

    while True:
        try:
            # El Engine es ahora 100% responsable de la telemetría y el tracking multi-órdenes.
            # Este hilo solo mantiene vivo el daemon.
            time.sleep(1)

        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
