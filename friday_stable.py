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
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": st.session_state.access_token,
        "client-id": st.session_state.client_id,
    }


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
        raise RuntimeError(
            f"{label}: HTTP {r.status_code}: "
            f"{body.get('errorMessage') or body.get('remarks') or body.get('message') or str(body)[:500]}"
        )
    return body


def parse_data(body):
    return body.get("data", body) if isinstance(body, dict) else {}


def parse_datetime(values):
    s = pd.Series(values)
    if s.empty:
        return pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(s, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).astype("datetime64[ns]")


def norm_time(df):
    if df.empty:
        return df
    cols = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    candidates = ["timestamp", "datetime", "date_time", "timestamp_ist", "datetime_ist", "exchange_timestamp", "trade_time", "time", "date"]
    col = next((cols[c] for c in candidates if c in cols), None)
    if col is None:
        raise ValueError(f"No timestamp column found. Columns: {list(df.columns)}")
    out = df.copy()
    out["timestamp"] = parse_datetime(out[col])
    out = out.dropna(subset=["timestamp"])
    return out.sort_values("timestamp").reset_index(drop=True)


def find_col(df, candidates):
    cols = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for c in candidates:
        k = c.lower().replace(" ", "_")
        if k in cols:
            return cols[k]
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
        d = pd.read_csv(f, low_memory=False)
        if not d.empty:
            frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_spot(df):
    x = norm_time(df)
    p = num(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if p is None:
        raise ValueError("NIFTY Spot file has no recognizable close/price column.")
    out = pd.DataFrame({"timestamp": x.timestamp, "nifty_spot": p})
    return out.dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    x = norm_time(df)
    p = num(x, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if p is None:
        raise ValueError("India VIX file has no recognizable close/price column.")
    out = pd.DataFrame({"timestamp": x.timestamp, "vix_close": p})
    return out.dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def normalize_expiry(values):
    s = pd.Series(values)
    dt = pd.to_datetime(s, errors="coerce")
    numeric = pd.to_numeric(s, errors="coerce")
    if numeric.notna().mean() > 0.8:
        med = float(numeric.dropna().abs().median()) if numeric.notna().any() else 0
        if med >= 1e12:
            dt = pd.to_datetime(numeric, unit="ms", errors="coerce")
        elif med >= 1e9:
            dt = pd.to_datetime(numeric, unit="s", errors="coerce")
    return dt.dt.date


def normalize_options(df):
    x = norm_time(df)
    strike = find_col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = find_col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike is None or side is None:
        raise ValueError(f"Options need timestamp, strike and CE/PE type. Columns: {list(df.columns)}")
    r = pd.DataFrame({
        "timestamp": x.timestamp,
        "strike": pd.to_numeric(x[strike], errors="coerce"),
        "side": x[side].astype(str).str.upper().str.strip(),
    })
    r["side"] = r["side"].replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
    r["expiry"] = normalize_expiry(x[expiry]) if expiry else pd.Series(pd.NaT, index=x.index)
    for names, target in [
        (["close", "ltp", "last_price", "price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["volume", "vol", "traded_volume"], "volume"),
    ]:
        v = num(x, names)
        r[target] = v if v is not None else np.nan
    r = r.dropna(subset=["timestamp", "strike"])
    r = r[r.side.isin(["CE", "PE"])]
    return r.drop_duplicates(["timestamp", "expiry", "strike", "side"], keep="last").sort_values("timestamp").reset_index(drop=True)


def synchronize(options, spot, vix=None, tol_min=20):
    opt = options.sort_values("timestamp").copy()
    sp = spot[["timestamp", "nifty_spot"]].sort_values("timestamp").copy()
    merged = pd.merge_asof(
        opt, sp, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=tol_min)
    ).dropna(subset=["nifty_spot"])
    if vix is not None and not vix.empty and not merged.empty:
        vx = vix[["timestamp", "vix_close"]].sort_values("timestamp").copy()
        merged = pd.merge_asof(
            merged.sort_values("timestamp"), vx, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=tol_min)
        )
    return merged.sort_values("timestamp").reset_index(drop=True)


def build_features(merged):
    """Vectorized feature builder; avoids per-timestamp/per-strike Python loops."""
    if merged.empty:
        return pd.DataFrame()
    x = merged.copy()
    x["strike_num"] = pd.to_numeric(x["strike"], errors="coerce")
    x = x.dropna(subset=["strike_num", "nifty_spot"])
    if x.empty:
        return pd.DataFrame()

    # Keep only the nearest still-valid expiry at each timestamp where possible.
    x["expiry_sort"] = pd.to_datetime(x["expiry"], errors="coerce")
    stamp_date = x["timestamp"].dt.date
    x = x[x["expiry_sort"].isna() | (x["expiry_sort".dt.date >= stamp_date)]].copy()
    if x.empty:
        return pd.DataFrame()
    min_exp = x.groupby("timestamp")["expiry_sort"].transform("min")
    x = x[x["expiry_sort"].isna() | (x["expiry_sort"] == min_exp)].copy()

    key = ["timestamp", "expiry_sort", "strike_num"]
    # One wide table containing CE/PE prices and all option fields.
    wide = x.pivot_table(
        index=key,
        columns="side",
        values=["close", "iv", "oi", "volume"],
        aggfunc="last",
    ).reset_index()
    flat = []
    for c in wide.columns:
        if isinstance(c, tuple):
            flat.append("_".join(str(v) for v in c if str(v) != ""))
        else:
            flat.append(str(c))
    wide.columns = flat
    if "close_CE" not in wide or "close_PE" not in wide:
        return pd.DataFrame()
    wide = wide.dropna(subset=["close_CE", "close_PE"])
    if wide.empty:
        return pd.DataFrame()

    spot_by_key = x.groupby(key, dropna=False)["nifty_spot"].first().reset_index()
    wide = wide.merge(spot_by_key, on=key, how="left")
    wide["atm_dist"] = (wide["strike_num"] - wide["nifty_spot"]).abs()
    wide = wide.sort_values(["timestamp", "atm_dist"])
    atm = wide.drop_duplicates("timestamp", keep="first").copy()

    oi = x.pivot_table(index="timestamp", columns="side", values="oi", aggfunc="sum")
    oi = oi.reindex(atm.timestamp)
    ce_oi = oi["CE"] if "CE" in oi else pd.Series(np.nan, index=oi.index)
    pe_oi = oi["PE"] if "PE" in oi else pd.Series(np.nan, index=oi.index)

    f = pd.DataFrame({
        "timestamp": atm.timestamp.values,
        "nifty_spot": atm.nifty_spot.values,
        "atm_strike": atm.strike_num.values,
        "ce_close": atm.get("close_CE", np.nan).values,
        "pe_close": atm.get("close_PE", np.nan).values,
        "ce_iv": atm.get("iv_CE", np.nan).values,
        "pe_iv": atm.get("iv_PE", np.nan).values,
        "ce_oi": atm.get("oi_CE", np.nan).values,
        "pe_oi": atm.get("oi_PE", np.nan).values,
        "ce_volume": atm.get("volume_CE", np.nan).values,
        "pe_volume": atm.get("volume_PE", np.nan).values,
        "pcr_oi": (pe_oi.to_numpy() / ce_oi.to_numpy()),
        "vix_close": atm["vix_close"].values if "vix_close" in atm else np.nan,
    })
    f["straddle"] = f["ce_close"] + f["pe_close"]
    f["atm_iv"] = pd.concat([f["ce_iv"], f["pe_iv"]], axis=1).mean(axis=1)
    f = f.sort_values("timestamp").reset_index(drop=True)

    f["spot_ret_1"] = f.nifty_spot.pct_change()
    f["spot_ret_4"] = f.nifty_spot.pct_change(4)
    f["spot_ret_16"] = f.nifty_spot.pct_change(16)
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
    f["forward_spot_4"] = f.nifty_spot.shift(-4) / f.nifty_spot - 1
    f["forward_spot_16"] = f.nifty_spot.shift(-16) / f.nifty_spot - 1
    f["forward_straddle_4"] = f.straddle.shift(-4) / f.straddle - 1
    f["forward_straddle_16"] = f.straddle.shift(-16) / f.straddle - 1
    return f


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
                "avg_next_4_spot_pct": d.forward_spot_4.mean(),
                "avg_next_16_spot_pct": d.forward_spot_16.mean(),
                "avg_next_4_straddle_pct": d.forward_straddle_4.mean(),
                "avg_next_16_straddle_pct": d.forward_straddle_16.mean(),
                "next_4_spot_up_rate": (d.forward_spot_4 > 0).mean(),
                "next_4_straddle_up_rate": (d.forward_straddle_4 > 0).mean(),
            })
    return pd.DataFrame(rows).sort_values("observations", ascending=False) if rows else pd.DataFrame()


