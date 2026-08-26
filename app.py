import io, time, zipfile, json, urllib.error, urllib.request
import numpy as np
import pandas as pd
import streamlit as st

try:
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.ensemble import ExtraTreesRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, mean_absolute_error, r2_score
    from sklearn.feature_selection import mutual_info_regression
    SKLEARN_OK = True
except Exception:
    SKLEARN_OK = False

st.set_page_config(page_title="FRIDAY — Autonomous Market Research Engine", layout="wide")
DHAN_API = "https://api.dhan.co/v2"
QUARTERS = {
    "Q1 2024": ("2024-01-01", "2024-03-31 23:59:59"), "Q2 2024": ("2024-04-01", "2024-06-30 23:59:59"),
    "Q3 2024": ("2024-07-01", "2024-09-30 23:59:59"), "Q4 2024": ("2024-10-01", "2024-12-31 23:59:59"),
    "Q1 2025": ("2025-01-01", "2025-03-31 23:59:59"), "Q2 2025": ("2025-04-01", "2025-06-30 23:59:59"),
    "Q3 2025": ("2025-07-01", "2025-09-30 23:59:59"), "Q4 2025": ("2025-10-01", "2025-12-31 23:59:59"),
    "Q1 2026": ("2026-01-01", "2026-03-31 23:59:59"), "Q2 2026": ("2026-04-01", "2026-06-30 23:59:59"),
    "Q3 2026": ("2026-07-01", "2026-09-30 23:59:59"), "Q4 2026": ("2026-10-01", "2026-12-31 23:59:59"),
}
HORIZONS = [1, 3, 5, 10, 15, 30, 60, 120]

# ---------- Dhan connection ----------
def dhan_request(path, client_id, token, method="GET", body=None):
    headers={"Accept":"application/json","access-token":token,"client-id":client_id}
    data=None
    if body is not None:
        headers["Content-Type"]="application/json"; data=json.dumps(body).encode()
    req=urllib.request.Request(DHAN_API+path,method=method,headers=headers,data=data)
    try:
        with urllib.request.urlopen(req,timeout=25) as r:
            raw=r.read().decode("utf-8",errors="replace"); return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail=e.read().decode("utf-8",errors="replace")[:800]
        raise RuntimeError(f"Dhan HTTP {e.code}: {detail}") from e

def dhan_login_box():
    for k,v in {"dhan_client_id":"","dhan_token":"","dhan_connected":False}.items(): st.session_state.setdefault(k,v)
    st.sidebar.markdown("### 🔐 DHAN CONNECTION")
    st.sidebar.caption("Session-only credentials; token is not stored in GitHub.")
    with st.sidebar.form("main_dhan_login",clear_on_submit=False):
        cid=st.text_input("Dhan Client ID / Username",value=st.session_state.dhan_client_id)
        tok=st.text_input("Dhan Access Token",value=st.session_state.dhan_token,type="password")
        connect=st.form_submit_button("🔗 CONNECT TO DHAN",use_container_width=True,type="primary")
    if connect:
        cid,tok=cid.strip(),tok.strip()
        if not cid or not tok: st.sidebar.error("Enter both Client ID and Access Token.")
        else:
            try:
                profile=dhan_request("/profile",cid,tok)
                st.session_state.dhan_client_id=cid; st.session_state.dhan_token=tok; st.session_state.dhan_connected=True
                validity=(profile.get("data") or {}).get("tokenValidity") if isinstance(profile,dict) else None
                st.sidebar.success("✅ DHAN CONNECTED")
                if validity: st.sidebar.caption(f"Token validity: {validity}")
            except Exception as e:
                st.session_state.dhan_connected=False; st.sidebar.error("❌ Dhan connection failed"); st.sidebar.caption(str(e))
    if st.session_state.dhan_connected:
        st.sidebar.success(f"Connected: {st.session_state.dhan_client_id}")
        if st.sidebar.button("LOGOUT / CLEAR DHAN",use_container_width=True):
            st.session_state.dhan_client_id=""; st.session_state.dhan_token=""; st.session_state.dhan_connected=False; st.rerun()
    else: st.sidebar.warning("Not connected to Dhan")

