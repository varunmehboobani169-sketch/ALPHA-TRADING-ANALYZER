import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import pandas as pd
import streamlit as st

DEFAULT_CLIENT_ID = "1113195747"
DHAN_API = "https://api.dhan.co/v2"
MAX_OPTION_WORKERS = 3
PAIR_TOLERANCE = pd.Timedelta(minutes=2)
SPOT_TOLERANCE = pd.Timedelta(minutes=20)

QUARTERS = {
    "Q1 2024": (date(2024,1,1), date(2024,3,31)),
    "Q2 2024": (date(2024,4,1), date(2024,6,30)),
    "Q3 2024": (date(2024,7,1), date(2024,9,30)),
    "Q4 2024": (date(2024,10,1), date(2024,12,31)),
    "Q1 2025": (date(2025,1,1), date(2025,3,31)),
    "Q2 2025": (date(2025,4,1), date(2025,6,30)),
    "Q3 2025": (date(2025,7,1), date(2025,9,30)),
    "Q4 2025": (date(2025,10,1), date(2025,12,31)),
    "Q1 2026": (date(2026,1,1), date(2026,3,31)),
    "Q2 2026": (date(2026,4,1), date(2026,6,30)),
    "Q3 2026": (date(2026,7,1), date(2026,8,26)),
}

st.set_page_config(page_title="FRIDAY", layout="wide")


def parse_datetime(values):
    s = pd.Series(values)
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(s, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).astype("datetime64[ns]")


def find_col(df, names):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in lookup:
            return lookup[key]
    for key, original in lookup.items():
        if any(name.lower().replace(" ", "_") in key for name in names):
            return original
    return None