def resolve_vix_id():
    try:
        m = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master-detailed.csv", low_memory=False)
        cols = {str(c).strip().upper(): c for c in m.columns}
        sid = cols.get("SECURITY_ID")
        for c in [cols.get("SYMBOL_NAME"), cols.get("DISPLAY_NAME"), cols.get("UNDERLYING_SYMBOL")]:
            if sid and c:
                mask = m[c].astype(str).str.upper().str.replace(" ", "", regex=False).str.contains("INDIAVIX", na=False)
                if mask.any():
                    v = pd.to_numeric(m.loc[mask, sid].iloc[0], errors="coerce")
                    if pd.notna(v):
                        return int(v)
    except Exception:
        pass
    return VIX_ID_FALLBACK


def download_index_quarter(dataset, year, quarter, timeframe):
    sid = NIFTY_ID if dataset == "NIFTY Spot" else resolve_vix_id()
    q = pd.Period(f"{year}-Q{quarter}")
    start, end = pd.Timestamp(q.start_time), pd.Timestamp(q.end_time)
    if timeframe == "Daily":
        body = api_post("/charts/historical", {
            "securityId": str(sid), "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0, "oi": False,
            "fromDate": start.strftime("%Y-%m-%d"), "toDate": end.strftime("%Y-%m-%d")
        }, f"{dataset} {year} Q{quarter}")
        d = parse_data(body)
        return pd.DataFrame(d) if isinstance(d, dict) else pd.DataFrame()
    interval = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60}[timeframe]
    parts, cur = [], start
    while cur <= end:
        ce = min(cur + pd.Timedelta(days=89), end)
        body = api_post("/charts/intraday", {
            "securityId": str(sid), "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": interval, "oi": False,
            "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"), "toDate": ce.strftime("%Y-%m-%d %H:%M:%S")
        }, f"{dataset} {year} Q{quarter}")
        d = parse_data(body)
        if isinstance(d, dict) and d.get("timestamp"):
            parts.append(pd.DataFrame({
                "timestamp": parse_datetime(d["timestamp"]),
                "open": d.get("open"), "high": d.get("high"), "low": d.get("low"), "close": d.get("close"), "volume": d.get("volume")
            }))
        cur = ce + pd.Timedelta(seconds=1)
    return pd.concat(parts, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True) if parts else pd.DataFrame()


