import sys
import os
import time
import uuid
import warnings
import importlib
from pathlib import Path
from typing import Tuple, List, Dict, Any

# Silenciar advertencias
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", module="aiohttp")

STATUS_QUIET = os.getenv("STATUS_QUIET", "true").lower() == "true"

def qprint(*args, **kwargs):
    if not STATUS_QUIET:
        print(*args, **kwargs)

qprint("\n\n" + "="*50)
qprint("--- STATUS.PY V14: HYBRID MODE (BINANCE/PACIFICA) ---")
qprint("="*50 + "\n")

# ==============================================================================
# 1. CARGADOR DE ENV
# ==============================================================================
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
    # Búsqueda recursiva del .env si no carga
    if os.getenv("PACIFICA_ACCOUNT") is None and os.getenv("BINANCE_API_KEY") is None:
        here = Path(__file__).resolve().parent
        for path in [here, here.parent, here.parent.parent]:
            env_file = path / ".env"
            if env_file.exists():
                load_dotenv(dotenv_path=env_file, override=True)
                break
except Exception: pass

# ==============================================================================
# 2. SELECCIÓN DE CLIENTE Y MODO
# ==============================================================================
TARGET_MODE = os.getenv("TARGET_MODE", "PACIFICA").upper()
qprint(f"ℹ️ MODO DETECTADO: {TARGET_MODE}")

try:
    from exchanges.lighter_api import LighterClient
except ImportError:
    print("❌ Error: No se encuentra exchanges.lighter_api")
    sys.exit(1)

# Import condicional del Target
PacificaClient = None
BinanceAdapter = None

if TARGET_MODE == "BINANCE":
    try:
        from exchanges.binance_adapter import BinanceAdapter
    except ImportError:
        print("❌ Error: No se encuentra exchanges.binance_adapter")
        sys.exit(1)
else:
    try:
        from exchanges.pacifica_api import PacificaClient
    except ImportError:
        print("❌ Error: No se encuentra exchanges.pacifica_api")
        sys.exit(1)

