#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple, List, Dict

# ==========================================================
# ENV loader
# ==========================================================
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
    if os.getenv("PACIFICA_ACCOUNT") is None:
        here = Path(__file__).resolve().parent
        for path in [here, here.parent, here.parent.parent]:
            env_file = path / ".env"
            if env_file.exists():
                load_dotenv(dotenv_path=env_file, override=True)
                break
except Exception:
    pass

# ==========================================================
# IMPORTS: tus clientes
# ==========================================================
try:
    from exchanges.pacifica_api import PacificaClient
    from exchanges.lighter_api import LighterClient
except ImportError as e:
    print(f"❌ Error: No se encuentran los clientes API. {e}")
    sys.exit(1)

# ==========================================================
# Config
# ==========================================================
SYMBOL = (os.getenv("SYMBOL", "ETH-USDT") or "ETH-USDT").upper().replace("/", "-").strip()
POLL_SECONDS = int(os.getenv("NO_SL_CLOSE_POLL_SECONDS", "10"))
ONE_SHOT = (os.getenv("NO_SL_CLOSE_ONE_SHOT", "false").lower() == "true")
MIN_ABS_QTY = float(os.getenv("NO_SL_CLOSE_MIN_ABS_QTY", "0.0000001"))
VERBOSE = (os.getenv("NO_SL_CLOSE_VERBOSE", "true").lower() == "true")

# ✅ Ventana de gracia para no cerrar operaciones recién abiertas
GRACE_SECONDS = int(os.getenv("NO_SL_CLOSE_GRACE_SECONDS", "12"))

def _log(msg: str):
    if VERBOSE:
        print(msg, flush=True)

def _infer_symbol_base(sym: str) -> str:
    s = (sym or "").upper().replace("/", "-").strip()
    if "-" in s:
        return s.split("-", 1)[0].strip() or "ETH"
    return s or "ETH"

def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def _is_open_qty(q: Optional[float]) -> bool:
    return (q is not None) and (abs(float(q)) > MIN_ABS_QTY)

def _match_symbol(sym_any: str, symbol_full: str, base: str) -> bool:
    s = (sym_any or "").upper().replace("/", "-").strip()
    return (s == symbol_full) or (s == base) or s.startswith(base)

# ==========================================================
# ✅ Memoria de posiciones abiertas detectadas (grace)
# ==========================================================
# Nota: se indexa por symbol_base para evitar falsos cierres en el “arranque” del SL
_first_seen_open: Dict[str, Dict[str, float]] = {
    "pac": {},  # symbol_base -> timestamp_first_seen_open
    "lig": {},
}

def _open_key(symbol_full: str) -> str:
    # clave mínima: base del símbolo (ej: ETH). Si operás múltiples quotes, podés cambiar a symbol_full.
    return _infer_symbol_base(symbol_full)

def _in_grace(dex: str, symbol_full: str, is_open: bool, now: float) -> bool:
    """
    True si la posición está en ventana de gracia (recién detectada abierta).
    - Si se cierra, limpiamos el estado.
    - Si es la primera vez que la vemos abierta, empezamos timer y damos gracia.
    """
    key = _open_key(symbol_full)
    m = _first_seen_open[dex]

    if not is_open:
        if key in m:
            del m[key]
        return False

    if key not in m:
        m[key] = now
        return True

    return (now - m[key]) < float(GRACE_SECONDS)

# ==========================================================
# PosInfo
# ==========================================================
@dataclass
class PosInfo:
    is_open: bool
    symbol: str
    size: float              # abs(size)
    side: Optional[str]      # "LONG"/"SHORT" (para lighter) o inferido
    raw: Any

