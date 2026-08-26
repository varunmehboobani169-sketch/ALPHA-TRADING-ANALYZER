import io
import time
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st

API = "https://api.dhan.co/v2"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_CLIENT_ID = "1113195747"
NIFTY_ID = 13
VIX_ID_FALLBACK = 26
RATE_LIMIT_SECONDS = 3.2


def now_ist():
    return datetime.now(LOCAL_TZ)


def init_state():
    st.session_state.setdefault("client_id", DEFAULT_CLIENT_ID)
    st.session_state.setdefault("access_token", "")
    st.session_state.setdefault("last_api_call", 0.0)
    st.session_state.setdefault("analysis", None)


def headers():
    if not st.session_state.client_id or not st.session_state.access_token:
        raise RuntimeError("Enter the Dhan Access Token.")
    return {"Accept":"application/json","Content-Type":"application/json","access-token":st.session_state.access_token,"client-id":st.session_state.client_id}


def api_post(path,payload,label):
    wait=RATE_LIMIT_SECONDS-(time.monotonic()-st.session_state.last_api_call)
    if wait>0: time.sleep(wait)
    r=requests.post(API+path,headers=headers(),json=payload,timeout=45)
    st.session_state.last_api_call=time.monotonic()
    try: body=r.json()
    except Exception: body={"raw":r.text}
    if r.status_code==429:
        time.sleep(3.5)
        r=requests.post(API+path,headers=headers(),json=payload,timeout=45)
        st.session_state.last_api_call=time.monotonic()
        try: body=r.json()
        except Exception: body={"raw":r.text}
    if not r.ok:
        raise RuntimeError(f"{label}: HTTP {r.status_code}: {body.get('errorMessage') or body.get('remarks') or body.get('message') or str(body)[:500]}")
    return body


def parse_data(body):
    return body.get("data",body) if isinstance(body,dict) else {}


def parse_datetime(values):
    s=pd.Series(values)
    if s.empty: return pd.Series(pd.NaT,index=s.index,dtype="datetime64[ns]")
    n=pd.to_numeric(s,errors="coerce")
    if n.notna().mean()>0.8:
        med=float(n.dropna().abs().median()) if n.notna().any() else 0
        unit="ns" if med>=1e18 else "us" if med>=1e15 else "ms" if med>=1e12 else "s" if med>=1e9 else None
        dt=pd.to_datetime(n,unit=unit,errors="coerce",utc=True) if unit else pd.to_datetime(s,errors="coerce",utc=True)
    else:
        dt=pd.to_datetime(s,errors="coerce",utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).astype("datetime64[ns]")


def norm_time(df):
    if df.empty: return df
    cols={str(c).strip().lower().replace(" ","_"):c for c in df.columns}
    cand=["timestamp","datetime","date_time","timestamp_ist","datetime_ist","exchange_timestamp","trade_time","time","date"]
    c=next((cols[k] for k in cand if k in cols),None)
    if c is None: raise ValueError(f"No timestamp column found. Columns: {list(df.columns)}")
    out=df.copy(); out["timestamp"]=parse_datetime(out[c])
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def find_col(df,cands):
    cols={str(c).strip().lower().replace(" ","_"):c for c in df.columns}
    for c in cands:
        k=c.lower().replace(" ","_")
        if k in cols: return cols[k]
    for norm,orig in cols.items():
        if any(c.lower().replace(" ","_") in norm for c in cands): return orig
    return None


def num(df,cands):
    c=find_col(df,cands)
    return pd.to_numeric(df[c],errors="coerce") if c else None


def read_csvs(files):
    frames=[]
    for f in files or []:
        d=pd.read_csv(f,low_memory=False)
        if not d.empty: frames.append(d)
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()


def normalize_spot(df):
    x=norm_time(df); p=num(x,["close","ltp","last_price","nifty","spot","index_close","price"])
    if p is None: raise ValueError("NIFTY Spot file has no recognizable close/price column.")
    return pd.DataFrame({"timestamp":x.timestamp,"nifty_spot":p}).dropna().sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    x=norm_time(df); p=num(x,["close","ltp","last_price","vix_close","vix","price"])
    if p is None: raise ValueError("India VIX file has no recognizable close/price column.")
    return pd.DataFrame({"timestamp":x.timestamp,"vix_close":p}).dropna().sort_values("timestamp").reset_index(drop=True)


