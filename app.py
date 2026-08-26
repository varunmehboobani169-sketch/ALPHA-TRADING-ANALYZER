# FRIDAY Cloud-safe launcher
# NIFTY Futures Contract-wise Historical Builder
import re
import requests

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/361848fd675ecda841c8a6564c9f2caa4d57967c/app.py"
source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()
text = source.text

if "import zipfile" not in text[:3000]:
    text = text.replace("import math\n", "import math\nimport zipfile\n", 1)

# Keep all ZIP generation cloud-safe by building downloads in memory.
text = re.sub(
    r'\s*buf = Path\("/mnt/data"\).*?with zipfile\.ZipFile\(buf, "w", zipfile\.ZIP_DEFLATED\) as z:',
    '''\n            from io import BytesIO\n            zip_buffer = BytesIO()\n            zip_name = f"FRIDAY_{ds.replace(' ','_')}_quarters.zip"\n            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:''',
    text, count=1, flags=re.S
)
text = text.replace("data=buf.read_bytes(),", "data=zip_buffer.getvalue(),")
text = text.replace("file_name=buf.name,", "file_name=zip_name,")
text = text.replace(
    'path = Path("/mnt/data/friday_quarterly_reports.zip")\n\n    master_rows = []\n    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:',
    'from io import BytesIO\n    zip_buffer = BytesIO()\n\n    master_rows = []\n    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:'
)
text = text.replace(
    '    return path\n\n\ndef _read_quarterly_zip_files(files):',
    '    zip_buffer.seek(0)\n    return zip_buffer\n\n\ndef _read_quarterly_zip_files(files):'
)
text = text.replace("data=selected_zip.read_bytes(),", "data=selected_zip.getvalue(),")
text = text.replace("data=all_zip.read_bytes(),", "data=all_zip.getvalue(),")

