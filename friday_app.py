import math
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st
from zoneinfo import ZoneInfo

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import LabelEncoder
    from sklearn.impute import SimpleImputer
except Exception:
    RandomForestClassifier = None
    LabelEncoder = None
    SimpleImputer = None

st.set_page_config(page_title="FRIDAY • AI Option Strategist", page_icon="🤖", layout="wide")
API = "https://api.dhan.co/v2"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
NIFTY_ID = 13
VIX_ID_FALLBACK = 26
STRATEGIES = ["BUY CE", "SELL PE", "BULL CALL SPREAD", "BULL PUT SPREAD", "BUY PE", "SELL CE", "BEAR PUT SPREAD", "BEAR CALL SPREAD", "SHORT STRADDLE", "SHORT STRANGLE", "IRON CONDOR", "NO TRADE"]
REGIMES = ["BULLISH", "BEARISH", "SIDEWAYS", "UNCLEAR"]
RATE_LIMIT_SECONDS = 3.2

def now_ist(): return datetime.now(LOCAL_TZ)

def init_state():
    defaults = {"client_id":"", "access_token":"", "model":None, "label_encoder":None, "imputer":None, "feature_columns":[], "training_summary":{}, "model_status":"NOT TRAINED", "live_features":{}, "last_training_rows":0, "last_api_call":0.0, "uploaded_option_files":[], "uploaded_future_files":[], "uploaded_spot_files":[], "uploaded_vix_files":[], "prepared_training_data":None, "prepared_data_summary":{}, "quarterly_reports":{}, "friday_master_review":None, "friday_vault_cache":{}}
    for k,v in defaults.items(): st.session_state.setdefault(k,v)

def headers():
    if not st.session_state.client_id or not st.session_state.access_token: raise RuntimeError("Enter Dhan Client ID and Access Token.")
    return {"Accept":"application/json","Content-Type":"application/json","access-token":st.session_state.access_token,"client-id":st.session_state.client_id}

def api_post(path,payload,label):
    wait=RATE_LIMIT_SECONDS-(time.monotonic()-st.session_state.last_api_call)
    if wait>0: time.sleep(wait)
    response=requests.post(API+path,headers=headers(),json=payload,timeout=45)
    st.session_state.last_api_call=time.monotonic()
    try: body=response.json()
    except Exception: body={"raw":response.text}
    if response.status_code==429:
        time.sleep(3.5)
        response=requests.post(API+path,headers=headers(),json=payload,timeout=45)
        st.session_state.last_api_call=time.monotonic()
        try: body=response.json()
        except Exception: body={"raw":response.text}
    if not response.ok: raise RuntimeError(f"{label}: HTTP {response.status_code}: {body.get('errorMessage') or body.get('remarks') or body.get('message') or str(body)[:500]}")
    return body

def parse_data(body):
    if isinstance(body,dict) and isinstance(body.get("data"),dict): return body["data"]
    return body if isinstance(body,dict) else {}

@st.cache_data(ttl=21600,show_spinner=False)
def load_master():
    df=pd.read_csv("https://images.dhan.co/api-data/api-scrip-master-detailed.csv",low_memory=False)
    df.columns=[str(c).strip() for c in df.columns]
    rename={"EXCH_ID":"exchange","SEGMENT":"segment","INSTRUMENT":"instrument","SECURITY_ID":"security_id","UNDERLYING_SECURITY_ID":"underlying_security_id","UNDERLYING_SYMBOL":"underlying_symbol","SYMBOL_NAME":"symbol_name","SEM_TRADING_SYMBOL":"trading_symbol","DISPLAY_NAME":"display_name","EXPIRY_DATE":"expiry_date"}
    df=df.rename(columns={c:rename.get(c,c) for c in df.columns})
    if "security_id" not in df.columns:
        for c in ["SM_SECURITY_ID","SEM_SECURITY_ID"]:
            if c in df.columns: df["security_id"]=df[c]; break
    df["security_id"]=pd.to_numeric(df["security_id"],errors="coerce")
    if "expiry_date" in df.columns: df["expiry_date"]=pd.to_datetime(df["expiry_date"],errors="coerce")
    return df.dropna(subset=["security_id"]).copy()

