# streamlit_app.py
import os
import threading
import asyncio
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from os import getenv
from streamlit_autorefresh import st_autorefresh

from feeds.pacifica import PacificaFeed
from feeds.lighter import LighterFeed
from trader.engine import ArbEngine


# ===================== CARGA .ENV (robusta) =====================
def _load_env_robust():
    # 1) Intenta cargar el .env "normal" (CWD)
    load_dotenv(override=False)

    # 2) Si no encuentra variables clave, busca .env hacia arriba desde este archivo
    if os.getenv("TELEGRAM_BOT_TOKEN") is None and os.getenv("TRADE_BASE_QTY") is None:
        here = Path(__file__).resolve()
        for up in (here.parent, *here.parents):
            envp = up / ".env"
            if envp.exists():
                load_dotenv(dotenv_path=envp, override=True)
                break


_load_env_robust()


# ===================== ENV =====================
SYMBOL = getenv("SYMBOL", "ETH")
QUOTE = getenv("QUOTE", "USDT")
LEVELS = int(getenv("LEVELS", "10"))

EDGE_THRESHOLD = float(getenv("EDGE_THRESHOLD", "0.002"))  # fracción (OPEN)
CLOSE_EDGE_TOL = float(getenv("CLOSE_EDGE_TOL", "0.0009"))  # legacy (no gobierna tu nueva lógica)
CLOSE_EDGE_TARGET = float(getenv("CLOSE_EDGE_TARGET", "0.0003"))  # NUEVO: target de cierre por spread contrario

LIGHTER_WS_URL = getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
PACIFICA_WS_URL = getenv("PACIFICA_WS_URL", "wss://ws.pacifica.fi/ws")
UI_REFRESH_MS = int(getenv("UI_REFRESH_MS", "500"))

lighter_index_env = getenv("LIGHTER_MARKET_INDEX")
try:
    LIGHTER_MARKET_INDEX = int(lighter_index_env) if lighter_index_env is not None else 0
except ValueError:
    LIGHTER_MARKET_INDEX = 0

st.set_page_config(page_title="Arb Panel", layout="wide")


# ===================== BG RUNNER =====================
def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


@st.cache_resource(show_spinner=False)
def start_data_workers(symbol: str, quote: str, levels: int, lighter_index: int):
    pf = PacificaFeed(symbol, quote, levels)
    lf = LighterFeed(symbol, quote, levels, market_index=lighter_index)
    t1 = threading.Thread(target=_run_async, args=(pf.connect(),), daemon=True)
    t2 = threading.Thread(target=_run_async, args=(lf.connect(),), daemon=True)
    t1.start()
    t2.start()
    return pf, lf


pf, lf = start_data_workers(SYMBOL, QUOTE, LEVELS, LIGHTER_MARKET_INDEX)


# ===================== ENGINE (SINGLETON REAL) =====================
@st.cache_resource(show_spinner=False)
def get_engine_singleton(_pf: PacificaFeed, _lf: LighterFeed, symbol: str, quote: str) -> ArbEngine:
    engine = ArbEngine(pac=_pf, lig=_lf, symbol=symbol, quote=quote)
    engine.start()
    return engine


engine = get_engine_singleton(pf, lf, SYMBOL, QUOTE)


# ===================== UI =====================
st.markdown(
    """
<style>
.smallcaps {font-size: 0.9rem; color: #888;}
.badge {padding: 2px 8px; border-radius: 999px; font-size: 0.85rem;}
.badge-ok {background: #e9ffe9; color: #1b7f1b; border: 1px solid #bdf2bd;}
.badge-warn {background: #fff6e6; color: #8a5a00; border: 1px solid #ffd9a1;}
.badge-info {background: #eef5ff; color: #0b4ea2; border: 1px solid #b8d4ff;}
</style>
""",
    unsafe_allow_html=True,
)

st.title(f"Arbitraje: {SYMBOL}-{QUOTE}")
st.caption(
    f"<span class='smallcaps'>Pacifica WS:</span> {PACIFICA_WS_URL}  |  "
    f"<span class='smallcaps'>Lighter WS:</span> {LIGHTER_WS_URL}  |  "
    f"<span class='smallcaps'>Lighter MARKET_INDEX:</span> {LIGHTER_MARKET_INDEX}",
    unsafe_allow_html=True,
)

st.sidebar.header("Motor (AUTO)")
st.sidebar.markdown(f"**EDGE_THRESHOLD (OPEN):** `{EDGE_THRESHOLD}`  ({EDGE_THRESHOLD*100:.4f}%)")
st.sidebar.markdown(f"**CLOSE_EDGE_TARGET (CLOSE):** `{CLOSE_EDGE_TARGET}`  ({CLOSE_EDGE_TARGET*100:.4f}%)")
st.sidebar.markdown(f"~~CLOSE_EDGE_TOL (legacy)~~: `{CLOSE_EDGE_TOL}`  ({CLOSE_EDGE_TOL*100:.4f}%)")
st.sidebar.caption("ℹ️ El cierre ahora se evalúa por el *spread contrario* vs CLOSE_EDGE_TARGET.")
st.sidebar.markdown("---")