def normalize_expiry(values):
    s=pd.Series(values); n=pd.to_numeric(s,errors="coerce")
    if n.notna().mean()>0.8:
        med=float(n.dropna().abs().median()) if n.notna().any() else 0
        dt=pd.to_datetime(n,unit="ms",errors="coerce") if med>=1e12 else pd.to_datetime(n,unit="s",errors="coerce") if med>=1e9 else pd.to_datetime(s,errors="coerce")
    else: dt=pd.to_datetime(s,errors="coerce")
    return dt.dt.date


def normalize_options(df):
    x=norm_time(df)
    strike=find_col(x,["strike","strike_price","strikeprice","strike_px"])
    side=find_col(x,["side","option_type","optiontype","type","cp","ce_pe","call_put"])
    expiry=find_col(x,["expiry","expiry_date","expirydate","exp_date","expiry_dt"])
    if strike is None or side is None: raise ValueError(f"Options need timestamp, strike and CE/PE type. Columns: {list(df.columns)}")
    r=pd.DataFrame({"timestamp":x.timestamp,"strike":pd.to_numeric(x[strike],errors="coerce"),"side":x[side].astype(str).str.upper().str.strip()})
    r["side"]=r.side.replace({"C":"CE","CALL":"CE","P":"PE","PUT":"PE"})
    r["expiry"]=normalize_expiry(x[expiry]) if expiry else pd.NaT
    for names,target in [(["close","ltp","last_price","price"],"close"),(["iv","implied_volatility","impliedvolatility","implied_vol"],"iv"),(["oi","open_interest","openinterest"],"oi"),(["volume","vol","traded_volume"],"volume")]:
        v=num(x,names); r[target]=v if v is not None else np.nan
    r=r.dropna(subset=["timestamp","strike"]); r=r[r.side.isin(["CE","PE"])]
    return r.sort_values("timestamp").reset_index(drop=True)


def synchronize(options,spot,vix=None,tol_min=20):
    opt=options.sort_values("timestamp"); sp=spot[["timestamp","nifty_spot"]].sort_values("timestamp")
    merged=pd.merge_asof(opt,sp,on="timestamp",direction="backward",tolerance=pd.Timedelta(minutes=tol_min)).dropna(subset=["nifty_spot"])
    if vix is not None and not vix.empty and not merged.empty:
        vx=vix[["timestamp","vix_close"]].sort_values("timestamp")
        merged=pd.merge_asof(merged.sort_values("timestamp"),vx,on="timestamp",direction="backward",tolerance=pd.Timedelta(minutes=tol_min))
    return merged.sort_values("timestamp").reset_index(drop=True)


