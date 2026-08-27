import base64
import io
import json
import math
import struct
import threading
import time as tm
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
CLIENT_DEFAULT = "1113195747"
LOCK = time(10, 0)
REFRESH_SECONDS = 180
WS = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2"

st.set_page_config(page_title="ALPHA Fixed 10AM Vega + OI", page_icon="📊", layout="wide")


def dhan(path, cid, token, method="GET", body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": cid},
        data=None if body is None else json.dumps(body).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Dhan HTTP {e.code}: {e.read().decode(errors='replace')[:800]}")


def expiry_list(cid, token):
    r = dhan("/optionchain/expirylist", cid, token, "POST", {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"})
    today = datetime.now(IST).date()
    out = []
    for value in r.get("data", []) if isinstance(r, dict) else []:
        try:
            d = datetime.strptime(str(value), "%Y-%m-%d").date()
            if d >= today:
                out.append(d.isoformat())
        except ValueError:
            pass
    return sorted(set(out))


def chain(cid, token, expiry):
    r = dhan("/optionchain", cid, token, "POST", {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": expiry})
    if not isinstance(r.get("data"), dict):
        raise RuntimeError("Invalid Option Chain response")
    return r["data"]


def spot_10(cid, token):
    day = datetime.now(IST).date().isoformat()
    r = dhan("/charts/intraday", cid, token, "POST", {"securityId": "13", "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": "1", "oi": False, "fromDate": f"{day} 09:15:00", "toDate": f"{day} 10:01:00"})
    ts, cl = r.get("timestamp", []), r.get("close", [])
    n = min(len(ts), len(cl))
    if not n:
        return None
    t = pd.Series(pd.to_datetime(ts[:n], unit="s", utc=True)).dt.tz_convert(IST).dt.tz_localize(None)
    f = pd.DataFrame({"t": t, "c": pd.to_numeric(cl[:n], errors="coerce")}).dropna()
    x = f[f.t.dt.strftime("%H:%M") == "10:00"]
    if x.empty:
        x = f[f.t.dt.time <= LOCK].tail(1)
    return None if x.empty else float(x.iloc[-1].c)


def normcdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(s, k, T, r, v, call):
    if min(s, k, T, v) <= 0:
        return 0.0
    root = math.sqrt(T)
    d1 = (math.log(s / k) + (r + 0.5 * v * v) * T) / (v * root)
    d2 = d1 - v * root
    dk = k * math.exp(-r * T)
    return s * normcdf(d1) - dk * normcdf(d2) if call else dk * normcdf(-d2) - s * normcdf(-d1)


def iv_and_vega(s, k, p, expiry, call, r=0.06):
    try:
        if not p or s <= 0 or k <= 0:
            return None, None
        ed = datetime.combine(datetime.strptime(expiry, "%Y-%m-%d").date(), time(15, 30), tzinfo=IST).astimezone(timezone.utc)
        T = max((ed - datetime.now(timezone.utc)).total_seconds() / 31536000, 1e-7)
        intrinsic = max(0, s - k) if call else max(0, k - s)
        if p < intrinsic * 0.98:
            return None, None
        lo, hi = 1e-5, 5.0
        if bs(s, k, T, r, hi, call) < p:
            return None, None
        for _ in range(60):
            mid = (lo + hi) / 2
            if bs(s, k, T, r, mid, call) > p:
                hi = mid
            else:
                lo = mid
        v = (lo + hi) / 2
        d1 = (math.log(s / k) + (r + 0.5 * v * v) * T) / (v * math.sqrt(T))
        vega = s * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * math.sqrt(T)
        return v, vega
    except Exception:
        return None, None


class Feed:
    """Single Dhan Quote WebSocket. Quote mode (17) provides LTP/volume plus OI packets."""

    def __init__(self, cid, token, ids):
        self.cid = cid
        self.token = token
        self.ids = tuple(ids)
        self.data = {}
        self.status = "starting"
        self.error = ""
        self.stop_flag = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        if websocket is None:
            self.status = "websocket-client missing"
            return
        while not self.stop_flag:
            ws = None
            try:
                self.status = "connecting"
                ws = websocket.create_connection(WS.format(token=self.token, client=self.cid), timeout=20, ping_interval=10, ping_timeout=5)
                subscribe = {
                    "RequestCode": 17,
                    "InstrumentCount": len(self.ids),
                    "InstrumentList": [{"ExchangeSegment": seg, "SecurityId": str(sec)} for seg, sec in self.ids],
                }
                ws.send(json.dumps(subscribe))
                self.status = "connected"
                self.error = ""
                while not self.stop_flag:
                    packet = ws.recv()
                    if not packet or isinstance(packet, str):
                        continue
                    b = bytes(packet)
                    if len(b) < 8:
                        continue
                    code = b[0]
                    sec = str(struct.unpack_from("<i", b, 4)[0])
                    if code == 4 and len(b) >= 51:
                        self.data.setdefault(sec, {}).update({
                            "ltp": float(struct.unpack_from("<f", b, 8)[0]),
                            "ltt": int(struct.unpack_from("<i", b, 14)[0]),
                            "volume": int(struct.unpack_from("<i", b, 22)[0]),
                            "sell_qty": int(struct.unpack_from("<i", b, 26)[0]),
                            "buy_qty": int(struct.unpack_from("<i", b, 30)[0]),
                            "updated": tm.time(),
                        })
                    elif code == 5 and len(b) >= 12:
                        self.data.setdefault(sec, {}).update({
                            "oi": int(struct.unpack_from("<i", b, 8)[0]),
                            "updated": tm.time(),
                        })
                    elif code == 2 and len(b) >= 17:
                        self.data.setdefault(sec, {}).update({
                            "ltp": float(struct.unpack_from("<f", b, 8)[0]),
                            "ltt": int(struct.unpack_from("<i", b, 12)[0]),
                            "updated": tm.time(),
                        })
                    elif code == 50 and len(b) >= 11:
                        self.error = f"WebSocket disconnect {struct.unpack_from('<h', b, 9)[0]}"
                        break
            except Exception as e:
                self.error = str(e)
            finally:
                self.status = "reconnecting" if not self.stop_flag else "stopped"
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass
            if not self.stop_flag:
                tm.sleep(3)

    def stop(self):
        self.stop_flag = True


@st.cache_resource(show_spinner=False)
def get_feed(cid, token, ids):
    return Feed(cid, token, ids)


def sound():
    sr, dur, freq = 22050, 0.7, 880.0
    hp = max(1, int(sr / (freq * 2)))
    raw = b"".join(struct.pack("<h", 12000 if (i // hp) % 2 == 0 else -12000) for i in range(int(sr * dur)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(raw)
    uri = "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()
    st.components.v1.html(f"<audio autoplay><source src='{uri}' type='audio/wav'></audio>", height=1)


def pct(a, b):
    return None if a in (None, 0) or b is None else (b / a - 1) * 100


st.session_state.setdefault("cid", CLIENT_DEFAULT)
st.session_state.setdefault("token", "")
st.session_state.setdefault("connected", False)
with st.sidebar.form("dhan"):
    st.markdown("## 🔐 DHAN")
    cid_input = st.text_input("Client ID", st.session_state.cid)
    token_input = st.text_input("Access Token", st.session_state.token, type="password")
    go = st.form_submit_button("CONNECT", use_container_width=True)
if go:
    try:
        dhan("/profile", cid_input.strip(), token_input.strip())
        st.session_state.cid = cid_input.strip()
        st.session_state.token = token_input.strip()
        st.session_state.connected = True
        st.cache_resource.clear()
        st.rerun()
    except Exception as e:
        st.sidebar.error(str(e))
if not st.session_state.connected:
    st.info("Enter your Dhan Access Token and connect.")
    st.stop()
if st.sidebar.button("LOGOUT / CLEAR"):
    st.session_state.connected = False
    st.session_state.token = ""
    st.cache_resource.clear()
    st.rerun()

threshold = st.sidebar.number_input("Vega spike %", 1.0, 200.0, 20.0, 1.0)
risk = st.sidebar.number_input("Risk-free rate %", 0.0, 15.0, 6.0, 0.25) / 100
sound_on = st.sidebar.checkbox("🔊 Warning sound", True)
manual = st.sidebar.text_input("Expiry YYYY-MM-DD", "", help="Blank = nearest active Dhan expiry")
oi_threshold = st.sidebar.number_input("Wing OI significant move %", 0.5, 50.0, 2.0, 0.5)
atm_iv_threshold = st.sidebar.number_input("ATM IV rise confirmation %", 0.2, 20.0, 1.0, 0.2)

# Rerender UI every 3 seconds. No REST request is performed by this refresh.
st_autorefresh(interval=3000, key="vega_oi_ui")

cid, token = st.session_state.cid, st.session_state.token
now = datetime.now(IST)
day = now.date().isoformat()
if st.session_state.get("day") != day:
    for key in ("locked", "strike", "spot10", "expiry", "ce_id", "pe_id", "wing_ids", "prev_ce", "prev_pe", "prev_atm_iv", "history", "last_alert", "expiry_cache", "expiry_day", "sample", "sample_bucket"):
        st.session_state.pop(key, None)
    st.session_state.day = day

if manual:
    try:
        expiry = datetime.strptime(manual.strip(), "%Y-%m-%d").date()
        if expiry < now.date():
            raise ValueError
        expiry = expiry.isoformat()
    except Exception:
        st.error("Expiry must be a valid future YYYY-MM-DD date.")
        st.stop()
else:
    if st.session_state.get("expiry_day") != day or not st.session_state.get("expiry_cache"):
        try:
            st.session_state.expiry_cache = expiry_list(cid, token)
            st.session_state.expiry_day = day
        except Exception as e:
            st.error(f"Expiry lookup failed: {e}")
            st.stop()
    if not st.session_state.expiry_cache:
        st.error("No active future NIFTY expiry returned by Dhan.")
        st.stop()
    expiry = st.session_state.expiry_cache[0]

st.title("📊 ALPHA • Fixed 10AM Vega + Seller OI Monitor")
st.caption("Fixed 10:00 ATM. Live prices and OI come from Dhan Quote WebSocket; no 3-minute Option Chain polling.")

if not st.session_state.get("locked"):
    if now.time() < LOCK:
        st.warning("⏳ Waiting for 10:00 IST to lock ATM.")
        st.stop()
    try:
        s10 = spot_10(cid, token)
        if s10 is None:
            st.warning("10:00 NIFTY candle not available yet.")
            st.stop()
        data = chain(cid, token, expiry)
        rows = []
        for k, node in (data.get("oc") or {}).items():
            try:
                strike = float(k)
            except Exception:
                continue
            for side, key in (("CE", "ce"), ("PE", "pe")):
                leg = node.get(key) or {}
                sid = leg.get("security_id")
                if sid:
                    rows.append((strike, side, str(sid)))
        strikes = sorted({x[0] for x in rows})
        if not strikes:
            raise RuntimeError("No option strikes returned by Dhan.")
        strike = min(strikes, key=lambda x: abs(x - s10))
        ce_id = next(x[2] for x in rows if x[0] == strike and x[1] == "CE")
        pe_id = next(x[2] for x in rows if x[0] == strike and x[1] == "PE")
        wanted = {}
        for stike, side, sid in rows:
            offset = round((stike - strike) / 50)
            if side == "CE" and offset in (0, 1, 2):
                wanted[(offset, side)] = sid
            if side == "PE" and offset in (0, -1, -2):
                wanted[(offset, side)] = sid
        required = [("IDX_I", "13"), ("NSE_FNO", ce_id), ("NSE_FNO", pe_id)]
        labels = {0: "ATM"}
        for off in (1, 2, -1, -2):
            sid = wanted.get((off, "CE" if off > 0 else "PE"))
            if sid:
                required.append(("NSE_FNO", sid))
                labels[off] = sid
        st.session_state.update(
            locked=True, strike=strike, spot10=s10, expiry=expiry, ce_id=ce_id, pe_id=pe_id,
            wing_ids={off: wanted.get((off, "CE" if off > 0 else "PE"), "") for off in (1, 2, -1, -2)},
            instruments=tuple(dict.fromkeys(required)), prev_ce=None, prev_pe=None, prev_atm_iv=None,
            history=[], last_alert=None, sample={}, sample_bucket=None,
        )
    except Exception as e:
        st.error(f"Unable to lock ATM: {e}")
        st.stop()

if expiry != st.session_state.expiry:
    for key in ("locked", "strike", "spot10", "ce_id", "pe_id", "wing_ids", "prev_ce", "prev_pe", "prev_atm_iv", "history", "last_alert"):
        st.session_state.pop(key, None)
    st.rerun()

feed_key = f"{cid}|{token}|{st.session_state.strike}|{expiry}|{st.session_state.get('instruments')}"
f = get_feed(cid, token, st.session_state.instruments)

def tick(sec):
    return f.data.get(str(sec), {})

spot = tick("13").get("ltp", st.session_state.spot10)
ce_ltp = tick(st.session_state.ce_id).get("ltp", 0)
pe_ltp = tick(st.session_state.pe_id).get("ltp", 0)
ce_iv, ce_v = iv_and_vega(spot, st.session_state.strike, ce_ltp, expiry, True, risk) if ce_ltp else (None, None)
pe_iv, pe_v = iv_and_vega(spot, st.session_state.strike, pe_ltp, expiry, False, risk) if pe_ltp else (None, None)
atm_iv = None if ce_iv is None and pe_iv is None else sum(x for x in (ce_iv, pe_iv) if x is not None) / len([x for x in (ce_iv, pe_iv) if x is not None])
atm_iv_pct = pct(st.session_state.get("prev_atm_iv"), atm_iv)
ce_v_pct = pct(st.session_state.get("prev_ce"), ce_v)
pe_v_pct = pct(st.session_state.get("prev_pe"), pe_v)

bucket = int(now.timestamp() // REFRESH_SECONDS)
new_bucket = st.session_state.get("sample_bucket") != bucket
if new_bucket:
    st.session_state.sample_bucket = bucket
    st.session_state.sample = {
        "atm_iv": atm_iv,
        "legs": {},
    }
    if atm_iv is not None:
        st.session_state.prev_atm_iv = atm_iv

# Save the first tick in a 3-minute bucket and compare the current live value with it.
sample = st.session_state.sample
if sample.get("atm_iv") is None and atm_iv is not None:
    sample["atm_iv"] = atm_iv
    st.session_state.prev_atm_iv = atm_iv

wing_rows = []
for off in (1, 2, -1, -2):
    sid = st.session_state.wing_ids.get(off, "")
    if not sid:
        continue
    tk = tick(sid)
    ltp = tk.get("ltp", 0)
    oi = tk.get("oi")
    prev = sample["legs"].setdefault(off, {"ltp": ltp, "oi": oi})
    oi_pct = pct(prev.get("oi"), oi)
    ltp_pct = pct(prev.get("ltp"), ltp)
    direction = "CALL" if off > 0 else "PUT"
    label = f"ATM{off:+d} {direction}"
    seller_confirmed = bool(oi_pct is not None and oi_pct >= oi_threshold and ltp_pct is not None and ltp_pct <= 0.5 and atm_iv_pct is not None and atm_iv_pct >= atm_iv_threshold)
    if oi_pct is None:
        interpretation = "WAITING OI"
    elif seller_confirmed:
        interpretation = "SELLER BUILDUP"
    elif oi_pct >= oi_threshold and ltp_pct is not None and ltp_pct > 0.5:
        interpretation = "OI BUILD + PREMIUM UP"
    elif oi_pct <= -oi_threshold:
        interpretation = "OI UNWINDING"
    else:
        interpretation = "NEUTRAL"
    wing_rows.append({
        "Wing": label,
        "Strike Offset": off,
        "LTP": ltp or None,
        "OI": oi,
        "OI Change / 3m %": oi_pct,
        "Premium Change / 3m %": ltp_pct,
        "ATM IV Change / 3m %": atm_iv_pct,
        "Seller View": interpretation,
    })

seller_count = sum(1 for r in wing_rows if r["Seller View"] == "SELLER BUILDUP")
seller_alert = seller_count >= 2 and atm_iv_pct is not None and atm_iv_pct >= atm_iv_threshold

# Vega spike uses the previous 3-minute bucket's ATM Vega.
spike_side = None
spike_pct = None
if ce_v_pct is not None and ce_v_pct >= threshold:
    spike_side, spike_pct = "ATM CE", ce_v_pct
if pe_v_pct is not None and pe_v_pct >= threshold and (spike_pct is None or pe_v_pct >= spike_pct):
    spike_side, spike_pct = "ATM PE", pe_v_pct

if atm_iv is not None and ce_v is not None and pe_v is not None:
    hist = st.session_state.get("history", [])
    if not hist or hist[-1]["bucket"] != bucket:
        hist.append({"bucket": bucket, "time": now.strftime("%H:%M:%S"), "ATM IV %": atm_iv * 100, "CE Vega": ce_v, "PE Vega": pe_v, "Vega Difference": ce_v - pe_v, "Seller Wings": seller_count})
        st.session_state.history = hist[-60:]

alert_key = f"{bucket}|{spike_side}|{seller_alert}"
new_alert = bool((spike_side or seller_alert) and alert_key != st.session_state.get("last_alert"))
if new_alert:
    st.session_state.last_alert = alert_key

if spike_side:
    st.error(f"⚠️ SUDDEN VEGA SPIKE — {spike_side}: +{spike_pct:.1f}% over previous 3-minute sample")
if seller_alert:
    st.error(f"⚠️ SELLER-SIDE OI SIGNAL — {seller_count}/4 wings show OI build + premium softness while ATM IV is rising")
if new_alert and sound_on:
    sound()

st.session_state.prev_ce = ce_v or st.session_state.get("prev_ce")
st.session_state.prev_pe = pe_v or st.session_state.get("prev_pe")

cols = st.columns(8)
cols[0].metric("🔒 10:00 ATM", f"{st.session_state.strike:,.0f}")
cols[1].metric("10:00 SPOT", f"₹{st.session_state.spot10:,.2f}")
cols[2].metric("LIVE SPOT", f"₹{spot:,.2f}")
cols[3].metric("EXPIRY", expiry)
cols[4].metric("ATM CE VEGA", "—" if ce_v is None else f"{ce_v:.3f}", None if ce_v_pct is None else f"{ce_v_pct:+.1f}%")
cols[5].metric("ATM PE VEGA", "—" if pe_v is None else f"{pe_v:.3f}", None if pe_v_pct is None else f"{pe_v_pct:+.1f}%")
cols[6].metric("ATM IV", "—" if atm_iv is None else f"{atm_iv*100:.2f}%", None if atm_iv_pct is None else f"{atm_iv_pct:+.2f}%")
cols[7].metric("Seller Wings", f"{seller_count}/4")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(pd.DataFrame([{
    "Strike": st.session_state.strike,
    "CE ID": st.session_state.ce_id,
    "PE ID": st.session_state.pe_id,
    "CE LTP": ce_ltp or None,
    "PE LTP": pe_ltp or None,
    "CE IV %": None if ce_iv is None else ce_iv * 100,
    "PE IV %": None if pe_iv is None else pe_iv * 100,
    "ATM IV %": None if atm_iv is None else atm_iv * 100,
    "CE Vega": ce_v,
    "PE Vega": pe_v,
}]), use_container_width=True, hide_index=True)

st.markdown("### 🧭 SELLER-SIDE OI MAP — ATM ±1 / ±2")
st.caption("Seller confirmation = wing OI increase ≥ threshold + option premium not rising materially + ATM IV rising ≥ confirmation threshold, measured against the previous 3-minute bucket. This is an inference signal, not proof of selling.")
if wing_rows:
    st.dataframe(pd.DataFrame(wing_rows), use_container_width=True, hide_index=True)
else:
    st.warning("The required ±1/±2 option contracts were not found in the returned chain.")

if st.session_state.history:
    st.markdown("### 📈 3-Minute ATM Vega / IV History")
    st.line_chart(pd.DataFrame(st.session_state.history).set_index("time")[["ATM IV %", "CE Vega", "PE Vega", "Vega Difference"]])

st.success(f"🟢 WebSocket: {f.status} | subscribed: {len(st.session_state.instruments)} | REST live Option Chain polling: OFF | ATM fixed: {st.session_state.strike:,.0f}")
if f.error:
    st.warning(f"WebSocket notice: {f.error}")
