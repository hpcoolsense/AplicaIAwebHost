import os
import time
import threading
import importlib
import traceback
import sys
import socket
import queue
import ctypes
from dataclasses import dataclass
from typing import Optional, Tuple, Any, Callable
from pathlib import Path

# ==============================================================================
# 0. IMPORTAR STATUS.PY DESDE LA RAÍZ
# ==============================================================================
try:
    root_dir = Path(__file__).resolve().parent.parent
    sys.path.append(str(root_dir))
    from status import SystemStatus
    print(f"[ENGINE] Módulo 'status.py' importado correctamente desde {root_dir}")
except ImportError:
    print("❌ [CRITICAL] No se encontró 'status.py' en la carpeta raíz.")
    sys.exit(1)

# === Cargar .env ===
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
    if os.getenv("TRADE_BASE_QTY") is None:
        here = Path(__file__).resolve()
        for up in (here.parents[1], here.parents[2], here.parents[3] if len(here.parents) > 3 else None):
            if up and (up / ".env").exists():
                load_dotenv(dotenv_path=(up / ".env"), override=True)
                break
except Exception:
    pass

from feeds.pacifica import PacificaFeed
from feeds.lighter import LighterFeed
from notifications.telegram_notifier import TelegramNotifier
from notifications.cycle_tracker import CyclePnlTracker

# ===== CONFIGURACIÓN GLOBAL ESTATICA =====
SYMBOL_DEFAULT        = os.getenv("SYMBOL", "ETH-USDT")
QUOTE_DEFAULT         = os.getenv("QUOTE", "USDT")
EDGE_THRESHOLD        = float(os.getenv("EDGE_THRESHOLD", "0.0005"))
CLOSE_EDGE_TARGET     = float(os.getenv("CLOSE_EDGE_TARGET", "0.0003"))

# FAIL-SAFE
TPSL_FAILSAFE_PCT     = float(os.getenv("TPSL_SLIP", "0.001"))

VERBOSE               = True

LOOP_DELAY_MS         = int(os.getenv("ENGINE_TICK_MS", "50"))
GAP_BETWEEN_PAIRS_MS  = int(os.getenv("MIN_MS_BETWEEN_PAIRS", "800"))
DUAL_TIMEOUT_MS       = int(os.getenv("DUAL_TIMEOUT_MS", "800"))
ENGINE_ONE_SHOT       = os.getenv("ENGINE_ONE_SHOT", "false").lower() == "true"
ENGINE_MAX_PAIRS_ENV  = os.getenv("ENGINE_MAX_PAIRS", "").strip()
ENGINE_PAPER          = os.getenv("ENGINE_PAPER", "false").lower() == "true"
ENGINE_ALLOW_REAL     = os.getenv("ENGINE_ALLOW_REAL", "true").lower() == "true"

# Low Latency toggles
USE_PERF_COUNTER_LOOP   = os.getenv("ENGINE_USE_PERF_COUNTER_LOOP", "true").lower() == "true"
NO_SLEEP_ON_SIGNAL      = os.getenv("ENGINE_NO_SLEEP_ON_SIGNAL", "true").lower() == "true"
EDGE_NO_SLEEP           = os.getenv("ENGINE_EDGE_NO_SLEEP", "true").lower() == "true"
DISABLE_GAP_THROTTLE    = os.getenv("ENGINE_DISABLE_GAP_THROTTLE", "true").lower() == "true"
FAST_JOIN_SECS          = float(os.getenv("ENGINE_FAST_JOIN_SECS", "0.03"))
WARMUP_CLIENTS_ON_START = os.getenv("ENGINE_WARMUP_CLIENTS_ON_START", "true").lower() == "true"

# Status polling (saca el costo del status.py del hot loop)
STATUS_POLL_SECS        = float(os.getenv("ENGINE_STATUS_POLL_SECS", "10"))
ENGINE_CPU_CORE         = os.getenv("ENGINE_CPU_CORE", "").strip()

# --- C++ NATIVE HFT BINDINGS ---
# Cargamos la librería compilada en C++ para saltarnos la interpretación de Python
try:
    _hft_lib = ctypes.CDLL("/home/tomas/arb-panel-ARM/hft_core.so")
    
    _hft_lib.check_edge_p2l.argtypes = [ctypes.c_double, ctypes.c_double]
    _hft_lib.check_edge_p2l.restype = ctypes.c_double
    
    _hft_lib.check_edge_l2p.argtypes = [ctypes.c_double, ctypes.c_double]
    _hft_lib.check_edge_l2p.restype = ctypes.c_double

    _hft_lib.check_close_condition.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_int]
    _hft_lib.check_close_condition.restype = ctypes.c_int
    print("[ENGINE] HFT Core bindings cargados nativamente (ARM64 C++)")
