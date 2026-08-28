import base64
import io
import math
import struct
import threading
import time
import urllib.request
import wave
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

try:
    import websocket
except ImportError:
    websocket = None

IST = ZoneInfo("Asia/Kolkata")
CLIENT_DEFAULT = "1113195747"
LOCK_TIME = dtime(10, 0)
UI_REFRESH_MS = 3000
BUCKET_SECONDS = 180
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
WS_URL = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2"

st.set_page_config(page_title="ALPHA Fixed 10AM Vega + OI", page_icon="📊", layout="wide")

for k, v in {
    "cid": CLIENT_DEFAULT, "token": "", "connected": False,
    "day": "", "locked": False, "setup_error": "", "strike": None,
    "spot10": None, "expiry": None, "ce_id": "", "pe_id": "", "wing_ids": {},
    "instruments": (("IDX_I", "13"),), "feed": None, "feed_key": "",
    "prev_ce": None, "prev_pe": None, "prev_atm_iv": None, "sample_bucket": None,
    "sample": {"atm_iv": None, "legs": {}}, "history": [], "last_alert": None,
}.items():
    st.session_state.setdefault(k, v.copy() if isinstance(v, dict) else list(v) if isinstance(v, list) else v)


def load_master():
    df = pd.read_csv(MASTER_URL, low_memory=False)
    df.columns = [str(c).strip().upper() for c in df.columns]
    aliases = {
        "SECURITY_ID": ["SECURITY_ID", "SEM_SMST_SECURITY_ID", "SM_SECURITY_ID"],
        "UNDERLYING_SECURITY_ID": ["UNDERLYING_SECURITY_ID"],
        "UNDERLYING_SYMBOL": ["UNDERLYING_SYMBOL"],
        "INSTRUMENT": ["INSTRUMENT", "SEM_INSTRUMENT_NAME"],
        "EXCH_ID": ["EXCH_ID", "SEM_EXM_EXCH_ID"],
        "SEGMENT": ["SEGMENT", "SEM_SEGMENT"],
        "EXPIRY": ["SM_EXPIRY_DATE", "SEM_EXPIRY_DATE", "EXPIRY_DATE"],
        "STRIKE": ["STRIKE_PRICE", "SEM_STRIKE_PRICE"],
        "OPTION_TYPE": ["OPTION_TYPE", "SEM_OPTION_TYPE"],
        "SYMBOL": ["SYMBOL_NAME", "SM_SYMBOL_NAME", "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL"],
    }
    out = {}
    for target, names in aliases.items():
        for name in names:
            if name in df.columns:
                out[target] = name
                break
    required = ["SECURITY_ID", "UNDERLYING_SECURITY_ID", "INSTRUMENT", "EXPIRY", "STRIKE", "OPTION_TYPE"]
    missing = [x for x in required if x not in out]
    if missing:
        raise RuntimeError(f"Instrument master missing fields: {', '.join(missing)}")
    x = df.copy()
    x["_sid"] = pd.to_numeric(x[out["SECURITY_ID"]], errors="coerce")
    x["_under"] = x[out["UNDERLYING_SECURITY_ID"]].astype(str).str.replace(r"\.0$", "", regex=True)
    x["_inst"] = x[out["INSTRUMENT"]].astype(str).str.upper().str.strip()
    x["_expiry"] = pd.to_datetime(x[out["EXPIRY"]], errors="coerce").dt.date
    x["_strike"] = pd.to_numeric(x[out["STRIKE"]], errors="coerce")
    x["_type"] = x[out["OPTION_TYPE"]].astype(str).str.upper().str.strip()
    x = x[(x._sid.notna()) & (x._under == "13") & (x._inst == "OPTIDX") & (x._type.isin(["CE", "PE"]))]
    if "EXCH_ID" in out:
        x = x[x[out["EXCH_ID"]].astype(str).str.upper().eq("NSE")]
    return x, out


@st.cache_data(ttl=3600, show_spinner=False)
def cached_master():
    return load_master()