def resolve_vix_id(master):
    for col in ["underlying_symbol","symbol_name","trading_symbol","display_name"]:
        if col in master.columns:
            s=master[col].astype(str).str.upper().str.replace(" ","",regex=False)
            rows=master[s.str.contains("INDIAVIX",regex=False,na=False)|s.eq("VIX")]
            ids=pd.to_numeric(rows["security_id"],errors="coerce").dropna()
            if not ids.empty: return int(ids.iloc[0])
    return VIX_ID_FALLBACK

def _find_column(df,candidates):
    lookup={str(c).strip().lower().replace(" ","_"):c for c in df.columns}
    for c in candidates:
        k=c.lower().replace(" ","_")
        if k in lookup: return lookup[k]
    for norm,orig in lookup.items():
        if any(c.lower().replace(" ","_") in norm for c in candidates): return orig
    return None

def _read_uploaded_csvs(files,label):
    if not files:return pd.DataFrame()
    frames=[]
    for f in files:
        df=pd.read_csv(f,low_memory=False)
        if not df.empty:
            df["_source_file"]=getattr(f,"name",label); frames.append(df)
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()

def _normalize_time_column(df):
    if df.empty:return df
    c=_find_column(df,["timestamp","datetime","date_time","date","time","timestamp_ist","datetime_ist"])
    if c is None:return df
    out=df.copy(); dt=pd.to_datetime(out[c],errors="coerce")
    try:
        if getattr(dt.dt,"tz",None) is not None: dt=dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception: pass
    out["timestamp"]=dt
    return out.dropna(subset=["timestamp"]).sort_values("timestamp")

def _pick_numeric(df,candidates):
    c=_find_column(df,candidates); return pd.to_numeric(df[c],errors="coerce") if c else None

def _normalize_spot(df):
    out=_normalize_time_column(df); p=_pick_numeric(out,["close","ltp","last_price","nifty","spot","index_close","price"])
    if out.empty or p is None:return pd.DataFrame(columns=["timestamp","nifty_spot"])
    r=pd.DataFrame({"timestamp":out["timestamp"].values,"nifty_spot":p.values}); return r.dropna()

def _normalize_future(df):
    out=_normalize_time_column(df); r=pd.DataFrame({"timestamp":out.get("timestamp",pd.Series(dtype="datetime64[ns]") )})
    if out.empty:return pd.DataFrame(columns=["timestamp","future_open","future_high","future_low","future_close"])
    for names,target in [(["open"],"future_open"),(["high"],"future_high"),(["low"],"future_low"),(["close","ltp","last_price"],"future_close")]:
        v=_pick_numeric(out,names); r[target]=v.values if v is not None else np.nan
    return r.dropna(subset=["timestamp"])

def _normalize_vix(df):
    out=_normalize_time_column(df); r=pd.DataFrame({"timestamp":out.get("timestamp",pd.Series(dtype="datetime64[ns]") )})
    if out.empty:return pd.DataFrame(columns=["timestamp","vix_open","vix_high","vix_low","vix_close"])
    for names,target in [(["open"],"vix_open"),(["high"],"vix_high"),(["low"],"vix_low"),(["close","ltp","last_price"],"vix_close")]:
        v=_pick_numeric(out,names); r[target]=v.values if v is not None else np.nan
    return r.dropna(subset=["timestamp"])

