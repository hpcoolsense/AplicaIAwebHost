from dotenv import load_dotenv
import traceback
import sys

# Cargar entorno
load_dotenv(override=True)

print("--- INICIANDO AUTOPSIA DE LIGHTER ACCOUNT ---")

try:
    from exchanges.lighter_api import LighterClient
    client = LighterClient()
    
    print("1. Cliente iniciado. Llamando a get_account_info()...")
    
    # Llamada directa sin try/except para que explote si tiene que explotar
    resultado = client.get_account_info()
    
    print(f"2. LA LLAMADA TERMINÓ.")
    print(f"   -> Tipo de dato recibido: {type(resultado)}")
    print(f"   -> Contenido RAW: {resultado}")
    
    if not resultado:
        print("3. ⚠️ CONCLUSIÓN: La función devuelve 'Falso/Vacío' sin dar error.")
    else:
        print("3. ✅ CONCLUSIÓN: La función devuelve datos válidos.")

except Exception:
    print("\n💥 ¡CRASH DETECTADO! Aquí está el error real:")
    traceback.print_exc()