def num_col(df, names):
    c = find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def read_csvs(files):
    frames = []
    for f in files or []:
        d = pd.read_csv(f, low_memory=False)
        if not d.empty:
            frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_time(df):
    c = find_col(df, ["timestamp", "datetime", "date_time", "exchange_timestamp", "timestamp_ist", "time", "date"])
    if c is None:
        raise ValueError(f"Timestamp column not found. Columns: {list(df.columns)}")
    out = df.copy()
    out["timestamp"] = parse_datetime(out[c])
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def normalize_options(df):
    x = normalize_time(df)
    strike = find_col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = find_col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike is None or side is None:
        raise ValueError(f"Options require strike and CE/PE columns. Found: {list(df.columns)}")
    out = pd.DataFrame({
        "timestamp": x["timestamp"],
        "strike": pd.to_numeric(x[strike], errors="coerce"),
        "side": x[side].astype(str).str.upper().str.strip(),
    })
    out["side"] = out["side"].replace({"C":"CE", "CALL":"CE", "P":"PE", "PUT":"PE"})
    out["expiry"] = pd.to_datetime(x[expiry], errors="coerce").dt.normalize() if expiry else pd.NaT
    for names, target in [
        (["close", "ltp", "last_price", "price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["volume", "vol", "traded_volume"], "volume"),
    ]:
        v = num_col(x, names)
        out[target] = v if v is not None else np.nan
    # Dhan rolling-option downloads may already identify the side as option_type.
    if "option_type" in x.columns and out["side"].eq("").all():
        out["side"] = x["option_type"].astype(str).str.upper()
    return out.dropna(subset=["timestamp", "strike"])[out["side"].isin(["CE", "PE"])].sort_values("timestamp").reset_index(drop=True)


def normalize_spot(df):
    x = normalize_time(df)
    p = num_col(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if p is None:
        raise ValueError("NIFTY Spot has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "nifty_spot": p}).dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    x = normalize_time(df)
    p = num_col(x, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if p is None:
        raise ValueError("India VIX has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "vix_close": p}).dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def synchronize(options, spot, vix):
    o = options.sort_values("timestamp").copy()
    s = spot.sort_values("timestamp").copy()
    m = pd.merge_asof(o, s, on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE)
    m = m.dropna(subset=["nifty_spot"])
    if not vix.empty and not m.empty:
        v = vix.sort_values("timestamp").copy()
        m = pd.merge_asof(m.sort_values("timestamp"), v, on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE)
    return m.sort_values("timestamp").reset_index(drop=True)


def pair_ce_pe(m):
    if m.empty:
        return pd.DataFrame()
    x = m.copy()
    x["timestamp"] = pd.to_datetime(x.timestamp).astype("datetime64[ns]")
    x["expiry_dt"] = pd.to_datetime(x.expiry, errors="coerce").dt.normalize()
    x["expiry_key"] = x["expiry_dt"].dt.strftime("%Y-%m-%d").fillna("NO_EXPIRY")
    x["strike"] = pd.to_numeric(x.strike, errors="coerce")
    x = x.dropna(subset=["timestamp", "strike"])
    ce = x[x.side == "CE"].copy()
    pe = x[x.side == "PE"].copy()
    if ce.empty or pe.empty:
        return pd.DataFrame()
    for c in ["nifty_spot", "vix_close", "close", "iv", "oi", "volume"]:
        if c in pe.columns:
            pe[c + "_pe"] = pe[c]
    pe = pe.rename(columns={"timestamp":"pe_timestamp"})
    ce = ce.sort_values(["timestamp", "expiry_key", "strike"], kind="mergesort").reset_index(drop=True)
    pe = pe.sort_values(["pe_timestamp", "expiry_key", "strike"], kind="mergesort").reset_index(drop=True)
    paired = pd.merge_asof(
        ce, pe,
        left_on="timestamp", right_on="pe_timestamp",
        by=["expiry_key", "strike"],
        direction="nearest", tolerance=PAIR_TOLERANCE,
        suffixes=("", "_dup"),
    ).dropna(subset=["pe_timestamp", "close", "close_pe"])
    if paired.empty:
        return pd.DataFrame()
    paired["pair_timestamp"] = paired.timestamp
    paired["nifty_spot_pair"] = paired.nifty_spot
    paired["vix_close_pair"] = paired.get("vix_close", np.nan)
    paired["pair_gap_seconds"] = (paired.pe_timestamp - paired.timestamp).abs().dt.total_seconds()
    return paired.sort_values("pair_timestamp").reset_index(drop=True)


def build_features(m):
    p = pair_ce_pe(m)
    if p.empty:
        return pd.DataFrame(), pd.DataFrame()
    p["spot_dist"] = (p.strike - p.nifty_spot_pair).abs()
    atm = p.sort_values(["pair_timestamp", "spot_dist"], kind="mergesort").drop_duplicates("pair_timestamp")
    def arr(c):
        return pd.to_numeric(atm[c], errors="coerce").to_numpy() if c in atm.columns else np.full(len(atm), np.nan)
    f = pd.DataFrame({
        "timestamp": atm.pair_timestamp.to_numpy(),
        "nifty_spot": arr("nifty_spot_pair"),
        "atm_strike": arr("strike"),
        "ce_close": arr("close"),
        "pe_close": arr("close_pe"),
        "ce_iv": arr("iv"),
        "pe_iv": arr("iv_pe"),
        "ce_oi": arr("oi"),
        "pe_oi": arr("oi_pe"),
        "ce_volume": arr("volume"),
        "pe_volume": arr("volume_pe"),
        "vix_close": arr("vix_close_pair"),
        "pair_gap_seconds": arr("pair_gap_seconds"),
    }).sort_values("timestamp").reset_index(drop=True)
    f["pcr_oi"] = f.pe_oi / f.ce_oi.replace(0, np.nan)
    f["straddle"] = f.ce_close + f.pe_close
    f["atm_iv"] = pd.concat([f.ce_iv, f.pe_iv], axis=1).mean(axis=1)
    for n in [1,4,16]:
        f[f"spot_ret_{n}"] = f.nifty_spot.pct_change(n)
    f["spot_vol_8"] = f.spot_ret_1.rolling(8).std()
    f["spot_ma_8"] = f.nifty_spot.rolling(8).mean()
    f["spot_ma_32"] = f.nifty_spot.rolling(32).mean()
    f["spot_trend"] = f.spot_ma_8 - f.spot_ma_32
    f["straddle_change"] = f.straddle.diff()
    f["straddle_ret"] = f.straddle.pct_change()
    f["iv_change"] = f.atm_iv.diff()
    f["vix_change"] = f.vix_close.diff()
    f["vix_ret"] = f.vix_close.pct_change()
    f["pcr_change"] = f.pcr_oi.diff()
    for n in [4,16]:
        f[f"forward_spot_{n}"] = f.nifty_spot.shift(-n) / f.nifty_spot - 1
        f[f"forward_straddle_{n}"] = f.straddle.shift(-n) / f.straddle - 1
    return f, p


def discover_patterns(f):
    rules = [
        ("IV rising + spot flat", (f.iv_change > 0) & (f.spot_ret_4.abs() < 0.001)),
        ("IV falling + spot flat", (f.iv_change < 0) & (f.spot_ret_4.abs() < 0.001)),
        ("PCR rising", f.pcr_change > 0),
        ("PCR falling", f.pcr_change < 0),
        ("VIX rising", f.vix_change > 0),
        ("VIX falling", f.vix_change < 0),
        ("Straddle expanding", f.straddle_change > 0),
        ("Straddle contracting", f.straddle_change < 0),
        ("Spot uptrend", f.spot_trend > 0),
        ("Spot downtrend", f.spot_trend < 0),
    ]
    rows = []
    for name, mask in rules:
        d = f.loc[mask].dropna(subset=["forward_spot_4", "forward_straddle_4"])
        if len(d) >= 10:
            rows.append({
                "pattern": name,
                "observations": len(d),
                "avg_next_4_spot_pct": d.forward_spot_4.mean() * 100,
                "avg_next_16_spot_pct": d.forward_spot_16.mean() * 100,
                "avg_next_4_straddle_pct": d.forward_straddle_4.mean() * 100,
                "avg_next_16_straddle_pct": d.forward_straddle_16.mean() * 100,
                "next_4_spot_up_rate": (d.forward_spot_4 > 0).mean() * 100,
                "next_4_straddle_up_rate": (d.forward_straddle_4 > 0).mean() * 100,
            })
    return pd.DataFrame(rows).sort_values("observations", ascending=False) if rows else pd.DataFrame()


def diagnostics(o,s,v,m,p,f):
    return pd.DataFrame({
        "Check":["Option rows","Spot rows","VIX rows","Option+Spot synced","CE rows","PE rows","CE/PE pairs","ATM observations","Option start","Option end","Spot start","Spot end"],
        "Value":[len(o),len(s),len(v),len(m),int((o.side=="CE").sum()) if not o.empty else 0,int((o.side=="PE").sum()) if not o.empty else 0,len(p),len(f),str(o.timestamp.min()) if not o.empty else "N/A",str(o.timestamp.max()) if not o.empty else "N/A",str(s.timestamp.min()) if not s.empty else "N/A",str(s.timestamp.max()) if not s.empty else "N/A"]
    })


def fmt_md(v):
    if pd.isna(v): return ""
    if isinstance(v, (float, np.floating)): return f"{float(v):.6g}"
    return str(v).replace("|", "\\|")


def markdown_table(df):
    if df is None or df.empty: return "_No rows._"
    cols=[str(c) for c in df.columns]
    lines=["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"]*len(cols)) + " |"]
    for row in df.itertuples(index=False,name=None):
        lines.append("| " + " | ".join(fmt_md(v) for v in row) + " |")
    return "\n".join(lines)


def build_report(period_label,o,s,v,m,p,f,patterns):
    safe=period_label.upper().replace(" ","_").replace("/","-")
    lines=[
        f"# FRIDAY — OPTION PATTERN RESEARCH — {period_label}",
        "",
        "## Scope",
        f"Analysis period: **{period_label}**",
        "Sources: Options + NIFTY Spot + India VIX. Futures excluded.",
        "",
        "## Input Diagnostics",
        markdown_table(diagnostics(o,s,v,m,p,f)),
        "",
        "## Pattern Summary",
        markdown_table(patterns),
        "",
        "## Research Notes",
        f"ATM observations analysed: **{len(f):,}**",
        "Current FRIDAY is a statistical pattern analyzer. The autonomous AI research engine is intentionally not enabled yet.",
        ""
    ]
    return "\n".join(lines),safe


def dhan_call(path,payload,token,client_id,max_retries=3):
    if not token: raise ValueError("Enter your Dhan Access Token in the sidebar first.")
    last=None
    for attempt in range(max_retries+1):
        req=urllib.request.Request(DHAN_API+path,data=json.dumps(payload).encode(),method="POST",headers={"Accept":"application/json","Content-Type":"application/json","access-token":token,"client-id":client_id})
        try:
            with urllib.request.urlopen(req,timeout=90) as r: return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail=e.read().decode("utf-8",errors="replace"); last=RuntimeError(f"Dhan HTTP {e.code}: {detail[:1200]}")
            if e.code not in (429,500,502,503,504) or attempt>=max_retries: raise last from e
        except urllib.error.URLError as e:
            last=RuntimeError(f"Dhan connection error: {e}")
            if attempt>=max_retries: raise last from e
        if attempt<max_retries: time.sleep(min(2**attempt,8))
    raise last or RuntimeError("Dhan request failed")


def dhan_profile(token,client_id):
    if not token: return False,"No token entered"
    req=urllib.request.Request(DHAN_API+"/profile",method="GET",headers={"Accept":"application/json","access-token":token,"client-id":client_id})
    try:
        with urllib.request.urlopen(req,timeout=30) as r: body=json.loads(r.read().decode("utf-8"))
        validity=body.get("data",{}).get("tokenValidity") or body.get("tokenValidity")
        return True,f"Token verified{f' | Valid until: {validity}' if validity else ''}"
    except Exception as e: return False,str(e)


def series_from_response(body,source_key=None):
    if not isinstance(body,dict): return pd.DataFrame()
    data=body.get("data",body)
    if source_key and isinstance(data,dict) and isinstance(data.get(source_key),dict): data=data[source_key]
    if not isinstance(data,dict) or not data.get("timestamp"): return pd.DataFrame()
    n=len(data["timestamp"])
    cols={k:data.get(k,[None]*n) for k in ["timestamp","open","high","low","close","iv","volume","oi","strike","spot"]}
    out=pd.DataFrame({k:(v if isinstance(v,list) else [None]*n) for k,v in cols.items()})
    out["timestamp"]=parse_datetime(out.timestamp)
    for c in cols:
        if c!="timestamp": out[c]=pd.to_numeric(out[c],errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def rolling_option_part(body,offset,opt):
    key="ce" if opt=="CALL" else "pe"
    df=series_from_response(body,key)
    if df.empty: return df
    df["strike_offset"]=offset; df["option_type"]="CE" if opt=="CALL" else "PE"
    return df


def date_chunks(start_date,end_date,max_days=10):
    out=[]; cur=start_date
    while cur<=end_date:
        ce=min(cur+timedelta(days=max_days-1),end_date); out.append((cur,ce)); cur=ce+timedelta(days=1)
    return out


def make_option_job(cur,ce,offset,opt):
    strike="ATM" if offset==0 else (f"ATM+{offset}" if offset>0 else f"ATM{offset}")
    return (cur,ce,offset,opt,{"exchangeSegment":"NSE_FNO","interval":"1","securityId":13,"instrument":"OPTIDX","expiryFlag":"WEEK","expiryCode":1,"strike":strike,"drvOptionType":opt,"requiredData":["open","high","low","close","iv","volume","strike","oi","spot"],"fromDate":cur.strftime("%Y-%m-%d"),"toDate":(ce+timedelta(days=1)).strftime("%Y-%m-%d")})


def run_option_job(job,token,client_id,q_label):
    _,_,offset,opt,payload=job
    body=dhan_call("/charts/rollingoption",payload,token,client_id,max_retries=3)
    part=rolling_option_part(body,offset,opt)
    if not part.empty: part["quarter"]=q_label
    return part


def option_quarter_download(q_label,start_date,end_date,token,client_id,progress_cb=None,status_cb=None):
    jobs=[make_option_job(cur,ce,offset,opt) for cur,ce in date_chunks(start_date,end_date,10) for offset in range(-10,11) for opt in ("CALL","PUT")]
    total=len(jobs)
    if not total: raise ValueError(f"No request windows were created for {q_label}.")
    if status_cb: status_cb(f"Preflight: testing Dhan weekly options request for {q_label}...")
    first=run_option_job(jobs[0],token,client_id,q_label); chunks=[first] if not first.empty else []; errors=[]; done=1
    if progress_cb: progress_cb(done/total)
    if status_cb: status_cb(f"Preflight passed. Downloading {total:,} requests with {MAX_OPTION_WORKERS} workers...")
    with ThreadPoolExecutor(max_workers=MAX_OPTION_WORKERS) as executor:
        future_map={executor.submit(run_option_job,j,token,client_id,q_label):j for j in jobs[1:]}
        for future in as_completed(future_map):
            job=future_map[future]
            try:
                part=future.result()
                if not part.empty: chunks.append(part)
            except Exception as exc:
                cur,ce,offset,opt,_=job; errors.append({"from_date":str(cur),"to_date":str(ce),"strike_offset":offset,"option_type":"CE" if opt=="CALL" else "PE","error":str(exc)})
            done+=1
            if progress_cb: progress_cb(done/total)
            if status_cb and (done%10==0 or done==total): status_cb(f"{done:,}/{total:,} requests complete | failed {len(errors)}")
            if done>20 and len(errors)/max(1,done-1)>0.25: raise RuntimeError(f"Dhan is failing too many requests ({len(errors)}/{done-1}).")
    if not chunks: raise RuntimeError(f"No usable option candles were returned for {q_label}.")
    out=pd.concat(chunks,ignore_index=True).drop_duplicates(subset=["timestamp","option_type","strike_offset","strike"]).sort_values(["timestamp","option_type","strike_offset"]).reset_index(drop=True); out.attrs["failed_requests"]=errors
    return out


def download_spot_or_vix(symbol,start_date,end_date,timeframe,token,client_id,progress_cb=None):
    sid="13" if symbol=="NIFTY" else "21"; interval=None if timeframe=="Daily" else {"1-minute":1,"5-minute":5,"15-minute":15,"25-minute":25,"60-minute":60}[timeframe]; chunks=[]; windows=date_chunks(start_date,end_date,90)
    for i,(cur,ce) in enumerate(windows,1):
        if interval is None:
            path="/charts/historical"; payload={"securityId":sid,"exchangeSegment":"IDX_I","instrument":"INDEX","expiryCode":0,"oi":False,"fromDate":cur.strftime("%Y-%m-%d"),"toDate":(ce+timedelta(days=1)).strftime("%Y-%m-%d")}
        else:
            path="/charts/intraday"; payload={"securityId":sid,"exchangeSegment":"IDX_I","instrument":"INDEX","interval":interval,"oi":False,"fromDate":cur.strftime("%Y-%m-%d 00:00:00"),"toDate":(ce+timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")}
        part=series_from_response(dhan_call(path,payload,token,client_id));
        if not part.empty: chunks.append(part)
        if progress_cb: progress_cb(i/len(windows))
    if not chunks: raise ValueError(f"Dhan returned no {symbol} candles for the selected range.")
    return pd.concat(chunks,ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def render_data_vault(token,client_id):
    st.title("FRIDAY — DATA VAULT"); st.caption("2025 + 2026 historical research collector. 2024 data already owned separately.")
    dataset=st.selectbox("Dataset",["NIFTY Spot","India VIX","NIFTY Weekly Options ATM±10"])
    if dataset=="NIFTY Weekly Options ATM±10": st.info("1-minute weekly expired-option data. 21 strike levels × CE/PE. OI + IV + volume + spot. Requests are split into 10-day windows with progress.")
    else:
        timeframe=st.selectbox("Timeframe",["1-minute","5-minute","15-minute","25-minute","60-minute","Daily"],index=0); st.info("Historical OHLC download. OI is disabled for Spot/VIX.")
    period=st.selectbox("Research Period",["Q1 2025","Q2 2025","Q3 2025","Q4 2025","FULL 2025","Q1 2026","Q2 2026","Q3 2026","2026 YTD","FULL 2026 AVAILABLE","Custom"])
    custom_start=st.date_input("Custom start",value=date(2025,1,1),min_value=date(2025,1,1),max_value=date(2026,8,26)); custom_end=st.date_input("Custom end",value=date(2025,3,31),min_value=date(2025,1,1),max_value=date(2026,8,26))
    ranges={**QUARTERS,"FULL 2025":(date(2025,1,1),date(2025,12,31)),"2026 YTD":(date(2026,1,1),date(2026,8,26)),"FULL 2026 AVAILABLE":(date(2026,1,1),date(2026,8,26)),"Custom":(custom_start,custom_end)}
    st.caption(f"Selected period: {ranges[period][0]} → {ranges[period][1]}")
    if st.button("DOWNLOAD QUARTER DATA",use_container_width=True):
        if not token: st.error("Enter your Dhan Access Token first."); return
        gauge=st.progress(0,text="FRIDAY Data Vault: 0%"); status=st.empty(); started=time.time()
        try:
            if period=="FULL 2025": ranges_to_get=list(QUARTERS.items())[4:8]
            elif period in ("2026 YTD","FULL 2026 AVAILABLE"): ranges_to_get=list(QUARTERS.items())[8:11]
            elif period=="Custom": ranges_to_get=[("Custom",(custom_start,custom_end))]
            else: ranges_to_get=[(period,ranges[period])]
            parts=[]; all_failed=[]
            for qi,(label,(qs,qe)) in enumerate(ranges_to_get):
                base=qi/len(ranges_to_get); span=1/len(ranges_to_get)
                def pg(p,base=base,span=span,label=label):
                    pct=min(99.9,(base+p*span)*100); gauge.progress(pct/100,text=f"FRIDAY Data Vault: {pct:.1f}% — {label}")
                def stmsg(msg,label=label): status.info(f"{label}: {msg} | elapsed {int(time.time()-started)}s")
                if dataset=="NIFTY Weekly Options ATM±10": part=option_quarter_download(label,qs,qe,token,client_id,pg,stmsg); all_failed.extend(part.attrs.get("failed_requests",[]))
                else: part=download_spot_or_vix("NIFTY" if dataset=="NIFTY Spot" else "INDIA VIX",qs,qe,timeframe,token,client_id,pg); part["period"]=label
                parts.append(part); gauge.progress((qi+1)/len(ranges_to_get),text=f"FRIDAY Data Vault: {(qi+1)/len(ranges_to_get)*100:.1f}% — {label} complete")
            if len(parts)==1:
                df=parts[0]; safe=period.replace(" ","_").replace("±","PLUS_MINUS"); csv=df.to_csv(index=False).encode(); report=build_data_report(period,dataset,df,all_failed)[0].encode(); pkg=io.BytesIO()
                with zipfile.ZipFile(pkg,"w",zipfile.ZIP_DEFLATED) as z: z.writestr(f"{safe}_{dataset.replace(' ','_').replace('±','PLUS_MINUS')}.csv",csv); z.writestr(f"FRIDAY_{safe}_DATA_REPORT.md",report)
                st.success(f"Download complete — {len(df):,} rows"); st.download_button("DOWNLOAD CSV",csv,f"FRIDAY_{safe}_{dataset.replace(' ','_').replace('±','PLUS_MINUS')}.csv","text/csv",use_container_width=True); st.download_button("DOWNLOAD DATA REPORT (.MD)",report,f"FRIDAY_{safe}_DATA_REPORT.md","text/markdown",use_container_width=True); st.download_button("DOWNLOAD PACKAGE (.ZIP)",pkg.getvalue(),f"FRIDAY_{safe}_PACKAGE.zip","application/zip",use_container_width=True)
            else:
                pkg=io.BytesIO()
                with zipfile.ZipFile(pkg,"w",zipfile.ZIP_DEFLATED) as z:
                    for label,part in zip([x[0] for x in ranges_to_get],parts):
                        safe=label.replace(" ","_"); z.writestr(f"{safe}_{dataset.replace(' ','_').replace('±','PLUS_MINUS')}.csv",part.to_csv(index=False)); z.writestr(f"FRIDAY_{safe}_DATA_REPORT.md",build_data_report(label,dataset,part,part.attrs.get("failed_requests",[]))[0])
                st.success(f"Quarter-wise package ready — {len(parts)} periods"); st.download_button("DOWNLOAD COMPLETE QUARTER PACKAGE (.ZIP)",pkg.getvalue(),f"FRIDAY_{period.replace(' ','_')}_COMPLETE_RESEARCH_PACKAGE.zip","application/zip",use_container_width=True)
            gauge.progress(1.0,text="FRIDAY Data Vault: 100% ✅")
        except Exception as exc: status.error(f"Download stopped after {time.time()-started:.0f}s: {exc}"); st.exception(exc)


def build_data_report(period_label,dataset,df,failed=None):
    failed=failed or []; rows=[["Research period",period_label],["Dataset",dataset],["Rows",f"{len(df):,}"],["Start",str(df.timestamp.min()) if "timestamp" in df and not df.empty else "N/A"],["End",str(df.timestamp.max()) if "timestamp" in df and not df.empty else "N/A"],["Failed API requests",f"{len(failed):,}"]]
    if "option_type" in df: rows += [["CE rows",f"{int((df.option_type=='CE').sum()):,}"],["PE rows",f"{int((df.option_type=='PE').sum()):,}"]]
    if "strike_offset" in df:
        offs=pd.to_numeric(df.strike_offset,errors="coerce").dropna(); rows += [["Strike offsets",f"{int(offs.min())} to {int(offs.max())}" if not offs.empty else "N/A"],["Distinct strikes",f"{df.strike.nunique():,}" if "strike" in df else "N/A"]]
    for c in ["iv","oi","volume"]:
        if c in df: rows.append([f"{c.upper()} non-null",f"{df[c].notna().mean()*100:.2f}%"])
    safe=period_label.upper().replace(" ","_"); report=[f"# FRIDAY — DATA RESEARCH REPORT — {period_label}","","## Dataset Summary",markdown_table(pd.DataFrame(rows,columns=["Metric","Value"])),"","## Research Use","Data-quality/download summary only. This is not an AI conclusion or trading recommendation."]
    if failed: report += ["","## Failed Requests",markdown_table(pd.DataFrame(failed))]
    return "\n".join(report),safe


def render_analyzer():
    st.title("FRIDAY — OPTION PATTERN RESEARCH")
    st.caption("Quarter-wise report generator restored for 2024, 2025 and 2026. Futures excluded.")
    period=st.selectbox("Research Period",["Q1 2024","Q2 2024","Q3 2024","Q4 2024","Full 2024","Q1 2025","Q2 2025","Q3 2025","Q4 2025","Full 2025","Q1 2026","Q2 2026","Q3 2026","2026 YTD","Custom"],index=5)
    defaults={"Q1 2024":(date(2024,1,1),date(2024,3,31)),"Q2 2024":(date(2024,4,1),date(2024,6,30)),"Q3 2024":(date(2024,7,1),date(2024,9,30)),"Q4 2024":(date(2024,10,1),date(2024,12,31)),"Full 2024":(date(2024,1,1),date(2024,12,31)),"Q1 2025":(date(2025,1,1),date(2025,3,31)),"Q2 2025":(date(2025,4,1),date(2025,6,30)),"Q3 2025":(date(2025,7,1),date(2025,9,30)),"Q4 2025":(date(2025,10,1),date(2025,12,31)),"Full 2025":(date(2025,1,1),date(2025,12,31)),"Q1 2026":(date(2026,1,1),date(2026,3,31)),"Q2 2026":(date(2026,4,1),date(2026,6,30)),"Q3 2026":(date(2026,7,1),date(2026,8,26)),"2026 YTD":(date(2026,1,1),date(2026,8,26))}
    start_default,end_default=defaults.get(period,(date(2025,1,1),date(2025,3,31)));
    c1,c2=st.columns(2)
    with c1: custom_start=st.date_input("Custom start",value=start_default)
    with c2: custom_end=st.date_input("Custom end",value=end_default)
    periods_for_upload={"Full 2024":QUARTERS.keys(),"Full 2025":["Q1 2025","Q2 2025","Q3 2025","Q4 2025"],"2026 YTD":["Q1 2026","Q2 2026","Q3 2026"],"Custom":["Custom"],"Q1 2024":["Q1 2024"],"Q2 2024":["Q2 2024"],"Q3 2024":["Q3 2024"],"Q4 2024":["Q4 2024"],"Q1 2025":["Q1 2025"],"Q2 2025":["Q2 2025"],"Q3 2025":["Q3 2025"],"Q4 2025":["Q4 2025"],"Q1 2026":["Q1 2026"],"Q2 2026":["Q2 2026"],"Q3 2026":["Q3 2026"]}
    wanted=list(periods_for_upload[period])
    st.info(f"Upload the option, NIFTY Spot and India VIX CSVs for: {', '.join(wanted)}. Multi-period selections create one report per quarter plus a complete ZIP.")
    opt=st.file_uploader("Option CSV(s)",type=["csv"],accept_multiple_files=True,key="research_opt")
    spot=st.file_uploader("NIFTY Spot CSV(s)",type=["csv"],accept_multiple_files=True,key="research_spot")
    vix=st.file_uploader("India VIX CSV(s) — optional",type=["csv"],accept_multiple_files=True,key="research_vix")
    if not opt or not spot:
        st.warning("Upload Option CSV(s) and NIFTY Spot CSV(s) first."); return
    if st.button("GENERATE QUARTER-WISE REPORTS",use_container_width=True):
        try:
            all_opt=normalize_options(read_csvs(opt)); all_spot=normalize_spot(read_csvs(spot)); all_vix=normalize_vix(read_csvs(vix)) if vix else pd.DataFrame()
            generated=[]; summaries=[]; package=io.BytesIO();
            for label in wanted:
                if label=="Custom": qs,qe=custom_start,custom_end
                else: qs,qe=defaults[label]
                o=all_opt[(all_opt.timestamp.dt.date>=qs)&(all_opt.timestamp.dt.date<=qe)].copy()
                s=all_spot[(all_spot.timestamp.dt.date>=qs)&(all_spot.timestamp.dt.date<=qe)].copy()
                v=all_vix[(all_vix.timestamp.dt.date>=qs)&(all_vix.timestamp.dt.date<=qe)].copy() if not all_vix.empty else pd.DataFrame()
                m=synchronize(o,s,v)
                p=pair_ce_pe(m)
                f,pairs=build_features(m)
                if f.empty: raise ValueError(f"{label}: no usable ATM observations after synchronization/pairing.")
                patterns=discover_patterns(f)
                report_text,safe=build_report(label,o,s,v,m,pairs,f,patterns)
                diag=diagnostics(o,s,v,m,pairs,f)
                generated.append((label,report_text,f,patterns,diag,pairs))
                summaries.append({"period":label,"option_rows":len(o),"spot_rows":len(s),"vix_rows":len(v),"synced":len(m),"pairs":len(pairs),"atm_observations":len(f),"patterns":len(patterns)})
            with zipfile.ZipFile(package,"w",zipfile.ZIP_DEFLATED) as z:
                for label,report_text,f,patterns,diag,pairs in generated:
                    safe=label.replace(" ","_")
                    z.writestr(f"FRIDAY_{safe}_REPORT.md",report_text)
                    z.writestr(f"FRIDAY_{safe}_features.csv",f.to_csv(index=False))
                    z.writestr(f"FRIDAY_{safe}_patterns.csv",patterns.to_csv(index=False))
                    z.writestr(f"FRIDAY_{safe}_diagnostics.csv",diag.to_csv(index=False))
                    z.writestr(f"FRIDAY_{safe}_pairs.csv",pairs.to_csv(index=False))
                z.writestr("QUARTER_SUMMARY.csv",pd.DataFrame(summaries).to_csv(index=False))
            st.success(f"Generated {len(generated)} quarter report(s).")
            st.dataframe(pd.DataFrame(summaries),use_container_width=True,hide_index=True)
            for label,report_text,f,patterns,diag,pairs in generated:
                safe=label.replace(" ","_")
                with st.expander(f"{label} — report"):
                    st.download_button(f"DOWNLOAD {label} REPORT (.MD)",report_text.encode(),f"FRIDAY_{safe}_REPORT.md","text/markdown",key=f"md_{safe}")
                    st.download_button(f"DOWNLOAD {label} FULL PACKAGE (.ZIP)",_make_report_zip(label,report_text,f,patterns,diag,pairs),f"FRIDAY_{safe}_RESEARCH.zip","application/zip",key=f"zip_{safe}")
            st.download_button("DOWNLOAD ALL QUARTER-WISE REPORTS (.ZIP)",package.getvalue(),f"FRIDAY_{period.replace(' ','_')}_QUARTER_WISE_REPORTS.zip","application/zip",use_container_width=True)
        except Exception as exc:
            st.error(f"Report generation stopped: {exc}"); st.exception(exc)


def _make_report_zip(label,report_text,f,patterns,diag,pairs):
    out=io.BytesIO(); safe=label.replace(" ","_")
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"FRIDAY_{safe}_REPORT.md",report_text); z.writestr(f"FRIDAY_{safe}_features.csv",f.to_csv(index=False)); z.writestr(f"FRIDAY_{safe}_patterns.csv",patterns.to_csv(index=False)); z.writestr(f"FRIDAY_{safe}_diagnostics.csv",diag.to_csv(index=False)); z.writestr(f"FRIDAY_{safe}_pairs.csv",pairs.to_csv(index=False))
    return out.getvalue()


with st.sidebar:
    st.subheader("FRIDAY")
    client_id=st.text_input("Dhan Client ID",value=DEFAULT_CLIENT_ID).strip()
    token=st.text_input("Dhan Access Token",value="",type="password").strip()
    if token:
        ok,msg=dhan_profile(token,client_id); (st.success if ok else st.error)(msg)
    module=st.radio("MODULE",["Data Vault","Pattern Research"],index=1)

if module=="Data Vault":
    render_data_vault(token,client_id)
else:
    render_analyzer()