def _normalize_options(df):
    out=_normalize_time_column(df); sc=_find_column(out,["strike","strike_price","strikeprice"]); sidec=_find_column(out,["side","option_type","type","cp","ce_pe"]); expc=_find_column(out,["expiry","expiry_date","expirydate","exp_date"]); ivc=_find_column(out,["iv","implied_volatility","impliedvolatility"]); oic=_find_column(out,["oi","open_interest","openinterest"]); volc=_find_column(out,["volume","vol"])
    if out.empty or sc is None or sidec is None: raise ValueError("Options file must contain recognizable timestamp, strike and CE/PE/option type columns.")
    r=pd.DataFrame({"timestamp":out["timestamp"].values,"strike":pd.to_numeric(out[sc],errors="coerce").values,"side":out[sidec].astype(str).str.upper().str.strip().values})
    r["side"]=r["side"].replace({"C":"CE","CALL":"CE","P":"PE","PUT":"PE"}); r["expiry"]=pd.to_datetime(out[expc],errors="coerce").dt.date if expc else pd.NaT; r["iv"]=pd.to_numeric(out[ivc],errors="coerce").values if ivc else np.nan; r["oi"]=pd.to_numeric(out[oic],errors="coerce").values if oic else np.nan; r["volume"]=pd.to_numeric(out[volc],errors="coerce").values if volc else np.nan
    close=_pick_numeric(out,["close","ltp","last_price"]); r["close"]=close.values if close is not None else np.nan
    return r.dropna(subset=["timestamp","strike"])

def _build_option_features(options,spot):
    if options.empty or spot.empty:return pd.DataFrame()
    opt=options.sort_values("timestamp"); sp=spot.sort_values("timestamp")[["timestamp","nifty_spot"]]
    opt=pd.merge_asof(opt,sp,on="timestamp",direction="backward",tolerance=pd.Timedelta("10min")).dropna(subset=["nifty_spot","strike","side"])
    rows=[]
    for ts,g in opt.groupby("timestamp",sort=True):
        if g["expiry"].notna().any():
            exps=[e for e in g["expiry"].dropna().unique() if e>=ts.date()];
            if exps: g=g[g["expiry"]==min(exps)]
        if g.empty:continue
        target=float(g.iloc[0].nifty_spot); strikes=g.strike.dropna().unique(); atm=min(strikes,key=lambda x:abs(float(x)-target)); a=g[np.isclose(g.strike.astype(float),float(atm))]; ce=a[a.side=="CE"]; pe=a[a.side=="PE"]
        if ce.empty or pe.empty:continue
        ce=ce.iloc[-1]; pe=pe.iloc[-1]; call_oi=g.loc[g.side=="CE","oi"].sum(min_count=1); put_oi=g.loc[g.side=="PE","oi"].sum(min_count=1); pcr=put_oi/call_oi if pd.notna(put_oi) and pd.notna(call_oi) and call_oi!=0 else np.nan
        rows.append({"timestamp":ts,"nifty_spot":target,"atm_strike":float(atm),"ce_iv":ce.iv,"pe_iv":pe.iv,"atm_iv":np.nanmean([ce.iv,pe.iv]),"ce_close":ce.close,"pe_close":pe.close,"straddle":ce.close+pe.close if pd.notna(ce.close) and pd.notna(pe.close) else np.nan,"pcr_oi":pcr})
    return pd.DataFrame(rows).sort_values("timestamp") if rows else pd.DataFrame()

def _build_quarterly_reports(feature):
    reports={}
    if feature is None or feature.empty or "timestamp" not in feature.columns:return reports
    x=feature.copy(); x["timestamp"]=pd.to_datetime(x.timestamp,errors="coerce"); x=x.dropna(subset=["timestamp"]).sort_values("timestamp"); x["quarter"]=x.timestamp.map(lambda t:f"{t.year} Q{t.quarter}")
    for q,qdf in x.groupby("quarter",sort=True):
        summary={"quarter":q,"rows":len(qdf),"start":str(qdf.timestamp.min()),"end":str(qdf.timestamp.max()),"avg_atm_iv":float(qdf.atm_iv.mean()) if "atm_iv" in qdf else np.nan,"avg_pcr_oi":float(qdf.pcr_oi.mean()) if "pcr_oi" in qdf else np.nan}
        daily=qdf.assign(date=qdf.timestamp.dt.date).groupby("date",as_index=False).agg(nifty_open=("nifty_spot","first"),nifty_close=("nifty_spot","last"),atm_iv_open=("atm_iv","first"),atm_iv_close=("atm_iv","last"),avg_pcr_oi=("pcr_oi","mean"),avg_straddle=("straddle","mean")); daily["nifty_change_pct"]=daily.nifty_close/daily.nifty_open-1; daily["iv_change"]=daily.atm_iv_close-daily.atm_iv_open
        reports[q]={"summary":pd.DataFrame([summary]),"daily":daily,"features":qdf.copy()}
    return reports