except Exception as e:
    _hft_lib = None
    print(f"⚠️ [ENGINE] ADVERTENCIA: No se pudo cargar hft_core.so. Fallback a Python Puro. ({e})")

def _round_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    n = round(x / step)
    return round(n * step, 10)

def _round_tick(px: float, tick: float) -> float:
    """Rounds a price to EXACTLY 1 decimal place (tick 0.1) ignoring the env tick"""
    return round(px, 1)

def _round_px(px: float, tick: float) -> float:
    """Alias for _round_tick (Forced 1 decimal)"""
    return round(px, 1)

def _invert_side(side: str) -> str:
    return "SELL" if (side or "").upper() == "BUY" else "BUY"

def _parse_symbol(sym: str, quote_fallback: str = "USDT") -> Tuple[str, str]:
    s = (sym or "").upper().replace("/", "-").strip()
    if "-" in s:
        b, q = s.split("-", 1)
        return (b.strip() or "ETH"), (q.strip() or quote_fallback.upper())
    return s or "ETH", quote_fallback.upper()

def _round_tick(px: float, tick: float) -> float:
    t = float(tick or 0.0)
    if t <= 0:
        return float(px)
    return round(round(float(px) / t) * t, 12)

def _resolve_client_ctor(module_name: str, candidates: Tuple[str, ...]) -> Optional[Callable[[], Any]]:
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return None
    for cname in candidates:
        ctor = getattr(mod, cname, None)
        if callable(ctor):
            return ctor
    return None

def _best_from_book(feed) -> Tuple[Optional[float], Optional[float]]:
    try:
        bb = max((p for p, _ in getattr(feed.book, "bids", [])), default=None)
        ba = min((p for p, _ in getattr(feed.book, "asks", [])), default=None)
        return bb, ba
    except Exception:
        return None, None

@dataclass
class OrderFill:
    ok: bool
    msg: str
    order_id: Optional[str] = None
    filled_qty: Optional[float] = None
    avg_px: Optional[float] = None
    raw: Optional[Any] = None

class ExecutionThreadPool:
    """Pre-warmed threads for submitting instant API calls without thread-spawn overhead."""
    def __init__(self, num_threads=4):
        self.q = queue.Queue()
        self.threads = []
        for i in range(num_threads):
            t = threading.Thread(target=self._worker, daemon=True, name=f"ExecPool-{i}")
            t.start()
            self.threads.append(t)
            
    def _worker(self):
        while True:
            func, args, kwargs = self.q.get()
            try:
                func(*args, **kwargs)
            except Exception as e:
                print(f"[ExecutionThreadPool] Err: {e}")
            finally:
                self.q.task_done()
                
    def submit(self, func, *args, **kwargs):
        self.q.put((func, args, kwargs))