# ---------- input helpers ----------
def find_col(df,names):
    lookup={str(c).strip().lower().replace(" ","_"):c for c in df.columns}; names=[n.lower().replace(" ","_") for n in names]
    for n in names:
        if n in lookup: return lookup[n]
    for k,v in lookup.items():
        if any(n in k for n in names): return v
    return None

def parse_dt(s):
    x=pd.Series(s); n=pd.to_numeric(x,errors="coerce")
    if n.notna().mean()>.8 and n.notna().any():
        med=float(n.dropna().abs().median())
        unit="ns" if med>=1e18 else "us" if med>=1e15 else "ms" if med>=1e12 else "s" if med>=1e9 else None
        dt=pd.to_datetime(n,unit=unit,errors="coerce",utc=True) if unit else pd.to_datetime(x,errors="coerce",utc=True)
    else: dt=pd.to_datetime(x,errors="coerce",utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None) if getattr(dt.dt,"tz",None) is not None else dt

def read_csvs(files):
    dfs=[pd.read_csv(f,low_memory=False) for f in (files or [])]
    return pd.concat([d for d in dfs if not d.empty],ignore_index=True) if dfs else pd.DataFrame()

def normalize_options(raw):
    ts=find_col(raw,["timestamp","datetime_ist","datetime","exchange_timestamp","time","date"]); side=find_col(raw,["option_type","side","optiontype","type","cp","ce_pe","call_put"]); strike=find_col(raw,["strike_price","strike","strikeprice","strike_px"]); offset=find_col(raw,["strike_offset","offset"]); ef=find_col(raw,["expiry_flag","expiryflag"]); ec=find_col(raw,["expiry_code","expirycode"]); spot=find_col(raw,["spot","underlying_spot","nifty_spot"])
    if ts is None or side is None or strike is None: raise ValueError("Options need timestamp, option type and strike columns.")
    out=pd.DataFrame({"timestamp":parse_dt(raw[ts]),"side":raw[side].astype(str).str.upper().str.strip().replace({"C":"CALL","P":"PUT","CALL":"CALL","PUT":"PUT"}),"strike":pd.to_numeric(raw[strike],errors="coerce"),"strike_offset":pd.to_numeric(raw[offset],errors="coerce") if offset else np.nan,"expiry_flag":raw[ef].astype(str).str.upper() if ef else "","expiry_code":pd.to_numeric(raw[ec],errors="coerce") if ec else np.nan,"option_spot":pd.to_numeric(raw[spot],errors="coerce") if spot else np.nan})
    for names,target in [(["open"],"open"),(["high"],"high"),(["low"],"low"),(["close","ltp","last_price","price"],"close"),(["volume","vol","traded_volume"],"volume"),(["oi","open_interest","openinterest"],"oi"),(["iv","implied_volatility","impliedvolatility","implied_vol"],"iv")]:
        c=find_col(raw,names); out[target]=pd.to_numeric(raw[c],errors="coerce") if c else np.nan
    return out.dropna(subset=["timestamp","strike"]).loc[lambda d:d.side.isin(["CALL","PUT"])].sort_values("timestamp").reset_index(drop=True)

def normalize_market(raw,kind):
    ts=find_col(raw,["timestamp","datetime_ist","datetime","exchange_timestamp","time","date"]); px=find_col(raw,["close","ltp","last_price","spot","nifty","vix","index_close","price"])
    if ts is None or px is None: raise ValueError(f"{kind}: timestamp/price column not found.")
    return pd.DataFrame({"timestamp":parse_dt(raw[ts]),"value":pd.to_numeric(raw[px],errors="coerce")}).dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)

def qslice(df,q):
    a,b=map(pd.Timestamp,QUARTERS[q]); return df[(df.timestamp>=a)&(df.timestamp<=b)].copy()

# ---------- core research ----------
def option_matrix(o):
    o=o.sort_values("timestamp").drop_duplicates(["timestamp","strike_offset","side"],keep="last"); pieces=[]
    for metric in ["close","iv","oi","volume","open","high","low","strike","option_spot"]:
        p=o.pivot(index="timestamp",columns=["side","strike_offset"],values=metric); p.columns=[f"{metric}_{s}_{int(off)}" for s,off in p.columns]; pieces.append(p)
    w=pd.concat(pieces,axis=1).sort_index(); meta=o.groupby("timestamp").agg(expiry_flag=("expiry_flag","first"),expiry_code=("expiry_code","first"),option_spot=("option_spot","median")); return w.join(meta).reset_index()