def _quarterly_report_zip(reports,selected=None):
    if not reports:return None
    from io import BytesIO
    b=BytesIO(); rows=[]
    with zipfile.ZipFile(b,"w",compression=zipfile.ZIP_DEFLATED) as z:
        for q in selected or sorted(reports):
            if q not in reports:continue
            p=reports[q]; safe=q.replace(" ","_"); z.writestr(f"{safe}/quarterly_summary.csv",p["summary"].to_csv(index=False)); z.writestr(f"{safe}/daily_analysis.csv",p["daily"].to_csv(index=False)); z.writestr(f"{safe}/decision_features.csv",p["features"].to_csv(index=False)); rows.append(p["summary"])
        if rows:z.writestr("MASTER_quarterly_summary.csv",pd.concat(rows,ignore_index=True).to_csv(index=False))
    b.seek(0); return b

def _read_quarterly_zip_files(files):
    rows=[]
    for f in files:
        with zipfile.ZipFile(f) as z:
            for name in z.namelist():
                if name.endswith("/quarterly_summary.csv") or name=="MASTER_quarterly_summary.csv":
                    with z.open(name) as fh: rows.append(pd.read_csv(fh))
    if not rows:raise ValueError("No quarterly_summary.csv found in uploaded ZIPs.")
    return pd.concat(rows,ignore_index=True).drop_duplicates()

def _resolve_historical_nifty_future(master,year,quarter):
    x=master.copy()
    if "exchange" in x.columns:x=x[x.exchange.astype(str).str.upper().eq("NSE")]
    if "instrument" in x.columns:x=x[x.instrument.astype(str).str.upper().str.contains("FUTIDX",regex=False,na=False)]
    mask=pd.Series(False,index=x.index)
    for c in ["underlying_symbol","symbol_name","trading_symbol","display_name"]:
        if c in x.columns:
            s=x[c].astype(str).str.upper().str.strip(); mask|=s.eq("NIFTY")|s.str.startswith("NIFTY-",na=False)
    x=x[mask].copy()
    if x.empty or "expiry_date" not in x.columns:return None
    x["expiry_date"]=pd.to_datetime(x["expiry_date"],errors="coerce"); x=x.dropna(subset=["expiry_date","security_id"]); x["security_id"]=pd.to_numeric(x.security_id,errors="coerce"); x=x.dropna(subset=["security_id"])
    q=pd.Period(f"{year}-Q{quarter}"); q_end=q.end_time
    after=x[x.expiry_date>=q_end].sort_values("expiry_date")
    row=after.iloc[0] if not after.empty else x[x.expiry_date<=q_end].sort_values("expiry_date").iloc[-1]
    return int(row.security_id),"NSE_FNO","FUTIDX"

def _resolve_basic_instrument(master,dataset):
    if dataset=="NIFTY Spot":return (13,"IDX_I","INDEX")
    if dataset=="India VIX":return (resolve_vix_id(master),"IDX_I","INDEX")
    return None

