import base64
import io
import json
import math
import struct
import threading
import time as time_module
import urllib.error
import urllib.request
import wave
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

try:
    import websocket
except ImportError:
    websocket = None

DHAN_API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
ATM_LOCK_TIME = time(10, 0)
DEFAULT_CLIENT_ID = "1113195747"
WS_URL = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2"

st.set_page_config(page_title="ALPHA • Fixed 10AM Vega", page_icon="📊", layout="wide")


def dhan_request(path, client_id, token, method="GET", body=None):
    headers = {"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client_id}
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


def valid_expiries(client_id, token, security_id, segment):
    response = dhan_request("/optionchain/expirylist", client_id, token, "POST", {"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment})
    values = response.get("data") if isinstance(response, dict) else None
    if not isinstance(values, list):
        return []
    today = datetime.now(IST).date()
    out = []
    for value in values:
        try:
            d = datetime.strptime(str(value), "%Y-%m-%d").date()
            if d >= today:
                out.append(d.isoformat())
        except ValueError:
            pass
    return sorted(set(out))


def resolve_expiry(client_id, token, security_id, segment, manual):
    today = datetime.now(IST).date()
    manual = manual.strip()
    if manual:
        try:
            d = datetime.strptime(manual, "%Y-%m-%d").date()
        except ValueError as exc:
            raise RuntimeError("Expiry must be YYYY-MM-DD.") from exc
        if d < today:
            raise RuntimeError("The selected expiry is in the past.")
        return d.isoformat()
    cache_key = f"{security_id}|{segment}|{today.isoformat()}"
    if st.session_state.get("expiry_cache_key") == cache_key and st.session_state.get("expiry_cache"):
        return st.session_state.expiry_cache[0]
    values = valid_expiries(client_id, token, security_id, segment)
    if not values:
        raise RuntimeError("Dhan returned no valid future NIFTY expiries.")
    st.session_state.expiry_cache_key = cache_key
    st.session_state.expiry_cache = values
    return values[0]


def option_chain(client_id, token, security_id, segment, expiry):
    response = dhan_request("/optionchain", client_id, token, "POST", {"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment, "Expiry": expiry})
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Dhan returned an invalid option-chain response.")
    return data


def spot_at_10(client_id, token, security_id, segment, day):
    response = dhan_request("/charts/intraday", client_id, token, "POST", {
        "securityId": str(security_id), "exchangeSegment": segment, "instrument": "INDEX", "interval": "1", "oi": False,
        "fromDate": f"{day} 09:15:00", "toDate": f"{day} 10:01:00"
    })
    timestamps = response.get("timestamp") or []
    closes = response.get("close") or []
    n = min(len(timestamps), len(closes))
    if not n:
        return None
    times = pd.Series(pd.to_datetime(timestamps[:n], unit="s", utc=True)).dt.tz_convert(IST).dt.tz_localize(None)
    frame = pd.DataFrame({"timestamp": times, "close": pd.to_numeric(closes[:n], errors="coerce")}).dropna()
    exact = frame[frame["timestamp"].dt.strftime("%H:%M") == "10:00"]
    if exact.empty:
        exact = frame[frame["timestamp"].dt.time <= ATM_LOCK_TIME].tail(1)
    return None if exact.empty else float(exact.iloc[-1]["close"])


def chain_rows(data):
    spot = float(data.get("last_price", 0) or 0)
    rows = []
    for strike_text, node in (data.get("oc") or {}).items():
        try:
            strike = float(strike_text)
        except (TypeError, ValueError):
            continue
        for side, key in (("CE", "ce"), ("PE", "pe")):
            leg = node.get(key) or {}
            rows.append({"Strike": strike, "Side": side, "LTP": float(leg.get("last_price", 0) or 0), "Security ID": str(leg.get("security_id", ""))})
    return spot, pd.DataFrame(rows)


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, years, rate, vol, call):
    if spot <= 0 or strike <= 0 or years <= 0 or vol <= 0:
        return 0.0
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * root)
    d2 = d1 - vol * root
    disc_k = strike * math.exp(-rate * years)
    if call:
        return spot * norm_cdf(d1) - disc_k * norm_cdf(d2)
    return disc_k * norm_cdf(-d2) - spot * norm_cdf(-d1)


def implied_vol(price, spot, strike, expiry_dt, rate, call):
    if min(price, spot, strike) <= 0:
        return None
    years = max((expiry_dt - datetime.now(timezone.utc)).total_seconds() / (365.0 * 24 * 3600), 1e-8)
    intrinsic = max(0.0, spot - strike) if call else max(0.0, strike - spot)
    if price < intrinsic * 0.98:
        return None
    lo, hi = 1e-5, 5.0
    if bs_price(spot, strike, years, rate, hi, call) < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        value = bs_price(spot, strike, years, rate, mid, call)
        if value > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def bs_vega(spot, strike, years, rate, vol):
    if not vol or spot <= 0 or strike <= 0 or years <= 0:
        return None
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * root)
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return spot * pdf * root


def calculate_vega(spot, strike, ltp, expiry, call, rate):
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        expiry_dt = datetime.combine(expiry_date, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
        years = max((expiry_dt - datetime.now(timezone.utc)).total_seconds() / (365.0 * 24 * 3600), 1e-8)
        iv = implied_vol(ltp, spot, strike, expiry_dt, rate, call)
        vega = bs_vega(spot, strike, years, rate, iv) if iv else None
        return iv, vega
    except Exception:
        return None, None


class DhanWSFeed:
    def __init__(self, client_id, token, instruments):
        self.client_id = client_id
        self.token = token
        self.instruments = instruments
        self.data = {}
        self.status = "starting"
        self.error = None
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        if websocket is None:
            self.status = "websocket-client missing"
            return
        while not self._stop:
            ws = None
            try:
                self.status = "connecting"
                url = WS_URL.format(token=self.token, client=self.client_id)
                ws = websocket.create_connection(url, timeout=20, ping_interval=10, ping_timeout=5)
                payload = {"RequestCode": 15, "InstrumentCount": len(self.instruments), "InstrumentList": [
                    {"ExchangeSegment": seg, "SecurityId": str(sec)} for seg, sec in self.instruments
                ]}
                ws.send(json.dumps(payload))
                self.status = "connected"
                self.error = None
                while not self._stop:
                    packet = ws.recv()
                    if not packet:
                        continue
                    if isinstance(packet, str):
                        continue
                    raw = bytes(packet)
                    if len(raw) < 17:
                        continue
                    code = raw[0]
                    if code == 2:
                        sec = str(struct.unpack_from("<i", raw, 4)[0])
                        ltp = float(struct.unpack_from("<f", raw, 8)[0])
                        ltt = int(struct.unpack_from("<i", raw, 12)[0])
                        if ltp > 0:
                            self.data[sec] = {"ltp": ltp, "ltt": ltt, "updated": time_module.time()}
                    elif code == 50 and len(raw) >= 11:
                        reason = struct.unpack_from("<h", raw, 9)[0]
                        self.error = f"Dhan WebSocket disconnect code {reason}"
                        break
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.status = "reconnecting" if not self._stop else "stopped"
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass
            if not self._stop:
                time_module.sleep(3)

    def stop(self):
        self._stop = True


@st.cache_resource(show_spinner=False)
def get_feed(client_id, token, instruments):
    return DhanWSFeed(client_id, token, tuple(instruments))


def warning_sound():
    sample_rate, duration, frequency = 22050, 0.7, 880.0
    half_period = max(1, int(sample_rate / (frequency * 2)))
    frames = [struct.pack("<h", 12000 if (i // half_period) % 2 == 0 else -12000) for i in range(int(sample_rate * duration))]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate); wav.writeframes(b"".join(frames))
    uri = "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode()
    st.components.v1.html(f"<audio autoplay><source src='{uri}' type='audio/wav'></audio>", height=1)


def login_box():
    st.sidebar.markdown("## 🔐 DHAN CONNECTION")
    st.sidebar.caption("Credentials stay in Streamlit session state only.")
    st.session_state.setdefault("dhan_client_id", DEFAULT_CLIENT_ID)
    st.session_state.setdefault("dhan_token", "")
    st.session_state.setdefault("dhan_connected", False)
    with st.sidebar.form("dhan_login", clear_on_submit=False):
        cid = st.text_input("Dhan Client ID / Username", value=st.session_state.dhan_client_id)
        token = st.text_input("Dhan Access Token", value=st.session_state.dhan_token, type="password")
        connect = st.form_submit_button("🔗 CONNECT TO DHAN", use_container_width=True, type="primary")
    if connect:
        cid, token = cid.strip(), token.strip()
        try:
            dhan_request("/profile", cid, token)
            st.session_state.dhan_client_id, st.session_state.dhan_token = cid, token
            st.session_state.dhan_connected = True
            st.rerun()
        except Exception as exc:
            st.session_state.dhan_connected = False
            st.sidebar.error(f"Dhan connection failed: {exc}")
    if st.session_state.dhan_connected:
        st.sidebar.success(f"Connected: {st.session_state.dhan_client_id}")
        if st.sidebar.button("LOGOUT / CLEAR DHAN", use_container_width=True):
            st.session_state.dhan_connected = False
            st.session_state.dhan_token = ""
            st.cache_resource.clear()
            st.rerun()


login_box()
st.sidebar.markdown("## ⚙️ SETTINGS")
security_id = st.sidebar.number_input("NIFTY 50 Security ID", min_value=1, value=13, step=1)
segment = st.sidebar.selectbox("Underlying Segment", ["IDX_I", "NSE_FNO"])
manual_expiry = st.sidebar.text_input("Expiry (YYYY-MM-DD)", value="")
risk_free = st.sidebar.number_input("Risk-free rate %", min_value=0.0, max_value=15.0, value=6.0, step=0.25) / 100
spike_threshold = st.sidebar.number_input("Sudden Vega Spike %", min_value=1.0, max_value=200.0, value=20.0, step=1.0)
sound_enabled = st.sidebar.checkbox("🔊 Warning Sound", value=True)

st.title("📊 ALPHA • Fixed 10AM ATM Vega Monitor — WebSocket")
st.caption("REST is used only for daily setup. Live CE/PE/spot prices are streamed through Dhan WebSocket, eliminating 3-minute Option Chain polling and the repeated 805/429 problem.")

if not st.session_state.dhan_connected:
    st.info("Connect your Dhan account from the sidebar.")
    st.stop()

cid, token = st.session_state.dhan_client_id, st.session_state.dhan_token
now = datetime.now(IST)
day = now.date().isoformat()
if st.session_state.get("vega_day") != day:
    for key in ["vega_day", "atm_locked", "atm_strike", "atm_spot", "atm_expiry", "feed_key", "prev_ce", "prev_pe", "last_spike", "history", "expiry_cache", "expiry_cache_key"]:
        st.session_state.pop(key, None)
    st.session_state.vega_day = day

try:
    expiry = resolve_expiry(cid, token, security_id, segment, manual_expiry)
except Exception as exc:
    st.error(f"Expiry resolution failed: {exc}")
    st.stop()

if st.session_state.get("atm_expiry") != expiry:
    for key in ["atm_locked", "atm_strike", "atm_spot", "feed_key", "prev_ce", "prev_pe", "history", "last_spike"]:
        st.session_state.pop(key, None)
    st.session_state.atm_expiry = expiry

if not st.session_state.get("atm_locked"):
    if now.time() < ATM_LOCK_TIME:
        st.warning("⏳ Waiting for 10:00 IST to lock the ATM strike.")
        st.stop()
    try:
        reference_spot = spot_at_10(cid, token, security_id, segment, day)
        if reference_spot is None:
            st.warning("10:00 NIFTY reference candle is unavailable. Retry after the next refresh.")
            st.stop()
        chain_data = option_chain(cid, token, security_id, segment, expiry)
        chain_spot, rows = chain_rows(chain_data)
        if rows.empty:
            raise RuntimeError("Dhan returned an empty option chain.")
        strikes = rows["Strike"].drop_duplicates().tolist()
        strike = min(strikes, key=lambda x: abs(x - reference_spot))
        ce_row = rows[(rows["Strike"] == strike) & (rows["Side"] == "CE")].iloc[0]
        pe_row = rows[(rows["Strike"] == strike) & (rows["Side"] == "PE")].iloc[0]
        st.session_state.atm_locked = True
        st.session_state.atm_strike = float(strike)
        st.session_state.atm_spot = float(reference_spot)
        st.session_state.ce_security = str(ce_row["Security ID"])
        st.session_state.pe_security = str(pe_row["Security ID"])
        st.session_state.history = []
        st.session_state.prev_ce = None
        st.session_state.prev_pe = None
        st.session_state.last_spike = None
    except Exception as exc:
        st.error(f"Unable to lock ATM: {exc}")
        st.stop()

instruments = [(segment, str(security_id)), ("NSE_FNO", st.session_state.ce_security), ("NSE_FNO", st.session_state.pe_security)]
feed = get_feed(cid, token, tuple(instruments))

spot_packet = feed.data.get(str(security_id), {})
ce_packet = feed.data.get(st.session_state.ce_security, {})
pe_packet = feed.data.get(st.session_state.pe_security, {})
spot = float(spot_packet.get("ltp", st.session_state.atm_spot) or st.session_state.atm_spot)
ce_ltp = float(ce_packet.get("ltp", 0) or 0)
pe_ltp = float(pe_packet.get("ltp", 0) or 0)

ce_iv, ce_vega = calculate_vega(spot, st.session_state.atm_strike, ce_ltp, expiry, True, risk_free) if ce_ltp > 0 else (None, None)
pe_iv, pe_vega = calculate_vega(spot, st.session_state.atm_strike, pe_ltp, expiry, False, risk_free) if pe_ltp > 0 else (None, None)

if ce_vega is not None and pe_vega is not None:
    prev_ce, prev_pe = st.session_state.get("prev_ce"), st.session_state.get("prev_pe")
    ce_pct = None if not prev_ce else (ce_vega / prev_ce - 1) * 100
    pe_pct = None if not prev_pe else (pe_vega / prev_pe - 1) * 100
    spike_side, spike_pct = None, None
    if ce_pct is not None and ce_pct >= spike_threshold:
        spike_side, spike_pct = "ATM CE", ce_pct
    if pe_pct is not None and pe_pct >= spike_threshold and (spike_pct is None or pe_pct >= spike_pct):
        spike_side, spike_pct = "ATM PE", pe_pct
    bucket = int(now.timestamp() // 180)
    history = st.session_state.get("history", [])
    if not history or history[-1]["bucket"] != bucket:
        history.append({"bucket": bucket, "time": now.strftime("%H:%M:%S"), "CE Vega": ce_vega, "PE Vega": pe_vega, "Vega Difference": ce_vega - pe_vega})
        st.session_state.history = history[-60:]
    new_spike = bool(spike_side and st.session_state.get("last_spike") != f"{bucket}|{spike_side}")
    if new_spike:
        st.session_state.last_spike = f"{bucket}|{spike_side}"
        if sound_enabled:
            warning_sound()
    st.session_state.prev_ce, st.session_state.prev_pe = ce_vega, pe_vega
else:
    ce_pct = pe_pct = None
    spike_side = spike_pct = None

if spike_side:
    st.error(f"⚠️ SUDDEN VEGA SPIKE — {spike_side}: +{spike_pct:.1f}%")

c = st.columns(7)
c[0].metric("🔒 10:00 ATM", f"{st.session_state.atm_strike:,.0f}")
c[1].metric("10:00 SPOT", f"₹{st.session_state.atm_spot:,.2f}")
c[2].metric("LIVE SPOT", f"₹{spot:,.2f}")
c[3].metric("EXPIRY", expiry)
c[4].metric("CE VEGA", "—" if ce_vega is None else f"{ce_vega:.3f}", None if ce_pct is None else f"{ce_pct:+.1f}%")
c[5].metric("PE VEGA", "—" if pe_vega is None else f"{pe_vega:.3f}", None if pe_pct is None else f"{pe_pct:+.1f}%")
c[6].metric("VEGA DIFFERENCE", "—" if ce_vega is None or pe_vega is None else f"{ce_vega - pe_vega:+.3f}")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(pd.DataFrame([{
    "Strike": st.session_state.atm_strike,
    "CE Security ID": st.session_state.ce_security,
    "PE Security ID": st.session_state.pe_security,
    "CE LTP": ce_ltp or None,
    "PE LTP": pe_ltp or None,
    "CE IV %": None if ce_iv is None else ce_iv * 100,
    "PE IV %": None if pe_iv is None else pe_iv * 100,
    "CE Vega": ce_vega,
    "PE Vega": pe_vega,
}]), use_container_width=True, hide_index=True)

history = st.session_state.get("history", [])
if history:
    st.markdown("### 📈 Vega history — one sample every 3 minutes")
    st.line_chart(pd.DataFrame(history).set_index("time")[["CE Vega", "PE Vega", "Vega Difference"]])

st.success(f"🟢 WebSocket: {feed.status} | Live ticks: {len(feed.data)} | ATM locked: {st.session_state.atm_strike:,.0f} | REST Option Chain polling: OFF")
if feed.error:
    st.warning(f"WebSocket notice: {feed.error}")
st.caption(f"Dashboard UI refreshes every few seconds; no Dhan REST polling is used for live monitoring. 3-minute history buckets remain for your requested view.")

# Streamlit fragment refreshes the UI without creating any additional Dhan REST requests.
try:
    st.fragment(run_every="3s")
except Exception:
    pass
