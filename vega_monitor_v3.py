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
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"
IST = ZoneInfo("Asia/Kolkata")
ATM_LOCK_TIME = time(10, 0)
REFRESH_SECONDS = 180

st.set_page_config(page_title="ALPHA • Fixed 10AM Vega", page_icon="📊", layout="wide")

st.markdown("""
<style>
.block-container{padding-top:1rem;padding-bottom:2rem}.small-muted{color:#8b949e;font-size:.82rem}
.fixed-card{padding:16px 18px;border-radius:14px;border:1px solid #30363d;background:#161b22}
.fixed-title{font-size:.76rem;color:#8b949e;text-transform:uppercase;letter-spacing:.08em}.fixed-value{font-size:1.5rem;font-weight:800}
.spike-card{padding:18px;border-radius:14px;border:2px solid #ff4b4b;background:rgba(255,75,75,.12)}
</style>
""", unsafe_allow_html=True)


def dhan_request(path, client_id, token, method="GET", body=None):
    headers={"Accept":"application/json","Content-Type":"application/json","access-token":token,"client-id":client_id}
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(DHAN_API+path, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw=r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Dhan HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def connect_box():
    st.sidebar.markdown("## 🔐 DHAN CONNECTION")
    st.sidebar.caption("Credentials stay in Streamlit session state only.")
    st.session_state.setdefault("dhan_client_id","")
    st.session_state.setdefault("dhan_token","")
    st.session_state.setdefault("dhan_connected",False)
    with st.sidebar.form("dhan_login", clear_on_submit=False):
        cid=st.text_input("Dhan Client ID / Username", value=st.session_state.dhan_client_id)
        tok=st.text_input("Dhan Access Token", value=st.session_state.dhan_token, type="password")
        go=st.form_submit_button("🔗 CONNECT TO DHAN", use_container_width=True, type="primary")
    if go:
        cid,tok=cid.strip(),tok.strip()
        if not cid or not tok:
            st.sidebar.error("Enter both Client ID and Access Token.")
        else:
            try:
                dhan_request("/profile",cid,tok)
                st.session_state.dhan_client_id=cid; st.session_state.dhan_token=tok; st.session_state.dhan_connected=True
                st.rerun()
            except Exception as exc:
                st.session_state.dhan_connected=False; st.sidebar.error("❌ Dhan connection failed"); st.sidebar.caption(str(exc))
    if st.session_state.dhan_connected:
        st.sidebar.success(f"Connected: {st.session_state.dhan_client_id}")
        if st.sidebar.button("LOGOUT / CLEAR DHAN", use_container_width=True):
            st.session_state.dhan_client_id=""; st.session_state.dhan_token=""; st.session_state.dhan_connected=False; st.rerun()
    else: st.sidebar.warning("Not connected to Dhan")


def public_instrument_master():
    cached=st.session_state.get("public_master")
    cached_at=st.session_state.get("public_master_at",0)
    if cached is not None and (datetime.now(IST).timestamp()-cached_at)<3600:
        return cached
    req=urllib.request.Request(SCRIP_MASTER,headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req,timeout=30) as r:
        raw=r.read()
    df=pd.read_csv(io.BytesIO(raw), low_memory=False)
    st.session_state.public_master=df; st.session_state.public_master_at=datetime.now(IST).timestamp()
    return df


def find_col(df, names):
    lower={str(c).strip().lower():c for c in df.columns}
    for n in names:
        if n.lower() in lower:return lower[n.lower()]
    for k,v in lower.items():
        if any(n.lower() in k for n in names):return v
    return None


def expiry_candidates(security_id=13):
    df=public_instrument_master()
    exp_col=find_col(df,["SEM_EXPIRY_DATE","expiry_date","expiry"])
    seg_col=find_col(df,["SEM_SEGMENT","segment"])
    sid_col=find_col(df,["SEM_SMST_SECURITY_ID","security_id","SMST_SECURITY_ID"])
    inst_col=find_col(df,["SEM_INSTRUMENT_NAME","instrument_name","instrument"])
    if not exp_col:return []
    work=df.copy()
    if seg_col:
        seg=work[seg_col].astype(str).str.upper()
        work=work[seg.eq("D") | seg.eq("NSE_FNO") | seg.eq("FNO")]
    if sid_col and security_id is not None:
        sid=pd.to_numeric(work[sid_col],errors="coerce")
        # Keep index-option related rows where possible; if master naming differs, fall back to all NSE F&O rows.
        if sid.eq(float(security_id)).any(): work=work[sid.eq(float(security_id))]
    if inst_col:
        names=work[inst_col].astype(str).str.upper()
        if names.str.contains("OPT",na=False).any():work=work[names.str.contains("OPT",na=False)]
    vals=pd.to_datetime(work[exp_col],errors="coerce",dayfirst=False).dropna().dt.strftime("%Y-%m-%d")
    today=datetime.now(IST).date()
    out=sorted({x for x in vals if pd.Timestamp(x).date()>=today})
    return out


def expiry_value():
    candidates=expiry_candidates()
    if not candidates: return None
    return candidates[0]


def option_chain(client_id, token, security_id, segment, expiry):
    body={"UnderlyingScrip":int(security_id),"UnderlyingSeg":segment,"Expiry":expiry}
    return dhan_request("/optionchain",client_id,token,method="POST",body=body).get("data",{})


def intraday_underlying(client_id, token, security_id, segment, trading_date):
    body={"securityId":str(security_id),"exchangeSegment":segment,"instrument":"INDEX","interval":"1","oi":False,
          "fromDate":f"{trading_date} 09:15:00","toDate":f"{trading_date} 10:01:00"}
    response=dhan_request("/charts/intraday",client_id,token,method="POST",body=body)
    if not isinstance(response,dict):return pd.DataFrame()
    ts=response.get("timestamp") or []; close=response.get("close") or []
    n=min(len(ts),len(close))
    if n==0:return pd.DataFrame()
    dt=pd.to_datetime(ts[:n],unit="s",utc=True).tz_convert(IST).tz_localize(None)
    return pd.DataFrame({"timestamp":dt,"close":pd.to_numeric(close[:n],errors="coerce")}).dropna().sort_values("timestamp").reset_index(drop=True)


def chain_frame(data):
    rows=[]; spot=float(data.get("last_price",0) or 0)
    for k,node in (data.get("oc",{}) or {}).items():
        try:strike=float(k)
        except Exception:continue
        ce=node.get("ce") or {}; pe=node.get("pe") or {}; cg=ce.get("greeks") or {}; pg=pe.get("greeks") or {}
        rows.append({"strike":strike,"CE Vega":float(cg.get("vega",0) or 0),"PE Vega":float(pg.get("vega",0) or 0),
                     "CE LTP":float(ce.get("last_price",0) or 0),"PE LTP":float(pe.get("last_price",0) or 0),
                     "CE OI":float(ce.get("oi",0) or 0),"PE OI":float(pe.get("oi",0) or 0),
                     "CE IV":float(ce.get("implied_volatility",0) or 0),"PE IV":float(pe.get("implied_volatility",0) or 0),
                     "CE Security ID":str(ce.get("security_id","")),"PE Security ID":str(pe.get("security_id",""))})
    return spot,pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)