def find_contracts(spot, today):
    df, out = cached_master()
    future = df[df._expiry >= today].copy()
    if future.empty:
        raise RuntimeError("No future NIFTY option contracts found in the instrument master.")
    expiry = min(future._expiry.dropna())
    book = future[future._expiry == expiry].copy()
    strikes = sorted(book._strike.dropna().unique())
    if not strikes:
        raise RuntimeError("No strikes found for the nearest NIFTY expiry.")
    atm = min(strikes, key=lambda s: abs(float(s) - float(spot)))
    steps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    step = min(steps) if steps else 50.0
    def sid(strike, side):
        row = book[(book._strike == strike) & (book._type == side)]
        if row.empty:
            return ""
        return str(int(row.iloc[0]._sid))
    ce = sid(atm, "CE")
    pe = sid(atm, "PE")
    if not ce or not pe:
        raise RuntimeError(f"ATM {atm:g} CE/PE contract not found in instrument master.")
    wings = {}
    for off in (1, 2):
        s = min(strikes, key=lambda z: abs(float(z) - (atm + off * step)))
        wings[off] = sid(s, "CE")
    for off in (-1, -2):
        s = min(strikes, key=lambda z: abs(float(z) - (atm + off * step)))
        wings[off] = sid(s, "PE")
    return expiry.isoformat(), float(atm), ce, pe, wings, step


class Feed:
    def __init__(self, client_id, token, instruments):
        self.client_id = client_id
        self.token = token
        self.instruments = tuple(instruments)
        self.data = {}
        self.status = "starting"
        self.error = ""
        self.stop_flag = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        if websocket is None:
            self.status = "feed library unavailable"
            return
        # Do NOT loop on handshake failures. One connection is intentional.
        try:
            self.status = "connecting"
            ws = websocket.create_connection(
                WS_URL.format(token=self.token, client=self.client_id),
                timeout=20,
                ping_interval=10,
                ping_timeout=5,
            )
            ws.send(json_dumps({
                "RequestCode": 17,
                "InstrumentCount": len(self.instruments),
                "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(i)} for s, i in self.instruments],
            }))
            self.status = "connected"
            while not self.stop_flag:
                packet = ws.recv()
                if not packet or isinstance(packet, str):
                    continue
                raw = bytes(packet)
                if len(raw) < 8:
                    continue
                code = raw[0]
                sec = str(struct.unpack_from("<i", raw, 4)[0])
                if code == 2 and len(raw) >= 17:
                    self.data.setdefault(sec, {}).update({"ltp": struct.unpack_from("<f", raw, 9)[0], "ltt": struct.unpack_from("<i", raw, 13)[0], "updated": time.time()})
                elif code == 4 and len(raw) >= 51:
                    self.data.setdefault(sec, {}).update({"ltp": struct.unpack_from("<f", raw, 9)[0], "ltt": struct.unpack_from("<i", raw, 15)[0], "volume": struct.unpack_from("<i", raw, 23)[0], "sell_qty": struct.unpack_from("<i", raw, 27)[0], "buy_qty": struct.unpack_from("<i", raw, 31)[0], "updated": time.time()})
                elif code == 5 and len(raw) >= 13:
                    self.data.setdefault(sec, {}).update({"oi": struct.unpack_from("<i", raw, 9)[0], "updated": time.time()})
                elif code == 50:
                    self.error = "Live feed disconnected by server"
                    break
        except Exception as exc:
            self.status = "offline"
            self.error = str(exc)

    def stop(self):
        self.stop_flag = True


# Small local alias keeps the websocket thread independent of any JSON package quirks.
def json_dumps(value):
    import json
    return json.dumps(value)