# ==========================================================
# 1) Pacifica: posición
# ==========================================================
def get_pacifica_position_info(pac: PacificaClient, symbol: str) -> PosInfo:
    base = _infer_symbol_base(symbol)
    try:
        positions = pac.get_positions()
    except Exception as e:
        return PosInfo(False, base, 0.0, None, {"error": str(e)})

    chosen = None
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        sym = (pos.get("symbol") or "")
        if _match_symbol(sym, symbol, base):
            chosen = pos
            break

    if not chosen:
        return PosInfo(False, base, 0.0, None, positions)

    amt = _safe_float(chosen.get("amount")) or _safe_float(chosen.get("position")) or _safe_float(chosen.get("size")) or 0.0
    is_open = _is_open_qty(amt)

    raw_side = (chosen.get("side") or chosen.get("position_side") or chosen.get("direction") or "").lower().strip()
    side = None
    if raw_side in ("bid", "buy", "long"):
        side = "LONG"
    elif raw_side in ("ask", "sell", "short"):
        side = "SHORT"

    chosen["_raw_side_norm"] = raw_side

    return PosInfo(is_open, (chosen.get("symbol") or base), abs(float(amt)), side, chosen)

# ==========================================================
# 2) Lighter: posición (tu lógica)
# ==========================================================
def get_lighter_position_info(lig: LighterClient, symbol: str) -> PosInfo:
    base = _infer_symbol_base(symbol)

    acc_info = None
    last_error = ""
    for _ in range(3):
        try:
            acc_info = lig.get_account_info()
            if acc_info:
                break
        except Exception as e:
            last_error = str(e)
        time.sleep(1.0)

    if not acc_info:
        return PosInfo(False, base, 0.0, None, {"error": f"no response: {last_error}"})

    if not hasattr(acc_info, "accounts"):
        return PosInfo(False, base, 0.0, None, {"error": "unknown structure", "raw": acc_info})

    for account in acc_info.accounts:
        if hasattr(account, "positions"):
            for pos in account.positions:
                signed_size = _safe_float(getattr(pos, "position", 0.0)) or 0.0
                if abs(signed_size) <= MIN_ABS_QTY:
                    continue

                sym = (getattr(pos, "symbol", None) or getattr(pos, "market", None) or getattr(pos, "base", None) or base)
                sym_n = str(sym).upper().replace("/", "-").strip()

                if not _match_symbol(sym_n, symbol, base):
                    continue

                side = "LONG" if signed_size > 0 else "SHORT"
                return PosInfo(True, sym_n, abs(float(signed_size)), side, pos)

    return PosInfo(False, base, 0.0, None, acc_info)

# ==========================================================
# 3) Detectar si hay SL (por órdenes abiertas)
# ==========================================================
def pacifica_has_sl_orders(pac: PacificaClient, symbol: str) -> bool:
    base = _infer_symbol_base(symbol)
    try:
        orders = pac.get_open_orders()
    except Exception:
        return False

    for o in orders or []:
        if not isinstance(o, dict):
            continue
        sym = (o.get("symbol") or "")
        if not _match_symbol(sym, symbol, base):
            continue

        typ = str(o.get("type") or o.get("order_type") or o.get("kind") or "").lower()
        if "stop" in typ or "sl" in typ:
            return True

        ro = str(o.get("reduce_only") or o.get("reduceOnly") or "").lower()
        if ro in ("true", "1"):
            return True

        if o.get("trigger_price") is not None or o.get("stop_price") is not None:
            return True

    return False

def lighter_has_sl_orders(lig: LighterClient, symbol: str) -> bool:
    """
    Regla mínima: si pending_order_count > 0 asumimos que hay protección (OCO/SL).
    """
    try:
        acc_info = lig.get_account_info()
    except Exception:
        return False
    if not acc_info or not hasattr(acc_info, "accounts"):
        return False

    for account in acc_info.accounts:
        if getattr(account, "pending_order_count", 0) > 0:
            return True
    return False

# ==========================================================
# 4) Cierres a mercado
# ==========================================================
def close_pacifica_market(pac: PacificaClient, p: PosInfo) -> bool:
    if not p.is_open or not isinstance(p.raw, dict):
        return False

    raw_side = (p.raw.get("_raw_side_norm") or p.raw.get("side") or "").lower().strip()
    qty = float(p.size)
    sym_base = _infer_symbol_base(p.symbol)

    if raw_side == "bid":
        close_side = "SELL"
    elif raw_side == "ask":
        close_side = "BUY"
    else:
        _log(f"[NO-SL] Pacifica: no puedo inferir side (raw_side={raw_side!r}), NO cierro.")
        return False

    _log(f"[NO-SL] Pacifica CLOSE MARKET | symbol={sym_base} close_side={close_side} qty={qty}")
    pac.place_market(symbol=sym_base, side=close_side, base_qty=qty)
    return True

