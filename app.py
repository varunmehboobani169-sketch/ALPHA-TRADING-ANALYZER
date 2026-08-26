import io
import zipfile
from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st

DEFAULT_CLIENT_ID = "1113195747"
PAIR_TOLERANCE = pd.Timedelta("2min")
SPOT_TOLERANCE = pd.Timedelta("20min")

st.set_page_config(page_title="FRIDAY", layout="wide")


def parse_datetime(values):
    s = pd.Series(values)
    if s.empty:
        return pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0.0
        if med >= 1e18: unit = "ns"
        elif med >= 1e15: unit = "us"
        elif med >= 1e12: unit = "ms"
        elif med >= 1e9: unit = "s"
        else: unit = None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(s, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).astype("datetime64[ns]")


def find_col(df, names):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in lookup: return lookup[key]
    for norm, original in lookup.items():
        if any(name.lower().replace(" ", "_") in norm for name in names): return original
    return None


def numeric_col(df, names):
    c = find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def read_csvs(files):
    frames=[]
    for f in files or []:
        d=pd.read_csv(f, low_memory=False)
        if not d.empty: frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_time(df):
    if df.empty: return df
    c=find_col(df,["timestamp","datetime","date_time","timestamp_ist","datetime_ist","exchange_timestamp","trade_time","time","date"])
    if c is None: raise ValueError(f"Timestamp column not found. Columns: {list(df.columns)}")
    out=df.copy(); out["timestamp"]=parse_datetime(out[c])
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def normalize_expiry(values):
    s=pd.Series(values); dt=pd.to_datetime(s,errors="coerce"); n=pd.to_numeric(s,errors="coerce")
    if n.notna().mean()>0.8:
        med=float(n.dropna().abs().median()) if n.notna().any() else 0
        if med>=1e12: dt=pd.to_datetime(n,unit="ms",errors="coerce")
        elif med>=1e9: dt=pd.to_datetime(n,unit="s",errors="coerce")
    return dt.dt.date


def normalize_options(df):
    x=normalize_time(df); strike=find_col(x,["strike","strike_price","strikeprice","strike_px"]); side=find_col(x,["side","option_type","optiontype","type","cp","ce_pe","call_put"]); expiry=find_col(x,["expiry","expiry_date","expirydate","exp_date","expiry_dt"])
    if strike is None or side is None: raise ValueError(f"Options require strike and CE/PE columns. Found: {list(df.columns)}")
    r=pd.DataFrame({"timestamp":x.timestamp,"strike":pd.to_numeric(x[strike],errors="coerce"),"side":x[side].astype(str).str.upper().str.strip()})
    r["side"]=r.side.replace({"C":"CE","CALL":"CE","P":"PE","PUT":"PE"})
    r["expiry"]=normalize_expiry(x[expiry]) if expiry else pd.Series(pd.NaT,index=x.index)
    for names,target in [(["close","ltp","last_price","price"],"close"),(["iv","implied_volatility","impliedvolatility","implied_vol"],"iv"),(["oi","open_interest","openinterest"],"oi"),(["volume","vol","traded_volume"],"volume")]:
        v=numeric_col(x,names); r[target]=v if v is not None else np.nan
    r=r.dropna(subset=["timestamp","strike"]); r=r[r.side.isin(["CE","PE"])]
    return r.sort_values("timestamp").reset_index(drop=True)


def normalize_spot(df):
    x=normalize_time(df); p=numeric_col(x,["close","ltp","last_price","nifty","spot","index_close","price"])
    if p is None: raise ValueError("NIFTY Spot has no recognizable close/price column.")
    return pd.DataFrame({"timestamp":x.timestamp,"nifty_spot":p}).dropna().drop_duplicates("timestamp",keep="last").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    x=normalize_time(df); p=numeric_col(x,["close","ltp","last_price","vix_close","vix","price"])
    if p is None: raise ValueError("India VIX has no recognizable close/price column.")
    return pd.DataFrame({"timestamp":x.timestamp,"vix_close":p}).dropna().drop_duplicates("timestamp",keep="last").sort_values("timestamp").reset_index(drop=True)


