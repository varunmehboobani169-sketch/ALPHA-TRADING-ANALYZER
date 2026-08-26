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
    return {"Accept": "application/json", "Content-Type": "application/json", "access-token": st.session_state.access_token, "client-id": st.session_state.client_id}


def api_post(path, payload, label):
    wait = RATE_LIMIT_SECONDS - (time.monotonic() - st.session_state.last_api_call)
    if wait > 0:
        time.sleep(wait)
    r = requests.post(API + path, headers=headers(), json=payload, timeout=45)
    st.session_state.last_api_call = time.monotonic()
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    if r.status_code == 429:
        time.sleep(3.5)
        r = requests.post(API + path, headers=headers(), json=payload, timeout=45)
        st.session_state.last_api_call = time.monotonic()
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
    if not r.ok:
        raise RuntimeError(f"{label}: HTTP {r.status_code}: {body.get('errorMessage') or body.get('remarks') or body.get('message') or str(body)[:500]}")
    return body


def parse_data(body):
    return body.get("data", body) if isinstance(body, dict) else {}


def parse_datetime(values):
    s = pd.Series(values)
    if s.empty:
        return pd.Series(pd.NaT, dtype="datetime64[ns]")
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        if med >= 1e18: unit = "ns"
        elif med >= 1e15: unit = "us"
        elif med >= 1e12: unit = "ms"
        elif med >= 1e9: unit = "s"
        else: unit = None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(s, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).astype("datetime64[ns]")


def normalize_time(df):
    if df.empty:
        return df
    cols = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    candidates = ["timestamp", "datetime", "date_time", "timestamp_ist", "datetime_ist", "exchange_timestamp", "trade_time", "time", "date"]
    col = next((cols[c] for c in candidates if c in cols), None)
    if col is None:
        raise ValueError(f"No timestamp column found. Columns: {list(df.columns)}")
    out = df.copy()
    out["timestamp"] = parse_datetime(out[col])
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def find_col(df, candidates):
    cols = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for c in candidates:
        if c.lower().replace(" ", "_") in cols:
            return cols[c.lower().replace(" ", "_")]
    for norm, orig in cols.items():
        if any(c.lower().replace(" ", "_") in norm for c in candidates):
            return orig
    return None


