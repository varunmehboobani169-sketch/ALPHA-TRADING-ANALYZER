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
NIFTY_ID = 13
VIX_ID_FALLBACK = 26
RATE_LIMIT_SECONDS = 3.2


def now_ist():
    return datetime.now(LOCAL_TZ)


def init_state():
    defaults = {
        "client_id": "", "access_token": "", "last_api_call": 0.0,
        "analysis": None, "analysis_summary": {}
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


def headers():
    if not st.session_state.client_id or not st.session_state.access_token:
        raise RuntimeError("Enter Dhan Client ID and Access Token.")
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


def parse_chart(body):
    d = parse_data(body)
    ts = d.get("timestamp")
    if not ts:
        return pd.DataFrame()
    dt = pd.to_datetime(
        pd.to_numeric(pd.Series(ts), errors="coerce"),
        unit="s", utc=True, errors="coerce"
    ).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = pd.DataFrame({"timestamp": dt})
    for c in ["open", "high", "low", "close", "volume"]:
        vals = d.get(c)
        if vals is not None and len(vals) == len(out):
            out[c] = pd.to_numeric(pd.Series(vals), errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def _find_col(df, names):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in lookup:
            return lookup[key]
    for norm, original in lookup.items():
        if any(name.lower().replace(" ", "_") in norm for name in names):
            return original
    return None


def read_csvs(files):
    frames = []
    for f in files or []:
        df = pd.read_csv(f, low_memory=False)
        if not df.empty:
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_time(df):
    if df.empty:
        return df
    c = _find_col(df, ["timestamp", "datetime", "date_time", "time", "date"])
    if c is None:
        raise ValueError("Could not find a timestamp/datetime column.")
    out = df.copy()
    dt = pd.to_datetime(out[c], errors="coerce")
    try:
        if getattr(dt.dt, "tz", None) is not None:
            dt = dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception:
        pass
    out["timestamp"] = dt
    return out.dropna(subset=["timestamp"]).sort_values("timestamp")


def numeric(df, names):
    c = _find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def normalize_spot(df):
    x = normalize_time(df)
    p = numeric(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if x.empty or p is None:
        raise ValueError("Spot data needs a recognizable NIFTY close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "nifty_spot": p}).dropna()


def normalize_vix(df):
    x = normalize_time(df)
    p = numeric(x, ["close", "ltp", "last_price", "vix", "vix_close", "price"])
    if x.empty or p is None:
        raise ValueError("VIX data needs a recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "vix_close": p}).dropna()


def normalize_options(df):
    x = normalize_time(df)
    strike = _find_col(x, ["strike", "strike_price", "strikeprice"])
    side = _find_col(x, ["side", "option_type", "type", "cp", "ce_pe"])
    expiry = _find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date"])
    if x.empty or strike is None or side is None:
        raise ValueError("Options need timestamp, strike and CE/PE option type columns.")
    r = pd.DataFrame({
        "timestamp": x.timestamp,
        "strike": pd.to_numeric(x[strike], errors="coerce"),
        "side": x[side].astype(str).str.upper().str.strip()
    })
    r["side"] = r.side.replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
    r["expiry"] = pd.to_datetime(x[expiry], errors="coerce").dt.date if expiry else pd.NaT
    for names, target in [
        (["close", "ltp", "last_price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["volume", "vol"], "volume"),
    ]:
        v = numeric(x, names)
        r[target] = v if v is not None else np.nan
    return r.dropna(subset=["timestamp", "strike"])


def build_features(options, spot, vix=None):
    opt = options.sort_values("timestamp")
    sp = spot.sort_values("timestamp")
    opt = pd.merge_asof(
        opt, sp, on="timestamp", direction="backward",
        tolerance=pd.Timedelta("10min")
    ).dropna(subset=["nifty_spot"])
    if vix is not None and not vix.empty:
        opt = pd.merge_asof(
            opt.sort_values("timestamp"), vix.sort_values("timestamp"),
            on="timestamp", direction="backward",
            tolerance=pd.Timedelta("15min")
        )
    rows = []
    for ts, g in opt.groupby("timestamp", sort=True):
        valid_exp = [e for e in g.expiry.dropna().unique() if e >= ts.date()]
        if valid_exp:
            g = g[g.expiry == min(valid_exp)]
        if g.empty:
            continue
        spot_px = float(g.nifty_spot.iloc[0])
        strikes = g.strike.dropna().unique()
        if len(strikes) == 0:
            continue
        atm = min(strikes, key=lambda s: abs(float(s) - spot_px))
        a = g[np.isclose(g.strike.astype(float), float(atm))]
        ce, pe = a[a.side == "CE"], a[a.side == "PE"]
        if ce.empty or pe.empty:
            continue
        ce, pe = ce.iloc[-1], pe.iloc[-1]
        call_oi = g.loc[g.side == "CE", "oi"].sum(min_count=1)
        put_oi = g.loc[g.side == "PE", "oi"].sum(min_count=1)
        pcr = put_oi / call_oi if pd.notna(call_oi) and call_oi != 0 else np.nan
        rows.append({
            "timestamp": ts, "nifty_spot": spot_px, "atm_strike": float(atm),
            "ce_close": ce.close, "pe_close": pe.close,
            "ce_iv": ce.iv, "pe_iv": pe.iv,
            "ce_oi": ce.oi, "pe_oi": pe.oi,
            "ce_volume": ce.volume, "pe_volume": pe.volume,
            "pcr_oi": pcr,
            "straddle": ce.close + pe.close if pd.notna(ce.close) and pd.notna(pe.close) else np.nan,
            "atm_iv": np.nanmean([ce.iv, pe.iv]),
            "vix_close": g.vix_close.iloc[-1] if "vix_close" in g else np.nan,
        })
    f = pd.DataFrame(rows).sort_values("timestamp") if rows else pd.DataFrame()
    if f.empty:
        return f
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
    x = f.copy()
    if x.empty:
        return pd.DataFrame(), x
    rules = [
        ("IV rising + spot flat", (x.iv_change > 0) & (x.spot_ret_4.abs() < 0.001)),
        ("IV falling + spot flat", (x.iv_change < 0) & (x.spot_ret_4.abs() < 0.001)),
        ("PCR rising", x.pcr_change > 0),
        ("PCR falling", x.pcr_change < 0),
        ("VIX rising", x.vix_change > 0),
        ("VIX falling", x.vix_change < 0),
        ("Straddle expanding", x.straddle_change > 0),
        ("Straddle contracting", x.straddle_change < 0),
        ("Spot uptrend", x.spot_trend > 0),
        ("Spot downtrend", x.spot_trend < 0),
    ]
    rows = []
    for name, mask in rules:
        d = x.loc[mask].copy()
        if len(d) < 10:
            continue
        rows.append({
            "pattern": name, "observations": len(d),
            "avg_next_4_spot_pct": d.forward_spot_4.mean(),
            "avg_next_16_spot_pct": d.forward_spot_16.mean(),
            "avg_next_4_straddle_pct": d.forward_straddle_4.mean(),
            "avg_next_16_straddle_pct": d.forward_straddle_16.mean(),
            "next_4_spot_up_rate": (d.forward_spot_4 > 0).mean(),
            "next_4_straddle_up_rate": (d.forward_straddle_4 > 0).mean(),
        })
    summary = pd.DataFrame(rows).sort_values("observations", ascending=False) if rows else pd.DataFrame()
    return summary, x


def _resolve_vix_id():
    try:
        master = pd.read_csv(
            "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
            low_memory=False
        )
        cols = {str(c).strip().upper(): c for c in master.columns}
        sid_col = cols.get("SECURITY_ID")
        names = [cols.get("SYMBOL_NAME"), cols.get("DISPLAY_NAME"), cols.get("UNDERLYING_SYMBOL")]
        names = [c for c in names if c]
        if sid_col and names:
            mask = pd.Series(False, index=master.index)
            for c in names:
                mask |= master[c].astype(str).str.upper().str.replace(" ", "", regex=False).str.contains("INDIAVIX", na=False)
            rows = master[mask]
            if not rows.empty:
                sid = pd.to_numeric(rows.iloc[0][sid_col], errors="coerce")
                if pd.notna(sid):
                    return int(sid)
    except Exception:
        pass
    return VIX_ID_FALLBACK


def download_index_quarter(dataset, year, quarter, timeframe):
    sid = NIFTY_ID if dataset == "NIFTY Spot" else _resolve_vix_id()
    q = pd.Period(f"{year}-Q{quarter}")
    start, end = pd.Timestamp(q.start_time), pd.Timestamp(q.end_time)
    if timeframe == "Daily":
        body = api_post(
            "/charts/historical",
            {
                "securityId": str(sid), "exchangeSegment": "IDX_I",
                "instrument": "INDEX", "expiryCode": 0, "oi": False,
                "fromDate": start.strftime("%Y-%m-%d"),
                "toDate": end.strftime("%Y-%m-%d")
            },
            f"{dataset} {year} Q{quarter}"
        )
        return parse_chart(body)
    interval = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60}[timeframe]
    parts, cur = [], start
    while cur <= end:
        ce = min(cur + pd.Timedelta(days=89), end)
        body = api_post(
            "/charts/intraday",
            {
                "securityId": str(sid), "exchangeSegment": "IDX_I",
                "instrument": "INDEX", "interval": interval, "oi": False,
                "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": ce.strftime("%Y-%m-%d %H:%M:%S")
            },
            f"{dataset} {year} Q{quarter}"
        )
        part = parse_chart(body)
        if not part.empty:
            parts.append(part)
        cur = ce + pd.Timedelta(seconds=1)
    return pd.concat(parts, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp") if parts else pd.DataFrame()


def render_vault():
    st.markdown("## DATA VAULT")
    st.caption("NIFTY Spot and India VIX only. Futures are intentionally out of scope for this build.")
    years = list(range(2020, now_ist().year + 1))
    c1, c2, c3 = st.columns(3)
    with c1:
        dataset = st.selectbox("Dataset", ["NIFTY Spot", "India VIX"])
    with c2:
        year = st.selectbox("Year", years, index=years.index(2024) if 2024 in years else len(years) - 1)
    with c3:
        quarter = st.selectbox("Quarter", [1, 2, 3, 4], index=0)
    timeframe = st.selectbox("Timeframe", ["15-minute", "1-minute", "5-minute", "25-minute", "60-minute", "Daily"], index=0)
    if st.button("DOWNLOAD QUARTER", use_container_width=True):
        try:
            df = download_index_quarter(dataset, year, quarter, timeframe)
            if df.empty:
                st.error("No data returned.")
            else:
                st.success(f"Downloaded {len(df):,} rows.")
                st.download_button(
                    "DOWNLOAD CSV", df.to_csv(index=False).encode(),
                    f"FRIDAY_{dataset.replace(' ', '_')}_{year}_Q{quarter}_{timeframe}.csv",
                    "text/csv", use_container_width=True
                )
                st.dataframe(df.head(300), use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(str(e))


def render_ai():
    st.markdown("## FRIDAY — OPTION PATTERN RESEARCH")
    st.write("FRIDAY uses Options + NIFTY Spot + India VIX to discover repeatable relationships. Futures are not part of the design.")
    opt = st.file_uploader("Option Data (CSV)", type=["csv"], accept_multiple_files=True)
    spot = st.file_uploader("NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True)
    vix = st.file_uploader("India VIX Data (CSV)", type=["csv"], accept_multiple_files=True)
    if not (opt and spot):
        st.info("Upload the Q1 Option + Spot data. Upload Q1 India VIX as well for the full context analysis.")
        return
    if st.button("ANALYZE PATTERNS", use_container_width=True):
        try:
            options = normalize_options(read_csvs(opt))
            spot_df = normalize_spot(read_csvs(spot))
            vix_df = normalize_vix(read_csvs(vix)) if vix else pd.DataFrame()
            features = build_features(options, spot_df, vix_df)
            if features.empty:
                raise ValueError("No synchronized ATM option/spot observations were created.")
            patterns, enriched = discover_patterns(features)
            st.session_state.analysis = enriched
            st.session_state.analysis_summary = {"rows": len(enriched)}
            st.success(f"Analyzed {len(enriched):,} synchronized timestamps.")
            if not patterns.empty:
                st.subheader("Pattern Summary")
                st.dataframe(patterns, use_container_width=True, hide_index=True)
            st.subheader("Feature Data")
            st.dataframe(enriched.tail(500), use_container_width=True, hide_index=True)
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv", enriched.to_csv(index=False))
                z.writestr("FRIDAY_pattern_summary.csv", patterns.to_csv(index=False))
            st.download_button(
                "DOWNLOAD ANALYSIS ZIP", b.getvalue(),
                "FRIDAY_Q1_PATTERN_ANALYSIS.zip", "application/zip", use_container_width=True
            )
        except Exception as e:
            st.error(str(e))


def inject_css():
    st.markdown(
        "<style>.stApp{background:linear-gradient(180deg,#05080d,#080d15)} "
        ".block-container{max-width:1500px;padding-top:1rem}</style>",
        unsafe_allow_html=True
    )


init_state()
inject_css()
with st.sidebar:
    st.title("FRIDAY")
    st.caption("Option Pattern Research Engine")
    st.session_state.client_id = st.text_input("Dhan Client ID", st.session_state.client_id).strip()
    st.session_state.access_token = st.text_input("Dhan Access Token", st.session_state.access_token, type="password").strip()

view = st.radio("MODULE", ["AI Strategist", "Data Vault"], horizontal=True)
if view == "Data Vault":
    render_vault()
else:
    render_ai()