def features(w):
    x=w.copy(); c=lambda n: x[n] if n in x else pd.Series(np.nan,index=x.index)
    for off in range(-10,11):
        ce,pe=c(f"close_CALL_{off}"),c(f"close_PUT_{off}"); civ,piv=c(f"iv_CALL_{off}"),c(f"iv_PUT_{off}"); coi,poi=c(f"oi_CALL_{off}"),c(f"oi_PUT_{off}"); cv,pv=c(f"volume_CALL_{off}"),c(f"volume_PUT_{off}")
        x[f"straddle_{off}"]=ce+pe; x[f"iv_mid_{off}"]=pd.concat([civ,piv],axis=1).mean(1); x[f"iv_skew_{off}"]=civ-piv; x[f"pcr_oi_{off}"]=poi/coi.replace(0,np.nan); x[f"oi_imb_{off}"]=(poi-coi)/(poi+coi).replace(0,np.nan); x[f"vol_imb_{off}"]=(pv-cv)/(pv+cv).replace(0,np.nan)
    x["atm_straddle"]=x["straddle_0"]; x["atm_iv"]=x["iv_mid_0"]; x["atm_pcr_oi"]=x["pcr_oi_0"]; x["atm_oi_imb"]=x["oi_imb_0"]; x["atm_vol_imb"]=x["vol_imb_0"]; x["atm_iv_skew"]=x["iv_skew_0"]
    x["straddle_wing_avg"]=x[[f"straddle_{i}" for i in(-10,-8,-6,-4,-2,2,4,6,8,10)]].mean(1); x["straddle_curvature"]=x["straddle_wing_avg"]-x["atm_straddle"]; x["straddle_range"]=x[[f"straddle_{i}" for i in range(-10,11)]].max(1)-x[[f"straddle_{i}" for i in range(-10,11)]].min(1)
    for col in ["atm_straddle","atm_iv","atm_pcr_oi","atm_oi_imb","atm_vol_imb","atm_iv_skew","straddle_curvature"]:
        for n in (1,3,5,15): x[f"{col}_chg_{n}m"]=x[col].pct_change(n,fill_method=None)
    return x

def align_states(f,spot,vix):
    s=spot.rename(columns={"value":"spot"}).sort_values("timestamp"); v=vix.rename(columns={"value":"vix"}).sort_values("timestamp"); x=f.sort_values("timestamp")
    x=pd.merge_asof(x,s,on="timestamp",direction="backward",tolerance=pd.Timedelta(minutes=20)); x=pd.merge_asof(x,v,on="timestamp",direction="backward",tolerance=pd.Timedelta(minutes=20))
    ss=s.copy(); ss["spot_ret_15"]=ss.spot.pct_change(1); ss["spot_ret_30"]=ss.spot.pct_change(2); ss["spot_ret_60"]=ss.spot.pct_change(4); ss["spot_ret_120"]=ss.spot.pct_change(8); ss["spot_vol_60"]=ss.spot.pct_change().rolling(4).std()
    vv=v.copy(); vv["vix_chg_15"]=vv.vix.diff(); vv["vix_chg_30"]=vv.vix.diff(2); vv["vix_chg_60"]=vv.vix.diff(4); vv["vix_ret_15"]=vv.vix.pct_change()
    x=pd.merge_asof(x,ss[["timestamp","spot_ret_15","spot_ret_30","spot_ret_60","spot_ret_120","spot_vol_60"]],on="timestamp",direction="backward",tolerance=pd.Timedelta(minutes=20)); x=pd.merge_asof(x,vv[["timestamp","vix_chg_15","vix_chg_30","vix_chg_60","vix_ret_15"]],on="timestamp",direction="backward",tolerance=pd.Timedelta(minutes=20)); return x.reset_index(drop=True)

def forward(base_ts,series_ts,series_val,h,tol):
    left=pd.DataFrame({"base":pd.to_datetime(base_ts)}); left["target"]=left.base+pd.Timedelta(minutes=h); right=pd.DataFrame({"future":pd.to_datetime(series_ts),"value":pd.to_numeric(series_val,errors="coerce")}).sort_values("future")
    j=pd.merge_asof(left.sort_values("target"),right,left_on="target",right_on="future",direction="forward",tolerance=pd.Timedelta(minutes=tol)); return np.asarray(j.value,dtype=float)

