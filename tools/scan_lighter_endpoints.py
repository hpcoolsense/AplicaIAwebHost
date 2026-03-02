# tools/scan_lighter_endpoints.py
import os, sys, asyncio, requests
from dotenv import load_dotenv
load_dotenv()

BASE = os.getenv("LIGHTER_REST_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/")

def build_auth():
    # usamos el mismo método que lighter_api para crear Authorization
    # sin importar que lighter_api.py cambie, generamos un token fresco aquí
    try:
        import lighter
    except Exception:
        raise RuntimeError(
            "Falta el paquete 'lighter'. Instala con:\n"
            '  pip install "git+https://github.com/elliottech/lighter-python.git#egg=lighter"'
        )
    api_key_priv = os.getenv("LIGHTER_API_KEY_PRIVATE_KEY", "").strip()
    eth_priv     = os.getenv("LIGHTER_ETH_PRIVATE_KEY", "").strip()
    api_key_idx  = int(os.getenv("LIGHTER_API_KEY_INDEX", "0"))
    account_idx  = int(os.getenv("LIGHTER_ACCOUNT_INDEX", "0"))
    if not api_key_priv or not eth_priv or not api_key_idx or not account_idx:
        raise RuntimeError("Faltan variables LIGHTER_* en .env (PRIVATE_KEY, ETH_PRIVATE_KEY, API_KEY_INDEX, ACCOUNT_INDEX)")
    # Windows-friendly loop
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    async def _build():
        sc = lighter.SignerClient(
            url=BASE,
            private_key=api_key_priv,
            account_index=account_idx,
            api_key_index=api_key_idx,
        )
        err = sc.check_client()
        if err is not None:
            raise RuntimeError(f"SignerClient check_client() error: {err}")
        token, err = sc.create_auth_token_with_expiry(600)
        if err is not None:
            raise RuntimeError(f"Error creando auth token: {err}")
        return token

    return asyncio.run(_build())

def try_get(session, path, params=None):
    url = BASE + path
    try:
        r = session.get(url, params=params, timeout=10)
        info = r.text[:180].replace("\n", " ")
        return f"{path:35s} -> {r.status_code}  {info}"
    except Exception as e:
        return f"{path:35s} -> ERR {e}"

def main():
    auth = build_auth()
    s = requests.Session()
    s.headers.update({"Authorization": auth})

    # Lista ampliada de posibles endpoints en distintos despliegues
    candidates = [
        "/", "/info",
        "/api/v1/apikeys",
        "/api/v1/order_books",
        "/api/v1/accounts",
        "/api/v1/account",
        "/api/v1/account_by_index",
        "/api/v1/accountsByL1Address",
        "/api/v1/accounts_by_l1_address",
        "/api/v1/accountAll",
        "/api/v1/account_all",
        "/api/v1/user",
        "/api/v1/me",
        # variantes con query
    ]
    print("BASE:", BASE)
    for p in candidates:
        print(try_get(s, p))
    # Pruebas con query account_index cuando aplica
    ai = os.getenv("LIGHTER_ACCOUNT_INDEX", "").strip()
    if ai:
        qpaths = ["/api/v1/account", "/api/v1/accounts", "/api/v1/account_by_index",
                  "/api/v1/accountAll", "/api/v1/account_all"]
        print("\nCon ?account_index=" + ai)
        for p in qpaths:
            print(try_get(s, p, params={"account_index": ai}))

if __name__ == "__main__":
    main()