def synchronize_options(options,spot,vix):
    o=options.sort_values("timestamp").copy(); s=spot.sort_values("timestamp").copy()
    o["timestamp"]=o["timestamp"].astype("datetime64[ns]"); s["timestamp"]=s["timestamp"].astype("datetime64[ns]")
    m=pd.merge_asof(o,s,on="timestamp",direction="backward",tolerance=SPOT_TOLERANCE).dropna(subset=["nifty_spot"])
    if not vix.empty and not m.empty:
        vx=vix.sort_values("timestamp").copy(); vx["timestamp"]=vx["timestamp"].astype("datetime64[ns]")
        m=pd.merge_asof(m.sort_values("timestamp"),vx,on="timestamp",direction="backward",tolerance=SPOT_TOLERANCE)
    return m.sort_values("timestamp").reset_index(drop=True)


def pair_ce_pe(m):
    if m.empty: return pd.DataFrame()
    x=m.copy(); x["timestamp"]=pd.to_datetime(x["timestamp"],errors="coerce").astype("datetime64[ns]"); x["expiry_dt"]=pd.to_datetime(x["expiry"],errors="coerce").dt.normalize(); day=x["timestamp"].dt.normalize()
    x=x[x["expiry_dt"].isna() | (x["expiry_dt"]>=day)].copy(); x["strike"]=pd.to_numeric(x["strike"],errors="coerce"); x=x.dropna(subset=["timestamp","strike"])
    if x.empty: return pd.DataFrame()
    x["expiry_key"]=x["expiry_dt"].dt.strftime("%Y-%m-%d").fillna("NO_EXPIRY")
    ce=x[x.side=="CE"].copy(); pe=x[x.side=="PE"].copy()
    if ce.empty or pe.empty: return pd.DataFrame()
    pe=pe.rename(columns={"timestamp":"pe_timestamp"})
    # merge_asof requires the time columns themselves to be globally sorted.
    ce=ce.sort_values(["timestamp","expiry_key","strike"],kind="mergesort").reset_index(drop=True)
    pe=pe.sort_values(["pe_timestamp","expiry_key","strike"],kind="mergesort").reset_index(drop=True)
    paired=pd.merge_asof(ce,pe,left_on="timestamp",right_on="pe_timestamp",by=["expiry_key","strike"],direction="nearest",tolerance=PAIR_TOLERANCE,suffixes=("_ce","_pe"))
    paired=paired.dropna(subset=["pe_timestamp","close_pe"]).copy()
    if paired.empty: return pd.DataFrame()
    paired["pair_timestamp"]=paired["timestamp"]
    return paired.sort_values("pair_timestamp").reset_index(drop=True)