def targets(x,spot):
    x=x.sort_values("timestamp").reset_index(drop=True); ss=spot.rename(columns={"value":"spot"}).sort_values("timestamp"); ats=x[["timestamp","atm_straddle"]].dropna()
    for h in HORIZONS:
        fs=forward(x.timestamp,ats.timestamp,ats.atm_straddle,h,2); fp=forward(x.timestamp,ss.timestamp,ss.spot,h,16); cs=np.asarray(pd.to_numeric(x.atm_straddle,errors="coerce"),dtype=float); cp=np.asarray(pd.to_numeric(x.spot,errors="coerce"),dtype=float)
        x[f"y_straddle_{h}m"]=np.where(np.isfinite(cs)&(cs!=0),fs/cs-1,np.nan); x[f"y_spot_{h}m"]=np.where(np.isfinite(cp)&(cp!=0),fp/cp-1,np.nan); x[f"y_dir_{h}m"]=np.where(np.isfinite(x[f"y_spot_{h}m"]),(x[f"y_spot_{h}m"]>0).astype(int),np.nan)
    return x.replace([np.inf,-np.inf],np.nan)

def stats(s):
    a=np.asarray(pd.to_numeric(s,errors="coerce"),dtype=float); a=a[np.isfinite(a)]
    if not len(a): return {"n":0,"mean":np.nan,"median":np.nan,"trimmed_mean":np.nan,"up_rate":np.nan,"p05":np.nan,"p95":np.nan}
    lo,hi=np.quantile(a,[.05,.95]); return {"n":len(a),"mean":float(a.mean()),"median":float(np.median(a)),"trimmed_mean":float(np.clip(a,lo,hi).mean()),"up_rate":float((a>0).mean()),"p05":float(np.quantile(a,.05)),"p95":float(np.quantile(a,.95))}

def discover(train,target="y_spot_15m"):
    if not SKLEARN_OK: raise RuntimeError("scikit-learn is required.")
    cols=[c for c in train.columns if not c.startswith("y_") and c!=target and pd.api.types.is_numeric_dtype(train[c]) and not pd.api.types.is_bool_dtype(train[c])]; sample=train[cols+[target]].replace([np.inf,-np.inf],np.nan)
    if len(sample)>30000: sample=sample.sample(30000,random_state=42)
    y=pd.to_numeric(sample[target],errors="coerce"); base=stats(y); rows=[]
    if base["n"]<200: return pd.DataFrame()
    for c in cols:
        s=pd.to_numeric(sample[c],errors="coerce"); ok=s.notna()&y.notna()
        if ok.sum()<200 or s[ok].nunique()<5: continue
        sv=np.asarray(s[ok],dtype=float); yv=np.asarray(y[ok],dtype=float); q=np.quantile(sv,[.1,.2,.8,.9]); best=None
        for qv,lab,upper in [(q[0],"q10",1),(q[1],"q20",1),(q[2],"q80",0),(q[3],"q90",0)]:
            stt=stats(yv[sv<=qv if upper else sv>=qv]); eff=abs(stt["trimmed_mean"]-base["trimmed_mean"])
            if stt["n"]>=100 and (best is None or eff>best[0]): best=(eff,lab,stt)
        if best: rows.append({"feature":c,"best_region":best[1],"n":best[2]["n"],"effect":best[0],"trimmed_mean":best[2]["trimmed_mean"],"up_rate":best[2]["up_rate"]})
    r=pd.DataFrame(rows)
    if r.empty:return r
    top=r.nlargest(min(100,len(r)),"effect")["feature"].tolist(); mi_df=sample[top].replace([np.inf,-np.inf],np.nan).fillna(sample[top].median(numeric_only=True)); good=y.notna()
    try: mi=mutual_info_regression(np.asarray(mi_df.loc[good],dtype=float),np.asarray(y.loc[good],dtype=float),random_state=42) if good.sum()>=300 else np.zeros(len(top))
    except Exception: mi=np.zeros(len(top))
    r["mutual_information"]=r.feature.map(dict(zip(top,mi))).fillna(0.0); r["discovery_score"]=r.mutual_information*(1+100*r.effect)*np.sqrt(r.n); return r.sort_values(["discovery_score","effect","n"],ascending=False).reset_index(drop=True)