def diagnostics(options, spot, vix, merged, features):
    return pd.DataFrame([
        ["Option rows", len(options)], ["Spot rows", len(spot)], ["VIX rows", len(vix)], ["Synchronized rows", len(merged)], ["ATM observations", len(features)],
        ["CE rows", int((options.side == "CE").sum()) if not options.empty else 0], ["PE rows", int((options.side == "PE").sum()) if not options.empty else 0],
        ["Option start", str(options.timestamp.min()) if not options.empty else "N/A"], ["Option end", str(options.timestamp.max()) if not options.empty else "N/A"],
        ["Spot start", str(spot.timestamp.min()) if not spot.empty else "N/A"], ["Spot end", str(spot.timestamp.max()) if not spot.empty else "N/A"],
    ], columns=["Check", "Value"])


def render_vault():
    st.markdown("## DATA VAULT")
    st.caption("NIFTY Spot and India VIX only. Futures are intentionally out of scope.")
    years = list(range(2020, now_ist().year + 1))
    c1, c2, c3 = st.columns(3)
    with c1: dataset = st.selectbox("Dataset", ["NIFTY Spot", "India VIX"])
    with c2: year = st.selectbox("Year", years, index=years.index(2024) if 2024 in years else len(years)-1)
    with c3: quarter = st.selectbox("Quarter", [1,2,3,4], index=0)
    timeframe = st.selectbox("Timeframe", ["15-minute","1-minute","5-minute","25-minute","60-minute","Daily"], index=0)
    if st.button("DOWNLOAD QUARTER", use_container_width=True):
        try:
            df = download_index_quarter(dataset, year, quarter, timeframe)
            if df.empty: st.error("No data returned.")
            else:
                st.success(f"Downloaded {len(df):,} rows.")
                st.download_button("DOWNLOAD CSV", df.to_csv(index=False).encode(), f"FRIDAY_{dataset.replace(' ','_')}_{year}_Q{quarter}_{timeframe}.csv", "text/csv", use_container_width=True)
                st.dataframe(df.head(300), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(str(exc))


def render_ai():
    st.markdown("## FRIDAY — OPTION PATTERN RESEARCH")
    st.write("Options + NIFTY Spot + India VIX only. Futures are removed from the design.")
    opt = st.file_uploader("Option Data (CSV)", type=["csv"], accept_multiple_files=True)
    spot = st.file_uploader("NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True)
    vix = st.file_uploader("India VIX Data (CSV)", type=["csv"], accept_multiple_files=True)
    if not (opt and spot):
        st.info("Upload Q1 Option + Spot data. Add Q1 India VIX for full context.")
        return
    if st.button("ANALYZE PATTERNS", use_container_width=True):
        progress = st.progress(0, text="FRIDAY processing: 0%")
        status = st.empty()
        try:
            status.info("10% — Reading files")
            progress.progress(10, text="FRIDAY processing: 10%")
            options_raw = read_csvs(opt); spot_raw = read_csvs(spot); vix_raw = read_csvs(vix) if vix else pd.DataFrame()

            status.info("25% — Normalizing Options")
            progress.progress(25, text="FRIDAY processing: 25%")
            options = normalize_options(options_raw)

            status.info("40% — Normalizing Spot")
            progress.progress(40, text="FRIDAY processing: 40%")
            spot_df = normalize_spot(spot_raw)

            status.info("50% — Normalizing VIX")
            progress.progress(50, text="FRIDAY processing: 50%")
            vix_df = normalize_vix(vix_raw) if not vix_raw.empty else pd.DataFrame()

            status.info("65% — Synchronizing")
            progress.progress(65, text="FRIDAY processing: 65%")
            merged = synchronize(options, spot_df, vix_df)
            if merged.empty:
                raise ValueError("No Option/Spot timestamps overlap within the 20-minute synchronization window.")

            status.info("82% — Building ATM features")
            progress.progress(82, text="FRIDAY processing: 82%")
            features = build_features(merged)
            if features.empty:
                raise ValueError("Spot synchronization worked, but no timestamp has both CE and PE at a valid ATM strike.")

            status.info("92% — Discovering patterns")
            progress.progress(92, text="FRIDAY processing: 92%")
            patterns = discover_patterns(features)

            st.session_state.analysis = features
            progress.progress(100, text="FRIDAY processing: 100% ✅")
            status.success(f"Analysis complete — {len(features):,} ATM observations.")
            st.subheader("Input Diagnostics")
            st.dataframe(diagnostics(options, spot_df, vix_df, merged, features), use_container_width=True, hide_index=True)
            if patterns.empty: st.warning("No pattern had at least 10 usable forward observations.")
            else:
                st.subheader("Pattern Summary")
                st.dataframe(patterns, use_container_width=True, hide_index=True)
            st.subheader("Feature Data")
            st.dataframe(features.tail(500), use_container_width=True, hide_index=True)
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv", features.to_csv(index=False))
                z.writestr("FRIDAY_pattern_summary.csv", patterns.to_csv(index=False))
                z.writestr("FRIDAY_diagnostics.csv", diagnostics(options, spot_df, vix_df, merged, features).to_csv(index=False))
            b.seek(0)
            st.download_button("DOWNLOAD ANALYSIS ZIP", b.getvalue(), "FRIDAY_Q1_PATTERN_ANALYSIS.zip", "application/zip", use_container_width=True)
        except Exception as exc:
            status.error(f"❌ Processing stopped: {exc}")
            st.error("The run was stopped safely. Nothing was written to disk.")


def inject_css():
    st.markdown("<style>.stApp{background:linear-gradient(180deg,#05080d,#080d15)} .block-container{max-width:1500px;padding-top:1rem}</style>", unsafe_allow_html=True)


init_state(); inject_css()
with st.sidebar:
    st.title("FRIDAY"); st.caption("Option Pattern Research Engine")
    st.session_state.client_id = st.text_input("Dhan Client ID", value=st.session_state.client_id or DEFAULT_CLIENT_ID).strip()
    st.session_state.access_token = st.text_input("Dhan Access Token", value=st.session_state.access_token, type="password").strip()
    st.caption(f"Client ID: {st.session_state.client_id or DEFAULT_CLIENT_ID}")

view = st.radio("MODULE", ["AI Strategist", "Data Vault"], horizontal=True)
if view == "Data Vault":
    render_vault()
else:
    render_ai()
