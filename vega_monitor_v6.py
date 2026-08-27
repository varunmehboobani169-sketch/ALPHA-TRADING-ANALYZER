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


def dhan_request(path, client_id, token, method="GET", body=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(DHAN_API + path, method=method, headers=headers, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Dhan HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def get_expiries(client_id, token, security_id, segment):
    response = dhan_request(
        "/optionchain/expirylist",
        client_id,
        token,
        method="POST",
        body={"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment},
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


def resolve_expiry(client_id, token, security_id, segment, manual_expiry):
    today = datetime.now(IST).date()
    manual_expiry = manual_expiry.strip()
    if manual_expiry:
        try:
            parsed = datetime.strptime(manual_expiry, "%Y-%m-%d").date()
        except ValueError as exc:
            raise RuntimeError("Expiry must be in YYYY-MM-DD format.") from exc
        if parsed < today:
            raise RuntimeError(f"Expiry {parsed.isoformat()} is in the past. Choose a future expiry.")
        return parsed.isoformat()

    cache_key = f"{security_id}|{segment}|{today.isoformat()}"
    if st.session_state.get("expiry_cache_key") == cache_key:
        cached = st.session_state.get("expiry_cache") or []
        if cached:
            return cached[0]

    values = get_expiries(client_id, token, security_id, segment)
    if not values:
        raise RuntimeError("Dhan returned no valid future expiries.")

    st.session_state.expiry_cache_key = cache_key
    st.session_state.expiry_cache = values
    return values[0]


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
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Dhan returned an invalid option-chain response.")
    return data


def spot_at_10(client_id, token, security_id, segment, day):
    response = dhan_request(
        "/charts/intraday",
        client_id,
        token,
        method="POST",
        body={
            "securityId": str(security_id),
            "exchangeSegment": segment,
            "instrument": "INDEX",
            "interval": "1",
            "oi": False,
            "fromDate": f"{day} 09:15:00",
            "toDate": f"{day} 10:01:00",
        },
    )
    timestamps = response.get("timestamp") or []
    closes = response.get("close") or []
    n = min(len(timestamps), len(closes))
    if not n:
        return None

    # pd.to_datetime(..., utc=True) returns a DatetimeIndex for a list.
    # Wrap it in a Series before using .dt so both Index/Series cases work.
    times = pd.Series(pd.to_datetime(timestamps[:n], unit="s", utc=True))
    times = times.dt.tz_convert(IST).dt.tz_localize(None)
    frame = pd.DataFrame(
        {
            "timestamp": times,
            "close": pd.to_numeric(closes[:n], errors="coerce"),
        }
    ).dropna()

    exact = frame[frame["timestamp"].dt.strftime("%H:%M") == "10:00"]
    if exact.empty:
        exact = frame[frame["timestamp"].dt.time <= ATM_LOCK_TIME].tail(1)
    return None if exact.empty else float(exact.iloc[-1]["close"])


def chain_frame(data):
    spot = float(data.get("last_price", 0) or 0)
    rows = []
    for strike_text, node in (data.get("oc") or {}).items():
        try:
            strike = float(strike_text)
        except (TypeError, ValueError):
            continue
        ce = node.get("ce") or {}
        pe = node.get("pe") or {}
        cg = ce.get("greeks") or {}
        pg = pe.get("greeks") or {}
        rows.append(
            {
                "Strike": strike,
                "CE Vega": float(cg.get("vega", 0) or 0),
                "PE Vega": float(pg.get("vega", 0) or 0),
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
    if not frame.empty:
        frame = frame.sort_values("Strike").reset_index(drop=True)
    return spot, frame


def warning_sound():
    sample_rate = 22050
    duration = 0.7
    frequency = 880.0
    half_period = max(1, int(sample_rate / (frequency * 2)))
    frames = [
        struct.pack("<h", 12000 if (i // half_period) % 2 == 0 else -12000)
        for i in range(int(sample_rate * duration))
    ]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(frames))
    uri = "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode()
    st.components.v1.html(
        f"<audio autoplay><source src='{uri}' type='audio/wav'></audio>",
        height=1,
    )


def login_box():
    st.sidebar.markdown("## 🔐 DHAN CONNECTION")
    st.sidebar.caption("Credentials stay in Streamlit session state only.")
    st.session_state.setdefault("dhan_client_id", "")
    st.session_state.setdefault("dhan_token", "")
    st.session_state.setdefault("dhan_connected", False)

    with st.sidebar.form("dhan_login", clear_on_submit=False):
        cid = st.text_input("Dhan Client ID / Username", value=st.session_state.dhan_client_id)
        token = st.text_input("Dhan Access Token", value=st.session_state.dhan_token, type="password")
        connect = st.form_submit_button("🔗 CONNECT TO DHAN", use_container_width=True, type="primary")

    if connect:
        cid, token = cid.strip(), token.strip()
        if not cid or not token:
            st.sidebar.error("Enter both Client ID and Access Token.")
        else:
            try:
                dhan_request("/profile", cid, token)
                st.session_state.dhan_client_id = cid
                st.session_state.dhan_token = token
                st.session_state.dhan_connected = True
                st.rerun()
            except Exception as exc:
                st.session_state.dhan_connected = False
                st.sidebar.error(f"Dhan connection failed: {exc}")

    if st.session_state.dhan_connected:
        st.sidebar.success(f"Connected: {st.session_state.dhan_client_id}")
        if st.sidebar.button("LOGOUT / CLEAR DHAN", use_container_width=True):
            st.session_state.dhan_client_id = ""
            st.session_state.dhan_token = ""
            st.session_state.dhan_connected = False
            st.rerun()


login_box()
st.sidebar.markdown("## ⚙️ SETTINGS")
security_id = st.sidebar.number_input("NIFTY 50 Security ID", min_value=1, value=13, step=1)
segment = st.sidebar.selectbox("Underlying Segment", ["IDX_I", "NSE_FNO"])
manual_expiry = st.sidebar.text_input("Expiry (YYYY-MM-DD)", value="", help="Leave blank for Dhan's nearest valid future expiry.")
spike_threshold = st.sidebar.number_input("Sudden Vega Spike %", min_value=1.0, max_value=200.0, value=20.0, step=1.0)
sound_enabled = st.sidebar.checkbox("🔊 Warning Sound", value=True)

st.title("📊 ALPHA • Fixed 10AM ATM Vega Monitor")
st.caption("The ATM strike is fixed from the 10:00 IST NIFTY reference candle and only that CE + PE straddle is tracked through the session.")

if not st.session_state.dhan_connected:
    st.info("Connect your Dhan account from the sidebar.")
    st.stop()

cid = st.session_state.dhan_client_id
token = st.session_state.dhan_token


def monitor_once():
    now = datetime.now(IST)
    day = now.date().isoformat()

    if st.session_state.get("vega_day") != day:
        for key in [
            "atm_locked", "atm_strike", "atm_spot", "atm_expiry", "last_chain",
            "prev_ce", "prev_pe", "last_spike", "history", "expiry_cache", "expiry_cache_key"
        ]:
            st.session_state.pop(key, None)
        st.session_state.vega_day = day

    try:
        expiry = resolve_expiry(cid, token, security_id, segment, manual_expiry)
    except Exception as exc:
        st.error(f"Expiry resolution failed: {exc}")
        return

    # Manual expiry changes require a fresh ATM lock for that expiry.
    if st.session_state.get("atm_expiry") != expiry:
        for key in ["atm_locked", "atm_strike", "atm_spot", "last_chain", "prev_ce", "prev_pe", "history", "last_spike"]:
            st.session_state.pop(key, None)
        st.session_state.atm_expiry = expiry

    # One Option Chain request is used to lock ATM. That same successful response becomes the first snapshot.
    if not st.session_state.get("atm_locked"):
        if now.time() < ATM_LOCK_TIME:
            st.warning("⏳ Waiting for 10:00 IST to lock the ATM strike.")
            return
        try:
            reference_spot = spot_at_10(cid, token, security_id, segment, day)
            if reference_spot is None:
                st.warning("10:00 NIFTY candle is unavailable. The next cycle will retry.")
                return
            chain_data = option_chain(cid, token, security_id, segment, expiry)
            _, frame = chain_frame(chain_data)
            if frame.empty:
                st.error("Dhan returned an empty option chain.")
                return
            locked_strike = float(frame.iloc[(frame["Strike"] - reference_spot).abs().argmin()]["Strike"])
            st.session_state.atm_locked = True
            st.session_state.atm_strike = locked_strike
            st.session_state.atm_spot = reference_spot
            st.session_state.last_chain = chain_data
            st.session_state.prev_ce = None
            st.session_state.prev_pe = None
            st.session_state.history = []
        except Exception as exc:
            st.error(f"Unable to lock ATM: {exc}")
            return
    else:
        try:
            chain_data = option_chain(cid, token, security_id, segment, expiry)
            st.session_state.last_chain = chain_data
        except Exception as exc:
            chain_data = st.session_state.get("last_chain")
            if not chain_data:
                st.error(f"Unable to load option chain: {exc}")
                return
            if "805" in str(exc) or "429" in str(exc):
                st.warning("⚠️ Dhan rate limit active. Showing the last good snapshot; no extra retry is sent before the next 3-minute cycle.")
            else:
                st.warning(f"⚠️ Refresh failed. Showing the last good snapshot: {exc}")

    current_spot, frame = chain_frame(chain_data)
    if frame.empty:
        st.error("No option-chain strikes returned.")
        return

    locked_strike = float(st.session_state.atm_strike)
    row = frame.iloc[(frame["Strike"] - locked_strike).abs().argmin()]
    ce = float(row["CE Vega"])
    pe = float(row["PE Vega"])
    difference = ce - pe

    prev_ce = st.session_state.get("prev_ce")
    prev_pe = st.session_state.get("prev_pe")
    ce_pct = None if prev_ce in (None, 0) else (ce / prev_ce - 1) * 100
    pe_pct = None if prev_pe in (None, 0) else (pe / prev_pe - 1) * 100

    spike_side = None
    spike_pct = None
    if ce_pct is not None and ce_pct >= spike_threshold:
        spike_side, spike_pct = "ATM CE", ce_pct
    if pe_pct is not None and pe_pct >= spike_threshold and (spike_pct is None or pe_pct >= spike_pct):
        spike_side, spike_pct = "ATM PE", pe_pct

    bucket = int(now.timestamp() // REFRESH_SECONDS)
    history = st.session_state.get("history", [])
    if not history or history[-1]["bucket"] != bucket:
        history.append({"bucket": bucket, "time": now.strftime("%H:%M"), "CE Vega": ce, "PE Vega": pe, "Vega Difference": difference})
    st.session_state.history = history[-60:]

    new_spike = False
    if spike_side:
        spike_key = f"{day}|{bucket}|{spike_side}"
        new_spike = spike_key != st.session_state.get("last_spike")
        if new_spike:
            st.session_state.last_spike = spike_key

    st.session_state.prev_ce = ce
    st.session_state.prev_pe = pe

    cols = st.columns(7)
    cols[0].metric("10:00 ATM STRIKE", f"{locked_strike:,.0f}")
    cols[1].metric("10:00 SPOT", f"₹{st.session_state.atm_spot:,.2f}")
    cols[2].metric("CURRENT SPOT", f"₹{current_spot:,.2f}")
    cols[3].metric("EXPIRY", expiry)
    cols[4].metric("ATM CE VEGA", f"{ce:.3f}", None if ce_pct is None else f"{ce_pct:+.1f}%")
    cols[5].metric("ATM PE VEGA", f"{pe:.3f}", None if pe_pct is None else f"{pe_pct:+.1f}%")
    cols[6].metric("CE − PE VEGA", f"{difference:+.3f}")

    st.markdown("### 🔒 FIXED 10:00 STRADDLE")
    st.dataframe(
        pd.DataFrame([{
            "Strike": locked_strike,
            "CE Vega": ce,
            "PE Vega": pe,
            "CE LTP": row["CE LTP"],
            "PE LTP": row["PE LTP"],
            "CE OI": row["CE OI"],
            "PE OI": row["PE OI"],
            "CE IV": row["CE IV"],
            "PE IV": row["PE IV"],
            "CE Security ID": row["CE Security ID"],
            "PE Security ID": row["PE Security ID"],
            "Refresh": now.strftime("%H:%M:%S"),
        }]),
        use_container_width=True,
        hide_index=True,
    )

    if new_spike:
        st.error(f"⚠️ SUDDEN VEGA SPIKE — {spike_side} {spike_pct:+.1f}% in the last 3-minute sample")
        if sound_enabled:
            warning_sound()
    elif spike_side:
        st.warning(f"⚠️ {spike_side} remains above threshold: {spike_pct:+.1f}%")

    if st.session_state.history:
        st.markdown("### 📈 Fixed-Straddle Vega History")
        hist = pd.DataFrame(st.session_state.history)
        st.line_chart(hist.set_index("time")[["CE Vega", "PE Vega"]], height=260)

    st.caption(
        f"ATM locked at 10:00 IST • Same strike tracked all day • Auto refresh target: every {REFRESH_SECONDS // 60} minutes • Spike threshold: {spike_threshold:.0f}%"
    )


# Native Streamlit timed fragment. It refreshes the actual monitor every 180 seconds.
if hasattr(st, "fragment"):
    @st.fragment(run_every=f"{REFRESH_SECONDS}s")
    def timed_monitor():
        monitor_once()
    timed_monitor()
else:
    st.warning("This Streamlit version does not support timed fragments. Upgrade Streamlit to enable automatic 3-minute refresh.")
    monitor_once()
