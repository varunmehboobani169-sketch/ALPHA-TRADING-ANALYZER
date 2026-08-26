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
PAIR_TOLERANCE = pd.Timedelta(minutes=2)
SPOT_TOLERANCE = pd.Timedelta(minutes=20)
MAX_OPTION_WORKERS = 3

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


def dhan_call(path, payload, token, client_id, max_retries=3):
    if not token:
        raise ValueError("Enter your Dhan Access Token in the sidebar first.")
    last_error = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            DHAN_API + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Accept":"application/json","Content-Type":"application/json","access-token":token,"client-id":client_id},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Dhan HTTP {e.code}: {detail[:1200]}")
            if e.code not in (429,500,502,503,504) or attempt >= max_retries:
                raise last_error from e
        except urllib.error.URLError as e:
            last_error = RuntimeError(f"Dhan connection error: {e}")
            if attempt >= max_retries:
                raise last_error from e
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 8))
    raise last_error or RuntimeError("Dhan request failed")


def dhan_profile(token, client_id):
    if not token:
        return False, "No token entered"
    req = urllib.request.Request(DHAN_API + "/profile", method="GET", headers={"Accept":"application/json","access-token":token,"client-id":client_id})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
        validity = body.get("data", {}).get("tokenValidity") or body.get("tokenValidity")
        return True, f"Token verified{f' | Valid until: {validity}' if validity else ''}"
    except Exception as e:
        return False, str(e)


def series_from_response(body, source_key=None):
    if not isinstance(body, dict): return pd.DataFrame()
    data = body.get("data", body)
    if source_key and isinstance(data, dict) and isinstance(data.get(source_key), dict): data = data[source_key]
    if not isinstance(data, dict) or not data.get("timestamp"): return pd.DataFrame()
    n = len(data["timestamp"])
    cols = {k: data.get(k, [None] * n) for k in ["timestamp","open","high","low","close","iv","volume","oi","strike","spot"]}
    out = pd.DataFrame({k:(v if isinstance(v, list) else [None] * n) for k,v in cols.items()})
    out["timestamp"] = parse_datetime(out["timestamp"])
    for c in cols:
        if c != "timestamp": out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def rolling_option_part(body, offset, option_type):
    key = "ce" if option_type == "CALL" else "pe"
    df = series_from_response(body, key)
    if df.empty: return df
    df["strike_offset"] = offset
    df["option_type"] = "CE" if option_type == "CALL" else "PE"
    return df


def date_chunks(start_date, end_date, max_days=10):
    chunks=[]
    cur=start_date
    while cur<=end_date:
        ce=min(cur+timedelta(days=max_days-1), end_date)
        chunks.append((cur,ce))
        cur=ce+timedelta(days=1)
    return chunks


