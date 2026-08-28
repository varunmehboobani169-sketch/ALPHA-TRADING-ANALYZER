import base64
import io
import json
import math
import struct
import threading
import time
import urllib.error
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

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_CLIENT = "1113195747"
LOCK_TIME = dtime(10, 0)
UI_MS = 3000
BUCKET_SEC = 180
SETUP_COOLDOWN = 90
FEED_RETRY_COOLDOWN = 300
WS_URL = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2"

st.set_page_config(page_title="ALPHA Fixed 10AM Vega + OI", page_icon="📊", layout="wide")

DEFAULTS = {
    "cid": DEFAULT_CLIENT, "token": "", "connected": False, "day": "",
    "locked": False, "setup_retry_at": 0.0, "setup_error": "",
    "expiry_cache": [], "expiry_day": "", "expiry": "", "strike": None,
    "spot10": None, "ce_id": "", "pe_id": "", "wing_ids": {},
    "instruments": tuple(), "snapshot": {}, "prev_ce": None, "prev_pe": None,
    "prev_atm_iv": None, "history": [], "last_alert": None,
    "sample_bucket": None, "sample": {"atm_iv": None, "legs": {}},
    "feed": None,
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v.copy() if isinstance(v, dict) else list(v) if isinstance(v, list) else v


def api(path, body=None):
    req = urllib.request.Request(
        API + path, method="POST" if body is not None else "GET",
        headers={"Accept":"application/json","Content-Type":"application/json","access-token":st.session_state.token,"client-id":st.session_state.cid},
        data=None if body is None else json.dumps(body).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:700]
        raise RuntimeError(f"API HTTP {e.code}: {detail}") from e


def expiry_list():
    r = api("/optionchain/expirylist", {"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I"})
    today = datetime.now(IST).date()
    out = []
    for x in r.get("data", []) if isinstance(r, dict) else []:
        try:
            d = datetime.strptime(str(x), "%Y-%m-%d").date()
            if d >= today: out.append(d.isoformat())
        except ValueError:
            pass
    return sorted(set(out))


def reference_spot():
    day = datetime.now(IST).date().isoformat()
    r = api("/charts/intraday", {"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX","interval":"1","oi":False,"fromDate":f"{day} 09:15:00","toDate":f"{day} 10:01:00"})
    ts, close = r.get("timestamp", []), r.get("close", [])
    n = min(len(ts), len(close))
    if not n: return None
    t = pd.Series(pd.to_datetime(ts[:n], unit="s", utc=True)).dt.tz_convert(IST).dt.tz_localize(None)
    df = pd.DataFrame({"t":t,"c":pd.to_numeric(close[:n],errors="coerce")}).dropna()
    z = df[df.t.dt.strftime("%H:%M")=="10:00"]
    if z.empty: z = df[df.t.dt.time<=LOCK_TIME].tail(1)
    return None if z.empty else float(z.iloc[-1].c)


def option_chain(expiry):
    r = api("/optionchain", {"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I","Expiry":expiry})
    if not isinstance(r.get("data"), dict): raise RuntimeError("Invalid option-chain response")
    return r["data"]


def normcdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(s,k,T,r,v,call):
    if min(s,k,T,v) <= 0: return 0.0
    root = math.sqrt(T)
    d1 = (math.log(s/k)+(r+0.5*v*v)*T)/(v*root)
    d2 = d1-v*root
    dk = k*math.exp(-r*T)
    return s*normcdf(d1)-dk*normcdf(d2) if call else dk*normcdf(-d2)-s*normcdf(-d1)


def iv_vega(s,k,p,expiry,call,r):
    try:
        if not p or s <= 0 or k <= 0: return None,None
        ed = datetime.combine(datetime.strptime(expiry,"%Y-%m-%d").date(), dtime(15,30), tzinfo=IST).astimezone(timezone.utc)
        T = max((ed-datetime.now(timezone.utc)).total_seconds()/31536000.0,1e-7)
        intrinsic = max(0,s-k) if call else max(0,k-s)
        if p < intrinsic*0.98: return None,None
        lo,hi=1e-5,5.0
        if bs(s,k,T,r,hi,call) < p: return None,None
        for _ in range(60):
            mid=(lo+hi)/2
            if bs(s,k,T,r,mid,call)>p: hi=mid
            else: lo=mid
        vol=(lo+hi)/2
        d1=(math.log(s/k)+(r+0.5*vol*vol)*T)/(vol*math.sqrt(T))
        vg=s*math.exp(-0.5*d1*d1)/math.sqrt(2*math.pi)*math.sqrt(T)
        return vol,vg
    except Exception:
        return None,None


def pct(a,b):
    return None if a in (None,0) or b is None else (b/a-1)*100


class Feed:
    def __init__(self, cid, token, instruments):
        self.cid=cid; self.token=token; self.instruments=tuple(instruments)
        self.data={}; self.status="starting"; self.error=""; self.stop_flag=False
        self.retry_at=0.0; self.thread=threading.Thread(target=self.run,daemon=True); self.thread.start()

    def run(self):
        if websocket is None:
            self.status="feed library unavailable"; return
        while not self.stop_flag:
            now=time.time()
            if now < self.retry_at:
                self.status="rate-limit cooldown"; time.sleep(min(2,self.retry_at-now)); continue
            ws=None
            try:
                self.status="connecting"
                ws=websocket.create_connection(WS_URL.format(token=self.token,client=self.cid), timeout=20)
                ws.send(json.dumps({"RequestCode":17,"InstrumentCount":len(self.instruments),"InstrumentList":[{"ExchangeSegment":s,"SecurityId":str(i)} for s,i in self.instruments]}))
                self.status="connected"; self.error=""
                while not self.stop_flag:
                    packet=ws.recv()
                    if not packet or isinstance(packet,str): continue
                    b=bytes(packet)
                    if len(b)<8: continue
                    code=b[0]; sid=str(struct.unpack_from("<i",b,4)[0])
                    if code==2 and len(b)>=17:
                        self.data.setdefault(sid,{}).update({"ltp":float(struct.unpack_from("<f",b,9)[0]),"ltt":int(struct.unpack_from("<i",b,13)[0]),"updated":time.time()})
                    elif code==4 and len(b)>=51:
                        self.data.setdefault(sid,{}).update({"ltp":float(struct.unpack_from("<f",b,9)[0]),"ltt":int(struct.unpack_from("<i",b,15)[0]),"volume":int(struct.unpack_from("<i",b,23)[0]),"sell_qty":int(struct.unpack_from("<i",b,27)[0]),"buy_qty":int(struct.unpack_from("<i",b,31)[0]),"updated":time.time()})
                    elif code==5 and len(b)>=13:
                        self.data.setdefault(sid,{}).update({"oi":int(struct.unpack_from("<i",b,9)[0]),"updated":time.time()})
                    elif code==50:
                        self.error="Live feed disconnected"; break
            except Exception as e:
                msg=str(e); self.error=msg
                if getattr(e,"status_code",None)==429 or "429" in msg or "Too Many Requests" in msg:
                    self.retry_at=time.time()+FEED_RETRY_COOLDOWN; self.status="rate-limit cooldown"
                    break
                self.retry_at=time.time()+30
            finally:
                try:
                    if ws: ws.close()
                except Exception: pass
            if self.stop_flag: break
            if self.status=="connected": self.retry_at=time.time()+15
            time.sleep(max(1,self.retry_at-time.time()))
        self.status="stopped"

    def stop(self): self.stop_flag=True


def stop_feed():
    f=st.session_state.get("feed")
    if f:
        try: f.stop()
        except Exception: pass
    st.session_state.feed=None


def ensure_feed():
    desired=tuple(st.session_state.instruments)
    f=st.session_state.get("feed")
    if f is None or f.stop_flag or f.instruments!=desired or f.cid!=st.session_state.cid or f.token!=st.session_state.token:
        if f: f.stop()
        f=Feed(st.session_state.cid,st.session_state.token,desired)
        st.session_state.feed=f
    return f


def warning_sound():
    sr,dur,freq=22050,.7,880.; hp=max(1,int(sr/(freq*2)))
    raw=b"".join(struct.pack("<h",12000 if (i//hp)%2==0 else -12000) for i in range(int(sr*dur)))
    buf=io.BytesIO()
    with wave.open(buf,"wb") as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(sr);w.writeframes(raw)
    uri="data:audio/wav;base64,"+base64.b64encode(buf.getvalue()).decode()
    st.components.v1.html(f"<audio autoplay><source src='{uri}' type='audio/wav'></audio>",height=1)


with st.sidebar.form("connection"):
    st.markdown("## 🔐 MARKET DATA CONNECTION")
    ci=st.text_input("Client ID",st.session_state.cid)
    tok=st.text_input("Access Token",st.session_state.token,type="password")
    connect=st.form_submit_button("CONNECT",use_container_width=True)
if connect:
    try:
        old=st.session_state.get("feed")
        if old: old.stop()
        # One authentication call only on explicit button press.
        req=urllib.request.Request(API+"/profile",method="GET",headers={"Accept":"application/json","access-token":tok.strip(),"client-id":ci.strip()})
        with urllib.request.urlopen(req,timeout=20): pass
        st.session_state.cid=ci.strip(); st.session_state.token=tok.strip(); st.session_state.connected=True
        st.session_state.setup_retry_at=0; st.session_state.setup_error=""; st.rerun()
    except Exception as e: st.sidebar.error(f"Connection failed: {e}")
if not st.session_state.connected:
    st.info("Enter your access token and connect to the market data feed."); st.stop()
if st.sidebar.button("LOGOUT / CLEAR",use_container_width=True):
    stop_feed(); st.session_state.connected=False; st.session_state.token=""; st.rerun()

vega_thr=st.sidebar.number_input("Vega spike %",1.0,200.0,20.0,1.0)
risk=st.sidebar.number_input("Risk-free rate %",0.0,15.0,6.0,.25)/100
sound_on=st.sidebar.checkbox("🔊 Warning sound",True)
manual=st.sidebar.text_input("Expiry YYYY-MM-DD","")
oi_thr=st.sidebar.number_input("Wing OI significant move %",.5,50.,2.,.5)
iv_thr=st.sidebar.number_input("ATM IV rise confirmation %",.2,20.,1.,.2)
st_autorefresh(interval=UI_MS,key="vega_v12_ui")

now=datetime.now(IST); day=now.date().isoformat()
if st.session_state.day!=day:
    stop_feed()
    for k in ("locked","setup_retry_at","setup_error","expiry_cache","expiry_day","expiry","strike","spot10","ce_id","pe_id","wing_ids","instruments","snapshot","prev_ce","prev_pe","prev_atm_iv","history","last_alert","sample","sample_bucket"):
        st.session_state.pop(k,None)
    for k,v in DEFAULTS.items():
        st.session_state.setdefault(k,v.copy() if isinstance(v,dict) else list(v) if isinstance(v,list) else v)
    st.session_state.day=day

if manual:
    try:
        d=datetime.strptime(manual.strip(),"%Y-%m-%d").date()
        if d<now.date(): raise ValueError
        expiry=d.isoformat()
    except ValueError:
        st.error("Expiry must be a valid current/future YYYY-MM-DD date."); st.stop()
else:
    if st.session_state.expiry_day!=day or not st.session_state.expiry_cache:
        if time.time()>=st.session_state.setup_retry_at:
            try:
                st.session_state.expiry_cache=expiry_list(); st.session_state.expiry_day=day; st.session_state.setup_error=""; st.session_state.setup_retry_at=0
            except Exception as e:
                st.session_state.setup_error=str(e); st.session_state.setup_retry_at=time.time()+SETUP_COOLDOWN
    if not st.session_state.expiry_cache:
        left=max(0,int(st.session_state.setup_retry_at-time.time()))
        st.title("📊 ALPHA • Fixed 10AM Vega + Seller OI Monitor")
        st.warning(f"Setup temporarily unavailable. Next controlled retry in {left}s.")
        st.stop()
    expiry=st.session_state.expiry_cache[0]

st.title("📊 ALPHA • Fixed 10AM Vega + Seller OI Monitor")
st.caption("10:00 ATM is fixed for the day. Live option prices and OI use one persistent feed connection; setup calls are throttled.")

if not st.session_state.locked:
    if now.time()<LOCK_TIME:
        st.warning("⏳ Waiting for 10:00 IST to lock the reference strike."); st.stop()
    if time.time()<st.session_state.setup_retry_at:
        st.warning(f"⏸️ Reference setup cooling down. Retry in {max(0,int(st.session_state.setup_retry_at-time.time()))}s."); st.stop()
    try:
        # One chart call + one chain call per controlled setup attempt.
        s10=reference_spot()
        if s10 is None: raise RuntimeError("10:00 reference candle not available")
        data=option_chain(expiry); rows=[]
        for raw,node in (data.get("oc") or {}).items():
            try:k=float(raw)
            except Exception:continue
            for side,key in (("CE","ce"),("PE","pe")):
                leg=node.get(key) or {}; sid=leg.get("security_id")
                if sid: rows.append((k,side,str(sid),leg))
        strikes=sorted({r[0] for r in rows})
        if not strikes: raise RuntimeError("No option strikes returned")
        atm=min(strikes,key=lambda x:abs(x-s10)); ce=next(r for r in rows if r[0]==atm and r[1]=="CE"); pe=next(r for r in rows if r[0]==atm and r[1]=="PE")
        gaps=[b-a for a,b in zip(strikes,strikes[1:]) if b>a]; step=min(gaps) if gaps else 50.0
        wings={}
        for k,side,sid,leg in rows:
            off=round((k-atm)/step)
            if (side=="CE" and off in (1,2)) or (side=="PE" and off in (-1,-2)): wings[(off,side)]=(sid,k,leg)
        inst=[("IDX_I","13"),("NSE_FNO",ce[2]),("NSE_FNO",pe[2])]
        for off in (1,2,-1,-2):
            x=wings.get((off,"CE" if off>0 else "PE"))
            if x: inst.append(("NSE_FNO",x[0]))
        snap={}
        for k,side,sid,leg in [ce,pe]+[(v[1],"CE" if o>0 else "PE",v[0],v[2]) for o,v in wings.items()]:
            snap[sid]={"ltp":leg.get("last_price"),"oi":leg.get("oi"),"iv":leg.get("implied_volatility"),"vega":(leg.get("greeks") or {}).get("vega"),"strike":k}
        st.session_state.update(locked=True,setup_retry_at=0.0,setup_error="",strike=atm,spot10=s10,expiry=expiry,ce_id=ce[2],pe_id=pe[2],wing_ids={o:(wings.get((o,"CE" if o>0 else "PE")) or [""])[0] for o in (1,2,-1,-2)},instruments=tuple(dict.fromkeys(inst)),snapshot=snap,prev_ce=snap.get(ce[2],{}).get("vega"),prev_pe=snap.get(pe[2],{}).get("vega"),prev_atm_iv=snap.get(ce[2],{}).get("iv"),history=[],last_alert=None,sample={},sample_bucket=None)
    except Exception as e:
        st.session_state.setup_error=str(e); st.session_state.setup_retry_at=time.time()+SETUP_COOLDOWN
        st.warning(f"⚠️ Reference strike not locked. No repeat setup request for {SETUP_COOLDOWN}s. Error: {e}"); st.stop()

feed=ensure_feed()

def quote(sid):
    live=feed.data.get(str(sid),{})
    base=st.session_state.snapshot.get(str(sid),{})
    out=dict(base); out.update({k:v for k,v in live.items() if v is not None})
    return out

spot=quote("13").get("ltp",st.session_state.spot10)
ceq=quote(st.session_state.ce_id); peq=quote(st.session_state.pe_id)
ce_ltp=ceq.get("ltp"); pe_ltp=peq.get("ltp")
ce_iv,ce_v=iv_vega(spot,st.session_state.strike,ce_ltp,expiry,True,risk) if ce_ltp else (ceq.get("iv"),ceq.get("vega"))
pe_iv,pe_v=iv_vega(spot,st.session_state.strike,pe_ltp,expiry,False,risk) if pe_ltp else (peq.get("iv"),peq.get("vega"))
if ce_iv is None: ce_iv=ceq.get("iv")
if pe_iv is None: pe_iv=peq.get("iv")
if ce_v is None: ce_v=ceq.get("vega")
if pe_v is None: pe_v=peq.get("vega")
atmivs=[x for x in (ce_iv,pe_iv) if x is not None]; atm_iv=sum(atmivs)/len(atmivs) if atmivs else None
cevp=pct(st.session_state.prev_ce,ce_v); pevp=pct(st.session_state.prev_pe,pe_v); ivp=pct(st.session_state.prev_atm_iv,atm_iv)

bucket=int(time.time()//BUCKET_SEC)
if st.session_state.sample_bucket!=bucket:
    st.session_state.sample_bucket=bucket; st.session_state.sample={"atm_iv":atm_iv,"legs":{}}
sample=st.session_state.sample
for off in (1,2,-1,-2):
    sid=st.session_state.wing_ids.get(off,"")
    if sid:
        q=quote(sid); sample["legs"].setdefault(off,{"ltp":q.get("ltp"),"oi":q.get("oi")})

rows=[]
for off in (1,2,-1,-2):
    sid=st.session_state.wing_ids.get(off,"")
    if not sid: continue
    q=quote(sid); base=sample["legs"].get(off,{})
    oip=pct(base.get("oi"),q.get("oi")); pp=pct(base.get("ltp"),q.get("ltp")); seller=bool(oip is not None and oip>=oi_thr and pp is not None and pp<=.5 and ivp is not None and ivp>=iv_thr)
    view="WAITING OI" if oip is None else "SELLER BUILDUP" if seller else "OI BUILD + PREMIUM UP" if oip>=oi_thr and pp is not None and pp>.5 else "OI UNWINDING" if oip<=-oi_thr else "NEUTRAL"
    rows.append({"Wing":f"ATM{off:+d} {'CALL' if off>0 else 'PUT'}","LTP":q.get("ltp"),"OI":q.get("oi"),"OI Change / 3m %":oip,"Premium Change / 3m %":pp,"ATM IV Change / 3m %":ivp,"Seller View":view})

seller_count=sum(r["Seller View"]=="SELLER BUILDUP" for r in rows); seller_alert=seller_count>=2 and ivp is not None and ivp>=iv_thr
spike_side=None; spike_pct=None
if cevp is not None and cevp>=vega_thr: spike_side,spike_pct="ATM CE",cevp
if pevp is not None and pevp>=vega_thr and (spike_pct is None or pevp>=spike_pct): spike_side,spike_pct="ATM PE",pevp

key=f"{bucket}|{spike_side}|{seller_alert}"
new=bool((spike_side or seller_alert) and key!=st.session_state.last_alert)
if new: st.session_state.last_alert=key
if spike_side: st.error(f"⚠️ SUDDEN VEGA SPIKE — {spike_side}: +{spike_pct:.1f}% over previous 3-minute sample")
if seller_alert: st.error(f"⚠️ SELLER-SIDE OI SIGNAL — {seller_count}/4 wings with OI build while ATM IV is rising")
if new and sound_on: warning_sound()

st.metric("🔒 10:00 ATM",f"{st.session_state.strike:,.0f}")
st.metric("10:00 SPOT",f"₹{st.session_state.spot10:,.2f}")
st.metric("LIVE SPOT",f"₹{spot:,.2f}")
st.metric("ATM CE VEGA","—" if ce_v is None else f"{ce_v:.3f}",None if cevp is None else f"{cevp:+.1f}%")
st.metric("ATM PE VEGA","—" if pe_v is None else f"{pe_v:.3f}",None if pevp is None else f"{pevp:+.1f}%")
st.metric("ATM IV","—" if atm_iv is None else f"{atm_iv*100:.2f}%",None if ivp is None else f"{ivp:+.2f}%")
st.metric("Seller Wings",f"{seller_count}/4")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(pd.DataFrame([{"Strike":st.session_state.strike,"CE":st.session_state.ce_id,"PE":st.session_state.pe_id,"CE LTP":ce_ltp,"PE LTP":pe_ltp,"CE IV %":None if ce_iv is None else ce_iv*100,"PE IV %":None if pe_iv is None else pe_iv*100,"ATM IV %":None if atm_iv is None else atm_iv*100,"CE Vega":ce_v,"PE Vega":pe_v}]),use_container_width=True,hide_index=True)

st.markdown("### 🧭 SELLER-SIDE OI MAP — ATM ±1 / ±2")
st.caption("Seller confirmation = wing OI increase + premium softness + rising ATM IV. This is an inference signal, not proof of participant intent.")
st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)

if st.session_state.history:
    st.markdown("### 📈 3-Minute ATM Vega / IV History")
    st.line_chart(pd.DataFrame(st.session_state.history).set_index("time")[["ATM IV %","CE Vega","PE Vega","Vega Difference"]])

st.success(f"🟢 Live Feed: {feed.status} | subscribed: {len(st.session_state.instruments)} | REST live polling: OFF | ATM fixed: {st.session_state.strike:,.0f}")
if feed.error and feed.status != "rate-limit cooldown": st.warning(f"Live feed notice: {feed.error}")
if feed.status == "rate-limit cooldown": st.warning("Live feed is temporarily rate-limited. No reconnect storm will be sent; the dashboard is using the last good option snapshot until the controlled retry.")