def num(df, candidates):
    c = find_col(df, candidates)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def read_csvs(files):
    frames = []
    for f in files or []:
        df = pd.read_csv(f, low_memory=False)
        if not df.empty:
            df["_source_file"] = getattr(f, "name", "uploaded")
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_spot(df):
    x = normalize_time(df)
    p = num(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if p is None:
        raise ValueError("NIFTY Spot file has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "nifty_spot": p}).dropna().sort_values("timestamp").reset_index(drop=True).astype({"timestamp": "datetime64[ns]"})


def normalize_vix(df):
    x = normalize_time(df)
    p = num(x, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if p is None:
        raise ValueError("India VIX file has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "vix_close": p}).dropna().sort_values("timestamp").reset_index(drop=True).astype({"timestamp": "datetime64[ns]"})


def normalize_expiry(values):
    s = pd.Series(values)
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        if med >= 1e12:
            dt = pd.to_datetime(n, unit="ms", errors="coerce")
        elif med >= 1e9:
            dt = pd.to_datetime(n, unit="s", errors="coerce")
        else:
            dt = pd.to_datetime(s, errors="coerce")
    else:
        dt = pd.to_datetime(s, errors="coerce")
    return dt.dt.date


def normalize_options(df):
    x = normalize_time(df)
    strike = find_col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = find_col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike is None or side is None:
        raise ValueError(f"Options need timestamp, strike and CE/PE type. Columns: {list(df.columns)}")
    r = pd.DataFrame({"timestamp": x.timestamp, "strike": pd.to_numeric(x[strike], errors="coerce"), "side": x[side].astype(str).str.upper().str.strip()})
    r["side"] = r.side.replace({"C":"CE", "CALL":"CE", "P":"PE", "PUT":"PE"})
    r["expiry"] = normalize_expiry(x[expiry]) if expiry else pd.NaT
    for names, target in [(["close", "ltp", "last_price", "price"], "close"), (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"), (["oi", "open_interest", "openinterest"], "oi"), (["volume", "vol", "traded_volume"], "volume")]:
        v = num(x, names)
        r[target] = v if v is not None else np.nan
    r = r.dropna(subset=["timestamp", "strike"])
    r = r[r.side.isin(["CE", "PE"])]
    return r.sort_values("timestamp").reset_index(drop=True).astype({"timestamp": "datetime64[ns]"})


def synchronize(options, spot, vix=None):
    opt = options.copy().sort_values("timestamp")
    sp = spot[["timestamp", "nifty_spot"]].copy().sort_values("timestamp")
    opt["timestamp"] = pd.to_datetime(opt.timestamp, errors="coerce").astype("datetime64[ns]")
    sp["timestamp"] = pd.to_datetime(sp.timestamp, errors="coerce").astype("datetime64[ns]")
    opt = opt.dropna(subset=["timestamp"])
    sp = sp.dropna(subset=["timestamp"])
    merged = pd.merge_asof(opt, sp, on="timestamp", direction="backward", tolerance=pd.Timedelta("20min"))
    merged = merged.dropna(subset=["nifty_spot"])
    if vix is not None and not vix.empty and not merged.empty:
        vx = vix[["timestamp", "vix_close"]].copy().sort_values("timestamp")
        vx["timestamp"] = pd.to_datetime(vx.timestamp, errors="coerce").astype("datetime64[ns]")
        merged = pd.merge_asof(merged.sort_values("timestamp"), vx, on="timestamp", direction="backward", tolerance=pd.Timedelta("20min"))
    return merged.sort_values("timestamp").reset_index(drop=True)


def build_features(merged):
    if merged.empty:
        return pd.DataFrame()
    rows = []
    for ts, g in merged.groupby("timestamp", sort=True):
        valid_exp = [e for e in g.expiry.dropna().unique() if e >= ts.date()]
        if valid_exp:
            g = g[g.expiry == min(valid_exp)]
        if g.empty:
            continue
        spot_px = float(pd.to_numeric(g.nifty_spot, errors="coerce").dropna().iloc[0])
        strikes = pd.to_numeric(g.strike, errors="coerce").dropna().unique()
        pairs = []
        for strike in strikes:
            z = g[np.isclose(g.strike.astype(float), float(strike))]
            if {"CE", "PE"}.issubset(set(z.side)):
                pairs.append(float(strike))
        if not pairs:
            continue
        atm = min(pairs, key=lambda s: abs(s - spot_px))
        a = g[np.isclose(g.strike.astype(float), atm)]
        ce, pe = a[a.side == "CE"].iloc[-1], a[a.side == "PE"].iloc[-1]
        call_oi = pd.to_numeric(g.loc[g.side == "CE", "oi"], errors="coerce").sum(min_count=1)
        put_oi = pd.to_numeric(g.loc[g.side == "PE", "oi"], errors="coerce").sum(min_count=1)
        pcr = put_oi / call_oi if pd.notna(call_oi) and call_oi else np.nan
        rows.append({
            "timestamp": ts, "nifty_spot": spot_px, "atm_strike": atm,
            "ce_close": ce.get("close", np.nan), "pe_close": pe.get("close", np.nan),
            "ce_iv": ce.get("iv", np.nan), "pe_iv": pe.get("iv", np.nan),
            "ce_oi": ce.get("oi", np.nan), "pe_oi": pe.get("oi", np.nan),
            "ce_volume": ce.get("volume", np.nan), "pe_volume": pe.get("volume", np.nan),
            "pcr_oi": pcr,
            "straddle": ce.get("close", np.nan) + pe.get("close", np.nan),
            "atm_iv": np.nanmean([ce.get("iv", np.nan), pe.get("iv", np.nan)]),
            "vix_close": g.vix_close.dropna().iloc[-1] if "vix_close" in g and g.vix_close.notna().any() else np.nan,
        })
    f = pd.DataFrame(rows)
    if f.empty:
        return f
    f["timestamp"] = pd.to_datetime(f.timestamp).astype("datetime64[ns]")
    f = f.sort_values("timestamp").reset_index(drop=True)
    f["spot_ret_1"] = f.nifty_spot.pct_change(); f["spot_ret_4"] = f.nifty_spot.pct_change(4); f["spot_ret_16"] = f.nifty_spot.pct_change(16)
    f["spot_vol_8"] = f.spot_ret_1.rolling(8).std(); f["spot_ma_8"] = f.nifty_spot.rolling(8).mean(); f["spot_ma_32"] = f.nifty_spot.rolling(32).mean(); f["spot_trend"] = f.spot_ma_8 - f.spot_ma_32
    f["straddle_change"] = f.straddle.diff(); f["straddle_ret"] = f.straddle.pct_change(); f["iv_change"] = f.atm_iv.diff(); f["vix_change"] = f.vix_close.diff(); f["vix_ret"] = f.vix_close.pct_change(); f["pcr_change"] = f.pcr_oi.diff()
    f["forward_spot_4"] = f.nifty_spot.shift(-4) / f.nifty_spot - 1; f["forward_spot_16"] = f.nifty_spot.shift(-16) / f.nifty_spot - 1
    f["forward_straddle_4"] = f.straddle.shift(-4) / f.straddle - 1; f["forward_straddle_16"] = f.straddle.shift(-16) / f.straddle - 1
    return f


def discover_patterns(f):
    rules = [
        ("IV rising + spot flat", (f.iv_change > 0) & (f.spot_ret_4.abs() < 0.001)),
        ("IV falling + spot flat", (f.iv_change < 0) & (f.spot_ret_4.abs() < 0.001)),
        ("PCR rising", f.pcr_change > 0), ("PCR falling", f.pcr_change < 0),
        ("VIX rising", f.vix_change > 0), ("VIX falling", f.vix_change < 0),
        ("Straddle expanding", f.straddle_change > 0), ("Straddle contracting", f.straddle_change < 0),
        ("Spot uptrend", f.spot_trend > 0), ("Spot downtrend", f.spot_trend < 0),
    ]
    rows = []
    for name, mask in rules:
        d = f.loc[mask].dropna(subset=["forward_spot_4", "forward_straddle_4"])
        if len(d) >= 10:
            rows.append({"pattern": name, "observations": len(d), "avg_next_4_spot_pct": d.forward_spot_4.mean(), "avg_next_16_spot_pct": d.forward_spot_16.mean(), "avg_next_4_straddle_pct": d.forward_straddle_4.mean(), "avg_next_16_straddle_pct": d.forward_straddle_16.mean(), "next_4_spot_up_rate": (d.forward_spot_4 > 0).mean(), "next_4_straddle_up_rate": (d.forward_straddle_4 > 0).mean()})
    return pd.DataFrame(rows).sort_values("observations", ascending=False) if rows else pd.DataFrame()


def diagnostics(options, spot, vix, merged, features):
    return pd.DataFrame([
        ["Option rows", len(options)], ["Spot rows", len(spot)], ["VIX rows", len(vix)], ["Synchronized rows", len(merged)], ["ATM observations", len(features)],
        ["CE rows", int((options.side == "CE").sum()) if not options.empty else 0], ["PE rows", int((options.side == "PE").sum()) if not options.empty else 0],
        ["Option start", str(options.timestamp.min()) if not options.empty else "N/A"], ["Option end", str(options.timestamp.max()) if not options.empty else "N/A"],
        ["Spot start", str(spot.timestamp.min()) if not spot.empty else "N/A"], ["Spot end", str(spot.timestamp.max()) if not spot.empty else "N/A"],
    ], columns=["Check", "Value"])


def resolve_vix_id():
    try:
        m = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master-detailed.csv", low_memory=False)
        cols = {str(c).strip().upper(): c for c in m.columns}; sid = cols.get("SECURITY_ID")
        for c in [cols.get("SYMBOL_NAME"), cols.get("DISPLAY_NAME"), cols.get("UNDERLYING_SYMBOL")]:
            if sid and c:
                mask = m[c].astype(str).str.upper().str.replace(" ", "", regex=False).str.contains("INDIAVIX", na=False)
                if mask.any():
                    value = pd.to_numeric(m.loc[mask, sid].iloc[0], errors="coerce")
                    if pd.notna(value): return int(value)
    except Exception:
        pass
    return VIX_ID_FALLBACK


def download_index_quarter(dataset, year, quarter, timeframe):
    sid = NIFTY_ID if dataset == "NIFTY Spot" else resolve_vix_id(); q = pd.Period(f"{year}-Q{quarter}")
    start, end = pd.Timestamp(q.start_time), pd.Timestamp(q.end_time)
    if timeframe == "Daily":
        body = api_post("/charts/historical", {"securityId": str(sid), "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0, "oi": False, "fromDate": start.strftime("%Y-%m-%d"), "toDate": end.strftime("%Y-%m-%d")}, f"{dataset} {year} Q{quarter}")
        return parse_data(body)
    interval = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60}[timeframe]
    parts=[]; cur=start
    while cur <= end:
        ce=min(cur+pd.Timedelta(days=89), end)
        body=api_post("/charts/intraday", {"securityId":str(sid),"exchangeSegment":"IDX_I","instrument":"INDEX","interval":interval,"oi":False,"fromDate":cur.strftime("%Y-%m-%d %H:%M:%S"),"toDate":ce.strftime("%Y-%m-%d %H:%M:%S")}, f"{dataset} {year} Q{quarter}")
        d=parse_data(body)
        if d.get("timestamp"): parts.append(pd.DataFrame({"timestamp":parse_datetime(d["timestamp"]), "open":d.get("open"), "high":d.get("high"), "low":d.get("low"), "close":d.get("close"), "volume":d.get("volume")}))
        cur=ce+pd.Timedelta(seconds=1)
    if not parts: return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def render_vault():
    st.markdown("## DATA VAULT"); st.caption("NIFTY Spot and India VIX only. Futures are intentionally out of scope.")
    years=list(range(2020, now_ist().year+1)); c1,c2,c3=st.columns(3)
    with c1: dataset=st.selectbox("Dataset", ["NIFTY Spot", "India VIX"])
    with c2: year=st.selectbox("Year", years, index=years.index(2024) if 2024 in years else len(years)-1)
    with c3: quarter=st.selectbox("Quarter", [1,2,3,4], index=0)
    timeframe=st.selectbox("Timeframe", ["15-minute","1-minute","5-minute","25-minute","60-minute","Daily"], index=0)
    if st.button("DOWNLOAD QUARTER", use_container_width=True):
        try:
            df=download_index_quarter(dataset,year,quarter,timeframe)
            if isinstance(df,dict): df=pd.DataFrame(df)
            if df.empty: st.error("No data returned.")
            else:
                st.success(f"Downloaded {len(df):,} rows.")
                st.download_button("DOWNLOAD CSV", pd.DataFrame(df).to_csv(index=False).encode(), f"FRIDAY_{dataset.replace(' ','_')}_{year}_Q{quarter}_{timeframe}.csv", "text/csv", use_container_width=True)
                st.dataframe(pd.DataFrame(df).head(300), use_container_width=True, hide_index=True)
        except Exception as exc: st.error(str(exc))


def render_ai():
    st.markdown("## FRIDAY — OPTION PATTERN RESEARCH")
    st.write("Options + NIFTY Spot + India VIX only. Futures are removed from the design.")
    opt=st.file_uploader("Option Data (CSV)", type=["csv"], accept_multiple_files=True)
    spot=st.file_uploader("NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True)
    vix=st.file_uploader("India VIX Data (CSV)", type=["csv"], accept_multiple_files=True)
    if not (opt and spot):
        st.info("Upload the Q1 Option + Spot data. Add Q1 India VIX for full context.")
        return
    if st.button("ANALYZE PATTERNS", use_container_width=True):
        try:
            options=normalize_options(read_csvs(opt)); spot_df=normalize_spot(read_csvs(spot)); vix_df=normalize_vix(read_csvs(vix)) if vix else pd.DataFrame()
            merged=synchronize(options,spot_df,vix_df); features=build_features(merged)
            st.subheader("Input Diagnostics"); st.dataframe(diagnostics(options,spot_df,vix_df,merged,features),use_container_width=True,hide_index=True)
            st.write(f"Synchronized option/spot rows: **{len(merged):,}**")
            if merged.empty:
                st.error("No option/spot timestamps overlap within 20 minutes. Check the diagnostics for date ranges and timestamp parsing.")
                return
            if features.empty:
                st.error("Spot synchronization worked, but no timestamp contained both CE and PE for a valid ATM strike. Check option type/strike columns.")
                return
            patterns=discover_patterns(features); st.session_state.analysis=features
            st.success(f"Analyzed {len(features):,} ATM observations.")
            if patterns.empty: st.warning("No pattern had at least 10 usable forward observations.")
            else: st.subheader("Pattern Summary"); st.dataframe(patterns,use_container_width=True,hide_index=True)
            st.subheader("Feature Data"); st.dataframe(features.tail(500),use_container_width=True,hide_index=True)
            b=io.BytesIO()
            with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv",features.to_csv(index=False)); z.writestr("FRIDAY_pattern_summary.csv",patterns.to_csv(index=False)); z.writestr("FRIDAY_diagnostics.csv",diagnostics(options,spot_df,vix_df,merged,features).to_csv(index=False))
            b.seek(0); st.download_button("DOWNLOAD ANALYSIS ZIP",b.getvalue(),"FRIDAY_Q1_PATTERN_ANALYSIS.zip","application/zip",use_container_width=True)
        except Exception as exc: st.exception(exc)


def inject_css():
    st.markdown("<style>.stApp{background:linear-gradient(180deg,#05080d,#080d15)} .block-container{max-width:1500px;padding-top:1rem}</style>", unsafe_allow_html=True)

init_state(); inject_css()
with st.sidebar:
    st.title("FRIDAY"); st.caption("Option Pattern Research Engine")
    st.session_state.client_id=st.text_input("Dhan Client ID", value=st.session_state.client_id or DEFAULT_CLIENT_ID).strip()
    st.session_state.access_token=st.text_input("Dhan Access Token", value=st.session_state.access_token, type="password").strip()
view=st.radio("MODULE", ["AI Strategist","Data Vault"], horizontal=True)
if view == "Data Vault": render_vault()
else: render_ai()
