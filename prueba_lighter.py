from dotenv import load_dotenv
import os
import traceback

# 1. Cargar entorno igual que el bot
load_dotenv(override=True)
print(f"🔎 ENV LEÍDO: SYMBOL={os.getenv('SYMBOL')} | INDEX={os.getenv('LIGHTER_MARKET_INDEX')}")

try:
    from exchanges.lighter_api import LighterClient
    print("✅ Librería importada correctamente.")
    
    print("⏳ Inicializando Cliente...")
    client = LighterClient()
    
    print(f"   -> OrderAPI cargada? {'SÍ' if client._OrderApi else 'NO (Causa del error)'}")
    
    print("🚀 Probando conexión real (get_active_orders)...")
    orders = client.get_active_orders()
    
    print(f"📊 RESULTADO RAW: {orders!r}")
    
    if orders is None:
        print("❌ FALLO: Devolvió None. El SDK no se cargó o faltan credenciales.")
    else:
        print("✅ ÉXITO: Lighter responde correctamente.")

except Exception:
    print("💥 EXCEPCIÓN DETECTADA:")
    traceback.print_exc()