class SystemStatus:
    def __init__(self):
        self.mode = TARGET_MODE
        
        # --- CARGA DEL CLIENTE TARGET ---
        if self.mode == "BINANCE":
            if not os.getenv("BINANCE_API_KEY"):
                print("❌ Error: Faltan credenciales BINANCE en .env")
                sys.exit(1)
            self.pac = BinanceAdapter() # Usamos 'self.pac' como nombre genérico para el target
        else:
            if not os.getenv("PACIFICA_ACCOUNT"):
                print("❌ Error: Faltan credenciales PACIFICA en .env")
                sys.exit(1)
            self.pac = PacificaClient()

        self.lig = LighterClient()
        
        # --- CONFIGURACIÓN DE SÍMBOLOS ---
        raw_symbol = os.getenv("SYMBOL", "ETH-USDT").upper().strip()
        self.full_pair = raw_symbol.replace("/", "-") 
        self.target_token = self.full_pair.split("-")[0]
        
        qprint(f"ℹ️ Config: Pair='{self.full_pair}' | Token='{self.target_token}' | Mode='{self.mode}'")

        # Distancia de protección (0.1%)
        self.rescue_dist_pct = 0.001 
        
        # Timers de espera
        self.pending_timers: Dict[str, float] = {}
        self.delay_seconds = 10.0 

    def _is_target(self, symbol_str: str) -> bool:
        if not symbol_str: return False
        return self.target_token in symbol_str.upper()

    # ==========================================================================
    # LÓGICA DE PRECIO
    # ==========================================================================
    def _get_pacifica_price_from_trades(self) -> float:
        # En modo Binance, no usamos trades recientes para precio, retornamos 0
        # y dejamos que el fallback de Lighter haga el trabajo.
        if self.mode == "BINANCE":
            return 0.0
            
        try:
            trades = self.pac.get_recent_trades(self.full_pair)
            if trades and isinstance(trades, list) and len(trades) > 0:
                last_trade = trades[0]
                px = float(last_trade.get('price') or last_trade.get('px') or last_trade.get('p') or 0)
                if px > 0: return px
        except Exception as e: pass
        return 0.0

    def _get_any_market_price(self) -> float:
        # 1. Intentar Target (Solo funciona en Pacifica)
        px_pac = self._get_pacifica_price_from_trades()
        if px_pac > 0: return px_pac

        # 2. Intentar Lighter (Fallback UNIVERSAL)
        try:
            book = self.lig.get_order_book(self.full_pair)
            if not book or not book.get('bids'):
                book = self.lig.get_order_book(self.target_token)
            
            if book and book.get('bids') and book.get('asks'):
                bb = float(book['bids'][0][0])
                ba = float(book['asks'][0][0])
                px = (bb + ba) / 2.0
                if px > 0: 
                    # print(f"⚠️ Usando precio de LIGHTER como referencia ({px})")
                    return px
        except: pass
        return 0.0

    # ==========================================================================
    # LOGICA DE PROTECCIÓN (ADAPTADA)
    # ==========================================================================
    
    def _protect_pac(self, symbol: str, amt: float, entry_price: float):
        print(f"🛡️ [AUTO-FIX] Blindando {self.mode} {symbol} ({amt}) @ {entry_price:.4f}...")
        try:
            if entry_price <= 0:
                print("⚠️ EntryPrice es 0. Consultando últimos trades...")
                entry_price = self._get_any_market_price()
                if entry_price <= 0:
                    print("❌ CRÍTICO: Imposible determinar precio.")
                    return
                print(f"   -> Precio recuperado: {entry_price:.4f}")

            is_long = amt > 0
            
            # Normalización de Side según modo
            if self.mode == "BINANCE":
                # BinanceAdapter espera 'LONG'/'SHORT' o 'BUY'/'SELL'
                pos_side = "BUY" if is_long else "SELL"
            else:
                # Pacifica espera 'ask'/'bid'
                pos_side = "ask" if is_long else "bid" 
            
            if is_long:
                tp = entry_price * (1.0 + self.rescue_dist_pct)
                sl = entry_price * (1.0 - self.rescue_dist_pct)
            else:
                tp = entry_price * (1.0 - self.rescue_dist_pct)
                sl = entry_price * (1.0 + self.rescue_dist_pct)

            # Redondeo a 2 decimales (Estándar seguro)
            # Nota: El BinanceAdapter hará el price_to_precision final
            tp = round(tp, 4) # Un poco más de precisión antes de enviar
            sl = round(sl, 4)

            # Enviamos orden
            res = self.pac.set_position_tpsl(
                symbol=self.target_token,
                position_side=pos_side, 
                tp_stop=tp,
                sl_stop=sl,
                tp_limit=tp,    # Ignorado por BinanceAdapter
                sl_limit=None,  # Ignorado por BinanceAdapter
                tp_client_order_id=str(uuid.uuid4()),
                sl_client_order_id=str(uuid.uuid4())
            )
            
            # Verificamos respuesta
            success = res.get("accepted") if self.mode == "BINANCE" else res.get("success")
            
            if success:
                 print(f"✅ {self.mode} BLINDADA: TP={tp} | SL={sl}")
            else:
                 print(f"⚠️ {self.mode} Error: {res}")

        except Exception as e:
            print(f"❌ Error protegiendo {self.mode}: {e}")

    def _protect_lig(self, symbol: str, amt: float, entry_price: float):
        print(f"🛡️ [AUTO-FIX] Blindando Lighter {symbol} ({amt}) Ref: {entry_price:.4f}...")
        try:
            if entry_price <= 0:
                entry_price = self._get_any_market_price()
                if entry_price <= 0:
                    print("❌ Error: No se pudo obtener precio de referencia.")
                    return

            is_long = amt > 0
            side = "BUY" if is_long else "SELL"
            
            if is_long:
                tp = entry_price * (1.0 + self.rescue_dist_pct)
                sl = entry_price * (1.0 - self.rescue_dist_pct)
            else:
                tp = entry_price * (1.0 - self.rescue_dist_pct)
                sl = entry_price * (1.0 + self.rescue_dist_pct)

            tp = round(tp, 2)
            sl = round(sl, 2)

            res = self.lig.set_tpsl_grouped(
                symbol=self.target_token, 
                side=side, 
                tp_trigger=tp, 
                sl_trigger=sl
            )
            
            if res and res.get('accepted'):
                print(f"✅ Lighter BLINDADA: TP={tp:.2f} | SL={sl:.2f}")
            else:
                print(f"❌ Lighter rechazó: {res}")

        except Exception as e:
            print(f"❌ Error protegiendo Lighter: {e}")

    # ==========================================================================
    # CHEQUEOS INTELIGENTES (UNIFICADO)
    # ==========================================================================
    def check_pacifica_clean(self, auto_fix: bool = False) -> Tuple[bool, str]:
        try:
            orders = self.pac.get_open_orders(self.target_token) if self.mode == "BINANCE" else self.pac.get_open_orders()
            if self.mode == "BINANCE" and hasattr(self.pac, "get_open_orders_by_client_ids"):
                tracked = self.pac.get_open_orders_by_client_ids(self.target_token)
                if tracked:
                    orders = (orders or []) + tracked
            if self.mode == "BINANCE" and hasattr(self.pac, "get_stream_open_orders"):
                stream_orders = self.pac.get_stream_open_orders(self.target_token)
                if stream_orders:
                    orders = (orders or []) + stream_orders
            
            my_orders_count = 0
            if orders:
                if self.mode == "BINANCE":
                    # En Binance, las órdenes condicionales a veces no traen symbol consistente.
                    my_orders_count = len(orders)
                else:
                    my_orders_count = len([o for o in orders if self._is_target(o.get('symbol', ''))])

            # --- NORMALIZACIÓN DE POSICIONES ---
            normalized_positions = []
            
            if self.mode == "BINANCE":
                # BinanceAdapter devuelve un solo dict o None
                pos_data = self.pac.get_open_position(self.target_token)
                if pos_data:
                    # Convertimos al formato que espera el loop de abajo
                    size = float(pos_data['size'])
                    side_sign = 1 if pos_data['side'] == 'BUY' else -1
                    normalized_positions.append({
                        "amount": size * side_sign,
                        "symbol": self.target_token,
                        "entryPrice": 0.0 # BinanceAdapter no devuelve entry price en este método simplificado, usaremos fallback
                    })
            else:
                # Pacifica devuelve lista
                normalized_positions = self.pac.get_positions()

            found_target_pos = False

            for pos in normalized_positions:
                amt = float(pos.get("amount", "0"))
                sym = pos.get("symbol", "?")
                entry = float(pos.get("entryPrice", 0) or pos.get("avgPrice", 0))

                if abs(amt) > 0.0000001 and self._is_target(sym):
                    found_target_pos = True
                    timer_key = f"PAC_{sym}"

                    # Si hay posición, debe haber órdenes de protección
                    if my_orders_count >= 1:
                        self.pending_timers.pop(timer_key, None)
                        return False, f"{self.mode} PROTEGIDO: Posición {amt} con {my_orders_count} órdenes."
                    else:
                        # Si hay 0 o 1 orden, entra acá para arreglarlo
                        if auto_fix:
                            first_seen = self.pending_timers.get(timer_key)
                            if first_seen is None:
                                self.pending_timers[timer_key] = time.time()
                                return False, f"{self.mode}: Insegura ({my_orders_count} órdenes). Esperando {int(self.delay_seconds)}s..."
                            else:
                                elapsed = time.time() - first_seen
                                if elapsed >= self.delay_seconds:
                                    self._protect_pac(sym, amt, entry)
                                    self.pending_timers.pop(timer_key, None)
                                    return False, f"{self.mode}: TP/SL reenviados (Corrección)."
                                else:
                                    remaining = self.delay_seconds - elapsed
                                    return False, f"{self.mode}: Esperando... ({remaining:.1f}s)"
                        
                        return False, f"{self.mode} RIESGO: Posición {amt} con solo {my_orders_count} órdenes."

            if not found_target_pos:
                keys_to_remove = [k for k in self.pending_timers if f"PAC_{self.target_token}" in k]
                for k in keys_to_remove: del self.pending_timers[k]

                # En BINANCE, si no hay posición, cancelamos TODO por seguridad
                if self.mode == "BINANCE" and auto_fix:
                    last = self.pending_timers.get("BIN_CANCEL")
                    now = time.time()
                    if last is None or (now - last) >= 3.0:
                        try:
                            self.pac.cancel_all_orders(self.target_token)
                        except Exception:
                            pass
                        self.pending_timers["BIN_CANCEL"] = now
                    return False, f"{self.mode}: Cancelando órdenes (pos=0)."

            # Si no hay posición pero sí órdenes pendientes, NO está limpio
            if my_orders_count > 0:
                if auto_fix:
                    try:
                        self.pac.cancel_all_orders(self.target_token)
                        return False, f"{self.mode}: Cancelando {my_orders_count} órdenes abiertas sin posición."
                    except Exception:
                        pass
                return False, f"{self.mode} RIESGO: {my_orders_count} órdenes abiertas sin posición."

            return True, f"{self.mode} Clean"
        except Exception as e:
            return False, f"{self.mode} Error: {str(e)}"

    def check_lighter_clean(self, auto_fix: bool = False) -> Tuple[bool, str]:
        pac_ref_price = 0.0
        try: pac_ref_price = self._get_any_market_price()
        except: pass

        acc_info = None
        for i in range(3):
            try:
                acc_info = self.lig.get_account_info()
                if acc_info: break
            except Exception: time.sleep(1.0)
        
        if not acc_info: return False, "Lighter Error: No responde"

        try:
            found_target_pos = False
            if hasattr(acc_info, 'accounts'):
                for account in acc_info.accounts:
                    pending_orders = getattr(account, 'pending_order_count', 0)
                    
                    if hasattr(account, 'positions'):
                        for pos in account.positions:
                            size = float(getattr(pos, 'position', 0))
                            sym = getattr(pos, 'symbol', '?')
                            
                            lig_entry = float(getattr(pos, 'entry_price', 0))
                            if lig_entry == 0: lig_entry = float(getattr(pos, 'avg_entry_price', 0))
                            final_ref = lig_entry if lig_entry > 0 else pac_ref_price

                            if abs(size) > 0.0000001 and self._is_target(sym):
                                found_target_pos = True
                                timer_key = f"LIG_{sym}"

                                if pending_orders >= 2:
                                    self.pending_timers.pop(timer_key, None)
                                    return False, f"Lighter PROTEGIDO: Posición {size} con {pending_orders} órdenes."
                                else:
                                    if auto_fix:
                                        first_seen = self.pending_timers.get(timer_key)
                                        if first_seen is None:
                                            self.pending_timers[timer_key] = time.time()
                                            return False, f"Lighter: Insegura ({pending_orders} órdenes). Esperando {int(self.delay_seconds)}s..."
                                        else:
                                            elapsed = time.time() - first_seen
                                            if elapsed >= self.delay_seconds:
                                                self._protect_lig(sym, size, final_ref)
                                                self.pending_timers.pop(timer_key, None)
                                                return False, "Lighter: TP/SL reenviados (Corrección)."
                                            else:
                                                remaining = self.delay_seconds - elapsed
                                                return False, f"Lighter: Esperando... ({remaining:.1f}s)"
                                    return False, f"Lighter RIESGO: Posición {size} con solo {pending_orders} órdenes."

            if not found_target_pos:
                 keys_to_remove = [k for k in self.pending_timers if f"LIG_{self.target_token}" in k]
                 for k in keys_to_remove: del self.pending_timers[k]

            return True, "Lighter Clean"
        except Exception as e:
            return False, f"Lighter Exception: {str(e)}"

    def close(self):
        try: self.lig.close()
        except: pass

    def is_system_ready(self, auto_fix: bool = False) -> bool:
        is_direct_run = (__name__ == "__main__")
        
        pac_ok, pac_msg = self.check_pacifica_clean(auto_fix=auto_fix)
        if is_direct_run:
            if STATUS_QUIET:
                print(f"[{time.strftime('%H:%M:%S')}] {self.mode}: {'✅' if pac_ok else '❌'}")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] {self.mode}: {'✅' if pac_ok else '❌ ' + pac_msg}")
        
        lig_ok, lig_msg = self.check_lighter_clean(auto_fix=auto_fix)
        if is_direct_run:
            if STATUS_QUIET:
                print(f"[{time.strftime('%H:%M:%S')}] Lig: {'✅' if lig_ok else '❌'}")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] Lig: {'✅' if lig_ok else '❌ ' + lig_msg}")
        
        return (pac_ok and lig_ok)

if __name__ == "__main__":
    checker = SystemStatus()
    qprint(f"👮‍♂️ GUARDIÁN ACTIVO - Pair: {checker.full_pair}")
    qprint(f"⏰ RETRASO DE PROTECCIÓN: {checker.delay_seconds} segundos")
    qprint("--------------------------------------------------")
    
    try:
        while True:
            checker.is_system_ready(auto_fix=True)
            time.sleep(1.0) # Ciclo de 10 segundos para el guardián
            
    except KeyboardInterrupt:
        print("\n🛑 Guardián detenido por usuario.")
    except Exception as e:
        print(f"\n❌ Error fatal en guardián: {e}")
    finally:
        checker.close()