def model_run(train,val,test,features_list,target="y_spot_15m"):
    if not features_list:return pd.DataFrame()
    ytr=pd.to_numeric(train[target],errors="coerce"); yv=pd.to_numeric(val[target],errors="coerce"); yt=pd.to_numeric(test[target],errors="coerce"); masks=[ytr.notna(),yv.notna(),yt.notna()]; Xs=[d.loc[m,features_list].replace([np.inf,-np.inf],np.nan) for d,m in zip((train,val,test),masks)]
    Xtr,Xv,Xt=Xs; ytr,yv,yt=ytr.loc[masks[0]],yv.loc[masks[1]],yt.loc[masks[2]]; rows=[]
    if ytr.nunique()>1:
        clf=Pipeline([("imp",SimpleImputer(strategy="median")),("scale",StandardScaler()),("model",LogisticRegression(max_iter=600,class_weight="balanced"))]); clf.fit(Xtr,(ytr>0).astype(int)); pv=clf.predict(Xv); pt=clf.predict(Xt); rows.append({"model":"LogisticRegression","target":target,"val_accuracy":accuracy_score(yv>0,pv),"val_balanced_accuracy":balanced_accuracy_score(yv>0,pv),"test_accuracy":accuracy_score(yt>0,pt),"test_balanced_accuracy":balanced_accuracy_score(yt>0,pt)})
    for name,pipe in [("ExtraTreesRegressor",Pipeline([("imp",SimpleImputer(strategy="median")),("model",ExtraTreesRegressor(n_estimators=160,max_depth=8,min_samples_leaf=30,n_jobs=-1,random_state=42))])), ("Ridge",Pipeline([("imp",SimpleImputer(strategy="median")),("scale",StandardScaler()),("model",Ridge(alpha=10))]))]:
        pipe.fit(Xtr,ytr); pv=pipe.predict(Xv); pt=pipe.predict(Xt); rows.append({"model":name,"target":target,"val_mae":mean_absolute_error(yv,pv),"val_r2":r2_score(yv,pv),"test_mae":mean_absolute_error(yt,pt),"test_r2":r2_score(yt,pt)})
    return pd.DataFrame(rows)

# ---------- Legacy-compatible research benchmark ----------
def legacy_benchmark(f,spot):
    x=f.copy(); sf=x["atm_straddle"]; sp=x["spot"]; iv=x["atm_iv"]; pcr=x["atm_pcr_oi"]; vix=x["vix"]
    x["nifty_spot"]=sp; x["atm_strike"]=x.get("strike_CALL_0"); x["ce_close"]=x.get("close_CALL_0"); x["pe_close"]=x.get("close_PUT_0"); x["ce_iv"]=x.get("iv_CALL_0"); x["pe_iv"]=x.get("iv_PUT_0"); x["ce_oi"]=x.get("oi_CALL_0"); x["pe_oi"]=x.get("oi_PUT_0"); x["ce_volume"]=x.get("volume_CALL_0"); x["pe_volume"]=x.get("volume_PUT_0"); x["vix_close"]=vix
    x["pair_gap_seconds"]=0.0; x["pcr_oi"]=pcr; x["straddle"]=sf; x["atm_iv"]=iv; x["spot_ret_1"]=sp.pct_change(1); x["spot_ret_4"]=sp.pct_change(4); x["spot_ret_16"]=sp.pct_change(16); x["spot_vol_8"]=sp.pct_change().rolling(8).std(); x["spot_ma_8"]=sp.rolling(8).mean(); x["spot_ma_32"]=sp.rolling(32).mean(); x["spot_trend"]=np.where(sp>x["spot_ma_32"],1,np.where(sp<x["spot_ma_32"],-1,0)); x["straddle_change"]=sf.diff(); x["straddle_ret"]=sf.pct_change(); x["iv_change"]=iv.diff(); x["vix_change"]=vix.diff(); x["vix_ret"]=vix.pct_change(); x["pcr_change"]=pcr.diff()
    x["forward_spot_4"]=forward(x.timestamp,spot.timestamp,spot.value,4,16); x["forward_spot_16"]=forward(x.timestamp,spot.timestamp,spot.value,16,16); x["forward_straddle_4"]=forward(x.timestamp,x.timestamp,sf,4,2); x["forward_straddle_16"]=forward(x.timestamp,x.timestamp,sf,16,2)
    cols=["timestamp","nifty_spot","atm_strike","ce_close","pe_close","ce_iv","pe_iv","ce_oi","pe_oi","ce_volume","pe_volume","vix_close","pair_gap_seconds","pcr_oi","straddle","atm_iv","spot_ret_1","spot_ret_4","spot_ret_16","spot_vol_8","spot_ma_8","spot_ma_32","spot_trend","straddle_change","straddle_ret","iv_change","vix_change","vix_ret","pcr_change","forward_spot_4","forward_spot_16","forward_straddle_4","forward_straddle_16"]
    return x[cols]

