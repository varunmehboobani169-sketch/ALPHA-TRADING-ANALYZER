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
ATM_LOCK = time(10, 0)
REFRESH_SECONDS = 180

st.set_page_config(page_title="ALPHA • Fixed 10AM Vega", page_icon="📊", layout="wide")


def dhan_request(path, client_id, token, method="GET", body=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }
    payload = json.dumps(body).encode() if body is not None else None
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
            d = datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= today:
            valid.append(d.isoformat())
    return sorted(set(valid))


def resolve_expiry(client_id, token, security_id, segment, manual):
    today = datetime.now(IST).date()
    if manual:
        try:
            parsed = datetime.strptime(manual, "%Y-%m-%d").date()
        except ValueError as exc:
            raise RuntimeError("Expiry must be YYYY-MM-DD.") from exc
        if parsed < today:
            raise RuntimeError("Expiry is in the past.")
        return parsed.isoformat()

    cached = st.session_state.get("expiry_cache")
    cached_day = st.session_state.get("expiry_cache_day")
    if cached and cached_day == today.isoformat():
        return cached[0]

    expiries = get_expiries(client_id, token, security_id, segment)
    if not expiries:
        raise RuntimeError("Dhan returned no valid future expiries for this underlying.")
    st.session_state.expiry_cache = expiries
    st.session_state.expiry_cache_day = today.isoformat()
    return expiries[0]


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
    data = response.get("data", {}) if isinstance(response, dict) else {}
    if not isinstance(data, dict):
        raise RuntimeError("Dhan returned an invalid option-chain response.")
    return data


def spot_at_10(client_id, token, security_id, segment, day):
    body = {
        "securityId": str(security_id),
        "exchangeSegment": segment,
        "instrument": "INDEX",
        "interval": "1",
        "oi": False,
        "fromDate": f"{day} 09:15:00",
        "toDate": f"{day} 10:01:00",
    }
    response = dhan_request("/charts/intraday", client_id, token, method="POST", body=body)
    timestamps = response.get("timestamp") or []
    closes = response.get("close") or []
    n = min(len(timestamps), len(closes))
    if not n:
        return None
    times = pd.to_datetime(timestamps[:n], unit="s", utc=True).dt.tz_convert(IST).dt.tz_localize(None)
    frame = pd.DataFrame({"timestamp": times, "close": pd.to_numeric(closes[:n], errors="coerce")}).dropna()
    exact = frame[frame["timestamp"].dt.strftime("%H:%M") == "10:00"]
    if exact.empty:
        exact = frame[frame["timestamp"].dt.time <= ATM_LOCK].tail(1)
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
    return spot, frame.sort_values("Strike").reset_index(drop=True) if not frame.empty else frame


