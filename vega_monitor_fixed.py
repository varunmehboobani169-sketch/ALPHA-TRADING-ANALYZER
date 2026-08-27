import json
import urllib.error
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

DHAN_API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
LOCK = time(10, 0)
REFRESH_SECONDS = 180

st.set_page_config(page_title="ALPHA • Fixed 10AM Vega", page_icon="📊", layout="wide")


def dhan(path, cid, token, method="GET", body=None):
    headers = {"Accept":"application/json","Content-Type":"application/json","access-token":token,"client-id":cid}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(DHAN_API + path, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:800]
        raise RuntimeError(f"Dhan HTTP {e.code}: {text}") from e


def chain(cid, token, sid, seg, expiry):
    return dhan("/optionchain", cid, token, "POST", {"UnderlyingScrip":int(sid),"UnderlyingSeg":seg,"Expiry":expiry}).get("data", {})


def expiries(cid, token, sid, seg):
    return [str(x) for x in dhan("/optionchain/expirylist", cid, token, "POST", {"UnderlyingScrip":int(sid),"UnderlyingSeg":seg}).get("data", [])]


def spot_1000(cid, token, sid, seg, day):
    body={"securityId":str(sid),"exchangeSegment":seg,"instrument":"INDEX","interval":"1","oi":False,"fromDate":f"{day} 09:15:00","toDate":f"{day} 10:01:00"}
    d=dhan("/charts/intraday",cid,token,"POST",body)
    ts=d.get("timestamp",[]); cl=d.get("close",[])
    if not ts or not cl: return None
    df=pd.DataFrame({"ts":pd.to_datetime(ts,unit="s",utc=True).tz_convert(IST).tz_localize(None),"close":pd.to_numeric(cl,errors="coerce")}).dropna()
    exact=df[df.ts.dt.strftime("%H:%M")=="10:00"]
    if exact.empty: exact=df[df.ts.dt.time<=LOCK].tail(1)
    return None if exact.empty else float(exact.iloc[-1].close)


def frame(c):
    rows=[]
    for strike,node in (c.get("oc") or {}).items():
        try: s=float(strike)
        except: continue
        ce=node.get("ce") or {}; pe=node.get("pe") or {}
        cg=ce.get("greeks") or {}; pg=pe.get("greeks") or {}
        rows.append({"strike":s,"CE Vega":float(cg.get("vega",0) or 0),"PE Vega":float(pg.get("vega",0) or 0),"CE LTP":float(ce.get("last_price",0) or 0),"PE LTP":float(pe.get("last_price",0) or 0),"CE OI":float(ce.get("oi",0) or 0),"PE OI":float(pe.get("oi",0) or 0),"CE IV":float(ce.get("implied_volatility",0) or 0),"PE IV":float(pe.get("implied_volatility",0) or 0),"CE ID":str(ce.get("security_id","")),"PE ID":str(pe.get("security_id",""))})
    return float(c.get("last_price",0) or 0),pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)


def login():
    st.sidebar.header("🔐 DHAN")
    st.session_state.setdefault("cid",""); st.session_state.setdefault("token",""); st.session_state.setdefault("connected",False)
    with st.sidebar.form("dhan"):
        cid=st.text_input("Client ID",st.session_state.cid)
        tok=st.text_input("Access Token",st.session_state.token,type="password")
        ok=st.form_submit_button("CONNECT",use_container_width=True)
    if ok:
        try:
            dhan("/profile",cid.strip(),tok.strip())
            st.session_state.cid=cid.strip(); st.session_state.token=tok.strip(); st.session_state.connected=True; st.rerun()
        except Exception as e: st.sidebar.error(str(e))
    if st.session_state.connected and st.sidebar.button("CLEAR CONNECTION",use_container_width=True):
        st.session_state.cid=""; st.session_state.token=""; st.session_state.connected=False; st.rerun()


login()
st.sidebar.markdown("### Monitor")
sid=st.sidebar.number_input("NIFTY Security ID",1,999999,13)
seg=st.sidebar.selectbox("Segment",["IDX_I","NSE_FNO"])
expiry_mode=st.sidebar.selectbox("Expiry",["NEAREST","NEXT"])
threshold=st.sidebar.number_input("Vega spike %",1.0,200.0,20.0,1.0)
sound=st.sidebar.checkbox("🔊 Warning sound",True)

st.title("📊 Fixed 10AM ATM Vega Monitor")
st.caption("The 10:00 IST ATM strike is fixed for the whole session; only that CE + PE straddle is monitored.")

