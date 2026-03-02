# tools/selftest.py
import os
import sys
import gc
import asyncio
import traceback
import pathlib
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Añadir raíz del proyecto al sys.path
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exchanges.pacifica_api import PacificaClient
from exchanges.lighter_api import LighterClient

def ok(m): print(f"[OK] {m}")
def bad(m): print(f"[X]  {m}")

def _drain_asyncio_tasks():
    """Best-effort para drenar tareas pendientes y cerrar sesiones aiohttp."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop and not loop.is_closed():
            loop.run_until_complete(asyncio.sleep(0))
    except Exception:
        pass
    # Forzar finalizadores que puedan cerrar sesiones pendientes
    try:
        gc.collect()
    except Exception:
        pass

def main():
    symbol = f"{os.getenv('SYMBOL','ETH').upper()}"

    # --- Pacifica (ED25519 Agent) ---
    try:
        pc = PacificaClient()  # sin argumentos; toma del .env
        r = pc.get_position(symbol)
        ok(f"Pacifica auth OK. Posición: {r}")
    except Exception as e:
        bad(f"Pacifica auth/GET falló: {type(e).__name__}: {e}")
        traceback.print_exc()

    # --- Lighter (SDK + token + REST) ---
    try:
        lc = LighterClient()  # sin argumentos; toma todo de .env
        try:
            r = lc.get_position(symbol)  # p.ej. "ETH"
            ok(f"Lighter auth OK. Posición: {r}")
        finally:
            # Cierra la sesión requests y pide cerrar aiohttp internas
            lc.close()
            _drain_asyncio_tasks()
    except Exception as e:
        bad(f"Lighter auth/GET falló: {type(e).__name__}: {e}")
        traceback.print_exc()
        _drain_asyncio_tasks()

if __name__ == "__main__":
    main()
