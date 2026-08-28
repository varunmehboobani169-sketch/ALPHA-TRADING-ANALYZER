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
DEFAULT_CLIENT_ID = "1113195747"
LOCK_TIME = time(10, 0)
BUCKET_SECONDS = 180
WS_URL = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2"

st.set_page_config(page_title="ALPHA Fixed 10AM Vega + OI", page_icon="📊", layout="wide")

STATE_DEFAULTS = {
    "cid": DEFAULT_CLIENT_ID, "token": "", "connected": False,
    "day": None, "locked": False, "strike": None, "spot10": None,
    "expiry": None, "ce_id": None, "pe_id": None, "wing_ids": {},
    "instruments": tuple(), "prev_ce": None, "prev_pe": None,
    "prev_atm_iv": None, "history": [], "last_alert": None,
    "expiry_cache": [], "expiry_day": None, "sample": {"atm_iv": None, "legs": {}},
    "sample_bucket": None,
}
for key, value in STATE_DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value.copy() if isinstance(value, dict) else list(value) if isinstance(value, list) else value


def api_request(path, client_id, token, method="GET", body=None):
    req = urllib.request.Request(API + path, method=method, headers={"Accept":"application/json","Content-Type":"application/json","access-token":token,"client-id":client_id}, data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"API HTTP {exc.code}: {detail}") from exc


def active_expiries(client_id, token):
    payload = api_request("/optionchain/expirylist", client_id, token, "POST", {"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I"})
    today = datetime.now(IST).date(); values=[]
    for raw in payload.get("data", []) if isinstance(payload, dict) else []:
        try:
            d=datetime.strptime(str(raw), "%Y-%m-%d").date()
            if d>=today: values.append(d.isoformat())
        except ValueError: continue
    return sorted(set(values))


def option_chain(client_id, token, expiry):
    payload=api_request("/optionchain", client_id, token, "POST", {"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I","Expiry":expiry})
    data=payload.get("data") if isinstance(payload,dict) else None
    if not isinstance(data,dict): raise RuntimeError("Invalid option-chain response")
    return data


def reference_spot(client_id, token):
    day=datetime.now(IST).date().isoformat()
    payload=api_request("/charts/intraday",client_id,token,"POST",{"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX","interval":"1","oi":False,"fromDate":f"{day} 09:15:00","toDate":f"{day} 10:01:00"})
    timestamps=payload.get("timestamp",[]); closes=payload.get("close",[]); n=min(len(timestamps),len(closes))
    if n==0:return None
    ts=pd.Series(pd.to_datetime(timestamps[:n],unit="s",utc=True)).dt.tz_convert(IST).dt.tz_localize(None)
    frame=pd.DataFrame({"t":ts,"c":pd.to_numeric(closes[:n],errors="coerce")}).dropna()
    exact=frame[frame.t.dt.strftime("%H:%M")=="10:00"]
    if exact.empty: exact=frame[frame.t.dt.time<=LOCK_TIME].tail(1)
    return None if exact.empty else float(exact.iloc[-1].c)


def norm_cdf(x): return .5*(1+math.erf(x/math.sqrt(2)))

def bs_price(s,k,T,r,v,call):
    if min(s,k,T,v)<=0:return 0.0
    root=math.sqrt(T); d1=(math.log(s/k)+(r+.5*v*v)*T)/(v*root); d2=d1-v*root; dk=k*math.exp(-r*T)
    return s*norm_cdf(d1)-dk*norm_cdf(d2) if call else dk*norm_cdf(-d2)-s*norm_cdf(-d1)


def implied_vol_and_vega(s,k,p,expiry,call,r=.06):
    try:
        if not p or s<=0 or k<=0:return None,None
        ed=datetime.combine(datetime.strptime(expiry,"%Y-%m-%d").date(),time(15,30),tzinfo=IST).astimezone(timezone.utc)
        T=max((ed-datetime.now(timezone.utc)).total_seconds()/31536000,1e-7); intrinsic=max(0,s-k) if call else max(0,k-s)
        if p<intrinsic*.98:return None,None
        lo,hi=1e-5,5.0
        if bs_price(s,k,T,r,hi,call)<p:return None,None
        for _ in range(60):
            mid=(lo+hi)/2
            if bs_price(s,k,T,r,mid,call)>p:hi=mid
            else:lo=mid
        v=(lo+hi)/2; d1=(math.log(s/k)+(r+.5*v*v)*T)/(v*math.sqrt(T)); vg=s*math.exp(-.5*d1*d1)/math.sqrt(2*math.pi)*math.sqrt(T)
        return v,vg
    except Exception:return None,None