def beep_uri():
    sr=22050; dur=.65; freq=880; period=max(1,int(sr/(freq*2))); frames=[]
    for i in range(int(sr*dur)): frames.append(struct.pack("<h",12000 if (i//period)%2==0 else -12000))
    b=io.BytesIO()
    with wave.open(b,"wb") as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(sr);w.writeframes(b"".join(frames))
    return "data:audio/wav;base64,"+base64.b64encode(b.getvalue()).decode()


def sound_alert():
    uri=beep_uri(); st.components.v1.html(f'''<audio autoplay><source src="{uri}" type="audio/wav"></audio><script>const a=document.querySelector('audio');if(a){{a.volume=1;a.play().catch(()=>{{}});}}</script>''',height=1)


connect_box()
st.sidebar.markdown("## ⚙️ SETTINGS")
security_id=st.sidebar.number_input("NIFTY 50 Security ID",min_value=1,value=13,step=1)
segment=st.sidebar.selectbox("Underlying Segment",["IDX_I","NSE_FNO"])
manual_expiry=st.sidebar.text_input("Expiry (YYYY-MM-DD)",value="",help="Leave blank to use nearest expiry from the public Dhan instrument master.")
spike_threshold=st.sidebar.number_input("Sudden Vega Spike Threshold %",1.0,200.0,20.0,1.0)
sound_enabled=st.sidebar.checkbox("🔊 Warning Sound",value=True)

st.title("📊 ALPHA • Fixed 10AM ATM Vega Monitor")
st.caption("One fixed 10:00 IST ATM straddle, refreshed every 3 minutes. No expiry-list API call is made on the refresh cycle.")

if not st.session_state.dhan_connected:
    st.info("Connect your Dhan account from the left sidebar."); st.stop()

cid=st.session_state.dhan_client_id; tok=st.session_state.dhan_token; now=datetime.now(IST); day_key=now.strftime("%Y-%m-%d")
if st.session_state.get("vega_day")!=day_key:
    for k in ["atm_locked","atm_strike","atm_spot","expiry","history","prev_ce","prev_pe","last_chain","last_good_at","last_spike_key"]:st.session_state.pop(k,None)
    st.session_state.vega_day=day_key

# Streamlit native periodic rerun, no third-party autorefresh package needed.
if hasattr(st,"fragment"):
    pass

expiry=manual_expiry.strip() or st.session_state.get("expiry") or expiry_value()
if not expiry:
    st.error("Could not determine an expiry from the public instrument master. Enter the expiry manually in YYYY-MM-DD."); st.stop()
st.session_state.expiry=expiry

if not st.session_state.get("atm_locked"):
    if now.time()<ATM_LOCK_TIME:
        st.warning("⏳ ATM locks at 10:00 IST from the NIFTY 1-minute candle."); st.stop()
    try:
        under=intraday_underlying(cid,tok,security_id,segment,day_key)
        exact=under[under.timestamp.dt.strftime("%H:%M")=="10:00"]
        if exact.empty: exact=under[under.timestamp.dt.time<=ATM_LOCK_TIME].tail(1)
        if exact.empty:st.warning("10:00 candle not available yet.");st.stop()
        ref=float(exact.iloc[-1].close)
        data=option_chain(cid,tok,security_id,segment,expiry)
        _,frm=chain_frame(data)
        if frm.empty:st.error("Option chain returned no strikes.");st.stop()
        strike=float(frm.iloc[(frm.strike-ref).abs().argmin()].strike)
        st.session_state.atm_locked=True;st.session_state.atm_strike=strike;st.session_state.atm_spot=ref;st.session_state.history=[]
    except Exception as exc:
        st.error(f"Unable to lock ATM: {exc}");st.stop()

strike=float(st.session_state.atm_strike)
try:
    data=option_chain(cid,tok,security_id,segment,expiry)
    st.session_state.last_chain=data; st.session_state.last_good_at=now.timestamp(); rate_limited=False
except Exception as exc:
    data=st.session_state.get("last_chain"); rate_limited="805" in str(exc) or "429" in str(exc)
    if not data:st.error(f"Unable to load option chain: {exc}");st.stop()

spot,frm=chain_frame(data)
row=frm.iloc[(frm.strike-strike).abs().argmin()]
ce=float(row["CE Vega"]); pe=float(row["PE Vega"]); diff=ce-pe
pce=st.session_state.get("prev_ce"); ppe=st.session_state.get("prev_pe")
ce_pct=None if pce in (None,0) else (ce/pce-1)*100; pe_pct=None if ppe in (None,0) else (pe/ppe-1)*100
spike_side="ATM CE" if ce_pct is not None and ce_pct>=spike_threshold and (pe_pct is None or ce_pct>=pe_pct) else "ATM PE" if pe_pct is not None and pe_pct>=spike_threshold else None
bucket=int(now.timestamp()//REFRESH_SECONDS)
hist=st.session_state.get("history",[])
if not hist or hist[-1]["bucket"]!=bucket: hist.append({"bucket":bucket,"time":now.strftime("%H:%M"),"CE Vega":ce,"PE Vega":pe,"Vega Difference":diff})
st.session_state.history=hist[-60:]
key=f"{day_key}|{bucket}|{spike_side}"; new_spike=bool(spike_side and key!=st.session_state.get("last_spike_key")); st.session_state.last_spike_key=key if new_spike else st.session_state.get("last_spike_key")
st.session_state.prev_ce=ce; st.session_state.prev_pe=pe

if rate_limited:st.warning("⚠️ Dhan rate limit (805) is active. Showing the last good snapshot; no extra retry is sent before the next 3-minute cycle.")

t=st.columns(7);t[0].metric("10:00 ATM STRIKE",f"{strike:,.0f}");t[1].metric("10:00 SPOT",f"₹{st.session_state.atm_spot:,.2f}");t[2].metric("CURRENT SPOT",f"₹{spot:,.2f}");t[3].metric("EXPIRY",expiry);t[4].metric("ATM CE VEGA",f"{ce:.3f}",None if ce_pct is None else f"{ce_pct:+.1f}%");t[5].metric("ATM PE VEGA",f"{pe:.3f}",None if pe_pct is None else f"{pe_pct:+.1f}%");t[6].metric("VEGA DIFFERENCE",f"{diff:+.3f}")

st.markdown("### 🔒 FIXED 10:00 STRADDLE")
st.dataframe(pd.DataFrame([{"Strike":strike,"CE Vega":ce,"PE Vega":pe,"CE LTP":row["CE LTP"],"PE LTP":row["PE LTP"],"CE OI":row["CE OI"],"PE OI":row["PE OI"],"CE IV":row["CE IV"],"PE IV":row["PE IV"],"Refresh":now.strftime("%H:%M:%S")}]),use_container_width=True,hide_index=True)
if new_spike:
    st.markdown(f'<div class="spike-card"><div class="fixed-title">⚠️ SUDDEN VEGA SPIKE</div><div class="fixed-value">{spike_side} +{(ce_pct if spike_side=="ATM CE" else pe_pct):.1f}% in the last 3-minute sample</div></div>',unsafe_allow_html=True)
    if sound_enabled:sound_alert()
elif spike_side:st.warning(f"⚠️ {spike_side} is above the spike threshold: +{(ce_pct if spike_side=="ATM CE" else pe_pct):.1f}%")

st.markdown(f"<div class='small-muted'>ATM locked at 10:00 IST • Same strike all day • API target: one option-chain request every 3 minutes • Spike threshold: {spike_threshold:.1f}%</div>",unsafe_allow_html=True)
if st.session_state.history:
    st.markdown("### 📈 Fixed-Straddle Vega History"); h=pd.DataFrame(st.session_state.history).set_index("time"); st.line_chart(h[["CE Vega","PE Vega"]],height=280)

# Simple browser-side timer causes a normal page reload every 180 seconds.
st.markdown("<script>setTimeout(()=>window.location.reload(),180000);</script>", unsafe_allow_html=True)