def close_lighter_market(lig: LighterClient, p: PosInfo) -> bool:
    if not p.is_open or p.side is None:
        return False

    qty = float(p.size)
    sym_base = _infer_symbol_base(p.symbol)

    px_ref = None
    try:
        px_ref = _safe_float(getattr(p.raw, "entry_price", None)) or _safe_float(getattr(p.raw, "avg_entry_price", None))
    except Exception:
        px_ref = None

    if not px_ref or px_ref <= 0:
        _log("[NO-SL] Lighter: no tengo px_ref (entry/avg), NO puedo market sin ref. Abort.")
        return False

    close_side = "SELL" if p.side == "LONG" else "BUY"
    _log(f"[NO-SL] Lighter CLOSE MARKET | symbol={sym_base} close_side={close_side} qty={qty} px_ref={px_ref}")
    lig.place_market(symbol=sym_base, side=close_side, qty_base=qty, avg_exec_px=float(px_ref))
    return True

# ==========================================================
# 5) Loop
# ==========================================================
def run_once(pac: PacificaClient, lig: LighterClient, symbol: str) -> Tuple[bool, str]:
    now = time.time()

    pac_pos = get_pacifica_position_info(pac, symbol)
    lig_pos = get_lighter_position_info(lig, symbol)

    pac_protected = pacifica_has_sl_orders(pac, symbol) if pac_pos.is_open else True
    lig_protected = lighter_has_sl_orders(lig, symbol) if lig_pos.is_open else True

    # ✅ grace (por símbolo base)
    pac_grace = _in_grace("pac", pac_pos.symbol if pac_pos.symbol else symbol, pac_pos.is_open, now)
    lig_grace = _in_grace("lig", lig_pos.symbol if lig_pos.symbol else symbol, lig_pos.is_open, now)

    _log(
        f"[NO-SL] status | pac_open={pac_pos.is_open} protected={pac_protected} grace={pac_grace} "
        f"| lig_open={lig_pos.is_open} protected={lig_protected} grace={lig_grace} "
        f"(grace={GRACE_SECONDS}s)"
    )

    did = False
    reasons: List[str] = []

    # Solo cerramos si NO está protegido y YA pasó el grace
    if pac_pos.is_open and (not pac_protected) and (not pac_grace):
        ok = close_pacifica_market(pac, pac_pos)
        did = did or ok
        reasons.append("pac_closed_no_sl" if ok else "pac_failed_close_no_sl")
    elif pac_pos.is_open and (not pac_protected) and pac_grace:
        reasons.append("pac_in_grace_no_sl")

    if lig_pos.is_open and (not lig_protected) and (not lig_grace):
        ok = close_lighter_market(lig, lig_pos)
        did = did or ok
        reasons.append("lig_closed_no_sl" if ok else "lig_failed_close_no_sl")
    elif lig_pos.is_open and (not lig_protected) and lig_grace:
        reasons.append("lig_in_grace_no_sl")

    if did:
        return True, ",".join(reasons) if reasons else "closed"
    return False, ",".join(reasons) if reasons else "ok"

def main():
    _log(f"[NO-SL] start | SYMBOL={SYMBOL} | poll={POLL_SECONDS}s | one_shot={ONE_SHOT} | grace={GRACE_SECONDS}s")
    pac = PacificaClient()
    lig = LighterClient()

    try:
        while True:
            changed, reason = run_once(pac, lig, SYMBOL)
            if changed:
                _log(f"[NO-SL] ✅ action: {reason}")
            else:
                _log(f"[NO-SL] - no action: {reason}")

            if ONE_SHOT:
                break
            time.sleep(max(1, POLL_SECONDS))
    except KeyboardInterrupt:
        _log("[NO-SL] stopped by user")
    finally:
        try:
            lig.close()
        except Exception:
            pass
        time.sleep(0.1)

if __name__ == "__main__":
    main()