if not st.session_state.connected: st.stop()

now=datetime.now(IST); day=now.date().isoformat()
if st.session_state.get("day")!=day:
    for k in list(st.session_state.keys()):
        if k.startswith("v_"): del st.session_state[k]
    st.session_state.day=day

@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def monitor():
    cid,tok=st.session_state.cid,st.session_state.token
    # expiry list cached for the session; it is not requested every refresh.
    if "v_expiries" not in st.session_state:
        try: st.session_state.v_expiries=expiries(cid,tok,sid,seg)
        except Exception as e: st.error(f"Expiry request failed: {e}"); return
    ex=st.session_state.v_expiries
    if not ex: st.error("No active expiries returned by Dhan."); return
    expiry=ex[0 if expiry_mode=="NEAREST" else min(1,len(ex)-1)]

    if "v_atm" not in st.session_state:
        if now.time()<LOCK:
            st.warning("⏳ Waiting for 10:00 IST to lock ATM."); return
        try:
            ref=spot_1000(cid,tok,sid,seg,day)
            if ref is None: st.warning("10:00 NIFTY reference candle is unavailable yet."); return
            c=chain(cid,tok,sid,seg,expiry); _,df=frame(c)
            if df.empty: st.error("No option strikes returned."); return
            st.session_state.v_atm=float(df.iloc[(df.strike-ref).abs().argmin()].strike)
            st.session_state.v_ref=ref
            st.session_state.v_prev_ce=None; st.session_state.v_prev_pe=None
            st.session_state.v_chain=c
        except Exception as e: st.error(f"ATM lock failed: {e}"); return

    try:
        c=chain(cid,tok,sid,seg,expiry); st.session_state.v_chain=c
    except Exception:
        c=st.session_state.get("v_chain")
        if c is None: st.error("Dhan rate limit/error and no previous snapshot available."); return
        st.warning("⚠️ Dhan rate limit reached. Showing the last good snapshot; next cycle will retry.")
    spot,df=frame(c)
    if df.empty: return
    atm=st.session_state.v_atm
    row=df.iloc[(df.strike-atm).abs().argmin()]
    ce,pe=float(row["CE Vega"]),float(row["PE Vega"])
    prev_ce,prev_pe=st.session_state.get("v_prev_ce"),st.session_state.get("v_prev_pe")
    ce_pct=None if prev_ce in (None,0) else (ce/prev_ce-1)*100
    pe_pct=None if prev_pe in (None,0) else (pe/prev_pe-1)*100
    spike=("ATM CE",ce_pct) if ce_pct is not None and ce_pct>=threshold and (pe_pct is None or ce_pct>=pe_pct) else ("ATM PE",pe_pct) if pe_pct is not None and pe_pct>=threshold else None
    st.session_state.v_prev_ce,st.session_state.v_prev_pe=ce,pe

    a,b,c1,d,e,f,g=st.columns(7)
    a.metric("10:00 ATM",f"{atm:,.0f}"); b.metric("10:00 SPOT",f"₹{st.session_state.v_ref:,.2f}"); c1.metric("CURRENT SPOT",f"₹{spot:,.2f}"); d.metric("EXPIRY",expiry); e.metric("CE VEGA",f"{ce:.3f}",None if ce_pct is None else f"{ce_pct:+.1f}%"); f.metric("PE VEGA",f"{pe:.3f}",None if pe_pct is None else f"{pe_pct:+.1f}%"); g.metric("CE − PE VEGA",f"{ce-pe:+.3f}")
    st.subheader("🔒 Fixed 10:00 Straddle")
    st.dataframe(pd.DataFrame([{"Strike":atm,"CE Vega":ce,"PE Vega":pe,"CE LTP":row["CE LTP"],"PE LTP":row["PE LTP"],"CE OI":row["CE OI"],"PE OI":row["PE OI"],"CE IV":row["CE IV"],"PE IV":row["PE IV"]}]),use_container_width=True,hide_index=True)
    if spike:
        st.error(f"⚠️ SUDDEN VEGA SPIKE — {spike[0]} {spike[1]:+.1f}% in the last 3-minute sample")
        if sound:
            st.components.v1.html("<script>new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA=').play().catch(()=>{});</script>",height=1)
    st.caption(f"Updated {now.strftime('%d-%b-%Y %H:%M:%S')} IST • refresh every 3 minutes • spike threshold {threshold:.0f}%")

monitor()
