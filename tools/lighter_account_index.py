# tools/lighter_account_index.py  (versión sin SDK)
import os, sys, json, requests, pathlib
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
load_dotenv(dotenv_path=str(ENV_PATH))

BASE = os.getenv("LIGHTER_REST_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/")
ETH_PRIV = (os.getenv("LIGHTER_ETH_PRIVATE_KEY") or "").strip()

def die(msg):
    print(f"[X] {msg}"); sys.exit(1)

try:
    from eth_account import Account  # pip install eth-account
except Exception:
    die("Falta 'eth-account'. Instala: pip install eth-account")

def main():
    if not ETH_PRIV:
        die("Falta LIGHTER_ETH_PRIVATE_KEY en .env")
    try:
        acct = Account.from_key(ETH_PRIV)
        l1 = acct.address
        print(f"[INFO] L1 address: {l1}")
    except Exception as e:
        die(f"No pude derivar L1 address: {e}")

    url = f"{BASE}/api/v1/accountsByL1Address"
    params = {"l1_address": l1}
    try:
        r = requests.get(url, params=params, timeout=15)
    except requests.RequestException as e:
        die(f"Network error: {e}")

    if 200 <= r.status_code < 300:
        try:
            js = r.json()
        except Exception:
            die(f"Respuesta no JSON: {r.text[:200]}")
        idx = None
        if isinstance(js, dict):
            for key in ("sub_accounts", "accounts"):
                arr = js.get(key)
                if isinstance(arr, list) and arr:
                    cand = arr[0]
                    if isinstance(cand, dict):
                        idx = cand.get("index") or cand.get("account_index") or cand.get("id")
                        if idx is not None:
                            break
        elif isinstance(js, list) and js:
            cand = js[0]
            if isinstance(cand, dict):
                idx = cand.get("index") or cand.get("account_index") or cand.get("id")

        if idx is None:
            print("[X] No encontré 'index' en la respuesta. Dump parcial:")
            try: print(json.dumps(js, indent=2)[:1200])
            except Exception: print(str(js)[:1200])
            die("Pásame esa salida y lo ajusto.")
        print(f"[OK] LIGHTER_ACCOUNT_INDEX sugerido: {idx}")
        print(f"\n=> Pon en .env:\nLIGHTER_ACCOUNT_INDEX={idx}")
        return

    # Si pide Auth (401/403), lo avisa claramente:
    die(f"HTTP {r.status_code}: {r.text[:300]}\nEl endpoint probablemente requiere Authorization. En ese caso usa la Opción B (Python 3.12 + SDK).")

if __name__ == "__main__":
    main()