# Estado del motor (auto)
mode = getattr(engine, "mode", "—")
open_active = getattr(engine, "open_active", False)
open_dir = getattr(engine, "open_direction", None)
pairs_done = getattr(engine, "pairs_executed_total", 0)
state = getattr(engine, "state", "—")

st.sidebar.metric("MODE", mode)
st.sidebar.metric("open_active", str(open_active))
st.sidebar.metric("open_direction", str(open_dir))
st.sidebar.metric("pairs_executed_total", str(pairs_done))
st.sidebar.caption(f"state: {state}")

# ===================== TELEGRAM (DIAGNÓSTICO + TEST) =====================
st.sidebar.markdown("---")
st.sidebar.subheader("Telegram")

tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

st.sidebar.write(f"TELEGRAM_BOT_TOKEN: {'✅' if tg_token else '❌'}")
st.sidebar.write(f"TELEGRAM_CHAT_ID: {'✅' if tg_chat else '❌'}")

# Botón de test (no toca la lógica del engine; solo prueba envío)
if st.sidebar.button("📨 Test Telegram", use_container_width=True):
    try:
        from notifications.telegram_notifier import TelegramNotifier

        n = TelegramNotifier()
        ok = n.send("✅ Test Telegram desde Streamlit (arb-panel)")
        if ok:
            st.sidebar.success("Enviado OK")
        else:
            st.sidebar.error("Falló el envío (revisar token/chat_id)")
    except Exception as e:
        st.sidebar.error(f"Error: {e!r}")

st_autorefresh(interval=UI_REFRESH_MS, key="__tick__")


# ===================== HELPERS =====================
def df_from_pairs(pairs, levels):
    pairs = list(pairs[:levels])
    if len(pairs) < levels:
        pairs += [("", "")] * (levels - len(pairs))
    return pd.DataFrame(
        {
            "Precio": [p if p != "" else "" for p, _ in pairs],
            "Cantidad": [q if q != "" else "" for _, q in pairs],
        }
    )


def best_prices(book):
    bb = max((p for p, _ in book.bids), default=None)
    ba = min((p for p, _ in book.asks), default=None)
    return bb, ba


def fmt_pct(x):
    return f"{x*100:.4f}%" if x is not None else "—"


def fmt_rule_target(x):
    return f"{x*100:.4f}%"


# ===================== ORDERBOOKS =====================
cL, cR = st.columns(2, gap="large")
with cL:
    st.subheader("Pacifica")
    a, b = st.columns(2)
    with a:
        st.caption("**Bids (compras)**")
        st.dataframe(df_from_pairs(pf.book.bids, LEVELS), width="stretch", height=420)
    with b:
        st.caption("**Asks (ventas)**")
        st.dataframe(df_from_pairs(pf.book.asks, LEVELS), width="stretch", height=420)

with cR:
    st.subheader("Lighter")
    a, b = st.columns(2)
    with a:
        st.caption("**Bids (compras)**")
        st.dataframe(df_from_pairs(lf.book.bids, LEVELS), width="stretch", height=420)
    with b:
        st.caption("**Asks (ventas)**")
        st.dataframe(df_from_pairs(lf.book.asks, LEVELS), width="stretch", height=420)

# ===================== SPREADS =====================
st.markdown("### Spreads en tiempo real")
sp_a, sp_b = st.columns(2)

pac_bid, pac_ask = best_prices(pf.book)
lig_bid, lig_ask = best_prices(lf.book)

# Definición actual de spreads (UI)
spread_A = (lig_bid - pac_ask) / pac_ask if (pac_ask and lig_bid) else None  # Pac→Lig (buy Pac ask / sell Lig bid)
spread_B = (pac_bid - lig_ask) / lig_ask if (lig_ask and pac_bid) else None  # Lig→Pac (buy Lig ask / sell Pac bid)

sp_a.info("Pac → Lig (buy Pac ask / sell Lig bid): " + fmt_pct(spread_A))
sp_b.info("Lig → Pac (buy Lig ask / sell Pac bid): " + fmt_pct(spread_B))

# ===================== NUEVO: REGLA VISUAL DE CIERRE (SIN TOCAR ENGINE) =====================
st.markdown("### Regla de CIERRE (visual)")
if open_active and open_dir == "Pac->Lig":
    # tu nueva regla: cierro cuando Lig→Pac >= target
    curr = spread_B
    ok = (curr is not None) and (curr >= CLOSE_EDGE_TARGET)
    badge = "<span class='badge badge-ok'>LISTO PARA CERRAR</span>" if ok else "<span class='badge badge-warn'>AÚN NO</span>"
    st.markdown(
        f"{badge} &nbsp; "
        f"Abierto: <code>Pac→Lig</code> → Cierra si <b>Lig→Pac</b> ≥ <code>{fmt_rule_target(CLOSE_EDGE_TARGET)}</code> | "
        f"Actual: <code>{fmt_pct(curr)}</code>",
        unsafe_allow_html=True,
    )
