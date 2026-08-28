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
from streamlit_autorefresh import st_autorefresh

try:
    import websocket
except ImportError:
    websocket = None

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_CLIENT_ID = "1113195747"
LOCK_TIME = time(10, 0)
UI_REFRESH_MS = 3000
COMPARE_SECONDS = 180
SETUP_RETRY_SECONDS = 60
WS_URL = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2"

st.set_page_config(page_title="ALPHA Fixed 10AM Vega + OI", page_icon="📊", layout="wide")

DEFAULTS = {
    "cid": DEFAULT_CLIENT_ID, "token": "", "connected": False,
    "day": "", "locked": False, "setup_retry_at": 0.0, "setup_error": "",
    "expiry_cache": [], "expiry_day": "", "expiry": "", "strike": None,
    "spot10": None, "ce_id": "", "pe_id": "", "wing_ids": {},
    "instruments": tuple(), "prev_ce": None, "prev_pe": None,
    "prev_atm_iv": None, "history": [], "last_alert": None,
    "sample_bucket": None, "sample": {"atm_iv": None, "legs": {}},
}
for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value.copy() if isinstance(value, dict) else list(value) if isinstance(value, list) else value


def api_request(path, client_id, token, method="GET", body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client_id},
        data=None if body is None else json.dumps(body).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"API HTTP {exc.code}: {detail}") from exc