def _download_quarter_dataset(resolved,dataset,interval,year,quarter,include_oi=False):
    period=pd.Period(f"{year}-Q{quarter}"); start=period.start_time.floor("s"); end=period.end_time.floor("s")
    sid,segment,instrument=resolved
    chunks=[]; cur=start
    while cur<end:
        ce=min(cur+pd.Timedelta(days=89),end)
        if interval=="Daily":
            ep="/charts/historical"; payload={"securityId":str(int(sid)),"exchangeSegment":segment,"instrument":instrument,"expiryCode":0,"oi":bool(include_oi),"fromDate":cur.strftime("%Y-%m-%d"),"toDate":ce.strftime("%Y-%m-%d")}
        else:
            ep="/charts/intraday"; payload={"securityId":str(int(sid)),"exchangeSegment":segment,"instrument":instrument,"interval":str(int(interval)),"oi":bool(include_oi),"fromDate":cur.strftime("%Y-%m-%d %H:%M:%S"),"toDate":ce.strftime("%Y-%m-%d %H:%M:%S")}
        body=api_post(ep,payload,f"{dataset} {year} Q{quarter} {interval}"); d=parse_data(body)
        if isinstance(d,dict) and d.get("timestamp"):
            dt=pd.to_datetime(pd.to_numeric(pd.Series(d["timestamp"]),errors="coerce"),unit="s",utc=True,errors="coerce").dt.tz_convert("Asia/Kolkata"); part=pd.DataFrame({"timestamp":dt})
            for a,b in [("open","open"),("high","high"),("low","low"),("close","close"),("volume","volume"),("oi","oi"),("open_interest","oi")]:
                vals=d.get(a)
                if vals is not None and len(vals)==len(part):part[b]=pd.to_numeric(pd.Series(vals),errors="coerce")
            chunks.append(part)
        cur=ce+pd.Timedelta(seconds=1)
    return pd.concat(chunks,ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True) if chunks else pd.DataFrame()

def render_data_vault():
    st.markdown('<div class="friday-hero"><div class="friday-title">DATA VAULT</div><div class="friday-sub">Quarter-wise NIFTY / Futures / India VIX OHLC downloader</div></div>',unsafe_allow_html=True)
    master=load_master(); a,b,c=st.columns(3)
    with a: dataset=st.selectbox("Dataset",["NIFTY Spot","NIFTY Futures","India VIX"],key="vault_dataset")
    with b: year=st.selectbox("Year",list(range(now_ist().year-5,now_ist().year+1)),key="vault_year")
    with c: quarter=st.selectbox("Quarter",[1,2,3,4],index=int(pd.Timestamp(now_ist()).quarter)-1,key="vault_quarter")
    include_oi=st.checkbox("Include OI",value=(dataset=="NIFTY Futures"),key="vault_oi")
    timeframe=st.selectbox("Timeframe",[15,1,5,25,60,"Daily"],index=0,format_func=lambda x:"Daily" if x=="Daily" else f"{x}-minute",key="vault_timeframe")
    if dataset=="NIFTY Spot":st.caption("FRIDAY default: NIFTY Spot 15-minute OHLC.")
    elif dataset=="NIFTY Futures":st.caption("FRIDAY default: NIFTY Futures 15-minute OHLC, using the historical NIFTY futures contract for the requested quarter.")
    else:st.caption("FRIDAY default: India VIX 15-minute or daily data.")
    st.info("Dhan historical API supports 1/5/15/25/60-minute candles and 90-day intraday request windows; each quarter is automatically chunked.")
    if st.button("⬇️ Download Quarter",use_container_width=True):
        try:
            resolved=_resolve_historical_nifty_future(master,year,quarter) if dataset=="NIFTY Futures" else _resolve_basic_instrument(master,dataset)
            if not resolved: st.error(f"Unable to resolve {dataset} for {year} Q{quarter}."); st.stop()
            qdf=_download_quarter_dataset(resolved,dataset,timeframe,year,quarter,include_oi)
            if qdf.empty:st.error("No data returned for the requested quarter.");return
            qdf.insert(1,"dataset",dataset);qdf.insert(2,"year",year);qdf.insert(3,"quarter",quarter)
            name=f"{dataset.replace(' ','_')}_{year}_Q{quarter}_{timeframe}_OHLC.csv"; data_bytes=qdf.to_csv(index=False).encode(); st.session_state.friday_vault_cache[(dataset,year,quarter,str(timeframe))]=qdf
            st.success(f"{len(qdf):,} rows downloaded.");st.download_button("⬇️ Download Quarter CSV",data=data_bytes,file_name=name,mime="text/csv",use_container_width=True);st.dataframe(qdf.head(300),use_container_width=True,hide_index=True)
        except Exception as exc:st.error(str(exc))
    if st.session_state.friday_vault_cache:
        grouped={}
        for key,qdf in st.session_state.friday_vault_cache.items():grouped.setdefault(key[0],[]).append((key[1],key[2],key[3],qdf))
        from io import BytesIO
        for ds,items in grouped.items():
            b=BytesIO()
            with zipfile.ZipFile(b,"w",compression=zipfile.ZIP_DEFLATED) as z:
                for yr,q,tf,qdf in sorted(items):z.writestr(f"{ds.replace(' ','_')}_{yr}_Q{q}_{tf}_OHLC.csv",qdf.to_csv(index=False))
            b.seek(0); st.download_button(f"⬇️ Download {ds} accumulated quarters",data=b.getvalue(),file_name=f"FRIDAY_{ds.replace(' ','_')}_quarters.zip",mime="application/zip",key=f"vault_{ds}")

