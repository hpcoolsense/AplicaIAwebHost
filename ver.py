# ver_error.py
import sys
import traceback

print("🔍 Intentando importar exchanges.lighter_api...")

try:
    import exchanges.lighter_api
    print("✅ ¡El archivo funciona perfectamente! El problema está en otro lado.")
except Exception as e:
    print("\n❌ EL ARCHIVO TIENE UN ERROR INTERNO:")
    print("========================================")
    traceback.print_exc()
    print("========================================")