def get_expiries(client_id, token):
    payload = api_request("/optionchain/expirylist", client_id, token, "POST", {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"})
    today = datetime.now(IST).date()
    result = []
    for raw in payload.get("data", []) if isinstance(payload, dict) else []:
        try:
            d = datetime.strptime(str(raw), "%Y-%m-%d").date()
            if d >= today:
                result.append(d.isoformat())
        except ValueError:
            pass
    return sorted(set(result))


def get_chain(client_id, token, expiry):
    payload = api_request("/optionchain", client_id, token, "POST", {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": expiry})
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Invalid option-chain response")
    return data


def get_reference_spot(client_id, token):
    day = datetime.now(IST).date().isoformat()
    payload = api_request(
        "/charts/intraday", client_id, token, "POST",
        {"securityId": "13", "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": "1", "oi": False,
         "fromDate": f"{day} 09:15:00", "toDate": f"{day} 10:01:00"},
    )
    ts, close = payload.get("timestamp", []), payload.get("close", [])
    n = min(len(ts), len(close))
    if not n:
        return None
    times = pd.Series(pd.to_datetime(ts[:n], unit="s", utc=True)).dt.tz_convert(IST).dt.tz_localize(None)
    frame = pd.DataFrame({"t": times, "c": pd.to_numeric(close[:n], errors="coerce")}).dropna()
    exact = frame[frame.t.dt.strftime("%H:%M") == "10:00"]
    if exact.empty:
        exact = frame[frame.t.dt.time <= LOCK_TIME].tail(1)
    return None if exact.empty else float(exact.iloc[-1].c)


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(s, k, T, r, vol, is_call):
    if min(s, k, T, vol) <= 0:
        return 0.0
    root = math.sqrt(T)
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * T) / (vol * root)
    d2 = d1 - vol * root
    disc_k = k * math.exp(-r * T)
    return s * norm_cdf(d1) - disc_k * norm_cdf(d2) if is_call else disc_k * norm_cdf(-d2) - s * norm_cdf(-d1)


def implied_vol_vega(spot, strike, premium, expiry, is_call, rate):
    try:
        if not premium or spot <= 0 or strike <= 0:
            return None, None
        expiry_dt = datetime.combine(datetime.strptime(expiry, "%Y-%m-%d").date(), time(15, 30), tzinfo=IST).astimezone(timezone.utc)
        T = max((expiry_dt - datetime.now(timezone.utc)).total_seconds() / 31536000.0, 1e-7)
        intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
        if premium < intrinsic * 0.98:
            return None, None
        lo, hi = 1e-5, 5.0
        if bs_price(spot, strike, T, rate, hi, is_call) < premium:
            return None, None
        for _ in range(60):
            mid = (lo + hi) / 2
            if bs_price(spot, strike, T, rate, mid, is_call) > premium:
                hi = mid
            else:
                lo = mid
        vol = (lo + hi) / 2
        d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
        vega = spot * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * math.sqrt(T)
        return vol, vega
    except Exception:
        return None, None


def change_pct(previous, current):
    if previous in (None, 0) or current is None:
        return None
    return (current / previous - 1.0) * 100.0


class MarketFeed:
    def __init__(self, client_id, token, instruments):
        self.client_id = client_id
        self.token = token
        self.instruments = tuple(instruments)
        self.data = {}
        self.status = "starting"
        self.error = ""
        self.stop_flag = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        if websocket is None:
            self.status = "feed library unavailable"
            return
        while not self.stop_flag:
            ws = None
            try:
                self.status = "connecting"
                ws = websocket.create_connection(
                    WS_URL.format(token=self.token, client=self.client_id),
                    timeout=20,
                    ping_interval=10,
                    ping_timeout=5,
                )
                ws.send(json.dumps({
                    "RequestCode": 17,
                    "InstrumentCount": len(self.instruments),
                    "InstrumentList": [{"ExchangeSegment": segment, "SecurityId": str(security_id)} for segment, security_id in self.instruments],
                }))
                self.status = "connected"
                self.error = ""
                while not self.stop_flag:
                    packet = ws.recv()
                    if not packet or isinstance(packet, str):
                        continue
                    raw = bytes(packet)
                    if len(raw) < 8:
                        continue
                    code = raw[0]
                    security_id = str(struct.unpack_from("<i", raw, 4)[0])
                    if code == 2 and len(raw) >= 17:
                        self.data.setdefault(security_id, {}).update({
                            "ltp": float(struct.unpack_from("<f", raw, 9)[0]),
                            "ltt": int(struct.unpack_from("<i", raw, 13)[0]),
                            "updated": time_module.time(),
                        })
                    elif code == 4 and len(raw) >= 51:
                        self.data.setdefault(security_id, {}).update({
                            "ltp": float(struct.unpack_from("<f", raw, 9)[0]),
                            "ltt": int(struct.unpack_from("<i", raw, 15)[0]),
                            "volume": int(struct.unpack_from("<i", raw, 23)[0]),
                            "sell_qty": int(struct.unpack_from("<i", raw, 27)[0]),
                            "buy_qty": int(struct.unpack_from("<i", raw, 31)[0]),
                            "updated": time_module.time(),
                        })
                    elif code == 5 and len(raw) >= 13:
                        self.data.setdefault(security_id, {}).update({
                            "oi": int(struct.unpack_from("<i", raw, 9)[0]),
                            "updated": time_module.time(),
                        })
                    elif code == 50:
                        self.error = "Live feed disconnected"
                        break
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.status = "reconnecting" if not self.stop_flag else "stopped"
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass
            if not self.stop_flag:
                time_module.sleep(3)

    def stop(self):
        self.stop_flag = True


@st.cache_resource(show_spinner=False)
def get_market_feed(client_id, token, instruments):
    return MarketFeed(client_id, token, instruments)


def warning_sound():
    sample_rate, duration, frequency = 22050, 0.7, 880.0
    half_period = max(1, int(sample_rate / (frequency * 2)))
    raw = b"".join(struct.pack("<h", 12000 if (i // half_period) % 2 == 0 else -12000) for i in range(int(sample_rate * duration)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(raw)
    uri = "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode()
    st.components.v1.html(f"<audio autoplay><source src='{uri}' type='audio/wav'></audio>", height=1)


# --- shared login values ---
with st.sidebar.form("connection"):
    st.markdown("## 🔐 MARKET DATA CONNECTION")
    client_input = st.text_input("Client ID", st.session_state.cid)
    token_input = st.text_input("Access Token", st.session_state.token, type="password")
    connect = st.form_submit_button("CONNECT", use_container_width=True)
if connect:
    try:
        api_request("/profile", client_input.strip(), token_input.strip())
        st.session_state.cid = client_input.strip()
        st.session_state.token = token_input.strip()
        st.session_state.connected = True
        st.cache_resource.clear()
        st.session_state.setup_retry_at = 0.0
        st.session_state.setup_error = ""
        st.rerun()
    except Exception as exc:
        st.sidebar.error(f"Connection failed: {exc}")
if not st.session_state.connected:
    st.info("Enter your access token and connect to the market data feed.")
    st.stop()
if st.sidebar.button("LOGOUT / CLEAR", use_container_width=True):
    st.session_state.connected = False
    st.session_state.token = ""
    st.cache_resource.clear()
    st.rerun()

vega_spike_threshold = st.sidebar.number_input("Vega spike %", 1.0, 200.0, 20.0, 1.0)
risk_free_rate = st.sidebar.number_input("Risk-free rate %", 0.0, 15.0, 6.0, 0.25) / 100.0
sound_enabled = st.sidebar.checkbox("🔊 Warning sound", True)
manual_expiry = st.sidebar.text_input("Expiry YYYY-MM-DD", "")
oi_threshold = st.sidebar.number_input("Wing OI significant move %", 0.5, 50.0, 2.0, 0.5)
atm_iv_threshold = st.sidebar.number_input("ATM IV rise confirmation %", 0.2, 20.0, 1.0, 0.2)
st_autorefresh(interval=UI_REFRESH_MS, key="vega_v11_ui")

cid, token = st.session_state.cid, st.session_state.token
now = datetime.now(IST)
today_key = now.date().isoformat()

if st.session_state.day != today_key:
    for key in ("locked", "setup_retry_at", "setup_error", "expiry_cache", "expiry_day", "expiry", "strike", "spot10", "ce_id", "pe_id", "wing_ids", "instruments", "prev_ce", "prev_pe", "prev_atm_iv", "history", "last_alert", "sample", "sample_bucket"):
        st.session_state.pop(key, None)
    for key, value in DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value.copy() if isinstance(value, dict) else list(value) if isinstance(value, list) else value
    st.session_state.day = today_key

# --- expiry is fetched at most once per day; failures are cooled down ---
if manual_expiry:
    try:
        manual_date = datetime.strptime(manual_expiry.strip(), "%Y-%m-%d").date()
        if manual_date < now.date():
            raise ValueError
        expiry = manual_date.isoformat()
    except ValueError:
        st.error("Expiry must be a valid current/future YYYY-MM-DD date.")
        st.stop()
else:
    if st.session_state.expiry_day != today_key or not st.session_state.expiry_cache:
        if time_module.time() >= st.session_state.setup_retry_at:
            try:
                st.session_state.expiry_cache = get_expiries(cid, token)
                st.session_state.expiry_day = today_key
                st.session_state.setup_retry_at = 0.0
                st.session_state.setup_error = ""
            except Exception as exc:
                st.session_state.setup_retry_at = time_module.time() + SETUP_RETRY_SECONDS
                st.session_state.setup_error = str(exc)
    if not st.session_state.expiry_cache:
        wait_s = max(0, int(st.session_state.setup_retry_at - time_module.time()))
        st.title("📊 ALPHA • Fixed 10AM Vega + Seller OI Monitor")
        if st.session_state.setup_error:
            st.warning(f"Setup temporarily unavailable. No repeat request for {wait_s}s. Last error: {st.session_state.setup_error}")
        else:
            st.info("Preparing the daily market-data setup…")
        st.stop()
    expiry = st.session_state.expiry_cache[0]

st.title("📊 ALPHA • Fixed 10AM Vega + Seller OI Monitor")
st.caption("10:00 reference strike is fixed for the day. Live prices and OI stream continuously; setup requests are throttled and live monitoring is feed-only.")

# --- one-shot reference strike setup; never retried on every UI rerun ---
if not st.session_state.locked:
    if now.time() < LOCK_TIME:
        st.warning("⏳ Waiting for 10:00 IST to lock the reference strike.")
        st.stop()
    if time_module.time() < st.session_state.setup_retry_at:
        wait_s = int(st.session_state.setup_retry_at - time_module.time())
        st.warning(f"⏸️ Reference-strike setup is cooling down. Next controlled retry in {wait_s}s.")
        st.stop()
    try:
        # At most one reference-price request and one option-chain request per controlled attempt.
        spot10 = get_reference_spot(cid, token)
        if spot10 is None:
            raise RuntimeError("The 10:00 reference candle is not available.")
        chain_data = get_chain(cid, token, expiry)
        rows = []
        for raw_strike, node in (chain_data.get("oc") or {}).items():
            try:
                strike_value = float(raw_strike)
            except Exception:
                continue
            for side, key in (("CE", "ce"), ("PE", "pe")):
                security_id = (node.get(key) or {}).get("security_id")
                if security_id:
                    rows.append((strike_value, side, str(security_id)))
        strikes = sorted({row[0] for row in rows})
        if not strikes:
            raise RuntimeError("No option strikes were returned.")
        fixed_strike = min(strikes, key=lambda value: abs(value - spot10))
        ce_id = next(row[2] for row in rows if row[0] == fixed_strike and row[1] == "CE")
        pe_id = next(row[2] for row in rows if row[0] == fixed_strike and row[1] == "PE")
        strike_step = min([b - a for a, b in zip(strikes, strikes[1:]) if b > a] or [50.0])
        wings = {}
        for strike_value, side, security_id in rows:
            offset = round((strike_value - fixed_strike) / strike_step)
            if side == "CE" and offset in (1, 2):
                wings[(offset, side)] = security_id
            elif side == "PE" and offset in (-1, -2):
                wings[(offset, side)] = security_id
        instruments = [("IDX_I", "13"), ("NSE_FNO", ce_id), ("NSE_FNO", pe_id)]
        for offset in (1, 2, -1, -2):
            security_id = wings.get((offset, "CE" if offset > 0 else "PE"))
            if security_id:
                instruments.append(("NSE_FNO", security_id))
        st.session_state.update(
            locked=True,
            setup_error="",
            setup_retry_at=0.0,
            strike=fixed_strike,
            spot10=spot10,
            expiry=expiry,
            ce_id=ce_id,
            pe_id=pe_id,
            wing_ids={offset: wings.get((offset, "CE" if offset > 0 else "PE"), "") for offset in (1, 2, -1, -2)},
            instruments=tuple(dict.fromkeys(instruments)),
            prev_ce=None,
            prev_pe=None,
            prev_atm_iv=None,
            history=[],
            last_alert=None,
            sample={"atm_iv": None, "legs": {}},
            sample_bucket=None,
        )
    except Exception as exc:
        st.session_state.setup_error = str(exc)
        st.session_state.setup_retry_at = time_module.time() + SETUP_RETRY_SECONDS
        st.warning(f"⚠️ Reference strike not locked. No repeat setup request for {SETUP_RETRY_SECONDS}s. Error: {exc}")
        st.stop()

feed = get_market_feed(cid, token, st.session_state.instruments)

def tick(security_id):
    return feed.data.get(str(security_id), {})

live_spot = tick("13").get("ltp", st.session_state.spot10)
ce_ltp = tick(st.session_state.ce_id).get("ltp", 0)
pe_ltp = tick(st.session_state.pe_id).get("ltp", 0)
ce_iv, ce_vega = implied_vol_vega(live_spot, st.session_state.strike, ce_ltp, expiry, True, risk_free_rate) if ce_ltp else (None, None)
pe_iv, pe_vega = implied_vol_vega(live_spot, st.session_state.strike, pe_ltp, expiry, False, risk_free_rate) if pe_ltp else (None, None)
atm_iv_values = [value for value in (ce_iv, pe_iv) if value is not None]
atm_iv = sum(atm_iv_values) / len(atm_iv_values) if atm_iv_values else None

ce_vega_pct = change_pct(st.session_state.prev_ce, ce_vega)
pe_vega_pct = change_pct(st.session_state.prev_pe, pe_vega)
atm_iv_pct = change_pct(st.session_state.prev_atm_iv, atm_iv)

bucket = int(now.timestamp() // COMPARE_SECONDS)
if st.session_state.sample_bucket != bucket:
    st.session_state.sample_bucket = bucket
    st.session_state.sample = {"atm_iv": atm_iv, "legs": {}}

sample = st.session_state.sample
if sample.get("atm_iv") is None and atm_iv is not None:
    sample["atm_iv"] = atm_iv

wing_rows = []
for offset in (1, 2, -1, -2):
    security_id = st.session_state.wing_ids.get(offset, "")
    if not security_id:
        continue
    quote = tick(security_id)
    ltp = quote.get("ltp", 0)
    oi = quote.get("oi")
    baseline = sample["legs"].setdefault(offset, {"ltp": ltp, "oi": oi})
    oi_pct = change_pct(baseline.get("oi"), oi)
    premium_pct = change_pct(baseline.get("ltp"), ltp)
    side = "CALL" if offset > 0 else "PUT"
    seller_build = bool(oi_pct is not None and oi_pct >= oi_threshold and premium_pct is not None and premium_pct <= 0.5 and atm_iv_pct is not None and atm_iv_pct >= atm_iv_threshold)
    if oi_pct is None:
        view = "WAITING OI"
    elif seller_build:
        view = "SELLER BUILDUP"
    elif oi_pct >= oi_threshold and premium_pct is not None and premium_pct > 0.5:
        view = "OI BUILD + PREMIUM UP"
    elif oi_pct <= -oi_threshold:
        view = "OI UNWINDING"
    else:
        view = "NEUTRAL"
    wing_rows.append({
        "Wing": f"ATM{offset:+d} {side}",
        "Strike Offset": offset,
        "LTP": ltp or None,
        "OI": oi,
        "OI Change / 3m %": oi_pct,
        "Premium Change / 3m %": premium_pct,
        "ATM IV Change / 3m %": atm_iv_pct,
        "Seller View": view,
    })

seller_count = sum(row["Seller View"] == "SELLER BUILDUP" for row in wing_rows)
seller_alert = seller_count >= 2 and atm_iv_pct is not None and atm_iv_pct >= atm_iv_threshold
spike_side, spike_pct = None, None
if ce_vega_pct is not None and ce_vega_pct >= vega_spike_threshold:
    spike_side, spike_pct = "ATM CE", ce_vega_pct
if pe_vega_pct is not None and pe_vega_pct >= vega_spike_threshold and (spike_pct is None or pe_vega_pct >= spike_pct):
    spike_side, spike_pct = "ATM PE", pe_vega_pct

if atm_iv is not None and ce_vega is not None and pe_vega is not None:
    history = st.session_state.history
    if not history or history[-1]["bucket"] != bucket:
        history.append({"bucket": bucket, "time": now.strftime("%H:%M:%S"), "ATM IV %": atm_iv * 100, "CE Vega": ce_vega, "PE Vega": pe_vega, "Vega Difference": ce_vega - pe_vega, "Seller Wings": seller_count})
        st.session_state.history = history[-60:]

alert_key = f"{bucket}|{spike_side}|{seller_alert}"
new_alert = bool((spike_side or seller_alert) and alert_key != st.session_state.last_alert)
if new_alert:
    st.session_state.last_alert = alert_key
if spike_side:
    st.error(f"⚠️ SUDDEN VEGA SPIKE — {spike_side}: +{spike_pct:.1f}% over previous 3-minute sample")
if seller_alert:
    st.error(f"⚠️ SELLER-SIDE OI SIGNAL — {seller_count}/4 wings show OI build + premium softness while ATM IV is rising")
if new_alert and sound_enabled:
    warning_sound()

st.session_state.prev_ce = ce_vega or st.session_state.prev_ce
st.session_state.prev_pe = pe_vega or st.session_state.prev_pe
st.session_state.prev_atm_iv = atm_iv or st.session_state.prev_atm_iv

cols = st.columns(8)
cols[0].metric("🔒 10:00 ATM", f"{st.session_state.strike:,.0f}")
cols[1].metric("10:00 SPOT", f"₹{st.session_state.spot10:,.2f}")
cols[2].metric("LIVE SPOT", f"₹{live_spot:,.2f}")
cols[3].metric("EXPIRY", expiry)
cols[4].metric("ATM CE VEGA", "—" if ce_vega is None else f"{ce_vega:.3f}", None if ce_vega_pct is None else f"{ce_vega_pct:+.1f}%")
cols[5].metric("ATM PE VEGA", "—" if pe_vega is None else f"{pe_vega:.3f}", None if pe_vega_pct is None else f"{pe_vega_pct:+.1f}%")
cols[6].metric("ATM IV", "—" if atm_iv is None else f"{atm_iv * 100:.2f}%", None if atm_iv_pct is None else f"{atm_iv_pct:+.2f}%")
cols[7].metric("Seller Wings", f"{seller_count}/4")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(pd.DataFrame([{
    "Strike": st.session_state.strike,
    "CE": st.session_state.ce_id,
    "PE": st.session_state.pe_id,
    "CE LTP": ce_ltp or None,
    "PE LTP": pe_ltp or None,
    "CE IV %": None if ce_iv is None else ce_iv * 100,
    "PE IV %": None if pe_iv is None else pe_iv * 100,
    "ATM IV %": None if atm_iv is None else atm_iv * 100,
    "CE Vega": ce_vega,
    "PE Vega": pe_vega,
}]), use_container_width=True, hide_index=True)

st.markdown("### 🧭 SELLER-SIDE OI MAP — ATM ±1 / ±2")
st.caption("Seller confirmation requires wing OI increase + premium softness + rising ATM IV. This is an inference signal, not proof of a participant's intent.")
if wing_rows:
    st.dataframe(pd.DataFrame(wing_rows), use_container_width=True, hide_index=True)
else:
    st.warning("The required wing contracts were not available in the setup response.")

if st.session_state.history:
    st.markdown("### 📈 3-Minute ATM Vega / IV History")
    st.line_chart(pd.DataFrame(st.session_state.history).set_index("time")[["ATM IV %", "CE Vega", "PE Vega", "Vega Difference"]])

st.success(f"🟢 Live Feed: {feed.status} | subscribed: {len(st.session_state.instruments)} | Live REST polling: OFF | ATM fixed: {st.session_state.strike:,.0f}")
if feed.error:
    st.warning(f"Live feed notice: {feed.error}")