def build_features_fast(merged,progress=None):
    if merged.empty: return pd.DataFrame()
    x=merged.copy()
    x["expiry_valid"]=x["expiry"].notna()
    if x["expiry_valid"].any():
        valid=x.loc[x.expiry_valid].copy()
        valid=valid[valid.apply(lambda r: pd.isna(r.expiry) or r.expiry>=r.timestamp.date(),axis=1)]
        if not valid.empty: x=valid
    x["strike_num"]=pd.to_numeric(x["strike"],errors="coerce")
    spot=pd.to_numeric(x["nifty_spot"],errors="coerce")
    x["atm_dist"]=(x["strike_num"]-spot).abs()
    # Keep only strikes that have both CE and PE at the same timestamp/expiry.
    pair=x.pivot_table(index=["timestamp","expiry","strike_num"],columns="side",values="close",aggfunc="last").reset_index()
    pair=pair.dropna(subset=["CE","PE"],how="any")
    if pair.empty: return pd.DataFrame()
    pair["nifty_spot"]=pair.merge(x.groupby(["timestamp","expiry"],dropna=False)["nifty_spot"].first().reset_index(),on=["timestamp","expiry"],how="left")["nifty_spot"]
    pair["atm_dist"]=(pair["strike_num"]-pair["nifty_spot"]).abs()
    atm=pair.sort_values(["timestamp","expiry","atm_dist"]).drop_duplicates("timestamp",keep="first")[["timestamp","expiry","strike_num","nifty_spot"]]
    base=merged.merge(atm,on=["timestamp","expiry","strike_num"],how="inner") if "strike_num" in merged.columns else None
    # Build ATM rows directly from selected key and source data.
    keys=atm.rename(columns={"strike_num":"strike"})
    m=merged.copy(); m["strike_num"]=pd.to_numeric(m["strike"],errors="coerce")
    sel=m.merge(atm[["timestamp","expiry","strike_num"]],on=["timestamp","expiry","strike_num"],how="inner")
    if sel.empty: return pd.DataFrame()
    ce=sel[sel.side=="CE"].sort_values("timestamp").drop_duplicates("timestamp",keep="last").set_index("timestamp")
    pe=sel[sel.side=="PE"].sort_values("timestamp").drop_duplicates("timestamp",keep="last").set_index("timestamp")
    idx=ce.index.intersection(pe.index)
    f=pd.DataFrame(index=idx)
    f["nifty_spot"]=ce.loc[idx,"nifty_spot"]
    f["atm_strike"]=ce.loc[idx,"strike_num"]
    f["ce_close"]=ce.loc[idx,"close"]; f["pe_close"]=pe.loc[idx,"close"]
    f["ce_iv"]=ce.loc[idx,"iv"]; f["pe_iv"]=pe.loc[idx,"iv"]
    f["ce_oi"]=ce.loc[idx,"oi"]; f["pe_oi"]=pe.loc[idx,"oi"]
    f["ce_volume"]=ce.loc[idx,"volume"]; f["pe_volume"]=pe.loc[idx,"volume"]
    f["pcr_oi"]=sel.pivot_table(index="timestamp",columns="side",values="oi",aggfunc="sum").reindex(idx).get("PE",pd.Series(index=idx))/sel.pivot_table(index="timestamp",columns="side",values="oi",aggfunc="sum").reindex(idx).get("CE",pd.Series(index=idx))
    f["straddle"]=f.ce_close+f.pe_close; f["atm_iv"]=pd.concat([f.ce_iv,f.pe_iv],axis=1).mean(axis=1)
    f["vix_close"]=ce["vix_close"].reindex(idx) if "vix_close" in ce else np.nan
    f.index.name="timestamp"; f=f.reset_index().sort_values("timestamp").reset_index(drop=True)
    f["spot_ret_1"]=f.nifty_spot.pct_change(); f["spot_ret_4"]=f.nifty_spot.pct_change(4); f["spot_ret_16"]=f.nifty_spot.pct_change(16)
    f["spot_vol_8"]=f.spot_ret_1.rolling(8).std(); f["spot_ma_8"]=f.nifty_spot.rolling(8).mean(); f["spot_ma_32"]=f.nifty_spot.rolling(32).mean(); f["spot_trend"]=f.spot_ma_8-f.spot_ma_32
    f["straddle_change"]=f.straddle.diff(); f["straddle_ret"]=f.straddle.pct_change(); f["iv_change"]=f.atm_iv.diff(); f["vix_change"]=f.vix_close.diff(); f["vix_ret"]=f.vix_close.pct_change(); f["pcr_change"]=f.pcr_oi.diff()
    f["forward_spot_4"]=f.nifty_spot.shift(-4)/f.nifty_spot-1; f["forward_spot_16"]=f.nifty_spot.shift(-16)/f.nifty_spot-1
    f["forward_straddle_4"]=f.straddle.shift(-4)/f.straddle-1; f["forward_straddle_16"]=f.straddle.shift(-16)/f.straddle-1
    return f


