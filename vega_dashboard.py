import base64
import io
import json
import struct
import urllib.error
import urllib.request
import wave
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

DHAN_API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
ATM_LOCK_TIME = time(10, 0)
SQUARE_OFF = time(15, 20)

st.set_page_config(
    page_title="ALPHA • Fixed 10AM Vega",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {padding-top:1rem;padding-bottom:2rem;}
    .small-muted {color:#8b949e;font-size:.82rem;}
    .fixed-card {padding:16px 18px;border-radius:14px;border:1px solid #30363d;background:#161b22;}
    .fixed-title {font-size:.76rem;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;}
    .fixed-value {font-size:1.5rem;font-weight:800;margin-top:3px;}
    .spike-card {padding:18px;border-radius:14px;border:2px solid #ff4b4b;background:rgba(255,75,75,.12);}
    </style>
    """,
    unsafe_allow_html=True,
)


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
    st.sidebar.caption("Credentials stay in Streamlit session state and are not written to GitHub.")
    st.session_state.setdefault("dhan_client_id", "")
    st.session_state.setdefault("dhan_token", "")
    st.session_state.setdefault("dhan_connected", False)

    with st.sidebar.form("dhan_login", clear_on_submit=False):
        client_id = st.text_input("Dhan Client ID / Username", value=st.session_state.dhan_client_id)
        token = st.text_input("Dhan Access Token", value=st.session_state.dhan_token, type="password")
        connect = st.form_submit_button("🔗 CONNECT TO DHAN", use_container_width=True, type="primary")

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


def expiry_list(client_id, token, security_id, segment):
    response = dhan_request(
        "/optionchain/expirylist",
        client_id,
        token,
        method="POST",
        body={"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment},
    )
    return [str(x) for x in (response.get("data") or [])]


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


def intraday_underlying(client_id, token, security_id, segment, trading_date):
    body = {
        "securityId": str(security_id),
        "exchangeSegment": segment,
        "instrument": "INDEX",
        "interval": "1",
        "oi": False,
        "fromDate": f"{trading_date} 09:15:00",
        "toDate": f"{trading_date} 10:01:00",
    }
    response = dhan_request("/charts/intraday", client_id, token, method="POST", body=body)
    if not isinstance(response, dict):
        return pd.DataFrame()
    ts = response.get("timestamp") or []
    close = response.get("close") or []
    if not ts or not close:
        return pd.DataFrame()
    n = min(len(ts), len(close))
    out = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(ts[:n], unit="s", utc=True).tz_convert(IST).tz_localize(None),
            "close": pd.to_numeric(close[:n], errors="coerce"),
        }
    ).dropna()
    return out.sort_values("timestamp").reset_index(drop=True)


def chain_frame(chain_data):
    spot = float(chain_data.get("last_price", 0) or 0)
    rows = []
    for strike_text, node in (chain_data.get("oc", {}) or {}).items():
        try:
            strike = float(strike_text)
        except (TypeError, ValueError):
            continue
        ce = node.get("ce") or {}
        pe = node.get("pe") or {}
        ceg = ce.get("greeks") or {}
        peg = pe.get("greeks") or {}
        rows.append(
            {
                "strike": strike,
                "CE Vega": float(ceg.get("vega", 0) or 0),
                "PE Vega": float(peg.get("vega", 0) or 0),
                "Vega Difference": float(ceg.get("vega", 0) or 0) - float(peg.get("vega", 0) or 0),
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
    return spot, pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)


def beep_uri():
    sample_rate = 22050
    duration = 0.65
    frequency = 880.0
    frames = []
    period = max(1, int(sample_rate / (frequency * 2)))
    for i in range(int(sample_rate * duration)):
        value = 12000 if (i // period) % 2 == 0 else -12000
        frames.append(struct.pack("<h", value))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(frames))
    return "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode()


def sound_alert():
    uri = beep_uri()
    st.components.v1.html(
        f"""
        <audio autoplay>
            <source src="{uri}" type="audio/wav">
        </audio>
        <script>
        const a = document.querySelector('audio');
        if (a) {{ a.volume = 1.0; a.play().catch(() => {{}}); }}
        </script>
        """,
        height=1,
    )


def auto_refresh_every_3_minutes():
    st.components.v1.html(
        """
        <script>
        setTimeout(function() {
            try { window.parent.location.reload(); }
            catch (e) { window.location.reload(); }
        }, 180000);
        </script>
        """,
        height=1,
    )


connect_box()

st.sidebar.markdown("## ⚙️ SETTINGS")
index_name = st.sidebar.selectbox("Index", ["NIFTY 50"])
security_id = st.sidebar.number_input("Underlying Security ID", min_value=1, value=13, step=1)
segment = st.sidebar.selectbox("Underlying Segment", ["IDX_I", "NSE_FNO"])
expiry_choice = st.sidebar.selectbox("Expiry", ["NEAREST", "NEXT"])
spike_threshold = st.sidebar.number_input(
    "Sudden Vega Spike Threshold %",
    min_value=1.0,
    max_value=200.0,
    value=20.0,
    step=1.0,
)
sound_enabled = st.sidebar.checkbox("🔊 Warning Sound", value=True)
show_history = st.sidebar.checkbox("Show Vega History", value=True)

st.title("📊 ALPHA • Fixed 10AM ATM Vega Monitor")
st.caption("ATM is locked from the 10:00 IST underlying price and the same CE + PE straddle is tracked for the rest of the trading day.")

if not st.session_state.dhan_connected:
    st.info("Connect your Dhan account from the left sidebar.")
    st.stop()

auto_refresh_every_3_minutes()

client_id = st.session_state.dhan_client_id
token = st.session_state.dhan_token
now = datetime.now(IST)
today = now.date()
day_key = today.isoformat()

if st.session_state.get("vega_day") != day_key:
    for key in [
        "vega_atm_locked",
        "vega_atm_strike",
        "vega_atm_spot",
        "vega_atm_time",
        "vega_history",
        "vega_last_spike_key",
        "vega_previous_ce",
        "vega_previous_pe",
    ]:
        st.session_state.pop(key, None)
    st.session_state.vega_day = day_key

try:
    expiries = expiry_list(client_id, token, security_id, segment)
except Exception as exc:
    st.error(f"Unable to load expiries: {exc}")
    st.stop()

if not expiries:
    st.error("No active expiries returned by Dhan.")
    st.stop()

expiry = expiries[0 if expiry_choice == "NEAREST" else min(1, len(expiries) - 1)]

# Lock ATM from the 10:00 underlying close, even when dashboard is opened later.
atm_locked = st.session_state.get("vega_atm_locked", False)
if not atm_locked:
    if now.time() < ATM_LOCK_TIME:
        st.warning("⏳ Waiting for 10:00 IST. ATM will be fixed from the 10:00 NIFTY spot candle.")
    else:
        try:
            underlying = intraday_underlying(client_id, token, security_id, segment, day_key)
            exact = underlying[underlying["timestamp"].dt.time == ATM_LOCK_TIME]
            if exact.empty:
                eligible = underlying[underlying["timestamp"].dt.time <= ATM_LOCK_TIME]
                exact = eligible.tail(1)
            if exact.empty:
                st.warning("10:00 underlying candle is not available yet. Retrying on the next refresh.")
                st.stop()
            ref_spot = float(exact.iloc[-1]["close"])
            current_chain = option_chain(client_id, token, security_id, segment, expiry)
            _, full_frame = chain_frame(current_chain)
            if full_frame.empty:
                st.error("Option chain returned no strikes.")
                st.stop()
            locked_strike = float(full_frame.iloc[(full_frame["strike"] - ref_spot).abs().argmin()]["strike"])
            st.session_state.vega_atm_locked = True
            st.session_state.vega_atm_strike = locked_strike
            st.session_state.vega_atm_spot = ref_spot
            st.session_state.vega_atm_time = "10:00"
            st.session_state.vega_history = []
            st.session_state.vega_last_spike_key = None
            st.session_state.vega_previous_ce = None
            st.session_state.vega_previous_pe = None
        except Exception as exc:
            st.error(f"Unable to lock 10:00 ATM: {exc}")
            st.stop()

if not st.session_state.get("vega_atm_locked"):
    st.stop()

locked_strike = float(st.session_state.vega_atm_strike)

try:
    chain_data = option_chain(client_id, token, security_id, segment, expiry)
    spot_now, frame = chain_frame(chain_data)
except Exception as exc:
    st.error(f"Unable to load option chain: {exc}")
    st.stop()

if frame.empty:
    st.error("No option-chain strikes returned.")
    st.stop()

fixed = frame.iloc[[(frame["strike"] - locked_strike).abs().argmin()]].copy()
row = fixed.iloc[0]
ce_vega = float(row["CE Vega"])
pe_vega = float(row["PE Vega"])
vega_diff = ce_vega - pe_vega

previous_ce = st.session_state.get("vega_previous_ce")
previous_pe = st.session_state.get("vega_previous_pe")
ce_pct = None if previous_ce in (None, 0) else (ce_vega / previous_ce - 1) * 100
pe_pct = None if previous_pe in (None, 0) else (pe_vega / previous_pe - 1) * 100

ce_spike = ce_pct is not None and ce_pct >= spike_threshold
pe_spike = pe_pct is not None and pe_pct >= spike_threshold
spike_side = "ATM CE" if ce_spike and (pe_pct is None or ce_pct >= pe_pct) else "ATM PE" if pe_spike else None
spike_pct = ce_pct if spike_side == "ATM CE" else pe_pct if spike_side == "ATM PE" else None

refresh_time = now.strftime("%H:%M:%S")
sample = {
    "time": refresh_time,
    "spot": spot_now,
    "ce_vega": ce_vega,
    "pe_vega": pe_vega,
    "diff": vega_diff,
}
history = st.session_state.get("vega_history", [])
if not history or history[-1]["time"] != refresh_time:
    history.append(sample)
st.session_state.vega_history = history[-60:]

# Alert only once per refresh sample.
spike_key = f"{day_key}|{refresh_time}|{spike_side}"
new_spike = bool(spike_side and st.session_state.get("vega_last_spike_key") != spike_key)
if new_spike:
    st.session_state.vega_last_spike_key = spike_key

st.session_state.vega_previous_ce = ce_vega
st.session_state.vega_previous_pe = pe_vega

if now.time() > SQUARE_OFF:
    st.info("Market session is past the configured 15:20 square-off time. The 10:00 ATM remains locked for reference.")

top = st.columns(7)
top[0].metric("10:00 ATM STRIKE", f"{locked_strike:,.0f}")
top[1].metric("10:00 SPOT", f"₹{st.session_state.vega_atm_spot:,.2f}")
top[2].metric("CURRENT SPOT", f"₹{spot_now:,.2f}")
top[3].metric("EXPIRY", expiry)
top[4].metric("ATM CE VEGA", f"{ce_vega:,.3f}", None if ce_pct is None else f"{ce_pct:+.1f}%")
top[5].metric("ATM PE VEGA", f"{pe_vega:,.3f}", None if pe_pct is None else f"{pe_pct:+.1f}%")
top[6].metric("VEGA DIFFERENCE", f"{vega_diff:+.3f}")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
straddle_view = pd.DataFrame(
    [
        {
            "Strike": locked_strike,
            "CE Vega": ce_vega,
            "PE Vega": pe_vega,
            "CE LTP": float(row["CE LTP"]),
            "PE LTP": float(row["PE LTP"]),
            "CE OI": float(row["CE OI"]),
            "PE OI": float(row["PE OI"]),
            "CE IV": float(row["CE IV"]),
            "PE IV": float(row["PE IV"]),
            "Last Refresh": refresh_time,
        }
    ]
)
st.dataframe(straddle_view, use_container_width=True, hide_index=True)

if new_spike:
    st.markdown(
        f"""
        <div class="spike-card">
        <div class="fixed-title">⚠️ SUDDEN VEGA SPIKE</div>
        <div class="fixed-value">{spike_side} +{spike_pct:.1f}% in the last 3-minute sample</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if sound_enabled:
        sound_alert()
elif spike_side:
    st.warning(f"⚠️ {spike_side} is above the spike threshold: +{spike_pct:.1f}%.")

st.markdown(
    f"<div class='small-muted'>ATM locked at 10:00 IST • Same strike tracked all day • Auto refresh: every 3 minutes • Spike threshold: {spike_threshold:.1f}%</div>",
    unsafe_allow_html=True,
)

if show_history and st.session_state.vega_history:
    st.markdown("### 📈 Fixed-Straddle Vega History")
    hist = pd.DataFrame(st.session_state.vega_history)
    st.line_chart(hist.set_index("time")[["ce_vega", "pe_vega"]], height=280)

with st.expander("📋 Dhan Security IDs", expanded=False):
    st.write(
        {
            "Fixed ATM CE Security ID": row["CE Security ID"],
            "Fixed ATM PE Security ID": row["PE Security ID"],
        }
    )

st.caption(
    f"Last refresh: {now.strftime('%d-%b-%Y %H:%M:%S')} IST • "
    "The alert compares each fixed ATM leg with the immediately previous 3-minute sample."
)
