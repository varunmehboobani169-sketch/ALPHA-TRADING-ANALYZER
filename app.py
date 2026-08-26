import io
import json
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta

import numpy as np
import pandas as pd
import streamlit as st

DEFAULT_CLIENT_ID = "1113195747"
PAIR_TOLERANCE = pd.Timedelta(minutes=2)
SPOT_TOLERANCE = pd.Timedelta(minutes=20)
DHAN_API = "https://api.dhan.co/v2"

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
        k = name.lower().replace(" ", "_")
        if k in lookup:
            return lookup[k]
    for k, v in lookup.items():
        if any(name.lower().replace(" ", "_") in k for name in names):
            return v
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
    c = find_col(df, ["timestamp", "datetime", "date_time", "timestamp_ist", "datetime_ist", "exchange_timestamp", "trade_time", "time", "date"])
    if c is None:
        raise ValueError(f"Timestamp column not found. Columns: {list(df.columns)}")
    out = df.copy()
    out["timestamp"] = parse_datetime(out[c])
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def normalize_expiry(v):
    return pd.to_datetime(pd.Series(v), errors="coerce").dt.normalize()


def normalize_options(df):
    x = normalize_time(df)
    strike = find_col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = find_col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike is None or side is None:
        raise ValueError(f"Options require strike and CE/PE columns. Found: {list(df.columns)}")
    out = pd.DataFrame({"timestamp": x.timestamp, "strike": pd.to_numeric(x[strike], errors="coerce"), "side": x[side].astype(str).str.upper().str.strip()})
    out["side"] = out.side.replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
    out["expiry"] = normalize_expiry(x[expiry]) if expiry else pd.NaT
    for names, target in [(["close", "ltp", "last_price", "price"], "close"), (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"), (["oi", "open_interest", "openinterest"], "oi"), (["volume", "vol", "traded_volume"], "volume")]:
        v = num_col(x, names)
        out[target] = v if v is not None else np.nan
    return out.dropna(subset=["timestamp", "strike"])[out.side.isin(["CE", "PE"])].sort_values("timestamp").reset_index(drop=True)


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
    o = options.sort_values("timestamp").copy(); s = spot.sort_values("timestamp").copy()
    o.timestamp = o.timestamp.astype("datetime64[ns]"); s.timestamp = s.timestamp.astype("datetime64[ns]")
    m = pd.merge_asof(o, s, on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE).dropna(subset=["nifty_spot"])
    if not vix.empty and not m.empty:
        v = vix.sort_values("timestamp").copy(); v.timestamp = v.timestamp.astype("datetime64[ns]")
        m = pd.merge_asof(m.sort_values("timestamp"), v, on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE)
    return m.sort_values("timestamp").reset_index(drop=True)


def pair_ce_pe(m):
    if m.empty: return pd.DataFrame()
    x = m.copy(); x["timestamp"] = pd.to_datetime(x.timestamp, errors="coerce").astype("datetime64[ns]"); x["expiry_dt"] = pd.to_datetime(x.expiry, errors="coerce").dt.normalize(); x["strike"] = pd.to_numeric(x.strike, errors="coerce")
    x = x[x.expiry_dt.isna() | (x.expiry_dt >= x.timestamp.dt.normalize())].dropna(subset=["timestamp", "strike"])
    if x.empty: return pd.DataFrame()
    x["expiry_key"] = x.expiry_dt.dt.strftime("%Y-%m-%d").fillna("NO_EXPIRY")
    ce = x[x.side == "CE"].copy(); pe = x[x.side == "PE"].copy()
    if ce.empty or pe.empty: return pd.DataFrame()
    for c in ["nifty_spot", "vix_close", "close", "iv", "oi", "volume"]:
        if c in pe.columns: pe[c + "_pe"] = pe[c]
    pe = pe.rename(columns={"timestamp": "pe_timestamp"})
    ce = ce.sort_values(["timestamp", "expiry_key", "strike"], kind="mergesort").reset_index(drop=True)
    pe = pe.sort_values(["pe_timestamp", "expiry_key", "strike"], kind="mergesort").reset_index(drop=True)
    paired = pd.merge_asof(ce, pe, left_on="timestamp", right_on="pe_timestamp", by=["expiry_key", "strike"], direction="nearest", tolerance=PAIR_TOLERANCE, suffixes=("", "_dup"))
    paired = paired.dropna(subset=["pe_timestamp", "close", "close_pe"])
    if paired.empty: return pd.DataFrame()
    paired["pair_timestamp"] = paired.timestamp
    paired["nifty_spot_pair"] = paired.nifty_spot
    paired["vix_close_pair"] = paired.get("vix_close", np.nan)
    paired["pair_gap_seconds"] = (paired.pe_timestamp - paired.timestamp).abs().dt.total_seconds()
    return paired.sort_values("pair_timestamp").reset_index(drop=True)


def build_features(m):
    p = pair_ce_pe(m)
    if p.empty: return pd.DataFrame()
    p["spot_dist"] = (p.strike - p.nifty_spot_pair).abs(); a = p.sort_values(["pair_timestamp", "spot_dist"], kind="mergesort").drop_duplicates("pair_timestamp")
    def arr(c): return pd.to_numeric(a[c], errors="coerce").to_numpy() if c in a.columns else np.full(len(a), np.nan)
    f = pd.DataFrame({"timestamp": a.pair_timestamp.to_numpy(), "nifty_spot": arr("nifty_spot_pair"), "atm_strike": arr("strike"), "ce_close": arr("close"), "pe_close": arr("close_pe"), "ce_iv": arr("iv"), "pe_iv": arr("iv_pe"), "ce_oi": arr("oi"), "pe_oi": arr("oi_pe"), "ce_volume": arr("volume"), "pe_volume": arr("volume_pe"), "vix_close": arr("vix_close_pair"), "pair_gap_seconds": arr("pair_gap_seconds")})
    f["pcr_oi"] = f.pe_oi / f.ce_oi.replace(0, np.nan)
    f["straddle"] = f.ce_close + f.pe_close
    f["atm_iv"] = pd.concat([f.ce_iv, f.pe_iv], axis=1).mean(axis=1)
    f = f.sort_values("timestamp").reset_index(drop=True)
    for n in [1, 4, 16]: f[f"spot_ret_{n}"] = f.nifty_spot.pct_change(n)
    f["spot_vol_8"] = f.spot_ret_1.rolling(8).std(); f["spot_ma_8"] = f.nifty_spot.rolling(8).mean(); f["spot_ma_32"] = f.nifty_spot.rolling(32).mean(); f["spot_trend"] = f.spot_ma_8 - f.spot_ma_32
    f["straddle_change"] = f.straddle.diff(); f["straddle_ret"] = f.straddle.pct_change(); f["iv_change"] = f.atm_iv.diff(); f["vix_change"] = f.vix_close.diff(); f["vix_ret"] = f.vix_close.pct_change(); f["pcr_change"] = f.pcr_oi.diff()
    for n in [4, 16]:
        f[f"forward_spot_{n}"] = f.nifty_spot.shift(-n) / f.nifty_spot - 1
        f[f"forward_straddle_{n}"] = f.straddle.shift(-n) / f.straddle - 1
    return f


def discover_patterns(f):
    rules = [("IV rising + spot flat", (f.iv_change > 0) & (f.spot_ret_4.abs() < .001)), ("IV falling + spot flat", (f.iv_change < 0) & (f.spot_ret_4.abs() < .001)), ("PCR rising", f.pcr_change > 0), ("PCR falling", f.pcr_change < 0), ("VIX rising", f.vix_change > 0), ("VIX falling", f.vix_change < 0), ("Straddle expanding", f.straddle_change > 0), ("Straddle contracting", f.straddle_change < 0), ("Spot uptrend", f.spot_trend > 0), ("Spot downtrend", f.spot_trend < 0)]
    rows = []
    for name, mask in rules:
        d = f.loc[mask].dropna(subset=["forward_spot_4", "forward_straddle_4"])
        if len(d) >= 10:
            rows.append({"pattern": name, "observations": len(d), "avg_next_4_spot_pct": d.forward_spot_4.mean(), "avg_next_16_spot_pct": d.forward_spot_16.mean(), "avg_next_4_straddle_pct": d.forward_straddle_4.mean(), "avg_next_16_straddle_pct": d.forward_straddle_16.mean(), "next_4_spot_up_rate": (d.forward_spot_4 > 0).mean(), "next_4_straddle_up_rate": (d.forward_straddle_4 > 0).mean()})
    return pd.DataFrame(rows).sort_values("observations", ascending=False) if rows else pd.DataFrame()


def diagnostics(o, s, v, m, paired, f):
    return pd.DataFrame({"Check": ["Option rows", "Spot rows", "VIX rows", "Option+Spot synced", "CE rows", "PE rows", "CE/PE pairs", "ATM observations", "Option start", "Option end", "Spot start", "Spot end"], "Value": [len(o), len(s), len(v), len(m), int((o.side == "CE").sum()) if not o.empty else 0, int((o.side == "PE").sum()) if not o.empty else 0, len(paired), len(f), str(o.timestamp.min()) if not o.empty else "N/A", str(o.timestamp.max()) if not o.empty else "N/A", str(s.timestamp.min()) if not s.empty else "N/A", str(s.timestamp.max()) if not s.empty else "N/A"]})


def build_report(period_label, o, s, v, m, paired, f, patterns):
    q = period_label.replace(" ", "_").replace("/", "-")
    lines = [f"# FRIDAY — OPTION PATTERN RESEARCH", f"Research Period: {period_label}", "", "## Scope", "Options + NIFTY Spot + India VIX. Futures excluded.", "", "## Data Diagnostics"]
    for _, r in diagnostics(o, s, v, m, paired, f).iterrows(): lines.append(f"- {r['Check']}: {r['Value']}")
    lines += ["", "## Pattern Summary"]
    if patterns.empty:
        lines.append("No pattern produced at least 10 usable forward observations.")
    else:
        lines.append(patterns.to_markdown(index=False))
    lines += ["", "## Interpretation Note", "This report is descriptive research output. It is not an AI conclusion or a trading recommendation.", "Forward 4/16 observations refer to the current data frequency."]
    return "\n".join(lines), q


def dhan_call(path, payload, token, client_id):
    if not token: raise ValueError("Enter your Dhan Access Token in the sidebar first.")
    req = urllib.request.Request(DHAN_API + path, data=json.dumps(payload).encode(), method="POST", headers={"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client_id})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace"); raise RuntimeError(f"Dhan HTTP {e.code}: {detail[:1000]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Dhan connection error: {e}") from e


def dhan_series(body):
    if not isinstance(body, dict): return pd.DataFrame(), "Invalid response"
    if body.get("status") not in (None, "success"):
        return pd.DataFrame(), str(body.get("remarks", body.get("data", body)))[:1000]
    data = body.get("data", body)
    if not isinstance(data, dict) or not data.get("timestamp"):
        return pd.DataFrame(), str(body)[:1000]
    n = len(data["timestamp"]); out = pd.DataFrame({k: (data.get(k) if isinstance(data.get(k), list) else [None] * n) for k in ["timestamp", "open", "high", "low", "close", "volume"]})
    out.timestamp = parse_datetime(out.timestamp)
    for c in ["open", "high", "low", "close", "volume"]: out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["timestamp", "open", "high", "low", "close"]).sort_values("timestamp").reset_index(drop=True), "success"


def instrument_candidates(symbol):
    if symbol == "NIFTY": return [("IDX_I", "13"), ("NSE_IDX", "13")]
    return [("IDX_I", "21"), ("NSE_IDX", "21"), ("IDX_I", "26"), ("NSE_IDX", "26")]


def download_index_ohlc(symbol, start_date, end_date, timeframe, token, client_id):
    if end_date < start_date: raise ValueError("End date must be on or after start date.")
    interval = None if timeframe == "Daily" else {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60}[timeframe]
    attempts = []; chunks = []; chosen = None
    for seg, sid in instrument_candidates(symbol):
        local = []; cur = start_date
        while cur <= end_date:
            ce = min(cur + timedelta(days=89), end_date)
            if timeframe == "Daily":
                path = "/charts/historical"; payload = {"securityId": sid, "exchangeSegment": seg, "instrument": "INDEX", "expiryCode": 0, "oi": False, "fromDate": cur.strftime("%Y-%m-%d"), "toDate": (ce + timedelta(days=1)).strftime("%Y-%m-%d")}
            else:
                path = "/charts/intraday"; payload = {"securityId": sid, "exchangeSegment": seg, "instrument": "INDEX", "interval": interval, "oi": False, "fromDate": cur.strftime("%Y-%m-%d 00:00:00"), "toDate": (ce + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")}
            body = dhan_call(path, payload, token, client_id); part, reason = dhan_series(body); attempts.append(f"{seg}/{sid} {cur}..{ce}: {reason}")
            if not part.empty: local.append(part)
            cur = ce + timedelta(days=1)
        if local: chosen = (seg, sid); chunks = local; break
    if not chunks: raise ValueError("No candles returned. Tried: " + " | ".join(attempts[-8:]))
    return pd.concat(chunks, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True), chosen


def render_data_vault(token, client_id):
    st.title("FRIDAY — DATA VAULT"); st.caption("Offline OHLC collector for NIFTY and India VIX. Futures and OI are excluded.")
    c1, c2, c3 = st.columns(3)
    with c1: dataset = st.selectbox("Dataset", ["NIFTY", "India VIX"])
    with c2: timeframe = st.selectbox("Timeframe", ["1-minute", "5-minute", "15-minute", "25-minute", "60-minute", "Daily"], index=2)
    with c3: st.metric("Dhan Client", client_id or DEFAULT_CLIENT_ID)
    d1, d2 = st.columns(2)
    with d1: start_date = st.date_input("Start date", value=date(2024, 4, 1), min_value=date(2021, 1, 1))
    with d2: end_date = st.date_input("End date", value=date(2024, 12, 31), min_value=date(2021, 1, 1))
    st.info("Each intraday request is automatically limited to 90 days. OI is always disabled.")
    if st.button("DOWNLOAD OHLC", use_container_width=True):
        bar = st.progress(0, text="FRIDAY Data Vault: 0%"); status = st.empty()
        try:
            if not token: raise ValueError("Enter your Dhan Access Token in the sidebar first.")
            bar.progress(20, text="FRIDAY Data Vault: 20% — Preparing request")
            df, chosen = download_index_ohlc("NIFTY" if dataset == "NIFTY" else "INDIA VIX", start_date, end_date, timeframe, token, client_id)
            bar.progress(70, text=f"FRIDAY Data Vault: 70% — Data received ({chosen[0]}/{chosen[1]})")
            df["timestamp"] = df.timestamp.dt.strftime("%Y-%m-%d %H:%M:%S")
            csv = df.to_csv(index=False).encode()
            bar.progress(100, text="FRIDAY Data Vault: 100% ✅"); status.success(f"Downloaded {len(df):,} candles")
            st.dataframe(df.head(300), use_container_width=True, hide_index=True)
            st.download_button("DOWNLOAD CSV", csv, f"FRIDAY_{dataset.replace(' ', '_')}_{start_date}_{end_date}_{timeframe}.csv", "text/csv", use_container_width=True)
        except Exception as e:
            status.error(f"Download stopped: {e}"); st.exception(e)


def render_analyzer(period_label):
    st.title("FRIDAY — OPTION PATTERN RESEARCH")
    st.caption(f"Research period: {period_label} | Options + NIFTY Spot + India VIX only. Futures are excluded.")
    opt = st.file_uploader("Option Data (CSV)", type=["csv"], accept_multiple_files=True)
    spot = st.file_uploader("NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True)
    vix = st.file_uploader("India VIX Data (CSV)", type=["csv"], accept_multiple_files=True)
    if not opt or not spot:
        st.info(f"Upload Option and NIFTY Spot CSV files for {period_label}. India VIX is optional."); return
    if st.button("ANALYZE PATTERNS", use_container_width=True):
        bar = st.progress(0, text="FRIDAY: 0%"); status = st.empty()
        try:
            bar.progress(10, text="FRIDAY: 10% — Reading files")
            o = normalize_options(read_csvs(opt)); s = normalize_spot(read_csvs(spot)); v = normalize_vix(read_csvs(vix)) if vix else pd.DataFrame()
            bar.progress(50, text="FRIDAY: 50% — Synchronizing"); m = synchronize(o, s, v)
            if m.empty: raise ValueError("No Option/Spot timestamps overlap within 20 minutes.")
            bar.progress(65, text="FRIDAY: 65% — Pairing CE/PE"); p = pair_ce_pe(m)
            if p.empty: raise ValueError("No CE/PE pairs found within ±2 minutes for the same strike and expiry.")
            bar.progress(75, text="FRIDAY: 75% — Selecting ATM"); f = build_features(m)
            if f.empty: raise ValueError("CE/PE pairing worked, but no ATM observations could be built.")
            bar.progress(90, text="FRIDAY: 90% — Measuring patterns"); patterns = discover_patterns(f); bar.progress(100, text="FRIDAY: 100% ✅"); status.success(f"Analysis complete — {len(f):,} ATM observations")
            diag = diagnostics(o, s, v, m, p, f)
            report_text, safe_label = build_report(period_label, o, s, v, m, p, f, patterns)
            st.subheader("Input Diagnostics"); st.dataframe(diag, use_container_width=True, hide_index=True)
            st.subheader("Pattern Summary")
            if patterns.empty: st.warning("No pattern produced at least 10 usable forward observations.")
            else: st.dataframe(patterns, use_container_width=True, hide_index=True)
            st.subheader("Feature Data"); st.dataframe(f.tail(500), use_container_width=True, hide_index=True)
            st.subheader("Downloadable Research Report")
            report_md = report_text.encode("utf-8")
            st.download_button("DOWNLOAD REPORT (.md)", report_md, f"FRIDAY_{safe_label}_REPORT.md", "text/markdown", use_container_width=True)
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr(f"FRIDAY_{safe_label}_REPORT.md", report_text)
                z.writestr(f"FRIDAY_{safe_label}_features.csv", f.to_csv(index=False))
                z.writestr(f"FRIDAY_{safe_label}_patterns.csv", patterns.to_csv(index=False))
                z.writestr(f"FRIDAY_{safe_label}_diagnostics.csv", diag.to_csv(index=False))
                z.writestr(f"FRIDAY_{safe_label}_pairs.csv", p.to_csv(index=False))
            st.download_button("DOWNLOAD FULL RESEARCH PACKAGE (.zip)", out.getvalue(), f"FRIDAY_{safe_label}_RESEARCH.zip", "application/zip", use_container_width=True)
        except Exception as e:
            status.error(f"Processing stopped: {e}"); st.exception(e)


with st.sidebar:
    st.subheader("FRIDAY")
    client_id = st.text_input("Dhan Client ID", value=DEFAULT_CLIENT_ID).strip()
    token = st.text_input("Dhan Access Token", value="", type="password").strip()
    if token: st.info("Token entered — verification occurs when Dhan is called.")
    module = st.radio("MODULE", ["Data Vault", "Pattern Research"], index=0)
    if module == "Pattern Research":
        period = st.selectbox("RESEARCH PERIOD", ["Q1 2024", "Q2 2024", "Q3 2024", "Q4 2024", "Full 2024", "Custom period"])
        if period == "Custom period":
            custom = st.text_input("Period label", value="Custom 2024")
            period = custom.strip() or "Custom period"

if module == "Data Vault": render_data_vault(token, client_id)
else: render_analyzer(period)