def make_option_job(cur, ce, offset, opt):
    strike = "ATM" if offset == 0 else (f"ATM+{offset}" if offset > 0 else f"ATM{offset}")
    payload={
        "exchangeSegment":"NSE_FNO","interval":"1","securityId":13,"instrument":"OPTIDX",
        "expiryFlag":"WEEK","expiryCode":1,"strike":strike,"drvOptionType":opt,
        "requiredData":["open","high","low","close","iv","volume","strike","oi","spot"],
        "fromDate":cur.strftime("%Y-%m-%d"),"toDate":(ce+timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    return (cur, ce, offset, opt, payload)


def run_option_job(job, token, client_id, q_label):
    _, _, offset, opt, payload = job
    body=dhan_call("/charts/rollingoption",payload,token,client_id,max_retries=3)
    part=rolling_option_part(body,offset,opt)
    if not part.empty: part["quarter"]=q_label
    return part


def option_quarter_download(q_label, start_date, end_date, token, client_id, progress_cb=None, status_cb=None):
    jobs=[]
    for cur,ce in date_chunks(start_date,end_date,10):
        for offset in range(-10,11):
            for opt in ("CALL","PUT"):
                jobs.append(make_option_job(cur,ce,offset,opt))
    total=len(jobs)
    if total==0: raise ValueError(f"No request windows were created for {q_label}.")
    if status_cb: status_cb(f"Preflight: testing Dhan weekly options request for {q_label}...")
    first=run_option_job(jobs[0],token,client_id,q_label)
    chunks=[first] if not first.empty else []
    errors=[]
    done=1
    if progress_cb: progress_cb(done/total)
    if status_cb: status_cb(f"Preflight passed. Downloading {total:,} requests with {MAX_OPTION_WORKERS} workers...")
    def submit_jobs():
        with ThreadPoolExecutor(max_workers=MAX_OPTION_WORKERS) as executor:
            future_map={executor.submit(run_option_job,job,token,client_id,q_label):job for job in jobs[1:]}
            for future in as_completed(future_map):
                job=future_map[future]
                try:
                    part=future.result()
                    if not part.empty: chunks.append(part)
                except Exception as exc:
                    cur,ce,offset,opt,_=job
                    errors.append({"from_date":str(cur),"to_date":str(ce),"strike_offset":offset,"option_type":"CE" if opt=="CALL" else "PE","error":str(exc)})
                yield
    for _ in submit_jobs():
        done+=1
        if progress_cb: progress_cb(done/total)
        if status_cb and (done%10==0 or done==total): status_cb(f"{done:,}/{total:,} requests complete | failed {len(errors)}")
        completed_nonpreflight=done-1
        if completed_nonpreflight>=20 and len(errors)/max(1,completed_nonpreflight)>0.25:
            raise RuntimeError(f"Dhan is failing too many requests ({len(errors)}/{completed_nonpreflight}). Download stopped early instead of producing an incomplete research file.")
    if not chunks: raise RuntimeError(f"No usable option candles were returned for {q_label}.")
    out=pd.concat(chunks,ignore_index=True).drop_duplicates(subset=["timestamp","option_type","strike_offset","strike"]).sort_values(["timestamp","option_type","strike_offset"]).reset_index(drop=True)
    out.attrs["failed_requests"]=errors
    return out


def download_spot_or_vix(symbol,start_date,end_date,timeframe,token,client_id,progress_cb=None):
    sid="13" if symbol=="NIFTY" else "21"
    interval={"1-minute":1,"5-minute":5,"15-minute":15,"25-minute":25,"60-minute":60,"Daily":None}[timeframe]
    windows=date_chunks(start_date,end_date,90)
    chunks=[]
    for i,(cur,ce) in enumerate(windows,1):
        if interval is None:
            path="/charts/historical"; payload={"securityId":sid,"exchangeSegment":"IDX_I","instrument":"INDEX","expiryCode":0,"oi":False,"fromDate":cur.strftime("%Y-%m-%d"),"toDate":(ce+timedelta(days=1)).strftime("%Y-%m-%d")}
        else:
            path="/charts/intraday"; payload={"securityId":sid,"exchangeSegment":"IDX_I","instrument":"INDEX","interval":interval,"oi":False,"fromDate":cur.strftime("%Y-%m-%d 00:00:00"),"toDate":(ce+timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")}
        part=series_from_response(dhan_call(path,payload,token,client_id))
        if not part.empty: chunks.append(part)
        if progress_cb: progress_cb(i/len(windows))
    if not chunks: raise ValueError(f"Dhan returned no {symbol} candles for the selected range.")
    return pd.concat(chunks,ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def format_md_value(v):
    if pd.isna(v): return ""
    if isinstance(v,(float,np.floating)): return f"{float(v):.6g}"
    return str(v).replace("|","\\|")


def markdown_table(df):
    if df is None or df.empty: return "_No rows._"
    cols=[str(c) for c in df.columns]
    lines=["| " + " | ".join(cols) + " |","| " + " | ".join(["---"]*len(cols)) + " |"]
    for row in df.itertuples(index=False,name=None): lines.append("| " + " | ".join(format_md_value(v) for v in row) + " |")
    return "\n".join(lines)


def build_data_report(period_label,dataset,df,failed=None):
    failed=failed or []
    safe=period_label.upper().replace(" ","_").replace("±","PLUS_MINUS")
    rows=[
        ["Research period",period_label],
        ["Dataset",dataset],
        ["Rows",f"{len(df):,}"],
        ["Start",str(df.timestamp.min()) if "timestamp" in df.columns and not df.empty else "N/A"],
        ["End",str(df.timestamp.max()) if "timestamp" in df.columns and not df.empty else "N/A"],
        ["Failed API requests",f"{len(failed):,}"],
    ]
    if "option_type" in df.columns:
        rows += [["CE rows",f"{int((df.option_type=='CE').sum()):,}"],["PE rows",f"{int((df.option_type=='PE').sum()):,}"]]
    if "strike_offset" in df.columns:
        offsets=sorted(pd.to_numeric(df.strike_offset,errors="coerce").dropna().unique().tolist())
        rows += [["Strike offsets",f"{int(min(offsets))} to {int(max(offsets))}" if offsets else "N/A"],["Distinct strikes",f"{df['strike'].nunique():,}" if 'strike' in df.columns else "N/A"]]
    if "iv" in df.columns: rows.append(["IV non-null",f"{df.iv.notna().mean()*100:.2f}%"])
    if "oi" in df.columns: rows.append(["OI non-null",f"{df.oi.notna().mean()*100:.2f}%"])
    if "volume" in df.columns: rows.append(["Volume non-null",f"{df.volume.notna().mean()*100:.2f}%"])
    report=[f"# FRIDAY — DATA RESEARCH REPORT — {period_label}","", "## Dataset Summary", markdown_table(pd.DataFrame(rows,columns=["Metric","Value"])), "", "## Research Use", "This file is a data-quality/download report for the scraped dataset. It does not claim that the current statistical engine or future AI has validated a trading relationship.", ""]
    if failed:
        report += ["## Failed Requests", markdown_table(pd.DataFrame(failed)), ""]
    return "\n".join(report), safe


def render_data_vault(token,client_id):
    st.title("FRIDAY — DATA VAULT")
    st.caption("2025 + 2026 historical research collector. 2024 data already owned separately.")
    dataset=st.selectbox("Dataset",["NIFTY Spot","India VIX","NIFTY Weekly Options ATM±10"])
    if dataset=="NIFTY Weekly Options ATM±10":
        st.info("1-minute weekly expired-option data. 21 strike levels × CE/PE. OI + IV + volume + spot. Dhan requests are split into 10-day windows. Progress is shown request-by-request.")
    else:
        timeframe=st.selectbox("Timeframe",["1-minute","5-minute","15-minute","25-minute","60-minute","Daily"],index=0)
        st.info("Historical OHLC download. OI is disabled for Spot/VIX.")
    period=st.selectbox("Research Period",["Q1 2025","Q2 2025","Q3 2025","Q4 2025","FULL 2025","Q1 2026","Q2 2026","Q3 2026","2026 YTD","FULL 2026 AVAILABLE","Custom"])
    custom_start=st.date_input("Custom start",value=date(2025,1,1),min_value=date(2025,1,1),max_value=date(2026,8,26))
    custom_end=st.date_input("Custom end",value=date(2025,3,31),min_value=date(2025,1,1),max_value=date(2026,8,26))
    ranges={**QUARTERS,"FULL 2025":(date(2025,1,1),date(2025,12,31)),"2026 YTD":(date(2026,1,1),date(2026,8,26)),"FULL 2026 AVAILABLE":(date(2026,1,1),date(2026,8,26)),"Custom":(custom_start,custom_end)}
    st.caption(f"Selected period: {ranges[period][0]} → {ranges[period][1]}")
    if st.button("DOWNLOAD QUARTER DATA",use_container_width=True):
        if not token:
            st.error("Enter your Dhan Access Token first."); return
        gauge=st.progress(0,text="FRIDAY Data Vault: 0%")
        status=st.empty(); started=time.time()
        try:
            if period=="FULL 2025": ranges_to_get=list(QUARTERS.items())[4:8]
            elif period in ("2026 YTD","FULL 2026 AVAILABLE"): ranges_to_get=list(QUARTERS.items())[8:11]
            elif period=="Custom": ranges_to_get=[("Custom",(custom_start,custom_end))]
            else: ranges_to_get=[(period,ranges[period])]
            parts=[]; all_failed=[]
            for qi,(label,(qs,qe)) in enumerate(ranges_to_get):
                base=qi/len(ranges_to_get); span=1/len(ranges_to_get)
                def update_progress(p,base=base,span=span,label=label):
                    pct=min(99.9,(base+p*span)*100); gauge.progress(pct/100,text=f"FRIDAY Data Vault: {pct:.1f}% — {label}")
                def update_status(msg,label=label): status.info(f"{label}: {msg} | elapsed {int(time.time()-started)}s")
                status.info(f"{label}: starting")
                if dataset=="NIFTY Weekly Options ATM±10":
                    part=option_quarter_download(label,qs,qe,token,client_id,update_progress,update_status); all_failed.extend(part.attrs.get("failed_requests",[]))
                else:
                    part=download_spot_or_vix("NIFTY" if dataset=="NIFTY Spot" else "INDIA VIX",qs,qe,timeframe,token,client_id,update_progress); part["period"]=label
                parts.append(part); gauge.progress((qi+1)/len(ranges_to_get),text=f"FRIDAY Data Vault: {(qi+1)/len(ranges_to_get)*100:.1f}% — {label} complete")
            if len(parts)==1:
                out_df=parts[0]; safe=period.replace(" ","_").replace("±","PLUS_MINUS")
                report_text,report_safe=build_data_report(period,dataset,out_df,all_failed)
                report_bytes=report_text.encode("utf-8"); csv_bytes=out_df.to_csv(index=False).encode("utf-8")
                package=io.BytesIO()
                with zipfile.ZipFile(package,"w",zipfile.ZIP_DEFLATED) as z:
                    z.writestr(f"{safe}_{dataset.replace(' ','_').replace('±','PLUS_MINUS')}.csv",csv_bytes)
                    z.writestr(f"FRIDAY_{report_safe}_DATA_REPORT.md",report_bytes)
                    if all_failed: z.writestr(f"FRIDAY_{report_safe}_FAILED_REQUESTS.csv",pd.DataFrame(all_failed).to_csv(index=False))
                st.success(f"Download complete — {len(out_df):,} rows")
                st.dataframe(out_df.head(500),use_container_width=True,hide_index=True)
                c1,c2,c3=st.columns(3)
                with c1: st.download_button("DOWNLOAD CSV",csv_bytes,f"FRIDAY_{safe}_{dataset.replace(' ','_').replace('±','PLUS_MINUS')}.csv","text/csv",use_container_width=True)
                with c2: st.download_button("DOWNLOAD REPORT (.MD)",report_bytes,f"FRIDAY_{report_safe}_DATA_REPORT.md","text/markdown",use_container_width=True)
                with c3: st.download_button("DOWNLOAD COMPLETE PACKAGE (.ZIP)",package.getvalue(),f"FRIDAY_{report_safe}_COMPLETE_RESEARCH_PACKAGE.zip","application/zip",use_container_width=True)
                if all_failed:
                    st.warning(f"Completed with {len(all_failed)} failed API requests. The report/package contains the failure log.")
            else:
                out=io.BytesIO()
                with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
                    for label,part in zip([x[0] for x in ranges_to_get],parts):
                        safe=label.replace(" ","_"); report_text,report_safe=build_data_report(label,dataset,part,part.attrs.get("failed_requests",[]))
                        z.writestr(f"{safe}_{dataset.replace(' ','_').replace('±','PLUS_MINUS')}.csv",part.to_csv(index=False)); z.writestr(f"FRIDAY_{report_safe}_DATA_REPORT.md",report_text)
                    if all_failed: z.writestr("FAILED_REQUESTS.csv",pd.DataFrame(all_failed).to_csv(index=False))
                    z.writestr("README.txt",f"FRIDAY research data package. Period={period}. Options are 1-minute weekly ATM-10..ATM+10 with OI/IV/volume/spot.")
                st.success(f"Quarter-wise package ready — {len(parts)} periods")
                st.download_button("DOWNLOAD COMPLETE QUARTER PACKAGE (.ZIP)",out.getvalue(),f"FRIDAY_{period.replace(' ','_')}_COMPLETE_RESEARCH_PACKAGE.zip","application/zip",use_container_width=True)
            gauge.progress(1.0,text="FRIDAY Data Vault: 100% ✅")
        except Exception as exc:
            status.error(f"Download stopped after {time.time()-started:.0f}s: {exc}")
            st.exception(exc)
            st.warning("No final package was produced because the run was incomplete. Retry only after fixing the exact error shown above.")


def render_analyzer():
    st.title("FRIDAY — OPTION PATTERN RESEARCH")
    st.caption("Upload any quarter; report naming follows the selected period. Q1 2026 is supported.")
    period=st.selectbox("Research Period",["Q1 2024","Q2 2024","Q3 2024","Q4 2024","Q1 2025","Q2 2025","Q3 2025","Q1 2026","Q2 2026","Q3 2026","Full 2024","Full 2025","2026 YTD","Custom"])
    custom_start=st.date_input("Custom start",value=date(2025,1,1)); custom_end=st.date_input("Custom end",value=date(2025,3,31))
    st.caption(f"Selected period: {custom_start} → {custom_end}")
    st.info("Use Data Vault to download the scraped Q1 2026 dataset and its complete research report/package. The autonomous AI research engine is not enabled yet.")

with st.sidebar:
    st.subheader("FRIDAY")
    client_id=st.text_input("Dhan Client ID",value=DEFAULT_CLIENT_ID).strip()
    token=st.text_input("Dhan Access Token",value="",type="password").strip()
    if token:
        ok,msg=dhan_profile(token,client_id); (st.success if ok else st.error)(msg)
    module=st.radio("MODULE",["Data Vault","Pattern Research"],index=0)

if module=="Data Vault": render_data_vault(token,client_id)
else: render_analyzer()