def discover_patterns(f):
    rules=[("IV rising + spot flat",(f.iv_change>0)&(f.spot_ret_4.abs()<0.001)),("IV falling + spot flat",(f.iv_change<0)&(f.spot_ret_4.abs()<0.001)),("PCR rising",f.pcr_change>0),("PCR falling",f.pcr_change<0),("VIX rising",f.vix_change>0),("VIX falling",f.vix_change<0),("Straddle expanding",f.straddle_change>0),("Straddle contracting",f.straddle_change<0),("Spot uptrend",f.spot_trend>0),("Spot downtrend",f.spot_trend<0)]
    rows=[]
    for name,mask in rules:
        d=f.loc[mask].dropna(subset=["forward_spot_4","forward_straddle_4"])
        if len(d)>=10: rows.append({"pattern":name,"observations":len(d),"avg_next_4_spot_pct":d.forward_spot_4.mean(),"avg_next_16_spot_pct":d.forward_spot_16.mean(),"avg_next_4_straddle_pct":d.forward_straddle_4.mean(),"avg_next_16_straddle_pct":d.forward_straddle_16.mean(),"next_4_spot_up_rate":(d.forward_spot_4>0).mean(),"next_4_straddle_up_rate":(d.forward_straddle_4>0).mean()})
    return pd.DataFrame(rows).sort_values("observations",ascending=False) if rows else pd.DataFrame()


def resolve_vix_id():
    try:
        m=pd.read_csv("https://images.dhan.co/api-data/api-scrip-master-detailed.csv",low_memory=False); cols={str(c).strip().upper():c for c in m.columns}; sid=cols.get("SECURITY_ID")
        for c in [cols.get("SYMBOL_NAME"),cols.get("DISPLAY_NAME"),cols.get("UNDERLYING_SYMBOL")]:
            if sid and c:
                mask=m[c].astype(str).str.upper().str.replace(" ","",regex=False).str.contains("INDIAVIX",na=False)
                if mask.any():
                    v=pd.to_numeric(m.loc[mask,sid].iloc[0],errors="coerce")
                    if pd.notna(v): return int(v)
    except Exception: pass
    return VIX_ID_FALLBACK


def download_index_quarter(dataset,year,quarter,timeframe):
    sid=NIFTY_ID if dataset=="NIFTY Spot" else resolve_vix_id(); q=pd.Period(f"{year}-Q{quarter}"); start,end=pd.Timestamp(q.start_time),pd.Timestamp(q.end_time)
    if timeframe=="Daily":
        return parse_data(api_post("/charts/historical",{"securityId":str(sid),"exchangeSegment":"IDX_I","instrument":"INDEX","expiryCode":0,"oi":False,"fromDate":start.strftime("%Y-%m-%d"),"toDate":end.strftime("%Y-%m-%d")},f"{dataset} {year} Q{quarter}"))
    interval={"1-minute":1,"5-minute":5,"15-minute":15,"25-minute":25,"60-minute":60}[timeframe]; parts=[]; cur=start
    while cur<=end:
        ce=min(cur+pd.Timedelta(days=89),end); d=parse_data(api_post("/charts/intraday",{"securityId":str(sid),"exchangeSegment":"IDX_I","instrument":"INDEX","interval":interval,"oi":False,"fromDate":cur.strftime("%Y-%m-%d %H:%M:%S"),"toDate":ce.strftime("%Y-%m-%d %H:%M:%S")},f"{dataset} {year} Q{quarter}"))
        if d.get("timestamp"): parts.append(pd.DataFrame({"timestamp":parse_datetime(d["timestamp"]),"open":d.get("open"),"high":d.get("high"),"low":d.get("low"),"close":d.get("close"),"volume":d.get("volume")}))
        cur=ce+pd.Timedelta(seconds=1)
    return pd.concat(parts,ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True) if parts else pd.DataFrame()