def legacy_patterns(f):
    conds={"Straddle contracting":f["straddle_ret"]<0,"Spot uptrend":f["spot_ret_4"]>0,"IV falling + spot flat":(f["iv_change"]<0)&(f["spot_ret_4"].abs()<0.0005),"Spot downtrend":f["spot_ret_4"]<0,"Straddle expanding":f["straddle_ret"]>0,"IV rising + spot flat":(f["iv_change"]>0)&(f["spot_ret_4"].abs()<0.0005),"PCR rising":f["pcr_change"]>0,"PCR falling":f["pcr_change"]<0,"VIX falling":f["vix_change"]<0,"VIX rising":f["vix_change"]>0}
    rows=[]
    for name,mask in conds.items():
        d=f.loc[mask].copy()
        if d.empty: continue
        s4=d["forward_spot_4"]/d["nifty_spot"]-1; s16=d["forward_spot_16"]/d["nifty_spot"]-1; st4=d["forward_straddle_4"]/d["straddle"]-1; st16=d["forward_straddle_16"]/d["straddle"]-1
        rows.append({"pattern":name,"observations":len(d),"avg_next_4_spot_pct":float(s4.mean()),"avg_next_16_spot_pct":float(s16.mean()),"avg_next_4_straddle_pct":float(st4.mean()),"avg_next_16_straddle_pct":float(st16.mean()),"next_4_spot_up_rate":float((s4>0).mean()),"next_4_straddle_up_rate":float((st4>0).mean())})
    return pd.DataFrame(rows)

def build_pairs(o):
    ce=o[o.side=="CALL"].copy(); pe=o[o.side=="PUT"].copy(); keys=["timestamp","strike_offset","strike"]; cols=["timestamp","strike_offset","strike","close","iv","oi","volume","open","high","low","option_spot"]
    ce=ce[cols].rename(columns={c:f"ce_{c}" for c in cols if c not in keys}); pe=pe[cols].rename(columns={c:f"pe_{c}" for c in cols if c not in keys}); p=ce.merge(pe,on=keys,how="inner"); p["pair_timestamp"]=p["timestamp"]; p["pair_gap_seconds"]=0.0
    p["nifty_spot"]=pd.to_numeric((p["ce_option_spot"]+p["pe_option_spot"])/2,errors="coerce")
    return p

def run_quarter(q,o,s,v):
    o,s,v=qslice(o,q),qslice(s,q),qslice(v,q)
    if o.empty or s.empty or v.empty: raise ValueError(f"{q}: Options, Spot and VIX are all required.")
    pairs=build_pairs(o); mat=option_matrix(o); aligned=align_states(features(mat),s,v); f=targets(aligned,s); legacy=legacy_benchmark(aligned,s); patterns=legacy_patterns(legacy)
    train,val,test=np.array_split(f.sort_values("timestamp"),[int(len(f)*.6),int(len(f)*.8)]); disc=discover(train); top=disc.head(40).feature.tolist() if not disc.empty else []; ml=model_run(train,val,test,top) if top and SKLEARN_OK else pd.DataFrame()
    audit=pd.DataFrame({"Check":["Option rows","Spot rows","VIX rows","Option+Spot synced","CE rows","PE rows","CE/PE pairs","ATM observations","Option start","Option end","Spot start","Spot end","VIX start","VIX end"],"Value":[len(o),len(s),len(v),int(o.timestamp.isin(s.timestamp).sum()),int((o.side=="CALL").sum()),int((o.side=="PUT").sum()),len(pairs),len(legacy),str(o.timestamp.min()),str(o.timestamp.max()),str(s.timestamp.min()),str(s.timestamp.max()),str(v.timestamp.min()),str(v.timestamp.max())]})
    return {"audit":audit,"features":f,"legacy_features":legacy,"discovery":disc,"ml":ml,"patterns":patterns,"pairs":pairs}