class ArbEngine:
    """
    MISMA LÓGICA / MISMAS FUNCIONES,
    pero hot-path más rápido:
      - status_checker afuera del loop (thread + Event)
      - apertura/cierre "ATAQUE" sin bloquear (join corto FAST_JOIN_SECS)
      - warmup de clientes opcional al start
      - loop con perf_counter y micro-sleep (o sin sleep en señal si NO_SLEEP_ON_SIGNAL)
    """

    def __init__(self, pac: Any, lig: LighterFeed, symbol: str = SYMBOL_DEFAULT, quote: str = QUOTE_DEFAULT, mode: str = "PACIFICA"):
        self.engine_paper       = ENGINE_PAPER
        self.engine_allow_real  = ENGINE_ALLOW_REAL
        self.engine_one_shot    = ENGINE_ONE_SHOT

        self.trade_base_qty = float(os.getenv("TRADE_BASE_QTY", "0.0028"))
        self.base_qty_step  = float(os.getenv("BASE_QTY_STEP", "0.0001"))

        self.enable_tpsl                        = os.getenv("ENABLE_TPSL", "true").lower() == "true"
        self.tpsl_pct                           = float(os.getenv("ENGINE_TPSL_PCT", "0.01")) # Forzado a 1% por defecto
        self.tpsl_limit_offset_pct              = float(os.getenv("ENGINE_TPSL_LIMIT_OFFSET_PCT", "0.0005"))
        try:
            self.grid_levels = max(1, int(os.getenv("LEVELS", "5")))
        except Exception:
            self.grid_levels = 5

        # >>> RESOLUCIÓN DE PRECIO DE PACIFICA (2 DECIMALES) <<<
        self.pacifica_tick = 0.01

        self.tpsl_retry_n        = int(os.getenv("ENGINE_TPSL_RETRY_N", "5"))
        self.tpsl_retry_delay_ms = int(os.getenv("ENGINE_TPSL_RETRY_DELAY_MS", "250"))

        self.mode_target = mode.upper()

        print(f"[ENGINE] cfg qty={self.trade_base_qty} mode={self.mode_target} enable_tpsl=SIEMPRE_ACTIVO (Espejo Mode) 🔄")
        print(f"[ENGINE] Fail-Safe Slip: {TPSL_FAILSAFE_PCT*100:.2f}% | PacTick: {self.pacifica_tick}")

        self.pac = pac
        self.lig = lig
        base, q = _parse_symbol(symbol or SYMBOL_DEFAULT, quote_fallback=quote or QUOTE_DEFAULT)
        self.symbol_base = base
        self.quote = q

        # === DYNAMIC CPU AFFINITY ===
        if ENGINE_CPU_CORE and hasattr(os, "sched_setaffinity"):
            try:
                core_id = int(ENGINE_CPU_CORE)
                os.sched_setaffinity(0, {core_id})
                print(f"🚀 [HFT] Process Soft-Pinned to CPU Core: {core_id} (PID: {os.getpid()})")
            except Exception as e:
                print(f"⚠️ [HFT] Could not pin process to Core {ENGINE_CPU_CORE}: {e}")

        self.state: str = "idle"
        self._stop = threading.Event()
        self._thr: Optional[threading.Thread] = None

        try:
            env_val = int(ENGINE_MAX_PAIRS_ENV) if ENGINE_MAX_PAIRS_ENV else None
        except Exception:
            env_val = None
        _limit = 1 if self.engine_one_shot else env_val
        self.max_pairs = None if (_limit is None or _limit <= 0) else int(_limit)
        self.pairs_executed_total = 0
        self._last_pair_time_ms = 0.0

        # === SELECCIÓN DINÁMICA DE CLIENTE ===
        if self.mode_target == "BINANCE":
            self._pac_ctor = _resolve_client_ctor("exchanges.binance_adapter", ("BinanceAdapter",))
        else:
            self._pac_ctor = _resolve_client_ctor("exchanges.pacifica_api", ("PacificaAPI", "PacificaClient"))

        self._lig_ctor = _resolve_client_ctor("exchanges.lighter_api", ("LighterAPI", "LighterClient"))
        self._pac_client = None
        self._lig_client = None

        # status.py -> thread + Event (para no pagar el costo en el loop)
        try:
            self.status_checker = SystemStatus()
        except Exception as e:
            print(f"⚠️ Error al iniciar SystemStatus: {e}")
            self.status_checker = None

        self._system_ready = threading.Event()
        self._system_ready.set()  # si no hay checker, queda siempre listo

        self.open_active: bool = False
        self.open_direction: Optional[str] = None
        self.open_qty: Optional[float] = None
        self.pac_open_side: Optional[str] = None
        self.lig_open_side_sent: Optional[str] = None

        self._master_tp: Optional[float] = None
        self._master_sl: Optional[float] = None
        self.direction: Optional[str] = None
        self._grid_levels = []

        self._open_in_flight = False
        self._close_in_flight = False
        self._close_sent_once = False

        self.active_slots = []
        self._last_oco_check_ms = 0.0

        self._notify_enabled = os.getenv("TELEGRAM_NOTIFICATIONS", "true").lower() == "true"
        self._tg = TelegramNotifier()
        self._pnl_tracker = CyclePnlTracker()

        # lock corto solo para flags críticos
        self._state_lock = threading.Lock()
        self.cooldown_until = 0.0
        
        # Pool fijo de hilos pre-calentados (HFT)
        self.exec_pool = ExecutionThreadPool(num_threads=4)

        # --- SIMULATION UDP LISTENER ---
        self._sim_override_type = None
        self._sim_override_until = 0.0
        self._sim_sock = None
        try:
            self._sim_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sim_sock.bind(("127.0.0.1", 5555))
            self._sim_sock.settimeout(0.5)
            threading.Thread(target=self._sim_listener, daemon=True).start()
            print("[ENGINE] UDP Edge Simulator listener on port 5555 started.")
        except Exception as e:
            print(f"[ENGINE] UDP Edge Simulator start failed: {e}")

    @property
    def mode(self) -> str:
        return "CLOSE" if self.open_active else "OPEN"

    def _sim_listener(self):
        while not self._stop.is_set():
            if not self._sim_sock:
                break
            try:
                data, _ = self._sim_sock.recvfrom(1024)
                cmd = data.decode("utf-8").strip().upper()
                if cmd == "OPEN":
                    print("\n👽 [SIMULATOR] OPEN command received! Injecting massive API Edge for 1s...")
                    self._sim_override_type = "OPEN"
                    self._sim_override_until = time.time() + 1.0
                elif cmd == "CLOSE":
                    print("\n👽 [SIMULATOR] CLOSE command received! Injecting massive close spread for 1s...")
                    self._sim_override_type = "CLOSE"
                    self._sim_override_until = time.time() + 1.0
            except socket.timeout:
                pass
            except Exception:
                pass

    def _base_qty(self) -> float:
        return _round_step(self.trade_base_qty, self.base_qty_step)

    def _now_ms(self) -> float:
        if USE_PERF_COUNTER_LOOP:
            return time.perf_counter() * 1000.0
        return time.time() * 1000.0

    def _best_bid_ask_safe(self, feed) -> Tuple[Optional[float], Optional[float]]:
        # --- SIMULATION OVERRIDE ---
        if time.time() < self._sim_override_until:
            if self._sim_override_type == "OPEN":
                if feed == self.pac:
                    return 100.0, 100.0
                elif feed == self.lig:
                    return 150.0, 150.0
            elif self._sim_override_type == "CLOSE":
                if feed == self.pac:
                    return 150.0, 100.0
                elif feed == self.lig:
                    return 150.0, 100.0

        bb = getattr(feed, "best_bid", None)
        ba = getattr(feed, "best_ask", None)
        if bb is not None and ba is not None:
            return bb, ba
        return _best_from_book(feed)

    @staticmethod
    def _edge_p2l(pac_ask: float, lig_bid: float) -> Optional[float]:
        if _hft_lib:
            res = _hft_lib.check_edge_p2l(pac_ask or 0.0, lig_bid or 0.0)
            return res if res > -9000.0 else None
        if pac_ask and lig_bid:
            edge = (lig_bid - pac_ask) / pac_ask
            return edge
        return None

    @staticmethod
    def _edge_l2p(lig_ask: float, pac_bid: float) -> Optional[float]:
        if _hft_lib:
            res = _hft_lib.check_edge_l2p(lig_ask or 0.0, pac_bid or 0.0)
            return res if res > -9000.0 else None
        if lig_ask and pac_bid:
            edge = (pac_bid - lig_ask) / lig_ask
            return edge
        return None

    def _compute_grid_levels(self, direction: str, pac_bid: float, pac_ask: float, lig_bid: float, lig_ask: float):
        # [Eliminado: Lógica de Grid de Múltiples Niveles HFT Anterior]
        return 0.0, []

    def _maybe_build_clients(self) -> bool:
        if self.engine_paper:
            return True
        if not self.engine_allow_real:
            self.state = "real-disabled"
            return False
        if self._pac_client and self._lig_client:
            return True
        if not self._pac_ctor or not self._lig_ctor:
            return False
        try:
            self._pac_client = self._pac_ctor()
            self._lig_client = self._lig_ctor()
            self.state = "real-ready"
            return True
        except Exception as e:
            self.state = f"real-clients-error:{e!r}"
            return False

    def _warmup_clients(self):
        if self.engine_paper:
            return
        try:
            ok = self._maybe_build_clients()
            if VERBOSE:
                print(f"[ENGINE] warmup clients: {'OK' if ok else 'FAIL'}")
        except Exception as e:
            if VERBOSE:
                print(f"[ENGINE] warmup clients error: {e!r}")

    def _should_notify(self) -> bool:
        if self.engine_paper:
            return False
        if not self._notify_enabled:
            return False
        try:
            return self._tg.enabled()
        except Exception:
            return False

    def _tg_send_async(self, text: str):
        def _job():
            try:
                self._tg.send(text)
            except Exception:
                pass
        threading.Thread(target=_job, daemon=True).start()

    def _send_pac_market(self, side: str, qty: float, is_close: bool = False) -> OrderFill:
        if self.engine_paper:
            return OrderFill(True, "paper", filled_qty=qty, avg_px=None)
        if not self._maybe_build_clients():
            return OrderFill(False, "no-clients")
        try:
            _ = self._pac_client.place_market(symbol=self.symbol_base, side=side, base_qty=qty, is_close=is_close)
            return OrderFill(True, "ok", filled_qty=qty)
        except Exception as e:
            print(f"\n⚠️ [Pacifica OPEN ERROR] {e!r}")
            return OrderFill(False, f"{self.mode_target.lower()} exception: {e!r}")

    def _send_lig_market(self, side: str, qty: float, px_ref: float, is_close: bool = False) -> OrderFill:
        if self.engine_paper:
            return OrderFill(True, "paper", filled_qty=qty, avg_px=None)
        if not self._maybe_build_clients():
            return OrderFill(False, "no-clients")
        try:
            res = self._lig_client.place_market(symbol=self.symbol_base, side=side, qty_base=qty, avg_exec_px=px_ref, is_close=is_close)
            return OrderFill(res.get("accepted", False), "ok", filled_qty=qty, raw=res)
        except Exception as e:
            print(f"\n⚠️ [Lighter OPEN ERROR] {e!r}")
            return OrderFill(False, f"lighter exception: {e!r}")

    # =========================
    # TP/SL SETTERS
    # =========================
    def _set_pac_tpsl_after_open(self, direction: str, pac_side: str, pac_bid: float, pac_ask: float, lig_bid: float, lig_ask: float) -> None:
        if VERBOSE:
            print(f"\\n[DEBUG] >>> HILO TP/SL HYBRID {self.mode_target} (Futures) vs Lighter (Spot) INICIADO. Esperando 1.5s...")
        if self.engine_paper or (not self.enable_tpsl):
            print("[DEBUG] TP/SL omitidos por Paper o Desactivado.")
            return
        if not self._maybe_build_clients():
            print("[DEBUG] TP/SL omitidos por falta de clientes.")
            return
        time.sleep(1.5)
        
        # 1. Mapeo de Direcciones
        is_target_long = direction.endswith("->Lig") 
        
        if is_target_long:
            # Apertura fue: pac_side="BUY", lig_side="SELL"
            lig_exit = "BUY"    # Recomprar Spot
            pac_exit = "SELL"   # Vender Futuros
            
            avg_entry = (pac_ask + lig_bid) / 2.0
            
            # Ganamos cuando Pacifica Sube (TP arriba), SL Abajo
            tp_px = avg_entry * 1.01  # +1% fijo
            sl_px = avg_entry * 0.99  # -1% fijo
                    
        else:
            # Apertura fue: pac_side="SELL", lig_side="BUY"
            lig_exit = "SELL"   # Vender Spot
            pac_exit = "BUY"    # Recomprar Futuros
            
            avg_entry = (pac_bid + lig_ask) / 2.0
            
            # Ganamos cuando Pacifica Baja (TP abajo), SL Arriba
            tp_px = avg_entry * 0.99  # -1% fijo
            sl_px = avg_entry * 1.01  # +1% fijo
            
        qty = self.open_qty
        
        # Aplicamos el limit offset de tu engine original para forzar llenar la Limit
        # tp_limit_offset se restaba a las ventas y sumaba a las compras empujándolo a mercado.
        # Pero usamos el mismo precio para ambas plataformas como solicitaste.
        
        # Pacifica Tick es configurado en env
        tick = self.pacifica_tick
        p_tp = _round_px(tp_px, tick)
        p_sl = _round_px(sl_px, tick)
        
        if pac_exit == "BUY":
            # Pacifica Short-Close -> Comprar (TP abajo, SL arriba)
            pac_tp_px, pac_sl_px = min(p_tp, p_sl), max(p_tp, p_sl)
            # Lighter Long-Close -> Vender (TP arriba, SL abajo)
            lig_tp_px, lig_sl_px = max(p_tp, p_sl), min(p_tp, p_sl)
        else:
            # Pacifica Long-Close -> Vender (TP arriba, SL abajo)
            pac_tp_px, pac_sl_px = max(p_tp, p_sl), min(p_tp, p_sl)
            # Lighter Short-Close -> Comprar (TP abajo, SL arriba)
            lig_tp_px, lig_sl_px = min(p_tp, p_sl), max(p_tp, p_sl)

        self._master_tp = pac_tp_px
        self._master_sl = pac_sl_px
        self._lig_tp_px = lig_tp_px
        self._lig_sl_px = lig_sl_px

        if VERBOSE:
            print(f"[TP/SL] Ejecutando: Cantidad={qty} | PAC_Exit_Dir={pac_exit} | LIG_Exit_Dir={lig_exit}")
        if VERBOSE:
            print(f"[TP/SL] Pacifica: TP={pac_tp_px} | SL={pac_sl_px}")
            print(f"[TP/SL] Lighter:  TP={lig_tp_px} | SL={lig_sl_px}")

        # 2. Despacho Pacifica (FUTUROS) -> TP Limit y SL Stop
        tp_id, sl_id = None, None
        try:
            res_p_tp = self._pac_client.place_limit(self.symbol_base, pac_exit, qty, pac_tp_px)
            tp_id = res_p_tp.get("data", {}).get("order_id")
            print(f"✅ [Pacifica TP Limit] Enviado -> {res_p_tp}")
        except Exception as e:
            print(f"⚠️ [Pacifica TP Limit] Falló envío: {e!r}")
            
        try:
            res_p_sl = self._pac_client.place_stop(self.symbol_base, pac_exit, qty, pac_sl_px)
            sl_id = res_p_sl.get("data", {}).get("order_id")
            print(f"✅ [Pacifica SL Stop] Enviado -> {res_p_sl}")
        except Exception as e:
            print(f"⚠️ [Pacifica SL Stop] Falló envío: {e!r}")

        with self._state_lock:
            self.active_slots.append({
                "pac_tp_id": tp_id,
                "pac_sl_id": sl_id,
                "lig_open_side": getattr(self, "lig_open_side_sent", "BUY"),
                "lig_qty": qty,
                "lig_sl_px": lig_sl_px,
                "pac_exit": pac_exit
            })

        # 3. Despacho Lighter (SPOT) -> Solo enviamos TP Limit. (Lighter no soporta SL Stops).
        try:
            res_l_tp = self._lig_client.place_limit(self.symbol_base, lig_exit, qty, lig_tp_px)
            print(f"✅ [Lighter TP Limit] Enviado a Lighter en el precio {lig_tp_px}")
        except Exception as e:
            print(f"⚠️ [Lighter TP Limit] Falló envío: {e!r}")
            
        print("⚠️ [Lighter SL] OMITIDO: Lighter Spot no soporta Stop-Loss nativos condicionales.")

    def _set_lig_tpsl_after_open(self, direction: str, lig_side: str, pac_bid: float, pac_ask: float, lig_bid: float, lig_ask: float) -> None:
        return

    # =========================================================================
    # 🔔 NOTIFICACIONES DETALLADAS (Helper)
    # =========================================================================
    def _notify_execution(self, dex_name: str, action: str, func, *args) -> OrderFill:
        fill = func(*args)
        if self._should_notify():
            icon = "✅" if fill.ok else "⚠️"
            qty_str = f"{fill.filled_qty:.4f}" if fill.filled_qty else "?"
            msg = f"{icon} <b>{dex_name} {action}</b>: {fill.msg} ({qty_str})"
            self._tg_send_async(msg)
        return fill

    # =========================================================================
    # HOT PATH: APERTURA / CIERRE (optimizados)
    # =========================================================================
    def _open_once(self, direction: str, pac_bid: float, pac_ask: float, lig_bid: float, lig_ask: float):
        self.direction = direction

        with self._state_lock:
            if self._open_in_flight or self.open_active:
                return
            self._open_in_flight = True
            self._master_tp, self._master_sl = None, None

        qty = self._base_qty()
        self.open_qty, self.open_direction = qty, direction

        # Lógica genérica: Si direction termina en "->Lig" es compra en target
        is_target_long = direction.endswith("->Lig")

        pac_side, lig_side = ("BUY", "SELL") if is_target_long else ("SELL", "BUY")
        lig_px_ref = lig_bid if is_target_long else lig_ask
        pac_px_ref = pac_ask if is_target_long else pac_bid

        self.pac_open_side, self.lig_open_side_sent = pac_side, lig_side

        # 1) aviso oportunidad (NO bloqueante)
        if self._should_notify():
            try:
                spread_pct = abs(pac_px_ref - lig_px_ref) / pac_px_ref * 100
            except Exception:
                spread_pct = 0.0
            self._tg_send_async(
                f"🚀 <b>OPORTUNIDAD ({self.mode_target}): {direction}</b>\n"
                f"Spread: {spread_pct:.3f}%\n"
                f"Qty: {qty} | Ejecutando..."
            )

        # 2) ATAQUE: enviar ambas órdenes YA al pool pre-calentado
        self.exec_pool.submit(self._send_pac_market, pac_side, qty)
        self.exec_pool.submit(self._send_lig_market, lig_side, qty, lig_px_ref)

        # marcamos abierto inmediatamente (igual que tu intención: velocidad > confirmación)
        with self._state_lock:
            self._open_in_flight = False

        # 3) confirmación apertura (async)
        if self._should_notify():
            self._tg_send_async("🟢 <b>POSICIÓN ABIERTA</b>\nEsperando TP/SL o Cierre por Spread...\n⏳ Iniciando cooldown de 10 minutos.")

        # 4) TP/SL en background (Solo Pacifica activo)
        threading.Thread(
            target=self._set_pac_tpsl_after_open,
            args=(direction, pac_side, pac_bid, pac_ask, lig_bid, lig_ask),
            daemon=True
        ).start()

        # contabilización de pares (respeta el engine original)
        self.pairs_executed_total += 1
        self._last_pair_time_ms = self._now_ms()
        
        # INICIAMOS EL TIMER DE 10 MINUTOS TRAS ABRIR
        self.cooldown_until = time.time() + 600.0

    def _close_once(self, pac_bid: float, pac_ask: float, lig_bid: float, lig_ask: float):
        with self._state_lock:
            if self._close_in_flight or self._close_sent_once:
                return
            if not self.open_active:
                return
            self._close_in_flight, self._close_sent_once = True, True

        qty = self.open_qty or self._base_qty()

        pac_close, lig_close = _invert_side(self.pac_open_side), _invert_side(self.lig_open_side_sent)
        lig_px_ref = lig_ask if lig_close == "BUY" else lig_bid

        # 1) aviso cierre (no bloqueante)
        if self._should_notify():
            self._tg_send_async("🔻 <b>CERRANDO POSICIÓN...</b>")

        # 2) ATAQUE: enviar cierres en paralelo al pool pre-calentado
        self.exec_pool.submit(self._notify_execution, self.mode_target, "CLOSE", self._send_pac_market, pac_close, qty, True)
        self.exec_pool.submit(self._notify_execution, "Lighter", "CLOSE", self._send_lig_market, lig_close, qty, lig_px_ref, True)

        # cancel-all en background (lento)
        def _cancel_job():
            try:
                if not self.engine_paper and self._maybe_build_clients():
                    if self._pac_client:
                        self._pac_client.cancel_all_orders(self.symbol_base)
                    if self._lig_client:
                        self._lig_client.cancel_all_orders(self.symbol_base)
            except Exception:
                pass

        threading.Thread(target=_cancel_job, daemon=True).start()

        with self._state_lock:
            self.open_active = False
            self._close_in_flight = False

        if self._should_notify():
            self._tg_send_async("🏁 <b>APERTURA COMPLETADA</b>\nLímites listos. Cooldown de 10 Minutos activado...")
            
        self.cooldown_until = time.time() + 600.0  # 10 minutos (600 segundos)

    # =========================================================================
    # STATUS THREAD REMOVIDO PARA PERMITIR APERTURAS LIBRES
    # =========================================================================
    def _status_loop(self):
        # Siempre listo. Ignoramos posiciones previas.
        self._system_ready.set()
        return
    # LOOP
    # =========================================================================
    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._stop.clear()

        if WARMUP_CLIENTS_ON_START:
            self._warmup_clients()

        threading.Thread(target=self._status_loop, daemon=True).start()

        self._thr = threading.Thread(target=self._loop, daemon=True)
        self._thr.start()
        if VERBOSE:
            print(f"[ENGINE] running (low-latency hot path) | MODE: {self.mode_target}")

    def stop(self):
        self._stop.set()

    def _loop(self):
        self.state = "running"
        base_sleep = 0.0 if EDGE_NO_SLEEP else max(0.0, LOOP_DELAY_MS / 1000.0)
        last_tel_t = 0 # Telemetry throttle

        # Label para identificar target (Pac o Bin)
        lbl = "Bin" if self.mode_target == "BINANCE" else "Pac"

        while not self._stop.is_set():
            try:
                # throttle entre pares (si no est?? deshabilitado)
                if (not DISABLE_GAP_THROTTLE) and self._last_pair_time_ms:
                    if (self._now_ms() - self._last_pair_time_ms) < float(GAP_BETWEEN_PAIRS_MS):
                        time.sleep(0 if EDGE_NO_SLEEP else min(base_sleep, 0.01))
                        continue

                pac_bid, pac_ask = self._best_bid_ask_safe(self.pac)
                lig_bid, lig_ask = self._best_bid_ask_safe(self.lig)

                if None in (pac_bid, pac_ask, lig_bid, lig_ask):
                    time.sleep(0 if EDGE_NO_SLEEP else base_sleep)
                    continue

                # Telemetry timing
                now_s = time.time()
                do_tel = (now_s - last_tel_t) >= 1.0

                # =========================
                # APERTURA (SCAN)
                # =========================
                is_scanning = (len(self.active_slots) < 8) and (now_s > getattr(self, "cooldown_until", 0.0))

                if is_scanning and (not self._open_in_flight) and (self.pairs_executed_total < (self.max_pairs or 999999)):
                    e_p2l = self._edge_p2l(pac_ask, lig_bid)
                    e_l2p = self._edge_l2p(lig_ask, pac_bid)
                    
                    if do_tel:
                        last_tel_t = now_s
                        p2l_s = f"{e_p2l*100:+.4f}%" if e_p2l is not None else "None"
                        l2p_s = f"{e_l2p*100:+.4f}%" if e_l2p is not None else "None"
                        sys.stdout.write(f"\r[SCAN] P2L: {p2l_s} | L2P: {l2p_s} | Target: {EDGE_THRESHOLD*100:.4f}% | Slots: {len(self.active_slots)}/8   ")
                        sys.stdout.flush()

                    if e_p2l is not None and e_p2l >= EDGE_THRESHOLD:
                        self._open_once(f"{lbl}->Lig", pac_bid, pac_ask, lig_bid, lig_ask)
                    elif e_l2p is not None and e_l2p >= EDGE_THRESHOLD:
                        self._open_once(f"Lig->{lbl}", pac_bid, pac_ask, lig_bid, lig_ask)

                # =========================
                # MANTENIMIENTO (HOLDING & OCO)
                # =========================
                if self.active_slots:
                    if not is_scanning and do_tel:
                        last_tel_t = now_s
                        sys.stdout.write(f"\r[HOLDING] {len(self.active_slots)}/8 posiciones abiertas | OCO Tracking...   ")
                        sys.stdout.flush()

                    # OCO Checker de Pacifica (cada 3 segundos)
                    if now_s * 1000 - self._last_oco_check_ms > 3000:
                        self._last_oco_check_ms = now_s * 1000
                        try:
                            if getattr(self, "_pac_client", None):
                                open_orders = self._pac_client.get_open_orders()
                                active_ids = {str(o.get("order_id")) for o in open_orders}
                                
                                for slot in list(self.active_slots):
                                    tp_id = str(slot.get("pac_tp_id"))
                                    sl_id = str(slot.get("pac_sl_id"))
                                    
                                    # Si no encontramos el TP en el libro abierto, se llenó!
                                    if tp_id != "None" and tp_id not in active_ids:
                                        print("\n✅ [OCO] Pacifica TP llenado! Cancelando SL sobrante y cerrando slot.")
                                        try: self._pac_client.cancel_stop_order(self.symbol_base, sl_id)
                                        except: pass
                                        with self._state_lock:
                                            if slot in self.active_slots: self.active_slots.remove(slot)
                                        
                                    # Si no encontramos el SL en el libro abierto, se llenó!
                                    elif sl_id != "None" and sl_id not in active_ids:
                                        print("\n✅ [OCO] Pacifica SL llenado! Cancelando TP sobrante y cerrando slot.")
                                        try: self._pac_client.cancel_order(self.symbol_base, tp_id)
                                        except: pass
                                        with self._state_lock:
                                            if slot in self.active_slots: self.active_slots.remove(slot)
                        except Exception as e:
                            pass

                    # SOFTWARE STOP-LOSS ESPÍA PARA LIGHTER (SPOT)
                    for slot in list(self.active_slots):
                        side_forced = _invert_side(slot.get("lig_open_side", "BUY"))
                        triggered = False
                        
                        if side_forced == "BUY":
                            if lig_ask >= slot.get("lig_sl_px", float("inf")):
                                print(f"\n🚨 [Lighter SL Emulado] ASK {lig_ask} cruzó SL {slot['lig_sl_px']}! Recomprando Pánico...")
                                self.exec_pool.submit(self._send_lig_market, "BUY", slot["lig_qty"], lig_ask, True)
                                triggered = True
                                
                        elif side_forced == "SELL":
                            if lig_bid <= slot.get("lig_sl_px", 0.0):
                                print(f"\n🚨 [Lighter SL Emulado] BID {lig_bid} destrozó SL {slot['lig_sl_px']}! Vendiendo Pánico...")
                                self.exec_pool.submit(self._send_lig_market, "SELL", slot["lig_qty"], lig_bid, True)
                                triggered = True
                                
                        if triggered:
                            # 1. Aseguramos borrar cualquier orden nativa límite colgante de Pacifica
                            # para este slot en caso de que Lighter SL saltara antes que la de Pacifica.
                            try:
                                if slot.get("pac_tp_id") != "None":
                                    self._pac_client.cancel_order(self.symbol_base, slot["pac_tp_id"])
                                if slot.get("pac_sl_id") != "None":
                                    self._pac_client.cancel_stop_order(self.symbol_base, slot["pac_sl_id"])
                            except Exception:
                                pass
                                
                            # 2. Eliminamos el Tracking de Memoria OCO
                            with self._state_lock:
                                if slot in self.active_slots: self.active_slots.remove(slot)

                # Sleep normal
                time.sleep(base_sleep)

            except Exception:
                import traceback
                traceback.print_exc()
                time.sleep(0)

        self.state = "stopped"