def render_vault():
    st.markdown("## DATA VAULT"); st.caption("NIFTY Spot and India VIX only. Futures are out of scope.")
    years=list(range(2020,now_ist().year+1)); c1,c2,c3=st.columns(3)
    with c1: dataset=st.selectbox("Dataset",["NIFTY Spot","India VIX"])
    with c2: year=st.selectbox("Year",years,index=years.index(2024) if 2024 in years else len(years)-1)
    with c3: quarter=st.selectbox("Quarter",[1,2,3,4],index=0)
    timeframe=st.selectbox("Timeframe",["15-minute","1-minute","5-minute","25-minute","60-minute","Daily"],index=0)
    if st.button("DOWNLOAD QUARTER",use_container_width=True):
        try:
            df=download_index_quarter(dataset,year,quarter,timeframe); df=pd.DataFrame(df)
            if df.empty: st.error("No data returned.")
            else:
                st.success(f"Downloaded {len(df):,} rows."); st.download_button("DOWNLOAD CSV",df.to_csv(index=False).encode(),f"FRIDAY_{dataset.replace(' ','_')}_{year}_Q{quarter}_{timeframe}.csv","text/csv",use_container_width=True); st.dataframe(df.head(300),use_container_width=True,hide_index=True)
        except Exception as exc: st.error(str(exc))


def render_ai():
    st.markdown("## FRIDAY — FAST OPTION PATTERN RESEARCH")
    st.caption("Optimized for Q1 Options + NIFTY Spot + India VIX. Futures are not used.")
    opt=st.file_uploader("Option Data (CSV)",type=["csv"],accept_multiple_files=True); spot=st.file_uploader("NIFTY Spot Data (CSV)",type=["csv"],accept_multiple_files=True); vix=st.file_uploader("India VIX Data (CSV)",type=["csv"],accept_multiple_files=True)
    if not(opt and spot): st.info("Upload Q1 Option + Spot data. VIX is optional but recommended."); return
    if st.button("ANALYZE PATTERNS",use_container_width=True):
        bar=st.progress(0,text="Starting..."); stage=st.empty()
        try:
            stage.write("1/6 Reading files"); bar.progress(10); options=normalize_options(read_csvs(opt))
            stage.write("2/6 Reading NIFTY Spot"); bar.progress(25); spot_df=normalize_spot(read_csvs(spot))
            stage.write("3/6 Reading India VIX"); bar.progress(35); vix_df=normalize_vix(read_csvs(vix)) if vix else pd.DataFrame()
            stage.write("4/6 Synchronizing timestamps"); bar.progress(50); merged=synchronize(options,spot_df,vix_df)
            if merged.empty:
                st.error("No option/spot timestamps overlap within 20 minutes."); st.dataframe(pd.DataFrame({"input":["options","spot","vix"],"rows":[len(options),len(spot_df),len(vix_df)]}),hide_index=True); return
            stage.write("5/6 Building ATM features (fast vectorized mode)"); bar.progress(65); features=build_features_fast(merged)
            if features.empty:
                st.error("Spot synchronization worked, but no timestamp had both CE and PE at an ATM strike."); return
            stage.write("6/6 Finding patterns"); bar.progress(90); patterns=discover_patterns(features); bar.progress(100,text="✅ Analysis complete")
            st.session_state.analysis=features
            st.success(f"Analyzed {len(features):,} ATM observations.")
            if not patterns.empty: st.subheader("Pattern Summary"); st.dataframe(patterns,use_container_width=True,hide_index=True)
            else: st.warning("No pattern had at least 10 usable observations.")
            st.subheader("Feature Data"); st.dataframe(features.tail(500),use_container_width=True,hide_index=True)
            b=io.BytesIO();
            with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv",features.to_csv(index=False)); z.writestr("FRIDAY_pattern_summary.csv",patterns.to_csv(index=False))
            b.seek(0); st.download_button("DOWNLOAD ANALYSIS ZIP",b.getvalue(),"FRIDAY_Q1_PATTERN_ANALYSIS.zip","application/zip",use_container_width=True)
        except Exception as exc:
            bar.empty(); st.exception(exc)


init_state()
with st.sidebar:
    st.title("FRIDAY"); st.caption("Fast Option Pattern Research Engine")
    st.session_state.client_id=st.text_input("Dhan Client ID",value=st.session_state.client_id or DEFAULT_CLIENT_ID).strip()
    st.session_state.access_token=st.text_input("Dhan Access Token",value=st.session_state.access_token,type="password").strip()
view=st.radio("MODULE",["AI Strategist","Data Vault"],horizontal=True)
if view=="Data Vault": render_vault()
else: render_ai()
