import os
import sys
import asyncio
import inspect
import time
from dotenv import load_dotenv

# Cargar entorno
load_dotenv(override=True)

print("========================================")
print("🩺 DIAGNÓSTICO DE APIS (AUTOPSIA)")
print("========================================")

# 1. DIAGNÓSTICO LIGHTER
print("\n[1/3] INSPECCIONANDO LIGHTER SDK...")
try:
    from exchanges.lighter_api import LighterClient
    client = LighterClient()
    
    # Importar internamente lo que usa la API
    mod, SignerClient = client._mod, client._SignerClient
    from lighter.signer_client import CreateOrderTxReq
    
    print("✅ SDK Cargado.")
    print(f"👉 Nombre del módulo: {mod.__name__}")
    
    # INSPECCIÓN DE ARGUMENTOS
    sig = inspect.signature(CreateOrderTxReq)
    print(f"🧐 CreateOrderTxReq espera estos argumentos exactos:\n{sig}")
    
    # Ver si hay diferencias entre mayúsculas y minúsculas
    params = list(sig.parameters.keys())
    print(f"🔑 Lista de campos: {params}")

    if 'ClientOrderIndex' in params:
        print("💡 CONCLUSIÓN: El SDK usa PascalCase (Mayúsculas).")
    elif 'client_order_index' in params:
        print("💡 CONCLUSIÓN: El SDK usa snake_case (Minúsculas).")
    else:
        print("⚠️ CONCLUSIÓN: Formato desconocido.")

except Exception as e:
    print(f"❌ Error cargando Lighter: {e}")


# 2. PRUEBA PACIFICA
print("\n[2/3] PRUEBA DE FIRMA PACIFICA...")
try:
    from exchanges.pacifica_api import PacificaClient
    pac = PacificaClient()
    
    symbol = os.getenv("SYMBOL", "BNB-USDT").split("-")[0]
    print(f"📡 Enviando orden de prueba (LIMIT LEJOS DEL PRECIO) a Pacifica en {symbol}...")
    
    # Precio absurdo para que no se ejecute
    res = pac.place_limit(symbol=symbol, side="SELL", base_qty=0.001, price=99999.0)
    print(f"📩 Respuesta: {res}")
    
    if res.get("success"):
        print("✅ Pacifica LIMIT funciona.")
    else:
        print(f"❌ Pacifica falló: {res}")

except Exception as e:
    print(f"❌ Error Pacifica: {e}")

print("\n========================================")
print("FIN DEL DIAGNÓSTICO")