def inject_css():
    st.markdown("<style>.stApp{background:linear-gradient(180deg,#05080d,#080d15)} .block-container{max-width:1500px;padding-top:1rem}.friday-hero{background:linear-gradient(135deg,#0f1722,#07101a);border:1px solid rgba(45,180,255,.24);border-radius:18px;padding:24px 28px}.friday-title{font-size:40px;font-weight:900;color:#7edcff;letter-spacing:3px}.friday-sub{color:#8da1b5}.friday-card{background:rgba(10,18,29,.86);border:1px solid rgba(126,220,255,.15);border-radius:16px;padding:18px;min-height:122px}.k-label{font-size:11px;color:#8799ad;text-transform:uppercase;letter-spacing:1px}.k-value{font-size:28px;font-weight:800;color:#eef7ff}.good{color:#3df07b}.warn{color:#ffd166}.bad{color:#ff5d5d}.section{font-size:18px;font-weight:800;color:#e6f2ff;margin:18px 0 8px}</style>",unsafe_allow_html=True)

init_state();inject_css()
with st.sidebar:
    st.markdown('<div style="font-size:42px;font-weight:900;color:#7edcff;letter-spacing:3px;">FRIDAY</div><div style="color:#8da1b5;margin-bottom:20px;">AI OPTION STRATEGIST</div>',unsafe_allow_html=True)
    st.session_state.client_id=st.text_input("Dhan Client ID",value=st.session_state.client_id).strip();st.session_state.access_token=st.text_input("Dhan Access Token",value=st.session_state.access_token,type="password").strip()

friday_view=st.radio("FRIDAY MODULE",["AI Strategist","Data Vault"],horizontal=True,key="friday_view")
if friday_view=="Data Vault":render_data_vault();st.stop()

st.markdown('<div class="friday-hero"><div class="friday-title">FRIDAY</div><div class="friday-sub">AI OPTION STRATEGY SELECTION ENGINE</div></div>',unsafe_allow_html=True)
st.info("AI Strategist and multi-file training features remain available in the FRIDAY source build. Use Data Vault for quarterly market-data collection.")

# Placeholder-safe AI screen: preserves the strategy-selection module without silently producing a model prediction.
if RandomForestClassifier is None:
    st.warning("scikit-learn is not installed. AI training is unavailable until the dependency is installed.")
else:
    st.markdown('<div class="section">AI STRATEGIST</div>',unsafe_allow_html=True)
    st.caption("Upload and prepare your 1-minute options + 15-minute NIFTY/Futures + VIX historical datasets in the full FRIDAY source build before training.")
