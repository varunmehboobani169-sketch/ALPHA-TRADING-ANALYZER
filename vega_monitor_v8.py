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
WS = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2"

st.set_page_config(page_title="ALPHA Fixed 10AM Vega", page_icon="📊", layout="wide")


def dhan(path, cid, token, method="GET", body=None):
    req = urllib.request.Request(API + path, method=method, headers={"Accept":"application/json","Content-Type":"application/json","access-token":token,"client-id":cid}, data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Dhan HTTP {e.code}: {e.read().decode(errors='replace')[:800]}")


def expiry_list(cid, token):
    r = dhan("/optionchain/expirylist", cid, token, "POST", {"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I"})
    today = datetime.now(IST).date()
    return sorted({datetime.strptime(str(x), "%Y-%m-%d").date().isoformat() for x in r.get("data", []) if datetime.strptime(str(x), "%Y-%m-%d").date() >= today})


def chain(cid, token, expiry):
    r = dhan("/optionchain", cid, token, "POST", {"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I","Expiry":expiry})
    if not isinstance(r.get("data"), dict): raise RuntimeError("Invalid Option Chain response")
    return r["data"]


def spot_10(cid, token):
    day = datetime.now(IST).date().isoformat()
    r = dhan("/charts/intraday", cid, token, "POST", {"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX","interval":"1","oi":False,"fromDate":f"{day} 09:15:00","toDate":f"{day} 10:01:00"})
    ts, cl = r.get("timestamp", []), r.get("close", [])
    n=min(len(ts),len(cl))
    if not n:return None
    t=pd.Series(pd.to_datetime(ts[:n],unit="s",utc=True)).dt.tz_convert(IST).dt.tz_localize(None)
    f=pd.DataFrame({"t":t,"c":pd.to_numeric(cl[:n],errors="coerce")}).dropna()
    x=f[f.t.dt.strftime("%H:%M")=="10:00"]
    if x.empty:x=f[f.t.dt.time<=LOCK].tail(1)
    return None if x.empty else float(x.iloc[-1].c)


def normcdf(x): return .5*(1+math.erf(x/math.sqrt(2)))


def bs(s,k,T,r,v,call):
    if min(s,k,T,v)<=0:return 0
    d1=(math.log(s/k)+(r+.5*v*v)*T)/(v*math.sqrt(T)); d2=d1-v*math.sqrt(T); dk=k*math.exp(-r*T)
    return s*normcdf(d1)-dk*normcdf(d2) if call else dk*normcdf(-d2)-s*normcdf(-d1)


def iv_and_vega(s,k,p,expiry,call,r=.06):
    try:
        ed=datetime.combine(datetime.strptime(expiry,"%Y-%m-%d").date(),time(15,30),tzinfo=IST).astimezone(timezone.utc)
        T=max((ed-datetime.now(timezone.utc)).total_seconds()/31536000,1e-7)
        lo,hi=1e-5,5.0
        intrinsic=max(0,s-k) if call else max(0,k-s)
        if p<=0 or p<intrinsic*.98:return None,None
        for _ in range(55):
            mid=(lo+hi)/2
            if bs(s,k,T,r,mid,call)>p:hi=mid
            else:lo=mid
        v=(lo+hi)/2
        d1=(math.log(s/k)+(r+.5*v*v)*T)/(v*math.sqrt(T))
        vega=s*math.exp(-.5*d1*d1)/math.sqrt(2*math.pi)*math.sqrt(T)
        return v,vega
    except Exception:return None,None


class Feed:
    def __init__(self,cid,token,ids):
        self.cid=cid; self.token=token; self.ids=tuple(ids); self.data={}; self.status="starting"; self.error=""; self.stop_flag=False
        threading.Thread(target=self.run,daemon=True).start()
    def run(self):
        if websocket is None:self.status="websocket-client missing";return
        while not self.stop_flag:
            ws=None
            try:
                self.status="connecting"
                ws=websocket.create_connection(WS.format(token=self.token,client=self.cid),timeout=20,ping_interval=10,ping_timeout=5)
                ws.send(json.dumps({"RequestCode":15,"InstrumentCount":len(self.ids),"InstrumentList":[{"ExchangeSegment":s,"SecurityId":str(i)} for s,i in self.ids]}))
                self.status="connected"; self.error=""
                while not self.stop_flag:
                    p=ws.recv()
                    if not p or isinstance(p,str):continue
                    b=bytes(p)
                    if len(b)<17:continue
                    code=b[0]
                    if code==2:
                        sec=str(struct.unpack_from("<i",b,4)[0]); ltp=float(struct.unpack_from("<f",b,8)[0])
                        if ltp>0:self.data[sec]={"ltp":ltp,"at":tm.time()}
                    elif code==50 and len(b)>=11:
                        self.error=f"WebSocket disconnect {struct.unpack_from('<h',b,9)[0]}";break
            except Exception as e:self.error=str(e)
            finally:
                self.status="reconnecting" if not self.stop_flag else "stopped"
                try:
                    if ws:ws.close()
                except Exception:pass
            if not self.stop_flag:tm.sleep(3)


@st.cache_resource(show_spinner=False)
def feed(cid,token,ids):return Feed(cid,token,ids)


def sound():
    sr=22050; n=int(sr*.7); hp=max(1,int(sr/(880*2)))
    raw=b''.join(struct.pack('<h',12000 if (i//hp)%2==0 else -12000) for i in range(n)); buf=io.BytesIO()
    with wave.open(buf,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(sr);w.writeframes(raw)
    uri='data:audio/wav;base64,'+base64.b64encode(buf.getvalue()).decode(); st.components.v1.html(f"<audio autoplay><source src='{uri}' type='audio/wav'></audio>",height=1)


st.session_state.setdefault("cid",CLIENT_DEFAULT); st.session_state.setdefault("token",""); st.session_state.setdefault("connected",False)
with st.sidebar.form("dhan"):
    st.markdown("## 🔐 DHAN")
    cid=st.text_input("Client ID",st.session_state.cid)
    token=st.text_input("Access Token",st.session_state.token,type="password")
    go=st.form_submit_button("CONNECT",use_container_width=True)
if go:
    try:
        dhan("/profile",cid.strip(),token.strip()); st.session_state.cid=cid.strip();st.session_state.token=token.strip();st.session_state.connected=True;st.rerun()
    except Exception as e:st.sidebar.error(str(e))
if not st.session_state.connected:
    st.info("Enter your Dhan Access Token and connect.");st.stop()
if st.sidebar.button("LOGOUT / CLEAR"):
    st.session_state.connected=False;st.session_state.token="";st.cache_resource.clear();st.rerun()

threshold=st.sidebar.number_input("Vega spike %",1.0,200.0,20.0,1.0)
risk=st.sidebar.number_input("Risk-free rate %",0.0,15.0,6.0,.25)/100
sound_on=st.sidebar.checkbox("🔊 Warning sound",True)
manual=st.sidebar.text_input("Expiry YYYY-MM-DD","",help="Blank = nearest active Dhan expiry")

# UI reruns every 3 seconds; after setup there are NO REST requests on these reruns.
st_autorefresh(interval=3000,key="vega_ui")

cid,token=st.session_state.cid,st.session_state.token
now=datetime.now(IST); day=now.date().isoformat()
if st.session_state.get("day")!=day:
    for k in ("locked","strike","spot10","expiry","ce_id","pe_id","prev_ce","prev_pe","history","last_alert","expiry_cache","expiry_day"):st.session_state.pop(k,None)
    st.session_state.day=day

if manual:
    try:
        expiry=datetime.strptime(manual.strip(),"%Y-%m-%d").date().isoformat()
        if datetime.strptime(expiry,"%Y-%m-%d").date()<now.date():raise ValueError
    except Exception:st.error("Expiry must be a valid future YYYY-MM-DD date.");st.stop()
else:
    if st.session_state.get("expiry_day")!=day or not st.session_state.get("expiry_cache"):
        try:st.session_state.expiry_cache=expiry_list(cid,token);st.session_state.expiry_day=day
        except Exception as e:st.error(f"Expiry lookup failed: {e}");st.stop()
    if not st.session_state.expiry_cache:st.error("No active future NIFTY expiry returned by Dhan.");st.stop()
    expiry=st.session_state.expiry_cache[0]

st.title("📊 ALPHA • Fixed 10AM ATM Vega Monitor — WebSocket")
st.caption("10:00 ATM is fixed for the day. REST is used only for setup; live NIFTY + CE + PE prices are streamed continuously by Dhan WebSocket.")

if not st.session_state.get("locked"):
    if now.time()<LOCK:
        st.warning("⏳ Waiting for 10:00 IST to lock ATM.");st.stop()
    try:
        s10=spot_10(cid,token)
        if s10 is None:st.warning("10:00 NIFTY candle not available yet.");st.stop()
        data=chain(cid,token,expiry); rows=[]
        for k,node in (data.get("oc") or {}).items():
            try:strike=float(k)
            except:continue
            for side,key in (("CE","ce"),("PE","pe")):
                leg=node.get(key) or {}
                if leg.get("security_id"):rows.append((strike,side,str(leg["security_id"])))
        strikes=sorted({x[0] for x in rows});strike=min(strikes,key=lambda x:abs(x-s10))
        ce_id=next(x[2] for x in rows if x[0]==strike and x[1]=="CE");pe_id=next(x[2] for x in rows if x[0]==strike and x[1]=="PE")
        st.session_state.update(locked=True,strike=strike,spot10=s10,expiry=expiry,ce_id=ce_id,pe_id=pe_id,prev_ce=None,prev_pe=None,history=[],last_alert=None)
    except Exception as e:st.error(f"Unable to lock ATM: {e}");st.stop()

# If the user changes manual expiry after locking, force a fresh 10AM lock for that expiry.
if expiry!=st.session_state.expiry:
    for k in ("locked","strike","spot10","ce_id","pe_id","prev_ce","prev_pe","history","last_alert"):st.session_state.pop(k,None)
    st.rerun()

ids=(("IDX_I","13"),("NSE_FNO",st.session_state.ce_id),("NSE_FNO",st.session_state.pe_id))
f=feed(cid,token,ids)
spot=f.data.get("13",{}).get("ltp",st.session_state.spot10); ce_ltp=f.data.get(st.session_state.ce_id,{}).get("ltp",0); pe_ltp=f.data.get(st.session_state.pe_id,{}).get("ltp",0)
ce_iv,ce_v=iv_and_vega(spot,st.session_state.strike,ce_ltp,expiry,True,risk) if ce_ltp else (None,None)
pe_iv,pe_v=iv_and_vega(spot,st.session_state.strike,pe_ltp,expiry,False,risk) if pe_ltp else (None,None)

ce_pct=None if not ce_v or not st.session_state.prev_ce else (ce_v/st.session_state.prev_ce-1)*100
pe_pct=None if not pe_v or not st.session_state.prev_pe else (pe_v/st.session_state.prev_pe-1)*100
side=None;pct=None
if ce_pct is not None and ce_pct>=threshold:side,pct="ATM CE",ce_pct
if pe_pct is not None and pe_pct>=threshold and (pct is None or pe_pct>=pct):side,pct="ATM PE",pe_pct
bucket=int(now.timestamp()//180)
h=st.session_state.get("history",[])
if ce_v is not None and pe_v is not None and (not h or h[-1]["bucket"]!=bucket):h.append({"bucket":bucket,"time":now.strftime("%H:%M:%S"),"CE Vega":ce_v,"PE Vega":pe_v,"Difference":ce_v-pe_v});st.session_state.history=h[-60:]
if side:
    alert=f"{bucket}|{side}"
    if alert!=st.session_state.last_alert:
        st.session_state.last_alert=alert
        if sound_on:sound()
        st.error(f"⚠️ SUDDEN VEGA SPIKE — {side}: +{pct:.1f}%")

st.session_state.prev_ce=ce_v or st.session_state.prev_ce;st.session_state.prev_pe=pe_v or st.session_state.prev_pe
cols=st.columns(7)
cols[0].metric("🔒 10:00 ATM",f"{st.session_state.strike:,.0f}");cols[1].metric("10:00 SPOT",f"₹{st.session_state.spot10:,.2f}");cols[2].metric("LIVE SPOT",f"₹{spot:,.2f}");cols[3].metric("EXPIRY",expiry);cols[4].metric("CE VEGA","—" if ce_v is None else f"{ce_v:.3f}",None if ce_pct is None else f"{ce_pct:+.1f}%");cols[5].metric("PE VEGA","—" if pe_v is None else f"{pe_v:.3f}",None if pe_pct is None else f"{pe_pct:+.1f}%");cols[6].metric("VEGA DIFF","—" if ce_v is None or pe_v is None else f"{ce_v-pe_v:+.3f}")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(pd.DataFrame([{"Strike":st.session_state.strike,"CE ID":st.session_state.ce_id,"PE ID":st.session_state.pe_id,"CE LTP":ce_ltp or None,"PE LTP":pe_ltp or None,"CE IV %":None if ce_iv is None else ce_iv*100,"PE IV %":None if pe_iv is None else pe_iv*100,"CE Vega":ce_v,"PE Vega":pe_v}]),use_container_width=True,hide_index=True)
if st.session_state.history:st.line_chart(pd.DataFrame(st.session_state.history).set_index("time")[["CE Vega","PE Vega","Difference"]])
st.success(f"🟢 WebSocket: {f.status} | ticks: {len(f.data)} | REST live polling: OFF | ATM fixed at {st.session_state.strike:,.0f}")
if f.error:st.warning(f"WebSocket notice: {f.error}")