def build_features(m):
    p=pair_ce_pe(m)
    if p.empty: return pd.DataFrame()
    p["spot_dist"]=(p["strike"]-p["nifty_spot"]).abs(); p=p.sort_values(["pair_timestamp","spot_dist"],kind="mergesort"); atm=p.drop_duplicates("pair_timestamp",keep="first")
    if atm.empty: return pd.DataFrame()
    def arr(name): return pd.to_numeric(atm[name],errors="coerce").to_numpy() if name in atm.columns else np.full(len(atm),np.nan)
    f=pd.DataFrame({"timestamp":atm.pair_timestamp.to_numpy(),"nifty_spot":arr("nifty_spot"),"atm_strike":arr("strike"),"ce_close":arr("close_ce"),"pe_close":arr("close_pe"),"ce_iv":arr("iv_ce"),"pe_iv":arr("iv_pe"),"ce_oi":arr("oi_ce"),"pe_oi":arr("oi_pe"),"ce_volume":arr("volume_ce"),"pe_volume":arr("volume_pe"),"vix_close":arr("vix_close_ce")})
    f["pcr_oi"]=np.divide(f.ce_oi,f.ce_oi,out=np.full(len(f),np.nan),where=np.isfinite(f.ce_oi)&(f.ce_oi!=0))
    f["pcr_oi"]=np.divide(f.pe_oi,f.ce_oi,out=np.full(len(f),np.nan),where=np.isfinite(f.ce_oi)&(f.ce_oi!=0))
    f["straddle"]=f.ce_close+f.pe_close; f["atm_iv"]=pd.concat([f.ce_iv,f.pe_iv],axis=1).mean(axis=1); f=f.sort_values("timestamp").reset_index(drop=True)
    f["spot_ret_1"]=f.nifty_spot.pct_change(); f["spot_ret_4"]=f.nifty_spot.pct_change(4); f["spot_ret_16"]=f.nifty_spot.pct_change(16); f["spot_vol_8"]=f.spot_ret_1.rolling(8).std(); f["spot_ma_8"]=f.nifty_spot.rolling(8).mean(); f["spot_ma_32"]=f.nifty_spot.rolling(32).mean(); f["spot_trend"]=f.spot_ma_8-f.spot_ma_32; f["straddle_change"]=f.straddle.diff(); f["straddle_ret"]=f.straddle.pct_change(); f["iv_change"]=f.atm_iv.diff(); f["vix_change"]=f.vix_close.diff(); f["vix_ret"]=f.vix_close.pct_change(); f["pcr_change"]=f.pcr_oi.diff(); f["forward_spot_4"]=f.nifty_spot.shift(-4)/f.nifty_spot-1; f["forward_spot_16"]=f.nifty_spot.shift(-16)/f.nifty_spot-1; f["forward_straddle_4"]=f.straddle.shift(-4)/f.straddle-1; f["forward_straddle_16"]=f.straddle.shift(-16)/f.straddle-1
    return f


def discover_patterns(f):
    rules=[("IV rising + spot flat",(f.iv_change>0)&(f.spot_ret_4.abs()<.001)),("IV falling + spot flat",(f.iv_change<0)&(f.spot_ret_4.abs()<.001)),("PCR rising",f.pcr_change>0),("PCR falling",f.pcr_change<0),("VIX rising",f.vix_change>0),("VIX falling",f.vix_change<0),("Straddle expanding",f.straddle_change>0),("Straddle contracting",f.straddle_change<0),("Spot uptrend",f.spot_trend>0),("Spot downtrend",f.spot_trend<0)]
    rows=[]
    for name,mask in rules:
        d=f.loc[mask].dropna(subset=["forward_spot_4","forward_straddle_4"])
        if len(d)>=10: rows.append({"pattern":name,"observations":len(d),"avg_next_4_spot_pct":d.forward_spot_4.mean(),"avg_next_16_spot_pct":d.forward_spot_16.mean(),"avg_next_4_straddle_pct":d.forward_straddle_4.mean(),"avg_next_16_straddle_pct":d.forward_straddle_16.mean(),"next_4_spot_up_rate":(d.forward_spot_4>0).mean(),"next_4_straddle_up_rate":(d.forward_straddle_4>0).mean()})
    return pd.DataFrame(rows).sort_values("observations",ascending=False) if rows else pd.DataFrame()


def diagnostics(o,s,v,m,f,paired):
    return pd.DataFrame({"Check":["Option rows","Spot rows","VIX rows","Option+Spot synced","CE rows","PE rows","CE/PE pairs","ATM observations","Option start","Option end","Spot start","Spot end"],"Value":[len(o),len(s),len(v),len(m),int((o.side=="CE").sum()) if not o.empty else 0,int((o.side=="PE").sum()) if not o.empty else 0,len(paired),len(f),str(o.timestamp.min()) if not o.empty else "N/A",str(o.timestamp.max()) if not o.empty else "N/A",str(s.timestamp.min()) if not s.empty else "N/A",str(s.timestamp.max()) if not s.empty else "N/A"]})