elif open_active and open_dir == "Lig->Pac":
    # tu nueva regla: cierro cuando Pac→Lig >= target
    curr = spread_A
    ok = (curr is not None) and (curr >= CLOSE_EDGE_TARGET)
    badge = "<span class='badge badge-ok'>LISTO PARA CERRAR</span>" if ok else "<span class='badge badge-warn'>AÚN NO</span>"
    st.markdown(
        f"{badge} &nbsp; "
        f"Abierto: <code>Lig→Pac</code> → Cierra si <b>Pac→Lig</b> ≥ <code>{fmt_rule_target(CLOSE_EDGE_TARGET)}</code> | "
        f"Actual: <code>{fmt_pct(curr)}</code>",
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        "<span class='badge badge-info'>SIN POSICIÓN ABIERTA</span> &nbsp; "
        "Esperando señal de apertura.",
        unsafe_allow_html=True,
    )

# ===================== DEBUG BOTONES =====================
st.markdown("### Forzar apertura (debug)")
c_forza, c_forzb = st.columns(2)

with c_forza:
    if st.button("Forzar **Pac → Lig** (Pac BUY / Lig SELL)", use_container_width=True):
        if pac_ask is None or lig_bid is None:
            st.error("No hay precios válidos (Pac ask o Lig bid).")
        else:
            res = engine._execute_pair_open(direction="Pac->Lig", buy_px=pac_ask, sell_px=lig_bid)
            if res.ok:
                st.success(f"✅ Forzado Pac→Lig enviado | buy_px={res.buy_px} sell_px={res.sell_px}")
            else:
                st.error(f"❌ Error al forzar Pac→Lig: {res.reason}")

with c_forzb:
    if st.button("Forzar **Lig → Pac** (Lig BUY / Pac SELL)", use_container_width=True):
        if lig_ask is None or pac_bid is None:
            st.error("No hay precios válidos (Lig ask o Pac bid).")
        else:
            res = engine._execute_pair_open(direction="Lig->Pac", buy_px=lig_ask, sell_px=pac_bid)
            if res.ok:
                st.success(f"✅ Forzado Lig→Pac enviado | buy_px={res.buy_px} sell_px={res.sell_px}")
            else:
                st.error(f"❌ Error al forzar Lig→Pac: {res.reason}")

st.markdown("### Forzar cierre (debug)")
if st.button("Forzar CIERRE de la operación abierta", use_container_width=True):
    res = engine._execute_pair_close()
    if res.ok:
        st.success("✅ Cierre forzado enviado")
    else:
        st.error(f"❌ No se pudo cerrar: {res.reason}")

# ===================== HISTORIAL (solo oportunidades visuales) =====================
if "op_history" not in st.session_state:
    st.session_state.op_history = []

now_s = datetime.now().strftime("%H:%M:%S")

# Nota: esto SOLO registra oportunidades en UI (no controla el engine)
if (not open_active) and spread_A is not None and spread_A >= EDGE_THRESHOLD and pac_ask and lig_bid:
    st.success(f"➡ (UI) Oportunidad Pac→Lig | Edge: {fmt_pct(spread_A)}")
    st.session_state.op_history.insert(
        0,
        {
            "ts": now_s,
            "tipo": "Pac→Lig",
            "buy_dex": "Pacifica",
            "buy_price": round(pac_ask, 6),
            "sell_dex": "Lighter",
            "sell_price": round(lig_bid, 6),
            "spread_%": round(spread_A * 100, 4),
        },
    )
elif (not open_active) and spread_B is not None and spread_B >= EDGE_THRESHOLD and lig_ask and pac_bid:
    st.success(f"⬅ (UI) Oportunidad Lig→Pac | Edge: {fmt_pct(spread_B)}")
    st.session_state.op_history.insert(
        0,
        {
            "ts": now_s,
            "tipo": "Lig→Pac",
            "buy_dex": "Lighter",
            "buy_price": round(lig_ask, 6),
            "sell_dex": "Pacifica",
            "sell_price": round(pac_bid, 6),
            "spread_%": round(spread_B * 100, 4),
        },
    )
else:
    st.info("Sin oportunidad UI por encima del umbral (o ya hay una operación abierta).")

st.markdown("### Historial de oportunidades (UI)")
if st.session_state.op_history:
    st.dataframe(pd.DataFrame(st.session_state.op_history), width="stretch", height=220)
else:
    st.info("Aún no hay oportunidades registradas.")

# ===================== PNL (proxy neto) =====================
fee_pct = EDGE_THRESHOLD * 100.0
pnl_total_pct = 0.0
for op in st.session_state.op_history:
    raw = op.get("spread_%")
    if isinstance(raw, (int, float)):
        net = raw - fee_pct
        if net > 0:
            pnl_total_pct += net

st.markdown("### PnL acumulado (proxy neto)")
m1, m2, m3 = st.columns(3)
with m1:
    st.metric("PNL total", f"{pnl_total_pct:.4f}%")
with m2:
    st.metric("Oportunidades (UI)", f"{len(st.session_state.op_history)}")
with m3:
    avg = (pnl_total_pct / len(st.session_state.op_history)) if st.session_state.op_history else 0.0
    st.metric("Promedio por op (proxy)", f"{avg:.4f}%")