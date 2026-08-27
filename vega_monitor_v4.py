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
REFRESH_SECONDS = 180

st.set_page_config(page_title="ALPHA • Fixed 10AM Vega", page_icon="📊", layout="wide")

st.markdown("""
<style>
.block-container {padding-top:1rem;padding-bottom:2rem}
.small-muted {color:#8b949e;font-size:.82rem}
.spike-card {padding:18px;border-radius:14px;border:2px solid #ff4b4b;background:rgba(255,75,75,.12)}
.fixed-title {font-size:.76rem;color:#8b949e;text-transform:uppercase;letter-spacing:.08em}
.fixed-value {font-size:1.5rem;font-weight:800}
</style>
""", unsafe_allow_html=True)


def dhan_request(path, client_id, token, method="GET", body=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        DHAN_API + path, method=method, headers=headers, data=data
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Dhan HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def connect_box():
    st.sidebar.markdown("## 🔐 DHAN CONNECTION")
    st.sidebar.caption("Credentials stay in Streamlit session state only.")
    st.session_state.setdefault("dhan_client_id", "")
    st.session_state.setdefault("dhan_token", "")
    st.session_state.setdefault("dhan_connected", False)

    with st.sidebar.form("dhan_login", clear_on_submit=False):
        client_id = st.text_input(
            "Dhan Client ID / Username",
            value=st.session_state.dhan_client_id,
        )
        token = st.text_input(
            "Dhan Access Token",
            value=st.session_state.dhan_token,
            type="password",
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
                st.rerun()
            except Exception as exc:
                st.session_state.dhan_connected = False
                st.sidebar.error("Dhan connection failed")
                st.sidebar.caption(str(exc))

    if st.session_state.dhan_connected:
        st.sidebar.success(
            f"Connected: {st.session_state.dhan_client_id}"
        )
        if st.sidebar.button("LOGOUT / CLEAR DHAN", use_container_width=True):
            st.session_state.dhan_client_id = ""
            st.session_state.dhan_token = ""
            st.session_state.dhan_connected = False
            st.rerun()
    else:
        st.sidebar.warning("Not connected to Dhan")


def get_expiries(client_id, token, security_id, segment):
    response = dhan_request(
        "/optionchain/expirylist",
        client_id,
        token,
        method="POST",
        body={
            "UnderlyingScrip": int(security_id),
            "UnderlyingSeg": segment,
        },
    )
    values = response.get("data") if isinstance(response, dict) else None
    if not isinstance(values, list):
        return []
    today = datetime.now(IST).date()
    valid = []
    for value in values:
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            continue
        if parsed >= today:
            valid.append(parsed.isoformat())
    return sorted(set(valid))


def choose_expiry(client_id, token, security_id, segment, manual_expiry):
    if manual_expiry:
        try:
            parsed = datetime.strptime(manual_expiry, "%Y-%m-%d").date()
        except ValueError as exc:
            raise RuntimeError("Expiry must be in YYYY-MM-DD format.") from exc
        if parsed < datetime.now(IST).date():
            raise RuntimeError("Entered expiry is already expired.")
        return parsed.isoformat()

    cache_day = st.session_state.get("expiry_cache_day")
    cached = st.session_state.get("expiry_cache")
    today = datetime.now(IST).date().isoformat()
    if cache_day == today and cached:
        return cached[0]

    expiries = get_expiries(client_id, token, security_id, segment)
    if not expiries:
        raise RuntimeError(
            "Dhan returned no valid future expiries for this underlying/segment."
        )
    st.session_state.expiry_cache = expiries
    st.session_state.expiry_cache_day = today
    return expiries[0]


def option_chain(client_id, token, security_id, segment, expiry):
    body = {
        "UnderlyingScrip": int(security_id),
        "UnderlyingSeg": segment,
        "Expiry": expiry,
    }
    response = dhan_request(
        "/optionchain", client_id, token, method="POST", body=body
    )
    return response.get("data", {}) if isinstance(response, dict) else {}


def intraday_underlying(client_id, token, security_id, segment, day):
    body = {
        "securityId": str(security_id),
        "exchangeSegment": segment,
        "instrument": "INDEX",
        "interval": "1",
        "oi": False,
        "fromDate": f"{day} 09:15:00",
        "toDate": f"{day} 10:01:00",
    }
    response = dhan_request(
        "/charts/intraday", client_id, token, method="POST", body=body
    )
    if not isinstance(response, dict):
        return pd.DataFrame()
    timestamps = response.get("timestamp") or []
    closes = response.get("close") or []
    n = min(len(timestamps), len(closes))
    if n == 0:
        return pd.DataFrame()
    times = pd.to_datetime(timestamps[:n], unit="s", utc=True)
    times = times.dt.tz_convert(IST).dt.tz_localize(None)
    return pd.DataFrame(
        {
            "timestamp": times,
            "close": pd.to_numeric(closes[:n], errors="coerce"),
        }
    ).dropna().sort_values("timestamp").reset_index(drop=True)


def chain_frame(data):
    rows = []
    spot = float(data.get("last_price", 0) or 0)
    for strike_text, node in (data.get("oc") or {}).items():
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
    frame = pd.DataFrame(rows)
    if frame.empty:
        return spot, frame
    return spot, frame.sort_values("strike").reset_index(drop=True)


def beep_uri():
    sample_rate = 22050
    duration = 0.65
    frequency = 880
    half_period = max(1, int(sample_rate / (frequency * 2)))
    frames = []
    for i in range(int(sample_rate * duration)):
        value = 12000 if (i // half_period) % 2 == 0 else -12000
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


connect_box()
st.sidebar.markdown("## ⚙️ SETTINGS")
security_id = st.sidebar.number_input(
    "NIFTY 50 Security ID", min_value=1, value=13, step=1
)
segment = st.sidebar.selectbox("Underlying Segment", ["IDX_I", "NSE_FNO"])
manual_expiry = st.sidebar.text_input(
    "Expiry (YYYY-MM-DD)",
    value="",
    help="Blank = use the nearest future expiry returned by Dhan's documented expiry-list API.",
)
spike_threshold = st.sidebar.number_input(
    "Sudden Vega Spike Threshold %",
    min_value=1.0,
    max_value=200.0,
    value=20.0,
    step=1.0,
)
sound_enabled = st.sidebar.checkbox("🔊 Warning Sound", value=True)

st.title("📊 ALPHA • Fixed 10AM ATM Vega Monitor")
st.caption(
    "The 10:00 IST ATM strike is fixed for the whole session. Only that CE + PE straddle is monitored every 3 minutes."
)

if not st.session_state.dhan_connected:
    st.info("Connect your Dhan account from the left sidebar.")
    st.stop()

cid = st.session_state.dhan_client_id
token = st.session_state.dhan_token
now = datetime.now(IST)
day_key = now.date().isoformat()

if st.session_state.get("vega_day") != day_key:
    for key in [
        "atm_locked", "atm_strike", "atm_spot", "atm_expiry",
        "last_chain", "last_good_at", "history", "prev_ce", "prev_pe",
        "last_spike_key", "expiry_cache", "expiry_cache_day",
    ]:
        st.session_state.pop(key, None)
    st.session_state.vega_day = day_key

if hasattr(st, "fragment"):
    refresh_note = f"Native Streamlit refresh every {REFRESH_SECONDS} seconds."
else:
    refresh_note = "Use the browser refresh button every 3 minutes on older Streamlit versions."

st.markdown(f"<div class='small-muted'>{refresh_note}</div>", unsafe_allow_html=True)

try:
    expiry = choose_expiry(cid, token, security_id, segment, manual_expiry.strip())
except Exception as exc:
    st.error(f"Expiry resolution failed: {exc}")
    st.stop()

if st.session_state.get("atm_expiry") and st.session_state.atm_expiry != expiry:
    st.session_state.atm_locked = False
    st.session_state.pop("atm_strike", None)
    st.session_state.pop("atm_spot", None)
    st.session_state.pop("last_chain", None)

st.session_state.atm_expiry = expiry

if not st.session_state.get("atm_locked"):
    if now.time() < ATM_LOCK_TIME:
        st.warning("⏳ Waiting for 10:00 IST. ATM will lock from the 10:00 NIFTY 1-minute candle.")
        st.stop()

    try:
        underlying = intraday_underlying(cid, token, security_id, segment, day_key)
        exact = underlying[underlying["timestamp"].dt.strftime("%H:%M") == "10:00"]
        if exact.empty:
            exact = underlying[underlying["timestamp"].dt.time <= ATM_LOCK_TIME].tail(1)
        if exact.empty:
            st.warning("10:00 NIFTY candle is not available yet. The next cycle will retry.")
            st.stop()

        reference_spot = float(exact.iloc[-1]["close"])
        data = option_chain(cid, token, security_id, segment, expiry)
        spot_from_chain, frame = chain_frame(data)
        if frame.empty:
            st.error("Dhan returned an empty option chain.")
            st.stop()

        atm_strike = float(frame.iloc[(frame["strike"] - reference_spot).abs().argmin()]["strike"])
        st.session_state.atm_locked = True
        st.session_state.atm_strike = atm_strike
        st.session_state.atm_spot = reference_spot
        st.session_state.last_chain = data
        st.session_state.last_good_at = now.timestamp()
        st.session_state.history = []
        st.session_state.prev_ce = None
        st.session_state.prev_pe = None
        st.session_state.last_spike_key = None
    except Exception as exc:
        st.error(f"Unable to lock ATM: {exc}")
        st.stop()
else:
    data = None
    try:
        data = option_chain(cid, token, security_id, segment, expiry)
        st.session_state.last_chain = data
        st.session_state.last_good_at = now.timestamp()
    except Exception as exc:
        data = st.session_state.get("last_chain")
        if not data:
            st.error(f"Unable to load option chain: {exc}")
            st.stop()
        if "429" in str(exc) or "805" in str(exc):
            st.warning("⚠️ Dhan rate limit active. Showing the last good snapshot; no extra retry is sent before the next 3-minute cycle.")
        else:
            st.warning(f"⚠️ Live refresh failed. Showing the last good snapshot: {exc}")

spot, frame = chain_frame(data)
if frame.empty:
    st.error("No option-chain strikes returned.")
    st.stop()

locked_strike = float(st.session_state.atm_strike)
row = frame.iloc[(frame["strike"] - locked_strike).abs().argmin()]
ce_vega = float(row["CE Vega"])
pe_vega = float(row["PE Vega"])
vega_difference = ce_vega - pe_vega

prev_ce = st.session_state.get("prev_ce")
prev_pe = st.session_state.get("prev_pe")
ce_pct = None if prev_ce in (None, 0) else (ce_vega / prev_ce - 1) * 100
pe_pct = None if prev_pe in (None, 0) else (pe_vega / prev_pe - 1) * 100

spike_side = None
spike_pct = None
if ce_pct is not None and ce_pct >= spike_threshold:
    spike_side, spike_pct = "ATM CE", ce_pct
if pe_pct is not None and pe_pct >= spike_threshold and (spike_pct is None or pe_pct >= spike_pct):
    spike_side, spike_pct = "ATM PE", pe_pct

bucket = int(now.timestamp() // REFRESH_SECONDS)
history = st.session_state.get("history", [])
if not history or history[-1]["bucket"] != bucket:
    history.append(
        {
            "bucket": bucket,
            "time": now.strftime("%H:%M"),
            "CE Vega": ce_vega,
            "PE Vega": pe_vega,
            "Vega Difference": vega_difference,
        }
    )
st.session_state.history = history[-60:]

spike_key = f"{day_key}|{bucket}|{spike_side}"
new_spike = bool(spike_side and spike_key != st.session_state.get("last_spike_key"))
if new_spike:
    st.session_state.last_spike_key = spike_key

st.session_state.prev_ce = ce_vega
st.session_state.prev_pe = pe_vega

if now.time() > time(15, 20):
    st.info("Market session is past 15:20 IST. The fixed 10:00 ATM remains available for reference.")

cols = st.columns(7)
cols[0].metric("10:00 ATM STRIKE", f"{locked_strike:,.0f}")
cols[1].metric("10:00 SPOT", f"₹{st.session_state.atm_spot:,.2f}")
cols[2].metric("CURRENT SPOT", f"₹{spot:,.2f}")
cols[3].metric("EXPIRY", expiry)
cols[4].metric("ATM CE VEGA", f"{ce_vega:.3f}", None if ce_pct is None else f"{ce_pct:+.1f}%")
cols[5].metric("ATM PE VEGA", f"{pe_vega:.3f}", None if pe_pct is None else f"{pe_pct:+.1f}%")
cols[6].metric("CE − PE VEGA", f"{vega_difference:+.3f}")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(
    pd.DataFrame(
        [
            {
                "Strike": locked_strike,
                "CE Vega": ce_vega,
                "PE Vega": pe_vega,
                "CE LTP": row["CE LTP"],
                "PE LTP": row["PE LTP"],
                "CE OI": row["CE OI"],
                "PE OI": row["PE OI"],
                "CE IV": row["CE IV"],
                "PE IV": row["PE IV"],
                "CE Security ID": row["CE Security ID"],
                "PE Security ID": row["PE Security ID"],
                "Refresh": now.strftime("%H:%M:%S"),
            }
        ]
    ),
    use_container_width=True,
    hide_index=True,
)

if new_spike:
    st.markdown(
        f"<div class='spike-card'><div class='fixed-title'>⚠️ SUDDEN VEGA SPIKE</div><div class='fixed-value'>{spike_side} {spike_pct:+.1f}% in the last 3-minute sample</div></div>",
        unsafe_allow_html=True,
    )
    if sound_enabled:
        sound_alert()
elif spike_side:
    st.warning(f"⚠️ {spike_side} is above the spike threshold: {spike_pct:+.1f}%.")

if st.session_state.get("history"):
    st.markdown("### 📈 Fixed-Straddle Vega History")
    hist = pd.DataFrame(st.session_state.history)
    st.line_chart(hist.set_index("time")[["CE Vega", "PE Vega"]], height=280)

st.markdown(
    f"<div class='small-muted'>ATM locked at 10:00 IST • Same strike tracked all day • Refresh cycle: {REFRESH_SECONDS // 60} minutes • Spike threshold: {spike_threshold:.0f}%</div>",
    unsafe_allow_html=True,
)

if hasattr(st, "fragment"):
    # A tiny fragment drives the three-minute rerun without third-party packages.
    @st.fragment(run_every=f"{REFRESH_SECONDS}s")
    def _refresh_clock():
        st.empty()
    _refresh_clock()