def pct(a,b): return None if a in (None,0) or b is None else (b/a-1)*100


class LiveFeed:
    def __init__(self,client_id,token,instruments):
        self.client_id=client_id; self.token=token; self.instruments=tuple(instruments); self.data={}; self.status="starting"; self.error=""; self.stop=False
        threading.Thread(target=self.run,daemon=True).start()
    def run(self):
        if websocket is None:self.status="feed library unavailable";return
        while not self.stop:
            ws=None
            try:
                self.status="connecting"; ws=websocket.create_connection(WS_URL.format(token=self.token,client=self.client_id),timeout=20,ping_interval=10,ping_timeout=5)
                ws.send(json.dumps({"RequestCode":17,"InstrumentCount":len(self.instruments),"InstrumentList":[{"ExchangeSegment":s,"SecurityId":str(i)} for s,i in self.instruments]})); self.status="connected"; self.error=""
                while not self.stop:
                    packet=ws.recv()
                    if not packet or isinstance(packet,str):continue
                    b=bytes(packet)
                    if len(b)<8:continue
                    code=b[0]; sec=str(struct.unpack_from("<i",b,4)[0])
                    if code==2 and len(b)>=17:self.data.setdefault(sec,{}).update({"ltp":float(struct.unpack_from("<f",b,9)[0]),"ltt":int(struct.unpack_from("<i",b,13)[0]),"updated":tm.time()})
                    elif code==4 and len(b)>=51:self.data.setdefault(sec,{}).update({"ltp":float(struct.unpack_from("<f",b,9)[0]),"ltt":int(struct.unpack_from("<i",b,15)[0]),"volume":int(struct.unpack_from("<i",b,23)[0]),"sell_qty":int(struct.unpack_from("<i",b,27)[0]),"buy_qty":int(struct.unpack_from("<i",b,31)[0]),"updated":tm.time()})
                    elif code==5 and len(b)>=13:self.data.setdefault(sec,{}).update({"oi":int(struct.unpack_from("<i",b,9)[0]),"updated":tm.time()})
                    elif code==50:self.error="Live feed disconnected";break
            except Exception as exc:self.error=str(exc)
            finally:
                self.status="reconnecting" if not self.stop else "stopped"
                try:
                    if ws:ws.close()
                except Exception:pass
            if not self.stop:tm.sleep(3)


@st.cache_resource(show_spinner=False)
def get_feed(client_id,token,instruments):return LiveFeed(client_id,token,instruments)


