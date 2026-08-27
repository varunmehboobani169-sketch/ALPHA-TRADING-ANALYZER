import json
import urllib.error
import urllib.request
from datetime import datetime

import pandas as pd
import streamlit as st

DHAN_API = "https://api.dhan.co/v2"

st.set_page_config(
    page_title="ALPHA • Vega Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------
# Styling
# -----------------------------
st.markdown(
    """
    <style>
    .block-container {padding-top: 1rem; padding-bottom: 2rem;}
    .small-muted {color:#8b949e;font-size:0.82rem;}
    .signal-box {padding:16px 18px;border-radius:14px;border:1px solid #30363d;background:#161b22;}
    .signal-title {font-size:0.78rem;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;}
    .signal-value {font-size:1.45rem;font-weight:700;margin-top:2px;}
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------
# Dhan helpers
# -----------------------------
def dhan_request(path, client_id, token, method="GET", body=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(DHAN_API + path, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Dhan HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def connect_box():
    st.sidebar.markdown("## 🔐 DHAN CONNECTION")
    st.sidebar.caption("Session-only credentials. They are not written to GitHub.")
    st.session_state.setdefault("dhan_client_id", "")
    st.session_state.setdefault("dhan_token", "")
    st.session_state.setdefault("dhan_connected", False)

    with st.sidebar.form("dhan_login", clear_on_submit=False):
        client_id = st.text_input(
            "Dhan Client ID / Username",
            value=st.session_state.dhan_client_id,
            placeholder="e.g. 1100xxxxxx",
        )
        token = st.text_input(
            "Dhan Access Token",
            value=st.session_state.dhan_token,
            type="password",
            placeholder="Paste current JWT token",
        )
        connect = st.form_submit_button(
            "🔗 CONNECT TO DHAN",
            use_container_width=True,
            type="primary",
        )

    if connect:
        client_id = client_id.strip()
        token = token.strip()
        if not client_id or not token:
            st.sidebar.error("Enter both Client ID and Access Token.")
        else:
            try:
                dhan_request("/profile", client_id, token)
                st.session_state.dhan_client_id = client_id
                st.session_state.dhan_token = token
                st.session_state.dhan_connected = True
                st.sidebar.success("✅ DHAN CONNECTED")
                st.rerun()
            except Exception as exc:
                st.session_state.dhan_connected = False
                st.sidebar.error("❌ Dhan connection failed")
                st.sidebar.caption(str(exc))

    if st.session_state.dhan_connected:
        st.sidebar.success(f"Connected: {st.session_state.dhan_client_id}")
        if st.sidebar.button("LOGOUT / CLEAR DHAN", use_container_width=True):
            st.session_state.dhan_client_id = ""
            st.session_state.dhan_token = ""
            st.session_state.dhan_connected = False
            st.rerun()
    else:
        st.sidebar.warning("Not connected to Dhan")


def dhan_credentials():
    return st.session_state.dhan_client_id, st.session_state.dhan_token


def expiry_list(client_id, token, security_id, segment):
    response = dhan_request(
        "/optionchain/expirylist",
        client_id,
        token,
        method="POST",
        body={"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment},
    )
    data = response.get("data", []) if isinstance(response, dict) else []
    return [str(x) for x in data]


def option_chain(client_id, token, security_id, segment, expiry):
    response = dhan_request(
        "/optionchain",
        client_id,
        token,
        method="POST",
        body={
            "UnderlyingScrip": int(security_id),
            "UnderlyingSeg": segment,
            "Expiry": expiry,
        },
    )
    return response.get("data", {}) if isinstance(response, dict) else {}


def orders(client_id, token):
    response = dhan_request("/orders", client_id, token)
    return response if isinstance(response, list) else response.get("data", response)


def trades(client_id, token):
    response = dhan_request("/trades", client_id, token)
    return response if isinstance(response, list) else response.get("data", response)


def positions(client_id, token):
    response = dhan_request("/positions", client_id, token)
    return response if isinstance(response, list) else response.get("data", response)


# -----------------------------
# Vega dashboard calculations
# -----------------------------
def chain_frame(chain_data):
    spot = float(chain_data.get("last_price", 0) or 0)
    oc = chain_data.get("oc", {}) or {}
    rows = []
    for strike_text, node in oc.items():
        try:
            strike = float(strike_text)
        except (TypeError, ValueError):
            continue
        ce = node.get("ce") or {}
        pe = node.get("pe") or {}
        ce_g = ce.get("greeks") or {}
        pe_g = pe.get("greeks") or {}
        rows.append(
            {
                "strike": strike,
                "CE Vega": float(ce_g.get("vega", 0) or 0),
                "PE Vega": float(pe_g.get("vega", 0) or 0),
                "Vega Difference": float(ce_g.get("vega", 0) or 0) - float(pe_g.get("vega", 0) or 0),
                "CE LTP": float(ce.get("last_price", 0) or 0),
                "PE LTP": float(pe.get("last_price", 0) or 0),
                "CE OI": float(ce.get("oi", 0) or 0),
                "PE OI": float(pe.get("oi", 0) or 0),
                "CE IV": float(ce.get("implied_volatility", 0) or 0),
                "PE IV": float(pe.get("implied_volatility", 0) or 0),
                "CE Security ID": str(ce.get("security_id", "")),
                "PE Security ID": str(pe.get("security_id", "")),
            }
        )
    frame = pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)
    return spot, frame


def nearest_atm(frame, spot):
    if frame.empty or not spot:
        return None
    return float(frame.iloc[(frame["strike"] - spot).abs().argmin()]["strike"])


def dashboard_metrics(frame, spot, width):
    atm = nearest_atm(frame, spot)
    if atm is None:
        return {}
    band = frame[(frame["strike"] >= atm - width) & (frame["strike"] <= atm + width)].copy()
    call_vega = band["CE Vega"].sum()
    put_vega = band["PE Vega"].sum()
    difference = call_vega - put_vega
    total = call_vega + put_vega
    return {
        "ATM": atm,
        "CALL VEGA": call_vega,
        "PUT VEGA": put_vega,
        "VEGA DIFFERENCE": difference,
        "TOTAL VEGA": total,
        "STRIKES": len(band),
        "BAND": band,
    }


def classify_signal(difference, prev_difference, continuous_count, trend_mode):
    if pd.isna(difference):
        return "WAIT"
    if prev_difference is None or pd.isna(prev_difference):
        return "WAIT"
    delta = difference - prev_difference
    direction = "BULLISH" if delta < 0 else "BEARISH" if delta > 0 else "NEUTRAL"
    if trend_mode != "BOTH" and direction != trend_mode:
        return "FILTERED"
    return direction if continuous_count <= 0 else f"{direction} • COUNT {continuous_count + 1}"


# -----------------------------
# Sidebar controls
# -----------------------------
connect_box()

st.sidebar.markdown("## ⚙️ STRATEGY SETTINGS")
index_options = {
    "NIFTY 50": (13, "IDX_I"),
}
index_name = st.sidebar.selectbox("Index", list(index_options.keys()))
security_id_default, segment_default = index_options[index_name]
security_id = st.sidebar.number_input("Underlying Security ID", min_value=1, value=security_id_default, step=1)
segment = st.sidebar.selectbox("Underlying Segment", [segment_default, "NSE_FNO", "IDX_I"])

entry_time = st.sidebar.time_input("Entry Time", value=datetime.strptime("09:20", "%H:%M").time())
square_off_time = st.sidebar.time_input("Square-Off Time", value=datetime.strptime("15:20", "%H:%M").time())
expiry_choice = st.sidebar.selectbox("Expiry", ["NEAREST", "NEXT"])
strike_mode = st.sidebar.selectbox("Trade Strike", ["ATM", "ITM", "OTM"])
lots = st.sidebar.number_input("Lots", min_value=1, value=1, step=1)
mode = st.sidebar.radio("Trading Mode", ["PAPER", "LIVE"], horizontal=True)

st.sidebar.markdown("### Trade logic")
track_sl = st.sidebar.toggle("Track Stop Loss", value=True)
continuous_count = st.sidebar.number_input("Continuous Count", min_value=0, value=2, step=1)
exit_reversal = st.sidebar.toggle("Exit on Reversal", value=True)
trend_mode = st.sidebar.selectbox("Trade Trend", ["BOTH", "BULLISH", "BEARISH"])
max_profit = st.sidebar.number_input("Maximum Profit (₹)", min_value=0.0, value=0.0, step=100.0)
max_loss = st.sidebar.number_input("Maximum Loss (₹)", min_value=0.0, value=0.0, step=100.0)

st.sidebar.markdown("### No-trade range")
no_trade_enabled = st.sidebar.toggle("Enable No-Trade Range", value=False)
range_low = st.sidebar.number_input("Range Low", min_value=0.0, value=0.0, step=50.0)
range_high = st.sidebar.number_input("Range High", min_value=0.0, value=0.0, step=50.0)

# -----------------------------
# Header
# -----------------------------
st.title("📊 ALPHA • Vega Dashboard")
st.caption(
    "Live Dhan option-chain monitor based on the Vega Difference workflow: combined-strike Vega, continuous confirmation, reversal handling and configurable trade controls."
)

if mode == "LIVE":
    st.warning(
        "LIVE mode is an execution switch, not a backtest. Dhan currently requires Static IP whitelisting for order placement/modification/cancellation APIs, so hosted deployments may not be able to execute orders unless the deployment IP is configured accordingly."
    )

if not st.session_state.dhan_connected:
    st.info("Connect your Dhan account from the left sidebar to load the live dashboard.")
    st.stop()

client_id, token = dhan_credentials()

# -----------------------------
# Load live chain
# -----------------------------
col_refresh, col_note = st.columns([1, 4])
with col_refresh:
    refresh = st.button("🔄 REFRESH NOW", use_container_width=True, type="primary")
with col_note:
    st.markdown("<div class='small-muted'>Dhan Option Chain API provides LTP, OI, Greeks, volume and IV across strikes. The dashboard uses the live chain response as its source.</div>", unsafe_allow_html=True)

if "expiry_cache" not in st.session_state or refresh:
    try:
        st.session_state.expiry_cache = expiry_list(client_id, token, security_id, segment)
    except Exception as exc:
        st.error(f"Unable to load expiries: {exc}")
        st.stop()

expiries = st.session_state.get("expiry_cache", [])
if not expiries:
    st.error("No active option expiries returned by Dhan.")
    st.stop()

expiry_index = 0 if expiry_choice == "NEAREST" else min(1, len(expiries) - 1)
expiry = expiries[expiry_index]

try:
    chain_data = option_chain(client_id, token, security_id, segment, expiry)
    spot, frame = chain_frame(chain_data)
except Exception as exc:
    st.error(f"Unable to load option chain: {exc}")
    st.stop()

metrics = dashboard_metrics(frame, spot, width=500)
if not metrics:
    st.error("Option chain loaded but no usable strikes were returned.")
    st.stop()

atm = metrics["ATM"]

# -----------------------------
# Top status row
# -----------------------------
status_cols = st.columns(6)
status_cols[0].metric("INDEX", index_name)
status_cols[1].metric("SPOT", f"₹{spot:,.2f}")
status_cols[2].metric("EXPIRY", expiry)
status_cols[3].metric("ATM", f"{atm:,.0f}")
status_cols[4].metric("CALL VEGA", f"{metrics['CALL VEGA']:,.2f}")
status_cols[5].metric("PUT VEGA", f"{metrics['PUT VEGA']:,.2f}")

# -----------------------------
# Signal engine
# -----------------------------
previous_difference = st.session_state.get("previous_difference")
current_difference = metrics["VEGA DIFFERENCE"]
change = None if previous_difference is None else current_difference - previous_difference

if change is None:
    direction = "WAIT"
elif change > 0:
    direction = "BEARISH"
elif change < 0:
    direction = "BULLISH"
else:
    direction = "NEUTRAL"

if no_trade_enabled and range_low <= spot <= range_high:
    signal = "NO-TRADE RANGE"
elif trend_mode != "BOTH" and direction not in ["WAIT", "NEUTRAL", trend_mode]:
    signal = "FILTERED"
else:
    signal = classify_signal(current_difference, previous_difference, continuous_count, trend_mode)

signal_cols = st.columns(4)
with signal_cols[0]:
    st.markdown(f"<div class='signal-box'><div class='signal-title'>Vega Difference</div><div class='signal-value'>{current_difference:,.2f}</div></div>", unsafe_allow_html=True)
with signal_cols[1]:
    change_text = "—" if change is None else f"{change:+,.2f}"
    st.markdown(f"<div class='signal-box'><div class='signal-title'>Change Since Last Refresh</div><div class='signal-value'>{change_text}</div></div>", unsafe_allow_html=True)
with signal_cols[2]:
    st.markdown(f"<div class='signal-box'><div class='signal-title'>Signal</div><div class='signal-value'>{signal}</div></div>", unsafe_allow_html=True)
with signal_cols[3]:
    st.markdown(f"<div class='signal-box'><div class='signal-title'>Mode</div><div class='signal-value'>{mode}</div></div>", unsafe_allow_html=True)

# -----------------------------
# Strike table around ATM
# -----------------------------
st.subheader("Vega Map")
display_band = frame[(frame["strike"] >= atm - 1000) & (frame["strike"] <= atm + 1000)].copy()
display_cols = ["strike", "CE Vega", "PE Vega", "Vega Difference", "CE LTP", "PE LTP", "CE OI", "PE OI", "CE IV", "PE IV"]
st.dataframe(
    display_band[display_cols].round(3),
    use_container_width=True,
    hide_index=True,
    column_config={
        "strike": st.column_config.NumberColumn("Strike", format="%0.f"),
        "CE Vega": st.column_config.NumberColumn(format="%0.2f"),
        "PE Vega": st.column_config.NumberColumn(format="%0.2f"),
        "Vega Difference": st.column_config.NumberColumn(format="%+0.2f"),
        "CE LTP": st.column_config.NumberColumn(format="₹%0.2f"),
        "PE LTP": st.column_config.NumberColumn(format="₹%0.2f"),
        "CE OI": st.column_config.NumberColumn(format="%0.0f"),
        "PE OI": st.column_config.NumberColumn(format="%0.0f"),
        "CE IV": st.column_config.NumberColumn(format="%0.2f"),
        "PE IV": st.column_config.NumberColumn(format="%0.2f"),
    },
)

# -----------------------------
# Controls / execution state
# -----------------------------
st.subheader("Trade Controller")
controller = st.columns(4)
with controller[0]:
    st.metric("Continuous Counter", st.session_state.get("signal_count", 0))
with controller[1]:
    st.metric("Track SL", "ON" if track_sl else "OFF")
with controller[2]:
    st.metric("Exit Reversal", "ON" if exit_reversal else "OFF")
with controller[3]:
    if st.button("🧹 RESET SIGNAL STATE", use_container_width=True):
        st.session_state.signal_count = 0
        st.session_state.previous_difference = None
        st.rerun()

if previous_difference is not None:
    same_direction = (current_difference - previous_difference > 0 and direction == "BEARISH") or (current_difference - previous_difference < 0 and direction == "BULLISH")
    if same_direction:
        st.session_state.signal_count = st.session_state.get("signal_count", 0) + 1
    else:
        st.session_state.signal_count = 0

st.session_state.previous_difference = current_difference

if st.session_state.get("signal_count", 0) >= continuous_count + 1 and signal in ["BULLISH", "BEARISH"]:
    st.success(f"✅ CONFIRMATION READY: {signal}")
    if mode == "PAPER":
        st.info("Paper mode: no order has been sent to Dhan.")
else:
    st.info("Waiting for the configured continuous Vega confirmation.")

# Live execution is intentionally visible but requires an explicit click.
if mode == "LIVE":
    st.subheader("Live Execution")
    st.caption("This dashboard does not auto-fire an order on refresh. Use the explicit button after reviewing the signal and selected strike.")
    strike = st.selectbox("Execution Strike", options=frame["strike"].tolist(), index=int(frame["strike"].sub(atm).abs().argmin()))
    chosen = frame[frame["strike"] == strike].iloc[0]
    live_cols = st.columns(3)
    live_cols[0].metric("Selected Strike", f"{strike:,.0f}")
    live_cols[1].metric("CE Security ID", chosen["CE Security ID"])
    live_cols[2].metric("PE Security ID", chosen["PE Security ID"])
    st.error("Execution plumbing is deliberately not automatic in this first dashboard version. Dhan order APIs require Static IP whitelisting and an explicit order contract.")

# -----------------------------
# Account activity
# -----------------------------
with st.expander("📋 Dhan Account Activity", expanded=False):
    tabs = st.tabs(["Positions", "Orders", "Trades"])
    for tab, loader, title in [
        (tabs[0], positions, "positions"),
        (tabs[1], orders, "orders"),
        (tabs[2], trades, "trades"),
    ]:
        with tab:
            try:
                payload = loader(client_id, token)
                if isinstance(payload, list) and payload:
                    st.dataframe(pd.DataFrame(payload), use_container_width=True, hide_index=True)
                elif isinstance(payload, dict) and payload:
                    st.json(payload)
                else:
                    st.caption(f"No {title} data returned.")
            except Exception as exc:
                st.warning(f"Unable to load {title}: {exc}")

st.caption(
    f"Last refresh: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')} IST • Credentials are held in Streamlit session state only."
)
