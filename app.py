import io
import time
import zipfile
import urllib.error
import urllib.request
import json

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

st.set_page_config(page_title="FRIDAY — Autonomous Research Engine", layout="wide")

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
    headers = {"Accept": "application/json", "access-token": token, "client-id": client_id}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(DHAN_API + path, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Dhan HTTP {e.code}: {detail}") from e


def dhan_login_box():
    if "dhan_client_id" not in st.session_state: st.session_state.dhan_client_id = ""
    if "dhan_token" not in st.session_state: st.session_state.dhan_token = ""
    if "dhan_connected" not in st.session_state: st.session_state.dhan_connected = False
    st.sidebar.markdown("### 🔐 DHAN CONNECTION")
    st.sidebar.caption("Enter credentials here to establish the Dhan connection for this session.")
    with st.sidebar.form("main_dhan_login", clear_on_submit=False):
        cid = st.text_input("Dhan Client ID / Username", value=st.session_state.dhan_client_id, placeholder="e.g. 1113195747")
        tok = st.text_input("Dhan Access Token", value=st.session_state.dhan_token, type="password", placeholder="Paste Dhan access token")
        connect = st.form_submit_button("🔗 CONNECT TO DHAN", use_container_width=True, type="primary")
    if connect:
        cid, tok = cid.strip(), tok.strip()
        if not cid or not tok:
            st.session_state.dhan_connected = False
            st.sidebar.error("Enter both Client ID and Access Token.")
        else:
            with st.spinner("Verifying Dhan connection…"):
                try:
                    profile = dhan_request("/profile", cid, tok)
                    st.session_state.dhan_client_id = cid
                    st.session_state.dhan_token = tok
                    st.session_state.dhan_connected = True
                    validity = None
                    if isinstance(profile, dict):
                        validity = (profile.get("data") or {}).get("tokenValidity") or profile.get("tokenValidity")
                    st.sidebar.success("✅ DHAN CONNECTED")
                    if validity: st.sidebar.caption(f"Token validity: {validity}")
                except Exception as e:
                    st.session_state.dhan_connected = False
                    st.sidebar.error("❌ Dhan connection failed")
                    st.sidebar.caption(str(e))
    if st.session_state.dhan_connected:
        st.sidebar.success(f"Connected: {st.session_state.dhan_client_id}")
        if st.sidebar.button("LOGOUT / CLEAR DHAN", use_container_width=True):
            st.session_state.dhan_client_id = ""
            st.session_state.dhan_token = ""
            st.session_state.dhan_connected = False
            st.rerun()
    else:
        st.sidebar.warning("Not connected to Dhan")

# ---------- input helpers ----------
def find_col(df, names):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    names = [n.lower().replace(" ", "_") for n in names]
    for n in names:
        if n in lookup: return lookup[n]
    for k, v in lookup.items():
        if any(n in k for n in names): return v
    return None


def parse_dt(s):
    x = pd.Series(s); n = pd.to_numeric(x, errors="coerce")
    if n.notna().mean() > .8 and n.notna().any():
        med = float(n.dropna().abs().median())
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(x, errors="coerce", utc=True)
    else: dt = pd.to_datetime(x, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)


def read_csvs(files):
    fs = [pd.read_csv(f, low_memory=False) for f in (files or [])]
    return pd.concat([x for x in fs if not x.empty], ignore_index=True) if fs else pd.DataFrame()


def normalize_options(raw):
    ts=find_col(raw,["timestamp","datetime_ist","datetime","exchange_timestamp","time","date"])
    side=find_col(raw,["option_type","side","optiontype","type","cp","ce_pe","call_put"])
    strike=find_col(raw,["strike_price","strike","strikeprice","strike_px"])
    offset=find_col(raw,["strike_offset","offset"]); ef=find_col(raw,["expiry_flag","expiryflag"]); ec=find_col(raw,["expiry_code","expirycode"]); spot=find_col(raw,["spot","underlying_spot","nifty_spot"])
    if ts is None or side is None or strike is None: raise ValueError("Options need timestamp, option type and strike columns.")
    out=pd.DataFrame({"timestamp":parse_dt(raw[ts]),"side":raw[side].astype(str).str.upper().str.strip().replace({"C":"CALL","P":"PUT","CALL":"CALL","PUT":"PUT"}),"strike":pd.to_numeric(raw[strike],errors="coerce")})
    out["strike_offset"]=pd.to_numeric(raw[offset],errors="coerce") if offset else np.nan
    out["expiry_flag"]=raw[ef].astype(str).str.upper() if ef else ""
    out["expiry_code"]=pd.to_numeric(raw[ec],errors="coerce") if ec else np.nan
    out["option_spot"]=pd.to_numeric(raw[spot],errors="coerce") if spot else np.nan
    for names,target in [(["open"],"open"),(["high"],"high"),(["low"],"low"),(["close","ltp","last_price","price"],"close"),(["volume","vol","traded_volume"],"volume"),(["oi","open_interest","openinterest"],"oi"),(["iv","implied_volatility","impliedvolatility","implied_vol"],"iv")]:
        c=find_col(raw,names); out[target]=pd.to_numeric(raw[c],errors="coerce") if c else np.nan
    return out.dropna(subset=["timestamp","strike"]).loc[lambda d:d.side.isin(["CALL","PUT"])].sort_values("timestamp").reset_index(drop=True)


def normalize_market(raw, kind):
    ts=find_col(raw,["timestamp","datetime_ist","datetime","exchange_timestamp","time","date"]); px=find_col(raw,["close","ltp","last_price","spot","nifty","vix","index_close","price"])
    if ts is None or px is None: raise ValueError(f"{kind}: timestamp/price column not found.")
    return pd.DataFrame({"timestamp":parse_dt(raw[ts]),"value":pd.to_numeric(raw[px],errors="coerce")}).dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def qslice(df,q):
    a,b=map(pd.Timestamp,QUARTERS[q]); return df[(df.timestamp>=a)&(df.timestamp<=b)].copy()

# ---------- research engine ----------
def option_matrix(o):
    o=o.sort_values("timestamp").drop_duplicates(["timestamp","strike_offset","side"],keep="last")
    pieces=[]
    for metric in ["close","iv","oi","volume","open","high","low","strike","option_spot"]:
        p=o.pivot(index="timestamp",columns=["side","strike_offset"],values=metric); p.columns=[f"{metric}_{s}_{int(off)}" for s,off in p.columns]; pieces.append(p)
    w=pd.concat(pieces,axis=1).sort_index(); meta=o.groupby("timestamp").agg(expiry_flag=("expiry_flag","first"),expiry_code=("expiry_code","first"),option_spot=("option_spot","median")); return w.join(meta).reset_index()


def features(w):
    x=w.copy()
    def c(n): return x[n] if n in x else pd.Series(np.nan,index=x.index)
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


def forward(x, series_ts, series_val, h, tol):
    left=pd.DataFrame({"base":pd.to_datetime(x)}); left["target"]=left.base+pd.Timedelta(minutes=h); right=pd.DataFrame({"future":pd.to_datetime(series_ts),"value":pd.to_numeric(series_val,errors="coerce")}).sort_values("future")
    j=pd.merge_asof(left.sort_values("target"),right,left_on="target",right_on="future",direction="forward",tolerance=pd.Timedelta(minutes=tol)); return j.value.to_numpy()


def targets(x,spot):
    x=x.sort_values("timestamp").reset_index(drop=True); ss=spot.rename(columns={"value":"spot"}).sort_values("timestamp"); ats=x[["timestamp","atm_straddle"]].dropna()
    for h in HORIZONS:
        fs=forward(x.timestamp,ats.timestamp,ats.atm_straddle,h,2); fp=forward(x.timestamp,ss.timestamp,ss.spot,h,16); cs=pd.to_numeric(x.atm_straddle,errors="coerce").to_numpy(); cp=pd.to_numeric(x.spot,errors="coerce").to_numpy()
        x[f"y_straddle_{h}m"]=np.where(np.isfinite(cs)&(cs!=0),fs/cs-1,np.nan); x[f"y_spot_{h}m"]=np.where(np.isfinite(cp)&(cp!=0),fp/cp-1,np.nan); x[f"y_dir_{h}m"]=np.where(np.isfinite(x[f"y_spot_{h}m"]),(x[f"y_spot_{h}m"]>0).astype(int),np.nan)
    return x.replace([np.inf,-np.inf],np.nan)


def stats(s):
    a=pd.to_numeric(s,errors="coerce").to_numpy(float); a=a[np.isfinite(a)]
    if not len(a): return {"n":0,"mean":np.nan,"median":np.nan,"trimmed_mean":np.nan,"up_rate":np.nan,"p05":np.nan,"p95":np.nan}
    lo,hi=np.quantile(a,[.05,.95]); return {"n":len(a),"mean":float(a.mean()),"median":float(np.median(a)),"trimmed_mean":float(np.clip(a,lo,hi).mean()),"up_rate":float((a>0).mean()),"p05":float(np.quantile(a,.05)),"p95":float(np.quantile(a,.95))}


def discover(train,target="y_spot_15m"):
    if not SKLEARN_OK: raise RuntimeError("scikit-learn is required.")
    cols=[c for c in train.columns if not c.startswith("y_") and c!=target and pd.api.types.is_numeric_dtype(train[c]) and not pd.api.types.is_bool_dtype(train[c])]; sample=train[cols+[target]].replace([np.inf,-np.inf],np.nan)
    if len(sample)>30000: sample=sample.sample(30000,random_state=42); y=pd.to_numeric(sample[target],errors="coerce"); base=stats(y); rows=[]
    else: y=pd.to_numeric(sample[target],errors="coerce"); base=stats(y); rows=[]
    if base["n"]<200: return pd.DataFrame()
    for c in cols:
        s=pd.to_numeric(sample[c],errors="coerce"); ok=s.notna()&y.notna()
        if ok.sum()<200 or s[ok].nunique()<5: continue
        sv,yv=s[ok].to_numpy(float),y[ok].to_numpy(float); q=np.quantile(sv,[.1,.2,.8,.9]); best=None
        for qv,lab,upper in[(q[0],"q10",1),(q[1],"q20",1),(q[2],"q80",0),(q[3],"q90",0)]:
            st=stats(yv[sv<=qv if upper else sv>=qv]); eff=abs(st["trimmed_mean"]-base["trimmed_mean"])
            if st["n"]>=100 and (best is None or eff>best[0]): best=(eff,lab,st)
        if best: rows.append({"feature":c,"best_region":best[1],"n":best[2]["n"],"effect":best[0],"trimmed_mean":best[2]["trimmed_mean"],"up_rate":best[2]["up_rate"]})
    r=pd.DataFrame(rows)
    if r.empty:return r
    top=r.nlargest(min(100,len(r)),"effect")["feature"].tolist(); mi_df=sample[top].replace([np.inf,-np.inf],np.nan).fillna(sample[top].median(numeric_only=True)); good=y.notna()
    try: mi=mutual_info_regression(mi_df.loc[good].to_numpy(float),y.loc[good].to_numpy(float),random_state=42) if good.sum()>=300 else np.zeros(len(top))
    except Exception: mi=np.zeros(len(top))
    r["mutual_information"]=r.feature.map(dict(zip(top,mi))).fillna(0.0); r["discovery_score"]=r.mutual_information*(1+100*r.effect)*np.sqrt(r.n); return r.sort_values(["discovery_score","effect","n"],ascending=False).reset_index(drop=True)


def model_run(train,val,test,features,target="y_spot_15m"):
    if not features:return pd.DataFrame()
    ytr=pd.to_numeric(train[target],errors="coerce"); yv=pd.to_numeric(val[target],errors="coerce"); yt=pd.to_numeric(test[target],errors="coerce"); masks=[ytr.notna(),yv.notna(),yt.notna()]; Xs=[]
    for d,m in zip((train,val,test),masks): Xs.append(d.loc[m,features].replace([np.inf,-np.inf],np.nan))
    Xtr,Xv,Xt=Xs; ytr,yv,yt=ytr.loc[masks[0]],yv.loc[masks[1]],yt.loc[masks[2]]; rows=[]
    if ytr.nunique()>1:
        clf=Pipeline([("imp",SimpleImputer(strategy="median")),("scale",StandardScaler()),("model",LogisticRegression(max_iter=600,class_weight="balanced"))]); clf.fit(Xtr,(ytr>0).astype(int)); pv=clf.predict(Xv); pt=clf.predict(Xt); rows.append({"model":"LogisticRegression","target":target,"val_accuracy":accuracy_score(yv>0,pv),"val_balanced_accuracy":balanced_accuracy_score(yv>0,pv),"test_accuracy":accuracy_score(yt>0,pt),"test_balanced_accuracy":balanced_accuracy_score(yt>0,pt)})
    for name,pipe,metric in [("ExtraTreesRegressor",Pipeline([("imp",SimpleImputer(strategy="median")),("model",ExtraTreesRegressor(n_estimators=160,max_depth=8,min_samples_leaf=30,n_jobs=-1,random_state=42))]),"r"),("Ridge",Pipeline([("imp",SimpleImputer(strategy="median")),("scale",StandardScaler()),("model",Ridge(alpha=10))]),"r")]:
        pipe.fit(Xtr,ytr); pv=pipe.predict(Xv); pt=pipe.predict(Xt); rows.append({"model":name,"target":target,"val_mae":mean_absolute_error(yv,pv),"val_r2":r2_score(yv,pv),"test_mae":mean_absolute_error(yt,pt),"test_r2":r2_score(yt,pt)})
    return pd.DataFrame(rows)


def run_quarter(q,o,s,v):
    o,s,v=qslice(o,q),qslice(s,q),qslice(v,q)
    if o.empty or s.empty or v.empty: raise ValueError(f"{q}: Options, Spot and VIX are all required.")
    mat=option_matrix(o); f=targets(align_states(features(mat),s,v),s); a=pd.DataFrame({"source":["Options","Spot","VIX"],"rows":[len(o),len(s),len(v)],"start":[str(o.timestamp.min()),str(s.timestamp.min()),str(v.timestamp.min())],"end":[str(o.timestamp.max()),str(s.timestamp.max()),str(v.timestamp.max())]}); train,val,test=np.array_split(f.sort_values("timestamp"),[int(len(f)*.6),int(len(f)*.8)]); disc=discover(train); top=disc.head(40).feature.tolist() if not disc.empty else []; ml=model_run(train,val,test,top) if top and SKLEARN_OK else pd.DataFrame(); return {"audit":a,"features":f,"discovery":disc,"ml":ml}


def make_report(q,r):
    def md(d): return d.to_markdown(index=False) if not d.empty else "No rows."
    return f"# FRIDAY — AUTONOMOUS RESEARCH REPORT — {q}\n\n## Research Clock\nOptions 1-minute; NIFTY Spot 15-minute; India VIX 15-minute. Spot/VIX are backward-aligned only.\n\n## Raw Data Audit\n{md(r['audit'])}\n\n## Fruitfulness Discovery\n{md(r['discovery'].head(100))}\n\n## Machine Learning\n{md(r['ml'])}\n\n## Promotion Rule\nNo discovery is treated as a strategy until it survives chronological validation and final testing."


def make_zip(results):
    b=io.BytesIO()
    with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
        for q,r in results.items():
            safe=q.replace(" ","_"); z.writestr(f"FRIDAY_{safe}_REPORT.md",make_report(q,r));
            for k in ["audit","discovery","ml","features"]: z.writestr(f"FRIDAY_{safe}_{k}.csv",r[k].to_csv(index=False))
    return b.getvalue()

# ---------- UI ----------
dhan_login_box()
st.title("FRIDAY — AUTONOMOUS MARKET RESEARCH ENGINE")
st.caption("Raw-data discovery → interactions → ML → chronological holdout validation → research memory")
if st.session_state.get("dhan_connected"):
    st.success(f"🔗 Dhan connection active for Client ID {st.session_state.dhan_client_id}")
else:
    st.info("Dhan connection is optional for offline CSV research. Connect in the sidebar when you need Dhan API access.")

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
        st.success(f"{q} completed — {len(r['features']):,} observations, {len(r['discovery']):,} candidates")
        st.dataframe(r["discovery"].head(30),use_container_width=True,hide_index=True)
        st.download_button(f"DOWNLOAD {q} REPORT",make_report(q,r).encode(),f"FRIDAY_{q.replace(' ','_')}_REPORT.md","text/markdown",key=f"report_{q}",use_container_width=True)
        st.download_button(f"DOWNLOAD {q} FULL PACKAGE",make_zip({q:r}),f"FRIDAY_{q.replace(' ','_')}_FULL_RESEARCH.zip","application/zip",key=f"zip_{q}",use_container_width=True)
    if len(results)>1: st.download_button("DOWNLOAD ALL QUARTERS",make_zip(results),"FRIDAY_ALL_RESEARCH.zip","application/zip",use_container_width=True)
    if errors: st.warning("Some quarters failed: "+" | ".join(errors))
    st.success(f"FRIDAY finished in {time.time()-start:.1f}s")