new_vault = r'''
def _friday_master_contracts(master, year, quarter):
    """Find all NIFTY futures contracts relevant to the selected quarter."""
    x = master.copy()
    if "security_id" not in x.columns:
        return pd.DataFrame()
    x["security_id"] = pd.to_numeric(x["security_id"], errors="coerce")
    x = x.dropna(subset=["security_id"]).copy()

    expiry_col = next((c for c in x.columns if "EXPIRY" in str(c).upper()), None)
    if expiry_col is None:
        return pd.DataFrame()
    x["_expiry"] = pd.to_datetime(x[expiry_col], errors="coerce")
    x = x.dropna(subset=["_expiry"])

    text_cols = [c for c in x.columns if x[c].dtype == "object"]
    if text_cols:
        blob = x[text_cols].fillna("").astype(str).agg(" ".join, axis=1).str.upper()
    else:
        blob = pd.Series("", index=x.index)
    nifty = blob.str.contains("NIFTY", regex=False, na=False)
    fut = blob.str.contains(r"FUTIDX|FUTURE|FUT", regex=True, na=False)
    y = x[nifty & fut].copy()

    if y.empty:
        mask = pd.Series(False, index=x.index)
        for c in ["underlying_symbol", "symbol_name", "trading_symbol", "display_name"]:
            if c in x.columns:
                s = x[c].fillna("").astype(str).str.upper().str.strip()
                mask |= s.eq("NIFTY") | s.str.startswith("NIFTY", na=False)
        if "instrument" in x.columns:
            ins = x["instrument"].fillna("").astype(str).str.upper()
            mask &= ins.str.contains("FUT", regex=False, na=False)
        y = x[mask].copy()
    if y.empty:
        return pd.DataFrame()

    q = pd.Period(f"{int(year)}-Q{int(quarter)}")
    qs, qe = pd.Timestamp(q.start_time.date()), pd.Timestamp(q.end_time.date())
    lo, hi = qs - pd.Timedelta(days=45), qe + pd.Timedelta(days=45)
    y = y[(y["_expiry"].dt.normalize() >= lo) & (y["_expiry"].dt.normalize() <= hi)].copy()

    cols = ["security_id", "_expiry"]
    for c in ["trading_symbol", "symbol_name", "display_name", "instrument", "exchange", "segment"]:
        if c in y.columns:
            cols.append(c)
    y = y[cols].drop_duplicates(["security_id", "_expiry"]).sort_values("_expiry")
    y["expiry"] = y["_expiry"].dt.strftime("%Y-%m-%d")
    y["contract"] = y["trading_symbol"].fillna("").astype(str) if "trading_symbol" in y.columns else ""
    y.loc[y["contract"].eq(""), "contract"] = "NIFTY FUT " + y["expiry"]
    y["status"] = np.where(y["_expiry"].dt.date < now_ist().date(), "EXPIRED", "ACTIVE")
    return y.reset_index(drop=True)


def _friday_futures_download(security_id, timeframe, start_dt, end_dt, include_oi, label):
    interval_map = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60}
    interval = interval_map.get(timeframe, 15)
    chunks, cur, end = [], pd.Timestamp(start_dt), pd.Timestamp(end_dt)
    while cur <= end:
        chunk_end = min(cur + pd.Timedelta(days=89), end)
        payload = {
            "securityId": str(int(security_id)), "exchangeSegment": "NSE_FNO",
            "instrument": "FUTIDX", "interval": interval, "oi": bool(include_oi),
            "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            body = api_post("/charts/intraday", payload, label)
            df = parse_chart_response(body)
            if not df.empty:
                chunks.append(df)
        except Exception as exc:
            return pd.DataFrame(), str(exc)
        cur = chunk_end + pd.Timedelta(seconds=1)
    if not chunks:
        return pd.DataFrame(), "Dhan returned no candles for this contract/time range."
    return pd.concat(chunks, ignore_index=True).drop_duplicates("datetime").sort_values("datetime"), ""


def render_data_vault():
    st.markdown("""<div class='hero'><h1>DATA VAULT</h1><p>Contract-wise NIFTY Futures / Spot / India VIX historical data builder</p></div>""", unsafe_allow_html=True)
    if "client_id" not in st.session_state or "access_token" not in st.session_state:
        init_state()

    dataset = st.selectbox("Dataset", ["NIFTY Futures", "NIFTY Spot", "India VIX"], key="vault_dataset")
    c1, c2, c3 = st.columns(3)
    with c1:
        years = list(range(2020, now_ist().year + 1))
        default_year = years.index(2024) if 2024 in years else len(years) - 1
        year = st.selectbox("Year", years, index=default_year, key="vault_year")
    with c2:
        quarter = st.selectbox("Quarter", [1, 2, 3, 4], index=0, key="vault_quarter")
    with c3:
        timeframe = st.selectbox("Timeframe", ["1-minute", "5-minute", "15-minute", "25-minute", "60-minute", "Daily"], index=2, key="vault_tf")
    include_oi = st.checkbox("Include OI", value=False, key="vault_oi")

    if dataset == "NIFTY Futures":
        st.info("NIFTY Futures are now handled contract-wise. FRIDAY identifies the exact expiry contracts, shows their Security IDs, and never substitutes the current future for an expired one.")
        try:
            master = load_master()
        except Exception as exc:
            st.error(f"Unable to load Dhan instrument master: {exc}")
            return
        contracts = _friday_master_contracts(master, year, quarter)
        if contracts.empty:
            st.error(f"No NIFTY Futures contracts were found in the Dhan instrument master for {year} Q{quarter}.")
            return

        st.subheader("Detected NIFTY Futures Contracts")
        show_cols = [c for c in ["contract", "expiry", "security_id", "status"] if c in contracts.columns]
        st.dataframe(contracts[show_cols], use_container_width=True, hide_index=True)
        choices = [f"{r.contract} | Expiry {r.expiry} | ID {int(r.security_id)} | {r.status}" for r in contracts.itertuples()]
        selected = st.multiselect("Contracts to download", choices, default=choices, key="vault_contracts")
        selected_rows = [contracts.iloc[choices.index(choice)] for choice in selected]

        b1, b2 = st.columns(2)
        download_clicked = b1.button("⬇️ DOWNLOAD SELECTED CONTRACTS", use_container_width=True)
        if b2.button("⬇️ DOWNLOAD ALL CONTRACTS", use_container_width=True):
            selected_rows = [contracts.iloc[i] for i in range(len(contracts))]
            download_clicked = True

        if download_clicked:
            if not selected_rows:
                st.warning("Select at least one contract.")
                return
            q = pd.Period(f"{year}-Q{quarter}")
            from io import BytesIO
            zip_buffer = BytesIO()
            manifest = []
            successful = 0
            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for row in selected_rows:
                    sid = int(row.security_id)
                    expiry = pd.Timestamp(row._expiry)
                    start_dt = max(pd.Timestamp(q.start_time), expiry - pd.Timedelta(days=120))
                    end_dt = min(pd.Timestamp(q.end_time), expiry + pd.Timedelta(days=1))
                    with st.spinner(f"Downloading {row.contract} (ID {sid})..."):
                        if timeframe == "Daily":
                            try:
                                body = api_post("/charts/historical", {
                                    "securityId": str(sid), "exchangeSegment": "NSE_FNO", "instrument": "FUTIDX",
                                    "expiryCode": 0, "oi": bool(include_oi),
                                    "fromDate": start_dt.strftime("%Y-%m-%d"), "toDate": end_dt.strftime("%Y-%m-%d")
                                }, f"{row.contract} daily")
                                df = parse_chart_response(body)
                                err = "" if not df.empty else "Dhan returned no candles."
                            except Exception as exc:
                                df, err = pd.DataFrame(), str(exc)
                        else:
                            df, err = _friday_futures_download(sid, timeframe, start_dt, end_dt, include_oi, str(row.contract))
                    fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row.contract)) + f"_{year}_Q{quarter}_{timeframe.replace('-', '')}.csv"
                    if not df.empty:
                        df["contract"], df["expiry"], df["security_id"] = str(row.contract), row.expiry, sid
                        z.writestr(fname, df.to_csv(index=False).encode("utf-8"))
                        successful += 1
                        manifest.append({"contract": str(row.contract), "expiry": row.expiry, "security_id": sid, "status": "DOWNLOADED", "rows": len(df), "error": ""})
                    else:
                        manifest.append({"contract": str(row.contract), "expiry": row.expiry, "security_id": sid, "status": "UNAVAILABLE", "rows": 0, "error": err})
                z.writestr(f"NIFTY_FUTURES_{year}_Q{quarter}_MANIFEST.csv", pd.DataFrame(manifest).to_csv(index=False).encode("utf-8"))
            zip_buffer.seek(0)
            st.dataframe(pd.DataFrame(manifest), use_container_width=True, hide_index=True)
            st.download_button("⬇️ DOWNLOAD CONTRACT-WISE ZIP", data=zip_buffer.getvalue(), file_name=f"FRIDAY_NIFTY_FUTURES_{year}_Q{quarter}_{timeframe}.zip", mime="application/zip", use_container_width=True)
            if successful == 0:
                st.warning("No selected futures contract returned candles from Dhan. The manifest preserves the exact contract IDs and errors; FRIDAY will not substitute another contract.")
        return

    st.info("NIFTY Spot and India VIX continue to use the Dhan historical API with quarter-safe chunking.")
    if st.button("⬇️ DOWNLOAD QUARTER", use_container_width=True, key="vault_basic_download"):
        try:
            master = load_master()
            sid = NIFTY_ID if dataset == "NIFTY Spot" else resolve_vix_id(master)
            q = pd.Period(f"{year}-Q{quarter}")
            start_dt, end_dt = pd.Timestamp(q.start_time), pd.Timestamp(q.end_time)
            segment, instrument = "IDX_I", "INDEX"
            if timeframe == "Daily":
                body = api_post("/charts/historical", {"securityId": str(int(sid)), "exchangeSegment": segment, "instrument": instrument, "expiryCode": 0, "oi": bool(include_oi), "fromDate": start_dt.strftime("%Y-%m-%d"), "toDate": end_dt.strftime("%Y-%m-%d")}, f"{dataset} {year} Q{quarter}")
                df = parse_chart_response(body)
            else:
                interval = {"1-minute":1,"5-minute":5,"15-minute":15,"25-minute":25,"60-minute":60}.get(timeframe,15)
                frames=[]; cur=start_dt
                while cur <= end_dt:
                    ce=min(cur+pd.Timedelta(days=89),end_dt)
                    body=api_post("/charts/intraday", {"securityId":str(int(sid)),"exchangeSegment":segment,"instrument":instrument,"interval":interval,"oi":bool(include_oi),"fromDate":cur.strftime("%Y-%m-%d %H:%M:%S"),"toDate":ce.strftime("%Y-%m-%d %H:%M:%S")}, f"{dataset} {year} Q{quarter}")
                    part=parse_chart_response(body)
                    if not part.empty: frames.append(part)
                    cur=ce+pd.Timedelta(seconds=1)
                df=pd.concat(frames,ignore_index=True).drop_duplicates("datetime").sort_values("datetime") if frames else pd.DataFrame()
            if df.empty:
                st.error("No data returned for the requested quarter.")
            else:
                st.success(f"Downloaded {len(df):,} rows.")
                st.download_button("⬇️ DOWNLOAD CSV", data=df.to_csv(index=False).encode("utf-8"), file_name=f"FRIDAY_{dataset.replace(' ','_')}_{year}_Q{quarter}_{timeframe}.csv", mime="text/csv", use_container_width=True)
        except Exception as exc:
            st.error(str(exc))
'''
pattern = r'\ndef render_data_vault\(\):.*?(?=\n(?:def |if __name__|render_data_vault\(\)))'
text, n = re.subn(pattern, new_vault, text, count=1, flags=re.S)
if n != 1:
    raise RuntimeError(f"render_data_vault replacement failed: {n}")
exec(compile(text, "friday_app.py", "exec"), globals(), globals())