def warning_sound():
    sr, duration, freq = 22050, 0.7, 880
    hp = max(1, int(sr / (freq * 2)))
    raw = b"".join(struct.pack("<h", 12000 if (i // hp) % 2 == 0 else -12000) for i in range(int(sr * duration)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(sr); audio.writeframes(raw)
    uri = "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()
    st.components.v1.html(f"<audio autoplay><source src='{uri}' type='audio/wav'></audio>", height=1)


def bs(s, k, T, r, v, call):
    if min(s, k, T, v) <= 0:
        return 0.0
    root = math.sqrt(T)
    d1 = (math.log(s / k) + (r + 0.5 * v * v) * T) / (v * root)
    d2 = d1 - v * root
    disc = k * math.exp(-r * T)
    return s * norm_cdf(d1) - disc * norm_cdf(d2) if call else disc * norm_cdf(-d2) - s * norm_cdf(-d1)


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def iv_vega(spot, strike, premium, expiry, call, rate):
    try:
        if not premium or spot <= 0 or strike <= 0:
            return None, None
        expiry_dt = datetime.combine(datetime.strptime(expiry, "%Y-%m-%d").date(), dtime(15, 30), tzinfo=IST).astimezone(timezone.utc)
        T = max((expiry_dt - datetime.now(timezone.utc)).total_seconds() / 31536000.0, 1e-7)
        intrinsic = max(0, spot - strike) if call else max(0, strike - spot)
        if premium < intrinsic * 0.98:
            return None, None
        lo, hi = 1e-5, 5.0
        if bs(spot, strike, T, rate, hi, call) < premium:
            return None, None
        for _ in range(60):
            mid = (lo + hi) / 2
            if bs(spot, strike, T, rate, mid, call) > premium:
                hi = mid
            else:
                lo = mid
        vol = (lo + hi) / 2
        d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
        vega = spot * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * math.sqrt(T)
        return vol, vega
    except Exception:
        return None, None


# Shared login state. No authentication request is made from this page.
if not st.session_state.connected or not st.session_state.token:
    st.info("Open Login first, enter your access token, then return here.")
    st.stop()

with st.sidebar:
    st.markdown("## 🔐 MARKET DATA")
    st.text_input("Client ID", value=st.session_state.cid, disabled=True)
    st.caption("Connection uses your current session.")
    vega_threshold = st.number_input("Vega spike %", 1.0, 200.0, 20.0, 1.0)
    rate = st.number_input("Risk-free rate %", 0.0, 15.0, 6.0, 0.25) / 100
    oi_threshold = st.number_input("Wing OI move %", 0.5, 50.0, 2.0, 0.5)
    iv_threshold = st.number_input("ATM IV rise %", 0.2, 20.0, 1.0, 0.2)
    sound_on = st.checkbox("🔊 Warning sound", True)
    if st.button("RESET DAILY LOCK", use_container_width=True):
        for k in ("locked", "strike", "spot10", "expiry", "ce_id", "pe_id", "wing_ids", "instruments", "feed", "feed_key", "prev_ce", "prev_pe", "prev_atm_iv", "sample_bucket", "sample", "history", "last_alert"):
            st.session_state.pop(k, None)
        st.rerun()

st_autorefresh(interval=UI_REFRESH_MS, key="v13_ui")
now = datetime.now(IST)
today = now.date()
if st.session_state.day != today.isoformat():
    st.session_state.day = today.isoformat()
    st.session_state.locked = False
    st.session_state.setup_error = ""
    st.session_state.feed = None

st.title("📊 ALPHA • Fixed 10AM Vega + Seller OI Monitor")
st.caption("10:00 ATM is fixed for the day. Contract mapping uses the instrument master; live prices and OI use one persistent quote feed.")

# Feed starts with NIFTY only so the reference price is available before the 10:00 lock.
if st.session_state.feed is None:
    st.session_state.feed = Feed(st.session_state.cid, st.session_state.token, (("IDX_I", "13"),))

feed = st.session_state.feed


def tick(sec):
    return feed.data.get(str(sec), {})

spot_now = tick("13").get("ltp")

if not st.session_state.locked:
    if now.time() < LOCK_TIME:
        st.warning("⏳ Waiting for 10:00 IST to lock the reference strike.")
        st.info("No REST setup calls are being made. The lock will use the live NIFTY feed plus the cached instrument master.")
        st.stop()
    if spot_now is None:
        st.warning("⚠️ Live NIFTY price is not available yet. Reference strike is not locked.")
        if feed.error:
            st.error(f"Live feed: {feed.error}")
        st.stop()
    try:
        expiry, strike, ce_id, pe_id, wings, step = find_contracts(float(spot_now), today)
        instruments = [("IDX_I", "13"), ("NSE_FNO", ce_id), ("NSE_FNO", pe_id)]
        for off in (1, 2, -1, -2):
            sid = wings.get(off)
            if sid:
                instruments.append(("NSE_FNO", sid))
        # Stop the NIFTY-only socket and create ONE combined socket with all 7 instruments.
        feed.stop()
        feed2 = Feed(st.session_state.cid, st.session_state.token, tuple(dict.fromkeys(instruments)))
        st.session_state.feed = feed2
        st.session_state.locked = True
        st.session_state.setup_error = ""
        st.session_state.expiry = expiry
        st.session_state.strike = strike
        st.session_state.spot10 = float(spot_now)
        st.session_state.ce_id = ce_id
        st.session_state.pe_id = pe_id
        st.session_state.wing_ids = wings
        st.session_state.instruments = tuple(dict.fromkeys(instruments))
        st.session_state.prev_ce = st.session_state.prev_pe = st.session_state.prev_atm_iv = None
        st.session_state.sample_bucket = None
        st.session_state.sample = {"atm_iv": None, "legs": {}}
        st.session_state.history = []
        st.session_state.last_alert = None
        feed = feed2
    except Exception as exc:
        st.session_state.setup_error = str(exc)
        st.warning(f"⚠️ Reference strike not locked: {exc}")
        st.info("Retrying is controlled by the 3-second UI refresh, but there is **no REST request** in this retry path. The instrument master is cached for 1 hour.")
        st.stop()

feed = st.session_state.feed
spot = feed.data.get("13", {}).get("ltp", st.session_state.spot10)
ce_ltp = feed.data.get(str(st.session_state.ce_id), {}).get("ltp")
pe_ltp = feed.data.get(str(st.session_state.pe_id), {}).get("ltp")
ce_iv, ce_vega = iv_vega(spot, st.session_state.strike, ce_ltp, st.session_state.expiry, True, rate) if ce_ltp else (None, None)
pe_iv, pe_vega = iv_vega(spot, st.session_state.strike, pe_ltp, st.session_state.expiry, False, rate) if pe_ltp else (None, None)
atm_values = [v for v in (ce_iv, pe_iv) if v is not None]
atm_iv = sum(atm_values) / len(atm_values) if atm_values else None
ce_v_pct = None if st.session_state.prev_ce in (None, 0) or ce_vega is None else (ce_vega / st.session_state.prev_ce - 1) * 100
pe_v_pct = None if st.session_state.prev_pe in (None, 0) or pe_vega is None else (pe_vega / st.session_state.prev_pe - 1) * 100
atm_iv_pct = None if st.session_state.prev_atm_iv in (None, 0) or atm_iv is None else (atm_iv / st.session_state.prev_atm_iv - 1) * 100

bucket = int(now.timestamp() // BUCKET_SECONDS)
if st.session_state.sample_bucket != bucket:
    st.session_state.sample_bucket = bucket
    st.session_state.sample = {"atm_iv": atm_iv, "legs": {}}
    if atm_iv is not None: st.session_state.prev_atm_iv = atm_iv
sample = st.session_state.sample
wing_rows = []
for off in (1, 2, -1, -2):
    sid = st.session_state.wing_ids.get(off, "")
    if not sid: continue
    q = feed.data.get(str(sid), {})
    ltp, oi = q.get("ltp"), q.get("oi")
    base = sample["legs"].setdefault(off, {"ltp": ltp, "oi": oi})
    oi_pct = None if base.get("oi") in (None, 0) or oi is None else (oi / base["oi"] - 1) * 100
    prem_pct = None if base.get("ltp") in (None, 0) or ltp is None else (ltp / base["ltp"] - 1) * 100
    side = "CALL" if off > 0 else "PUT"
    seller = bool(oi_pct is not None and oi_pct >= oi_threshold and prem_pct is not None and prem_pct <= 0.5 and atm_iv_pct is not None and atm_iv_pct >= iv_threshold)
    view = "WAITING" if oi_pct is None else "SELLER BUILDUP" if seller else "OI BUILD + PREMIUM UP" if oi_pct >= oi_threshold and prem_pct is not None and prem_pct > 0.5 else "OI UNWINDING" if oi_pct <= -oi_threshold else "NEUTRAL"
    wing_rows.append({"Wing": f"ATM{off:+d} {side}", "LTP": ltp, "OI": oi, "OI Change / 3m %": oi_pct, "Premium Change / 3m %": prem_pct, "ATM IV Change / 3m %": atm_iv_pct, "Seller View": view})

seller_count = sum(r["Seller View"] == "SELLER BUILDUP" for r in wing_rows)
seller_alert = seller_count >= 2 and atm_iv_pct is not None and atm_iv_pct >= iv_threshold
spike_side, spike_pct = None, None
if ce_v_pct is not None and ce_v_pct >= vega_threshold: spike_side, spike_pct = "ATM CE", ce_v_pct
if pe_v_pct is not None and pe_v_pct >= vega_threshold and (spike_pct is None or pe_v_pct > spike_pct): spike_side, spike_pct = "ATM PE", pe_v_pct

if atm_iv is not None and ce_vega is not None and pe_vega is not None:
    if not st.session_state.history or st.session_state.history[-1]["bucket"] != bucket:
        st.session_state.history.append({"bucket": bucket, "time": now.strftime("%H:%M:%S"), "ATM IV %": atm_iv * 100, "CE Vega": ce_vega, "PE Vega": pe_vega, "Vega Difference": ce_vega - pe_vega, "Seller Wings": seller_count})
        st.session_state.history = st.session_state.history[-60:]

alert_key = f"{bucket}|{spike_side}|{seller_alert}"
new_alert = bool((spike_side or seller_alert) and alert_key != st.session_state.last_alert)
if new_alert: st.session_state.last_alert = alert_key
if spike_side: st.error(f"⚠️ SUDDEN VEGA SPIKE — {spike_side}: +{spike_pct:.1f}%")
if seller_alert: st.error(f"⚠️ SELLER-SIDE OI SIGNAL — {seller_count}/4 wings + rising ATM IV")
if new_alert and sound_on: warning_sound()

st.session_state.prev_ce = ce_vega or st.session_state.prev_ce
st.session_state.prev_pe = pe_vega or st.session_state.prev_pe
st.session_state.prev_atm_iv = atm_iv or st.session_state.prev_atm_iv

cols = st.columns(8)
cols[0].metric("🔒 10:00 ATM", "—" if not st.session_state.strike else f"{st.session_state.strike:,.0f}")
cols[1].metric("10:00 SPOT", "—" if st.session_state.spot10 is None else f"₹{st.session_state.spot10:,.2f}")
cols[2].metric("LIVE SPOT", "—" if spot is None else f"₹{spot:,.2f}")
cols[3].metric("EXPIRY", st.session_state.expiry or "—")
cols[4].metric("ATM CE VEGA", "—" if ce_vega is None else f"{ce_vega:.3f}", None if ce_v_pct is None else f"{ce_v_pct:+.1f}%")
cols[5].metric("ATM PE VEGA", "—" if pe_vega is None else f"{pe_vega:.3f}", None if pe_v_pct is None else f"{pe_v_pct:+.1f}%")
cols[6].metric("ATM IV", "—" if atm_iv is None else f"{atm_iv*100:.2f}%", None if atm_iv_pct is None else f"{atm_iv_pct:+.2f}%")
cols[7].metric("Seller Wings", f"{seller_count}/4")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(pd.DataFrame([{"Strike": st.session_state.strike, "CE": st.session_state.ce_id, "PE": st.session_state.pe_id, "CE LTP": ce_ltp, "PE LTP": pe_ltp, "CE IV %": None if ce_iv is None else ce_iv*100, "PE IV %": None if pe_iv is None else pe_iv*100, "ATM IV %": None if atm_iv is None else atm_iv*100, "CE Vega": ce_vega, "PE Vega": pe_vega}]), use_container_width=True, hide_index=True)
st.markdown("### 🧭 SELLER-SIDE OI MAP — ATM ±1 / ±2")
st.caption("Seller confirmation = wing OI increase + premium softness + rising ATM IV. This is an inference signal, not proof of intent.")
st.dataframe(pd.DataFrame(wing_rows), use_container_width=True, hide_index=True)
if st.session_state.history:
    st.markdown("### 📈 3-Minute ATM Vega / IV History")
    st.line_chart(pd.DataFrame(st.session_state.history).set_index("time")[["ATM IV %", "CE Vega", "PE Vega", "Vega Difference"]])
st.success(f"🟢 Live Feed: {feed.status} | subscribed: {len(st.session_state.instruments)} | REST setup calls: 0 after login | ATM fixed: {st.session_state.strike:,.0f}")
if feed.error: st.warning(f"Live feed notice: {feed.error}")