def beep_uri():
    sr = 22050
    duration = 0.65
    freq = 880
    half_period = max(1, int(sr / (freq * 2)))
    frames = [
        struct.pack("<h", 12000 if (i // half_period) % 2 == 0 else -12000)
        for i in range(int(sr * duration))
    ]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(b"".join(frames))
    return "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode()


def play_warning():
    uri = beep_uri()
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
        cid = st.text_input("Dhan Client ID / Username", st.session_state.dhan_client_id)
        token = st.text_input("Dhan Access Token", st.session_state.dhan_token, type="password")
        connect = st.form_submit_button("🔗 CONNECT TO DHAN", use_container_width=True, type="primary")
    if connect:
        try:
            cid, token = cid.strip(), token.strip()
            dhan_request("/profile", cid, token)
            st.session_state.dhan_client_id = cid
            st.session_state.dhan_token = token
            st.session_state.dhan_connected = True
            st.rerun()
        except Exception as exc:
            st.sidebar.error(str(exc))
    if st.session_state.dhan_connected and st.sidebar.button("LOGOUT / CLEAR DHAN", use_container_width=True):
        st.session_state.dhan_client_id = ""
        st.session_state.dhan_token = ""
        st.session_state.dhan_connected = False
        st.rerun()


login_box()
st.sidebar.markdown("## ⚙️ SETTINGS")
security_id = st.sidebar.number_input("NIFTY 50 Security ID", min_value=1, value=13, step=1)
segment = st.sidebar.selectbox("Underlying Segment", ["IDX_I", "NSE_FNO"])
manual_expiry = st.sidebar.text_input("Expiry (YYYY-MM-DD)", value="")
spike_threshold = st.sidebar.number_input("Sudden Vega Spike %", 1.0, 200.0, 20.0, 1.0)
sound_enabled = st.sidebar.checkbox("🔊 Warning Sound", True)

st.title("📊 ALPHA • Fixed 10AM ATM Vega Monitor")
st.caption("10:00 IST ATM is locked once and the same CE + PE straddle is monitored every 3 minutes.")

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
            "prev_ce", "prev_pe", "last_spike", "history", "expiry_cache", "expiry_cache_day"
        ]:
            st.session_state.pop(key, None)
        st.session_state.vega_day = day

    try:
        expiry = resolve_expiry(cid, token, security_id, segment, manual_expiry.strip())
    except Exception as exc:
        st.error(f"Expiry resolution failed: {exc}")
        return

    if st.session_state.get("atm_expiry") and st.session_state.atm_expiry != expiry:
        for key in ["atm_locked", "atm_strike", "atm_spot", "last_chain", "prev_ce", "prev_pe", "history"]:
            st.session_state.pop(key, None)
    st.session_state.atm_expiry = expiry

    if not st.session_state.get("atm_locked"):
        if now.time() < ATM_LOCK:
            st.warning("⏳ Waiting for 10:00 IST to lock ATM.")
            return
        try:
            reference_spot = spot_at_10(cid, token, security_id, segment, day)
            if reference_spot is None:
                st.warning("10:00 NIFTY candle is not available yet. Next cycle will retry.")
                return
            chain_data = option_chain(cid, token, security_id, segment, expiry)
            _, frame = chain_frame(chain_data)
            if frame.empty:
                st.error("Dhan returned an empty option chain.")
                return
            locked = float(frame.iloc[(frame["Strike"] - reference_spot).abs().argmin()]["Strike"])
            st.session_state.atm_locked = True
            st.session_state.atm_strike = locked
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
                st.warning("⚠️ Dhan rate limit active. Showing last good snapshot; no retry until the next 3-minute cycle.")
            else:
                st.warning(f"⚠️ Refresh failed. Showing last good snapshot: {exc}")

    current_spot, frame = chain_frame(chain_data)
    if frame.empty:
        st.error("No option-chain strikes returned.")
        return

    locked_strike = float(st.session_state.atm_strike)
    row = frame.iloc[(frame["Strike"] - locked_strike).abs().argmin()]
    ce = float(row["CE Vega"])
    pe = float(row["PE Vega"])
    diff = ce - pe
    prev_ce = st.session_state.get("prev_ce")
    prev_pe = st.session_state.get("prev_pe")
    ce_pct = None if prev_ce in (None, 0) else (ce / prev_ce - 1) * 100
    pe_pct = None if prev_pe in (None, 0) else (pe / prev_pe - 1) * 100

    side = None
    pct = None
    if ce_pct is not None and ce_pct >= spike_threshold:
        side, pct = "ATM CE", ce_pct
    if pe_pct is not None and pe_pct >= spike_threshold and (pct is None or pe_pct >= pct):
        side, pct = "ATM PE", pe_pct

    bucket = int(now.timestamp() // REFRESH_SECONDS)
    history = st.session_state.get("history", [])
    if not history or history[-1]["bucket"] != bucket:
        history.append({"bucket": bucket, "time": now.strftime("%H:%M"), "CE Vega": ce, "PE Vega": pe, "Vega Difference": diff})
    st.session_state.history = history[-60:]

    spike_key = f"{day}|{bucket}|{side}"
    new_spike = bool(side and spike_key != st.session_state.get("last_spike"))
    if new_spike:
        st.session_state.last_spike = spike_key
    st.session_state.prev_ce = ce
    st.session_state.prev_pe = pe

    if now.time() > time(15, 20):
        st.info("Session is past 15:20 IST. Fixed ATM remains available for reference.")

    cols = st.columns(7)
    cols[0].metric("10:00 ATM STRIKE", f"{locked_strike:,.0f}")
    cols[1].metric("10:00 SPOT", f"₹{st.session_state.atm_spot:,.2f}")
    cols[2].metric("CURRENT SPOT", f"₹{current_spot:,.2f}")
    cols[3].metric("EXPIRY", expiry)
    cols[4].metric("ATM CE VEGA", f"{ce:.3f}", None if ce_pct is None else f"{ce_pct:+.1f}%")
    cols[5].metric("ATM PE VEGA", f"{pe:.3f}", None if pe_pct is None else f"{pe_pct:+.1f}%")
    cols[6].metric("CE − PE VEGA", f"{diff:+.3f}")

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
        st.error(f"⚠️ SUDDEN VEGA SPIKE — {side} {pct:+.1f}% in the last 3-minute sample")
        if sound_enabled:
            play_warning()
    elif side:
        st.warning(f"⚠️ {side} remains above the spike threshold: {pct:+.1f}%.")

    if history:
        st.markdown("### 📈 Fixed-Straddle Vega History")
        st.line_chart(pd.DataFrame(history).set_index("time")[["CE Vega", "PE Vega"]], height=280)

    st.caption(f"Updated {now.strftime('%d-%b-%Y %H:%M:%S')} IST • next refresh in ~{REFRESH_SECONDS // 60} minutes")


if hasattr(st, "fragment"):
    @st.fragment(run_every=f"{REFRESH_SECONDS}s")
    def live_monitor():
        monitor_once()
    live_monitor()
else:
    monitor_once()
