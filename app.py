import io
import json
import urllib.request
import urllib.error
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

DEFAULT_CLIENT_ID = "1113195747"
PAIR_TOLERANCE = pd.Timedelta(minutes=2)
SPOT_TOLERANCE = pd.Timedelta(minutes=20)
DHAN_API = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

st.set_page_config(page_title="FRIDAY", layout="wide")


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


def find_col(df, names):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in lookup:
            return lookup[key]
    for normalized, original in lookup.items():
        if any(name.lower().replace(" ", "_") in normalized for name in names):
            return original
    return None


def num_col(df, names):
    c = find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def read_csvs(files):
    parts = []
    for file in files or []:
        df = pd.read_csv(file, low_memory=False)
        if not df.empty:
            parts.append(df)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def normalize_time(df):
    if df.empty:
        return df
    c = find_col(df, ["timestamp", "datetime", "date_time", "timestamp_ist", "datetime_ist", "exchange_timestamp", "trade_time", "time", "date"])
    if c is None:
        raise ValueError(f"Timestamp column not found. Columns: {list(df.columns)}")
    out = df.copy()
    out["timestamp"] = parse_datetime(out[c])
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def normalize_expiry(values):
    raw = pd.Series(values)
    dt = pd.to_datetime(raw, errors="coerce")
    n = pd.to_numeric(raw, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        if med >= 1e12:
            dt = pd.to_datetime(n, unit="ms", errors="coerce")
        elif med >= 1e9:
            dt = pd.to_datetime(n, unit="s", errors="coerce")
    return dt.dt.normalize()


def normalize_options(df):
    x = normalize_time(df)
    strike_col = find_col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side_col = find_col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry_col = find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike_col is None or side_col is None:
        raise ValueError(f"Options require strike and CE/PE columns. Found: {list(df.columns)}")
    out = pd.DataFrame({
        "timestamp": x["timestamp"],
        "strike": pd.to_numeric(x[strike_col], errors="coerce"),
        "side": x[side_col].astype(str).str.upper().str.strip(),
    })
    out["side"] = out["side"].replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
    out["expiry"] = normalize_expiry(x[expiry_col]) if expiry_col else pd.NaT
    for names, target in [
        (["close", "ltp", "last_price", "price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["volume", "vol", "traded_volume"], "volume"),
    ]:
        value = num_col(x, names)
        out[target] = value if value is not None else float("nan")
    out = out.dropna(subset=["timestamp", "strike"])
    return out[out["side"].isin(["CE", "PE"])].sort_values("timestamp").reset_index(drop=True)


def normalize_spot(df):
    x = normalize_time(df)
    price = num_col(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if price is None:
        raise ValueError("NIFTY Spot has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x["timestamp"], "nifty_spot": price}).dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    x = normalize_time(df)
    price = num_col(x, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if price is None:
        raise ValueError("India VIX has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x["timestamp"], "vix_close": price}).dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def synchronize(options, spot, vix):
    if options.empty or spot.empty:
        return pd.DataFrame()
    o = options.copy().sort_values("timestamp").reset_index(drop=True)
    s = spot.copy().sort_values("timestamp").reset_index(drop=True)
    o["timestamp"] = o["timestamp"].astype("datetime64[ns]")
    s["timestamp"] = s["timestamp"].astype("datetime64[ns]")
    merged = pd.merge_asof(o, s, on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE).dropna(subset=["nifty_spot"])
    if not vix.empty and not merged.empty:
        vx = vix.copy().sort_values("timestamp").reset_index(drop=True)
        vx["timestamp"] = vx["timestamp"].astype("datetime64[ns]")
        merged = pd.merge_asof(merged.sort_values("timestamp"), vx, on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE)
    return merged.sort_values("timestamp").reset_index(drop=True)


def pair_ce_pe(merged):
    if merged.empty:
        return pd.DataFrame()
    x = merged.copy()
    x["timestamp"] = pd.to_datetime(x["timestamp"], errors="coerce").astype("datetime64[ns]")
    x["expiry_dt"] = pd.to_datetime(x["expiry"], errors="coerce") if "expiry" in x else pd.NaT
    x["expiry_dt"] = x["expiry_dt"].dt.normalize()
    x["strike"] = pd.to_numeric(x["strike"], errors="coerce")
    day = x["timestamp"].dt.normalize()
    x = x[x["expiry_dt"].isna() | (x["expiry_dt"] >= day)].dropna(subset=["timestamp", "strike"]).copy()
    if x.empty:
        return pd.DataFrame()
    x["expiry_key"] = x["expiry_dt"].dt.strftime("%Y-%m-%d").fillna("NO_EXPIRY")
    ce = x[x["side"] == "CE"].copy()
    pe = x[x["side"] == "PE"].copy()
    if ce.empty or pe.empty:
        return pd.DataFrame()
    shared = ["nifty_spot", "vix_close", "close", "iv", "oi", "volume", "timestamp"]
    pe = pe.rename(columns={c: f"{c}_pe" for c in shared if c in pe.columns})
    pe = pe.rename(columns={"timestamp_pe": "pe_timestamp"})
    ce = ce.sort_values(["timestamp", "expiry_key", "strike"], kind="mergesort").reset_index(drop=True)
    pe = pe.sort_values(["pe_timestamp", "expiry_key", "strike"], kind="mergesort").reset_index(drop=True)
    paired = pd.merge_asof(ce, pe, left_on="timestamp", right_on="pe_timestamp", by=["expiry_key", "strike"], direction="nearest", tolerance=PAIR_TOLERANCE)
    paired = paired.dropna(subset=["pe_timestamp", "close", "close_pe"]).copy()
    if paired.empty:
        return pd.DataFrame()
    paired["pair_timestamp"] = paired["timestamp"]
    paired["nifty_spot_pair"] = paired["nifty_spot"]
    paired["vix_close_pair"] = paired["vix_close"] if "vix_close" in paired.columns else float("nan")
    paired["pair_gap_seconds"] = (paired["pe_timestamp"] - paired["timestamp"]).abs().dt.total_seconds()
    return paired.sort_values("pair_timestamp").reset_index(drop=True)


def build_features(merged):
    paired = pair_ce_pe(merged)
    if paired.empty:
        return pd.DataFrame()
    paired["spot_dist"] = (paired["strike"] - paired["nifty_spot_pair"]).abs()
    atm = paired.sort_values(["pair_timestamp", "spot_dist"], kind="mergesort").drop_duplicates("pair_timestamp", keep="first").copy()
    if atm.empty:
        return pd.DataFrame()
    def series_for(name, default=float("nan")):
        if name in atm.columns:
            return pd.to_numeric(atm[name], errors="coerce").to_numpy()
        return pd.Series(default, index=atm.index).to_numpy()
    f = pd.DataFrame({
        "timestamp": atm["pair_timestamp"].to_numpy(),
        "nifty_spot": series_for("nifty_spot_pair"),
        "atm_strike": pd.to_numeric(atm["strike"], errors="coerce").to_numpy(),
        "ce_close": series_for("close"), "pe_close": series_for("close_pe"),
        "ce_iv": series_for("iv"), "pe_iv": series_for("iv_pe"),
        "ce_oi": series_for("oi"), "pe_oi": series_for("oi_pe"),
        "ce_volume": series_for("volume"), "pe_volume": series_for("volume_pe"),
        "vix_close": series_for("vix_close_pair"), "pair_gap_seconds": series_for("pair_gap_seconds"),
    })
    f["pcr_oi"] = (f["pe_oi"] / f["ce_oi"]).where(f["ce_oi"].notna() & (f["ce_oi"] != 0))
    f["straddle"] = f["ce_close"] + f["pe_close"]
    f["atm_iv"] = pd.concat([f["ce_iv"], f["pe_iv"]], axis=1).mean(axis=1)
    f = f.sort_values("timestamp").reset_index(drop=True)
    f["spot_ret_1"] = f["nifty_spot"].pct_change(); f["spot_ret_4"] = f["nifty_spot"].pct_change(4); f["spot_ret_16"] = f["nifty_spot"].pct_change(16)
    f["spot_vol_8"] = f["spot_ret_1"].rolling(8).std(); f["spot_ma_8"] = f["nifty_spot"].rolling(8).mean(); f["spot_ma_32"] = f["nifty_spot"].rolling(32).mean(); f["spot_trend"] = f["spot_ma_8"] - f["spot_ma_32"]
    f["straddle_change"] = f["straddle"].diff(); f["straddle_ret"] = f["straddle"].pct_change(); f["iv_change"] = f["atm_iv"].diff(); f["vix_change"] = f["vix_close"].diff(); f["vix_ret"] = f["vix_close"].pct_change(); f["pcr_change"] = f["pcr_oi"].diff()
    f["forward_spot_4"] = f["nifty_spot"].shift(-4) / f["nifty_spot"] - 1; f["forward_spot_16"] = f["nifty_spot"].shift(-16) / f["nifty_spot"] - 1; f["forward_straddle_4"] = f["straddle"].shift(-4) / f["straddle"] - 1; f["forward_straddle_16"] = f["straddle"].shift(-16) / f["straddle"] - 1
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


def diagnostics(o, s, v, m, paired, f):
    return pd.DataFrame({"Check": ["Option rows", "Spot rows", "VIX rows", "Option+Spot synced", "CE rows", "PE rows", "CE/PE pairs", "ATM observations", "Option start", "Option end", "Spot start", "Spot end"], "Value": [len(o), len(s), len(v), len(m), int((o.side == "CE").sum()) if not o.empty else 0, int((o.side == "PE").sum()) if not o.empty else 0, len(paired), len(f), str(o.timestamp.min()) if not o.empty else "N/A", str(o.timestamp.max()) if not o.empty else "N/A", str(s.timestamp.min()) if not s.empty else "N/A", str(s.timestamp.max()) if not s.empty else "N/A"]})


def dhan_call(path, payload, access_token, client_id):
    if not access_token:
        raise ValueError("Enter the Dhan Access Token in the sidebar first.")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(DHAN_API + path, data=body, method="POST", headers={"Accept": "application/json", "Content-Type": "application/json", "access-token": access_token, "client-id": client_id})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Dhan API HTTP {exc.code}: {detail[:800]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Dhan API connection error: {exc}") from exc


def resolve_security_id(symbol_text, fallback=None):
    try:
        master = pd.read_csv(MASTER_URL, low_memory=False)
        cols = {str(c).strip().upper(): c for c in master.columns}
        sid_col = cols.get("SECURITY_ID")
        if sid_col is None:
            return fallback
        candidates = []
        for key in ["SYMBOL_NAME", "DISPLAY_NAME", "TRADING_SYMBOL", "UNDERLYING_SYMBOL"]:
            c = cols.get(key)
            if c:
                mask = master[c].astype(str).str.upper().str.replace(" ", "", regex=False).str.contains(symbol_text.upper().replace(" ", ""), na=False)
                if mask.any():
                    vals = pd.to_numeric(master.loc[mask, sid_col], errors="coerce").dropna()
                    candidates.extend(vals.astype(int).tolist())
        return candidates[0] if candidates else fallback
    except Exception:
        return fallback


def dhan_series_from_body(body):
    data = body.get("data", body) if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        return pd.DataFrame()
    keys = ["timestamp", "open", "high", "low", "close", "volume"]
    if not data.get("timestamp"):
        return pd.DataFrame()
    n = len(data["timestamp"])
    out = pd.DataFrame({k: (data.get(k) if isinstance(data.get(k), list) else [None] * n) for k in keys})
    out["timestamp"] = parse_datetime(out["timestamp"])
    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["timestamp", "open", "high", "low", "close"]).sort_values("timestamp").reset_index(drop=True)


def download_index_ohlc(symbol, start_date, end_date, timeframe, access_token, client_id):
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")
    if timeframe == "Daily":
        sid = resolve_security_id(symbol, 13 if symbol == "NIFTY" else 26)
        if sid is None:
            raise ValueError(f"Could not resolve {symbol} Security ID from Dhan instrument master.")
        body = dhan_call("/charts/historical", {"securityId": str(sid), "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0, "oi": False, "fromDate": start_date.strftime("%Y-%m-%d"), "toDate": (end_date + timedelta(days=1)).strftime("%Y-%m-%d")}, access_token, client_id)
        return dhan_series_from_body(body)

    interval = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60}[timeframe]
    sid = resolve_security_id(symbol, 13 if symbol == "NIFTY" else 26)
    if sid is None:
        raise ValueError(f"Could not resolve {symbol} Security ID from Dhan instrument master.")
    chunks = []
    cur = start_date
    while cur <= end_date:
        ce = min(cur + timedelta(days=89), end_date)
        body = dhan_call("/charts/intraday", {"securityId": str(sid), "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": interval, "oi": False, "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"), "toDate": (ce + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")}, access_token, client_id)
        part = dhan_series_from_body(body)
        if not part.empty:
            chunks.append(part)
        cur = ce + timedelta(days=1)
    if not chunks:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return pd.concat(chunks, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def render_data_vault(access_token, client_id):
    st.title("FRIDAY — DATA VAULT")
    st.caption("Download and store NIFTY and India VIX OHLC data locally. Futures and OI are excluded.")
    c1, c2, c3 = st.columns(3)
    with c1:
        dataset = st.selectbox("Dataset", ["NIFTY", "India VIX"])
    with c2:
        timeframe = st.selectbox("Timeframe", ["1-minute", "5-minute", "15-minute", "25-minute", "60-minute", "Daily"], index=2)
    with c3:
        st.metric("Dhan Client", client_id or DEFAULT_CLIENT_ID)
    d1, d2 = st.columns(2)
    with d1:
        start_date = st.date_input("Start date", value=date(2024, 4, 1), min_value=date(2021, 1, 1))
    with d2:
        end_date = st.date_input("End date", value=date(2024, 12, 31), min_value=date(2021, 1, 1))

    st.info("Dhan's intraday historical endpoint supports 1/5/15/25/60-minute candles and limits each poll to 90 days, so FRIDAY automatically splits longer ranges into 90-day requests. OHLC is stored with Volume; OI is never requested.")
    if st.button("DOWNLOAD OHLC", use_container_width=True):
        bar = st.progress(0, text="FRIDAY Data Vault: 0%")
        status = st.empty()
        try:
            if not access_token:
                raise ValueError("Enter your Dhan Access Token in the sidebar before downloading.")
            status.info("20% — Resolving Security ID")
            bar.progress(20, text="FRIDAY Data Vault: 20% — Resolving instrument")
            status.info("50% — Downloading Dhan historical candles")
            bar.progress(50, text="FRIDAY Data Vault: 50% — Downloading")
            df = download_index_ohlc("NIFTY" if dataset == "NIFTY" else "INDIA VIX", start_date, end_date, timeframe, access_token, client_id)
            if df.empty:
                raise ValueError("Dhan returned no candles for the selected instrument/date range.")
            status.info("85% — Preparing offline CSV")
            bar.progress(85, text="FRIDAY Data Vault: 85% — Preparing CSV")
            df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
            csv = df.to_csv(index=False).encode("utf-8")
            bar.progress(100, text="FRIDAY Data Vault: 100% ✅")
            status.success(f"Downloaded {len(df):,} candles | {df.iloc[0]['timestamp']} → {df.iloc[-1]['timestamp']}")
            st.dataframe(df.head(300), use_container_width=True, hide_index=True)
            filename = f"FRIDAY_{dataset.replace(' ', '_')}_{start_date}_{end_date}_{timeframe}.csv"
            st.download_button("DOWNLOAD CSV", csv, filename, "text/csv", use_container_width=True)
        except Exception as exc:
            status.error(f"Download stopped: {exc}")
            st.exception(exc)


def render_analyzer(access_token):
    st.title("FRIDAY — OPTION PATTERN RESEARCH")
    st.caption("Options + NIFTY Spot + India VIX only. Futures are excluded.")
    opt_files = st.file_uploader("Q1 Option Data (CSV)", type=["csv"], accept_multiple_files=True)
    spot_files = st.file_uploader("Q1 NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True)
    vix_files = st.file_uploader("Q1 India VIX Data (CSV)", type=["csv"], accept_multiple_files=True)
    if not opt_files or not spot_files:
        st.info("Upload the Q1 Option and NIFTY Spot CSV files. India VIX is optional.")
        return
    if st.button("ANALYZE PATTERNS", use_container_width=True):
        bar = st.progress(0, text="FRIDAY: 0%"); status = st.empty()
        try:
            status.info("10% — Reading CSV files"); bar.progress(10, text="FRIDAY: 10% — Reading files")
            o_raw = read_csvs(opt_files); s_raw = read_csvs(spot_files); v_raw = read_csvs(vix_files) if vix_files else pd.DataFrame()
            status.info("30% — Normalizing data"); bar.progress(30, text="FRIDAY: 30% — Normalizing Options / Spot / VIX")
            o = normalize_options(o_raw); s = normalize_spot(s_raw); v = normalize_vix(v_raw) if not v_raw.empty else pd.DataFrame()
            status.info("50% — Synchronizing timestamps"); bar.progress(50, text="FRIDAY: 50% — Synchronizing")
            m = synchronize(o, s, v)
            if m.empty: raise ValueError("No Option/Spot timestamps overlap within 20 minutes.")
            status.info("65% — Pairing CE and PE"); bar.progress(65, text="FRIDAY: 65% — Pairing CE/PE within ±2 minutes")
            paired = pair_ce_pe(m)
            if paired.empty: raise ValueError("No CE/PE pairs found within ±2 minutes for the same strike and expiry. Check option timestamps, strike, expiry and CE/PE labels.")
            status.info("75% — Selecting ATM"); bar.progress(75, text="FRIDAY: 75% — Selecting ATM")
            f = build_features(m)
            if f.empty: raise ValueError("CE/PE pairing worked, but no ATM observations could be built.")
            status.info("90% — Measuring historical patterns"); bar.progress(90, text="FRIDAY: 90% — Measuring patterns")
            patterns = discover_patterns(f)
            bar.progress(100, text="FRIDAY: 100% ✅"); status.success(f"Analysis complete — {len(f):,} ATM observations")
            st.subheader("Input Diagnostics"); st.dataframe(diagnostics(o, s, v, m, paired, f), use_container_width=True, hide_index=True)
            st.subheader("Pattern Summary")
            if patterns.empty: st.warning("No pattern produced at least 10 usable forward observations.")
            else: st.dataframe(patterns, use_container_width=True, hide_index=True)
            st.subheader("Feature Data"); st.dataframe(f.tail(500), use_container_width=True, hide_index=True)
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv", f.to_csv(index=False)); z.writestr("FRIDAY_patterns.csv", patterns.to_csv(index=False)); z.writestr("FRIDAY_diagnostics.csv", diagnostics(o, s, v, m, paired, f).to_csv(index=False)); z.writestr("FRIDAY_pairs.csv", paired.to_csv(index=False))
            st.download_button("DOWNLOAD ANALYSIS ZIP", out.getvalue(), "FRIDAY_Q1_ANALYSIS.zip", "application/zip", use_container_width=True)
        except Exception as exc:
            status.error(f"Processing stopped: {exc}"); st.exception(exc)


with st.sidebar:
    st.subheader("FRIDAY")
    client_id = st.text_input("Dhan Client ID", value=DEFAULT_CLIENT_ID).strip()
    access_token = st.text_input("Dhan Access Token", value="", type="password", help="Enter your current Dhan access token. It stays in the Streamlit session and is not written to GitHub.").strip()
    if access_token: st.success("Dhan token entered for this session")
    module = st.radio("MODULE", ["Data Vault", "Pattern Research"], index=0)

if module == "Data Vault":
    render_data_vault(access_token, client_id)
else:
    render_analyzer(access_token)