def warning_sound():
    sr,dur,freq=22050,.7,880.; hp=max(1,int(sr/(freq*2))); raw=b"".join(struct.pack("<h",12000 if (i//hp)%2==0 else -12000) for i in range(int(sr*dur))); buf=io.BytesIO()
    with wave.open(buf,"wb") as audio:audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(sr);audio.writeframes(raw)
    data_uri="data:audio/wav;base64,"+base64.b64encode(buf.getvalue()).decode(); st.components.v1.html(f"<audio autoplay><source src='{data_uri}' type='audio/wav'></audio>",height=1)


with st.sidebar.form("connection"):
    st.markdown("## 🔐 MARKET DATA CONNECTION")
    client_input=st.text_input("Client ID",st.session_state.cid)
    token_input=st.text_input("Access Token",st.session_state.token,type="password")
    connect=st.form_submit_button("CONNECT",use_container_width=True)
if connect:
    try:
        api_request("/profile",client_input.strip(),token_input.strip()); st.session_state.cid=client_input.strip(); st.session_state.token=token_input.strip(); st.session_state.connected=True; st.cache_resource.clear(); st.rerun()
    except Exception as exc:st.sidebar.error(f"Connection failed: {exc}")
if not st.session_state.connected:st.info("Enter your access token and connect to the market data feed.");st.stop()
if st.sidebar.button("LOGOUT / CLEAR",use_container_width=True):st.session_state.connected=False;st.session_state.token="";st.cache_resource.clear();st.rerun()

vega_spike_threshold=st.sidebar.number_input("Vega spike %",1.,200.,20.,1.); risk_free_rate=st.sidebar.number_input("Risk-free rate %",0.,15.,6.,.25)/100.; sound_enabled=st.sidebar.checkbox("🔊 Warning sound",True); manual_expiry=st.sidebar.text_input("Expiry YYYY-MM-DD",""); oi_threshold=st.sidebar.number_input("Wing OI significant move %",.5,50.,2.,.5); atm_iv_threshold=st.sidebar.number_input("ATM IV rise confirmation %",.2,20.,1.,.2)
st_autorefresh(interval=3000,key="vega_v10_refresh")

cid,token=st.session_state.cid,st.session_state.token; now=datetime.now(IST); today_key=now.date().isoformat()
if st.session_state.day!=today_key:
    for key in ("locked","strike","spot10","expiry","ce_id","pe_id","wing_ids","instruments","prev_ce","prev_pe","prev_atm_iv","history","last_alert","expiry_cache","expiry_day","sample","sample_bucket"):st.session_state.pop(key,None)
    st.session_state.day=today_key
    for key,value in STATE_DEFAULTS.items():
        if key not in st.session_state:st.session_state[key]=value.copy() if isinstance(value,dict) else list(value) if isinstance(value,list) else value

if manual_expiry:
    try:expiry=datetime.strptime(manual_expiry.strip(),"%Y-%m-%d").date();
    except ValueError:st.error("Expiry must be a valid future YYYY-MM-DD date.");st.stop()
    if expiry<now.date():st.error("Expiry must be a valid future YYYY-MM-DD date.");st.stop()
    expiry=expiry.isoformat()
else:
    if st.session_state.get("expiry_day")!=today_key or not st.session_state.get("expiry_cache"):
        try:st.session_state.expiry_cache=active_expiries(cid,token);st.session_state.expiry_day=today_key
        except Exception as exc:st.error(f"Expiry lookup failed: {exc}");st.stop()
    if not st.session_state.expiry_cache:st.error("No active future expiry is available.");st.stop()
    expiry=st.session_state.expiry_cache[0]

st.title("📊 ALPHA • Fixed 10AM Vega + Seller OI Monitor")
st.caption("Fixed 10:00 ATM. Live prices and OI are streamed continuously; REST is used only for setup.")

if not st.session_state.get("locked"):
    if now.time()<LOCK_TIME:st.warning("⏳ Waiting for 10:00 IST to lock the reference strike.");st.stop()
    try:
        spot10=reference_spot(cid,token)
        if spot10 is None:st.warning("The 10:00 reference candle is not available yet.");st.stop()
        chain_data=option_chain(cid,token,expiry); rows=[]
        for raw_strike,node in (chain_data.get("oc") or {}).items():
            try:strike_value=float(raw_strike)
            except Exception:continue
            for label,key in (("CE","ce"),("PE","pe")):
                sid=(node.get(key) or {}).get("security_id")
                if sid:rows.append((strike_value,label,str(sid)))
        strikes=sorted({r[0] for r in rows})
        if not strikes:raise RuntimeError("No option strikes were returned.")
        fixed_strike=min(strikes,key=lambda x:abs(x-spot10)); ce_id=next(r[2] for r in rows if r[0]==fixed_strike and r[1]=="CE"); pe_id=next(r[2] for r in rows if r[0]==fixed_strike and r[1]=="PE")
        step=min([b-a for a,b in zip(strikes,strikes[1:]) if b>a] or [50.]); wings={}
        for strike_value,side,sid in rows:
            offset=round((strike_value-fixed_strike)/step)
            if side=="CE" and offset in (1,2):wings[(offset,side)]=sid
            if side=="PE" and offset in (-1,-2):wings[(offset,side)]=sid
        instruments=[("IDX_I","13"),("NSE_FNO",ce_id),("NSE_FNO",pe_id)]
        for off in (1,2,-1,-2):
            sid=wings.get((off,"CE" if off>0 else "PE"));
            if sid:instruments.append(("NSE_FNO",sid))
        st.session_state.update(locked=True,strike=fixed_strike,spot10=spot10,expiry=expiry,ce_id=ce_id,pe_id=pe_id,wing_ids={o:wings.get((o,"CE" if o>0 else "PE"),"") for o in (1,2,-1,-2)},instruments=tuple(dict.fromkeys(instruments)),prev_ce=None,prev_pe=None,prev_atm_iv=None,history=[],last_alert=None,sample={},sample_bucket=None)
    except Exception as exc:st.error(f"Unable to lock the reference strike: {exc}");st.stop()

if not st.session_state.get("instruments"):
    derived=[("IDX_I","13")]
    if st.session_state.get("ce_id"):derived.append(("NSE_FNO",str(st.session_state.ce_id)))
    if st.session_state.get("pe_id"):derived.append(("NSE_FNO",str(st.session_state.pe_id)))
    for sid in st.session_state.get("wing_ids",{}).values():
        if sid:derived.append(("NSE_FNO",str(sid)))
    st.session_state.instruments=tuple(dict.fromkeys(derived))

feed=get_feed(cid,token,st.session_state.instruments)
def tick(sec):return feed.data.get(str(sec),{})

live_spot=tick("13").get("ltp",st.session_state.spot10); ce_ltp=tick(st.session_state.ce_id).get("ltp",0); pe_ltp=tick(st.session_state.pe_id).get("ltp",0)
ce_iv,ce_vega=implied_vol_and_vega(live_spot,st.session_state.strike,ce_ltp,expiry,True,risk_free_rate) if ce_ltp else (None,None); pe_iv,pe_vega=implied_vol_and_vega(live_spot,st.session_state.strike,pe_ltp,expiry,False,risk_free_rate) if pe_ltp else (None,None)
atm_ivs=[v for v in (ce_iv,pe_iv) if v is not None]; atm_iv=sum(atm_ivs)/len(atm_ivs) if atm_ivs else None
bucket=int(now.timestamp()//BUCKET_SECONDS)
if st.session_state.get("sample_bucket")!=bucket:st.session_state.sample_bucket=bucket;st.session_state.sample={"atm_iv":atm_iv,"legs":{}}
sample=st.session_state.sample
if sample.get("atm_iv") is None and atm_iv is not None:sample["atm_iv"]=atm_iv
atm_iv_change=pct(sample.get("atm_iv"),atm_iv); ce_vega_change=pct(st.session_state.get("prev_ce"),ce_vega); pe_vega_change=pct(st.session_state.get("prev_pe"),pe_vega)
wing_rows=[]
for off in (1,2,-1,-2):
    sid=st.session_state.wing_ids.get(off,"")
    if not sid:continue
    current=tick(sid); ltp=current.get("ltp",0); oi=current.get("oi"); baseline=sample["legs"].setdefault(off,{"ltp":ltp,"oi":oi}); oi_change=pct(baseline.get("oi"),oi); premium_change=pct(baseline.get("ltp"),ltp); side="CALL" if off>0 else "PUT"
    seller=bool(oi_change is not None and oi_change>=oi_threshold and premium_change is not None and premium_change<=.5 and atm_iv_change is not None and atm_iv_change>=atm_iv_threshold)
    view="WAITING OI" if oi_change is None else "SELLER BUILDUP" if seller else "OI BUILD + PREMIUM UP" if oi_change>=oi_threshold and premium_change is not None and premium_change>.5 else "OI UNWINDING" if oi_change<=-oi_threshold else "NEUTRAL"
    wing_rows.append({"Wing":f"ATM{off:+d} {side}","Strike":st.session_state.strike+off*50,"LTP":ltp or None,"OI":oi,"OI Change / 3m %":oi_change,"Premium Change / 3m %":premium_change,"ATM IV Change / 3m %":atm_iv_change,"Seller View":view})
seller_count=sum(r["Seller View"]=="SELLER BUILDUP" for r in wing_rows); seller_alert=seller_count>=2 and atm_iv_change is not None and atm_iv_change>=atm_iv_threshold
spike_side,spike_change=None,None
if ce_vega_change is not None and ce_vega_change>=vega_spike_threshold:spike_side,spike_change="ATM CE",ce_vega_change
if pe_vega_change is not None and pe_vega_change>=vega_spike_threshold and (spike_change is None or pe_vega_change>=spike_change):spike_side,spike_change="ATM PE",pe_vega_change
alert_key=f"{bucket}|{spike_side}|{seller_alert}"; new_alert=bool((spike_side or seller_alert) and alert_key!=st.session_state.get("last_alert"))
if new_alert:st.session_state.last_alert=alert_key; warning_sound() if sound_enabled else None
st.session_state.prev_ce=ce_vega or st.session_state.get("prev_ce");st.session_state.prev_pe=pe_vega or st.session_state.get("prev_pe")
if atm_iv is not None and ce_vega is not None and pe_vega is not None:
    hist=st.session_state.get("history",[])
    if not hist or hist[-1]["bucket"]!=bucket:hist.append({"bucket":bucket,"time":now.strftime("%H:%M:%S"),"ATM IV %":atm_iv*100,"CE Vega":ce_vega,"PE Vega":pe_vega,"Vega Difference":ce_vega-pe_vega,"Seller Wings":seller_count});st.session_state.history=hist[-60:]

cols=st.columns(8)
cols[0].metric("🔒 10:00 ATM",f"{st.session_state.strike:,.0f}");cols[1].metric("10:00 SPOT",f"₹{st.session_state.spot10:,.2f}");cols[2].metric("LIVE SPOT",f"₹{live_spot:,.2f}");cols[3].metric("EXPIRY",expiry);cols[4].metric("ATM CE VEGA","—" if ce_vega is None else f"{ce_vega:.3f}",None if ce_vega_change is None else f"{ce_vega_change:+.1f}%");cols[5].metric("ATM PE VEGA","—" if pe_vega is None else f"{pe_vega:.3f}",None if pe_vega_change is None else f"{pe_vega_change:+.1f}%");cols[6].metric("ATM IV","—" if atm_iv is None else f"{atm_iv*100:.2f}%",None if atm_iv_change is None else f"{atm_iv_change:+.2f}%");cols[7].metric("Seller Wings",f"{seller_count}/4")
if spike_side:st.error(f"⚠️ SUDDEN VEGA SPIKE — {spike_side}: +{spike_change:.1f}% over the previous 3-minute sample")
if seller_alert:st.error(f"⚠️ SELLER-SIDE OI SIGNAL — {seller_count}/4 wings show OI build with premium softness while ATM IV is rising")
st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(pd.DataFrame([{"Strike":st.session_state.strike,"CE LTP":ce_ltp or None,"PE LTP":pe_ltp or None,"CE IV %":None if ce_iv is None else ce_iv*100,"PE IV %":None if pe_iv is None else pe_iv*100,"ATM IV %":None if atm_iv is None else atm_iv*100,"CE Vega":ce_vega,"PE Vega":pe_vega}]),use_container_width=True,hide_index=True)
st.markdown("### 🧭 SELLER-SIDE OI MAP — ATM ±1 / ±2")
st.caption("Seller confirmation combines rising wing OI, soft/flat option premium, and rising ATM IV over the 3-minute comparison window. It is an inference signal, not proof of a seller.")
if wing_rows:st.dataframe(pd.DataFrame(wing_rows),use_container_width=True,hide_index=True)
else:st.warning("The requested ±1/±2 contracts were not found in the returned option chain.")
if st.session_state.history:
    st.markdown("### 📈 3-Minute ATM Vega / IV History")
    st.line_chart(pd.DataFrame(st.session_state.history).set_index("time")[["ATM IV %","CE Vega","PE Vega","Vega Difference"]])
st.success(f"🟢 Live Feed: {feed.status} | subscribed: {len(st.session_state.instruments)} | REST live polling: OFF | ATM fixed: {st.session_state.strike:,.0f}")
if feed.error:st.warning(f"Live feed notice: {feed.error}")
