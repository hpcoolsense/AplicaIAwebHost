# notifications/balance_probe.py
from __future__ import annotations
from typing import Any, Optional


def _pick_number(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def _sum_like_dict(d: Any) -> Optional[float]:
    # intenta sumar valores numéricos de un dict (ej: {"USDT": 10, "ETH": 0.01})
    if not isinstance(d, dict):
        return None
    s = 0.0
    found = False
    for v in d.values():
        if isinstance(v, (int, float)):
            s += float(v)
            found = True
        elif isinstance(v, dict):
            # algunos SDKs devuelven {"free":..,"total":..}
            tv = v.get("total") if isinstance(v, dict) else None
            if isinstance(tv, (int, float)):
                s += float(tv)
                found = True
    return s if found else None


def try_get_total_balance_usd(client: Any) -> Optional[float]:
    """
    Intenta obtener 'balance total' (en USD/USDT o equivalente) de forma genérica.
    Si tu cliente expone un método específico, lo capturamos sin romper.
    """
    if client is None:
        return None

    # 1) métodos comunes
    for name in ("get_total_balance_usd", "total_balance_usd", "get_balance_usd", "get_equity_usd"):
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                return _pick_number(fn())
            except Exception:
                pass

    # 2) estilos ccxt-like
    for name in ("fetch_balance", "get_balance", "get_balances", "balance"):
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                b = fn()
                n = _pick_number(b)
                if n is not None:
                    return n
                n2 = _sum_like_dict(b)
                if n2 is not None:
                    return n2
            except Exception:
                pass

    return None