def md(d): return d.to_markdown(index=False) if not d.empty else "No rows."

def make_report(q,r):
    return f"""# FRIDAY — AUTONOMOUS RESEARCH REPORT — {q}

## Research Clock
Options 1-minute; NIFTY Spot 15-minute; India VIX 15-minute. Spot/VIX are backward-aligned only.

## Raw Data Audit
{md(r['audit'])}

## Legacy-Compatible Pattern Research
{md(r['patterns'])}

## Fruitfulness Discovery
{md(r['discovery'].head(100))}

## Machine Learning
{md(r['ml'])}

## Promotion Rule
No discovery is treated as a strategy until it survives chronological validation, final testing and adversarial review.
"""

def make_zip(results):
    b=io.BytesIO()
    with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
        for q,r in results.items():
            safe=q.replace(" ","_"); z.writestr(f"FRIDAY_{safe}_REPORT.md",make_report(q,r))
            for k in ["audit","features","legacy_features","patterns","discovery","ml","pairs"]: z.writestr(f"FRIDAY_{safe}_{k}.csv",r[k].to_csv(index=False))
    return b.getvalue()

# ---------- UI ----------
dhan_login_box()
st.title("FRIDAY — AUTONOMOUS MARKET RESEARCH ENGINE")
st.caption("Legacy-compatible benchmark + autonomous discovery + ML")
selected=st.multiselect("Research quarters",list(QUARTERS),default=["Q1 2025"])
opt=st.file_uploader("1. NIFTY Options — 1-minute CSV",type=["csv"],accept_multiple_files=True)
spot=st.file_uploader("2. NIFTY Spot — 15-minute CSV",type=["csv"],accept_multiple_files=True)
vix=st.file_uploader("3. India VIX — 15-minute CSV",type=["csv"],accept_multiple_files=True)
run=st.button("RUN FRIDAY — FULL RESEARCH",use_container_width=True,type="primary")
if run:
    if not selected or not opt or not spot or not vix: st.error("Select quarter(s) and upload Options + Spot + VIX."); st.stop()
    if not SKLEARN_OK: st.error("scikit-learn is required for the ML module."); st.stop()
    bar=st.progress(0,text="FRIDAY: 0% — loading"); start=time.time(); o=normalize_options(read_csvs(opt)); s=normalize_market(read_csvs(spot),"NIFTY Spot"); v=normalize_market(read_csvs(vix),"India VIX"); results={}; errors=[]
    for i,q in enumerate(selected,1):
        try: results[q]=run_quarter(q,o,s,v)
        except Exception as e: errors.append(f"{q}: {e}")
        pct=int(100*i/len(selected)); bar.progress(pct,text=f"FRIDAY: {pct}% — {q}")
    if not results: st.error("No quarter completed: "+" | ".join(errors)); st.stop()
    for q,r in results.items():
        st.success(f"{q} completed — {len(r['features']):,} research observations, {len(r['pairs']):,} CE/PE pairs")
        tabs=st.tabs(["AUDIT","LEGACY PATTERNS","DISCOVERY","ML","DOWNLOAD"])
        with tabs[0]: st.dataframe(r["audit"],use_container_width=True,hide_index=True)
        with tabs[1]: st.dataframe(r["patterns"],use_container_width=True,hide_index=True)
        with tabs[2]: st.dataframe(r["discovery"].head(50),use_container_width=True,hide_index=True)
        with tabs[3]: st.dataframe(r["ml"],use_container_width=True,hide_index=True)
        with tabs[4]:
            st.download_button(f"DOWNLOAD {q} REPORT",make_report(q,r).encode(),f"FRIDAY_{q.replace(' ','_')}_REPORT.md","text/markdown",key=f"report_{q}",use_container_width=True)
            st.download_button(f"DOWNLOAD {q} FULL PACKAGE",make_zip({q:r}),f"FRIDAY_{q.replace(' ','_')}_FULL_RESEARCH.zip","application/zip",key=f"zip_{q}",use_container_width=True)
    if errors: st.warning("Some quarters failed: "+" | ".join(errors))
    st.success(f"FRIDAY finished in {time.time()-start:.1f}s")