st.title("FRIDAY — OPTION PATTERN RESEARCH")
st.caption("Options + NIFTY Spot + India VIX only. Futures are excluded.")
with st.sidebar:
    st.subheader("FRIDAY")
    client_id=st.text_input("Dhan Client ID",value=DEFAULT_CLIENT_ID).strip()
    access_token=st.text_input("Dhan Access Token",value="",type="password",help="Enter your current Dhan access token. It stays in the Streamlit session and is not written to GitHub.").strip()
    st.caption(f"Client ID: {client_id or DEFAULT_CLIENT_ID}")
opt_files=st.file_uploader("Q1 Option Data (CSV)",type=["csv"],accept_multiple_files=True)
spot_files=st.file_uploader("Q1 NIFTY Spot Data (CSV)",type=["csv"],accept_multiple_files=True)
vix_files=st.file_uploader("Q1 India VIX Data (CSV)",type=["csv"],accept_multiple_files=True)
if not opt_files or not spot_files:
    st.info("Upload the Q1 Option and NIFTY Spot CSV files. India VIX is optional.")
else:
    if st.button("ANALYZE PATTERNS",use_container_width=True):
        bar=st.progress(0,text="FRIDAY: 0%"); status=st.empty()
        try:
            status.info("10% — Reading CSV files"); bar.progress(10,text="FRIDAY: 10% — Reading files"); o_raw=read_csvs(opt_files); s_raw=read_csvs(spot_files); v_raw=read_csvs(vix_files) if vix_files else pd.DataFrame()
            status.info("30% — Normalizing data"); bar.progress(30,text="FRIDAY: 30% — Normalizing Options / Spot / VIX"); o=normalize_options(o_raw); s=normalize_spot(s_raw); v=normalize_vix(v_raw) if not v_raw.empty else pd.DataFrame()
            status.info("50% — Synchronizing timestamps"); bar.progress(50,text="FRIDAY: 50% — Synchronizing"); m=synchronize_options(o,s,v)
            if m.empty: raise ValueError("No Option/Spot timestamps overlap within 20 minutes.")
            status.info("65% — Pairing CE and PE"); bar.progress(65,text="FRIDAY: 65% — Pairing CE/PE within ±2 minutes"); paired=pair_ce_pe(m)
            if paired.empty: raise ValueError("No CE/PE pairs found within ±2 minutes for the same strike and expiry. Check option timestamps, strike, expiry and CE/PE labels.")
            status.info("75% — Selecting ATM"); bar.progress(75,text="FRIDAY: 75% — Selecting ATM"); f=build_features(m)
            if f.empty: raise ValueError("CE/PE pairing worked, but no ATM observations could be built.")
            status.info("90% — Measuring historical patterns"); bar.progress(90,text="FRIDAY: 90% — Measuring patterns"); patterns=discover_patterns(f)
            bar.progress(100,text="FRIDAY: 100% ✅"); status.success(f"Analysis complete — {len(f):,} ATM observations")
            st.subheader("Input Diagnostics"); st.dataframe(diagnostics(o,s,v,m,f,paired),use_container_width=True,hide_index=True)
            st.subheader("Pattern Summary"); st.dataframe(patterns,use_container_width=True,hide_index=True) if not patterns.empty else st.warning("No pattern produced at least 10 usable forward observations.")
            st.subheader("Feature Data"); st.dataframe(f.tail(500),use_container_width=True,hide_index=True)
            out=io.BytesIO()
            with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv",f.to_csv(index=False)); z.writestr("FRIDAY_patterns.csv",patterns.to_csv(index=False)); z.writestr("FRIDAY_diagnostics.csv",diagnostics(o,s,v,m,f,paired).to_csv(index=False))
            st.download_button("DOWNLOAD ANALYSIS ZIP",out.getvalue(),"FRIDAY_Q1_ANALYSIS.zip","application/zip",use_container_width=True)
        except Exception as exc:
            status.error(f"Processing stopped: {exc}"); st.exception(exc)
