import io
import zipfile
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st

DEFAULT_CLIENT_ID = "1113195747"
IST = ZoneInfo("Asia/Kolkata")


def parse_datetime(values):
    s = pd.Series(values)
    if s.empty:
        return pd.Series([], dtype="datetime64[ns]")
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0.0
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(s, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt.dt.tz_convert(IST).dt.tz_localize(None).astype("datetime64[ns]")


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


def numeric_col(df, names):
    c = find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def read_csvs(files):
    frames = []
    for file in files or []:
        df = pd.read_csv(file, low_memory=False)
        if not df.empty:
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


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
        med = float(n.dropna().abs().median()) if n.notna().any() else 0.0
        if med >= 1e12:
            dt = pd.to_datetime(n, unit="ms", errors="coerce")
        elif med >= 1e9:
            dt = pd.to_datetime(n, unit="s", errors="coerce")
    return dt.dt.date


def normalize_options(df):
    x = normalize_time(df)
    strike_col = find_col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side_col = find_col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry_col = find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike_col is None or side_col is None:
        raise ValueError(f"Options require strike and CE/PE columns. Found: {list(df.columns)}")

    r = pd.DataFrame({
        "timestamp": x["timestamp"],
        "strike": pd.to_numeric(x[strike_col], errors="coerce"),
        "side": x[side_col].astype(str).str.upper().str.strip(),
    })
    r["side"] = r["side"].replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
    r["expiry"] = normalize_expiry(x[expiry_col]) if expiry_col else pd.Series(pd.NaT, index=x.index)
    for names, target in [
        (["close", "ltp", "last_price", "price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["volume", "vol", "traded_volume"], "volume"),
    ]:
        v = numeric_col(x, names)
        r[target] = v if v is not None else np.nan
    r = r.dropna(subset=["timestamp", "strike"])
    r = r[r["side"].isin(["CE", "PE"])]
    return r.drop_duplicates(["timestamp", "expiry", "strike", "side"], keep="last").sort_values("timestamp").reset_index(drop=True)


def normalize_spot(df):
    x = normalize_time(df)
    p = numeric_col(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if p is None:
        raise ValueError("NIFTY Spot has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x["timestamp"], "nifty_spot": p}).dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    x = normalize_time(df)
    p = numeric_col(x, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if p is None:
        raise ValueError("India VIX has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x["timestamp"], "vix_close": p}).dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def synchronize(options, spot, vix):
    if options.empty or spot.empty:
        return pd.DataFrame()
    o = options.sort_values("timestamp").copy()
    s = spot.sort_values("timestamp").copy()
    o["timestamp"] = o["timestamp"].astype("datetime64[ns]")
    s["timestamp"] = s["timestamp"].astype("datetime64[ns]")
    merged = pd.merge_asof(o, s, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20)).dropna(subset=["nifty_spot"])
    if not vix.empty and not merged.empty:
        vx = vix.sort_values("timestamp").copy()
        vx["timestamp"] = vx["timestamp"].astype("datetime64[ns]")
        merged = pd.merge_asof(merged.sort_values("timestamp"), vx, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20))
    return merged.sort_values("timestamp").reset_index(drop=True)


def build_features(m):
    if m.empty:
        return pd.DataFrame()
    x = m.copy()
    x["strike_num"] = pd.to_numeric(x["strike"], errors="coerce")
    x = x.dropna(subset=["strike_num", "nifty_spot"])
    if x.empty:
        return pd.DataFrame()

    x["expiry_dt"] = pd.to_datetime(x["expiry"], errors="coerce")
    x = x[x["expiry_dt"].isna() | (x["expiry_dt"].dt.normalize() >= x["timestamp"].dt.normalize())].copy()
    if x.empty:
        return pd.DataFrame()
    nearest = x.groupby("timestamp")["expiry_dt"].transform("min")
    x = x[x["expiry_dt"].isna() | (x["expiry_dt"] == nearest)].copy()
    if x.empty:
        return pd.DataFrame()

    key = ["timestamp", "expiry_dt", "strike_num"]
    wide = x.pivot_table(index=key, columns="side", values=["close", "iv", "oi", "volume"], aggfunc="last")
    if not isinstance(wide.columns, pd.MultiIndex):
        return pd.DataFrame()
    wide = wide.reset_index()
    wide.columns = ["_".join(str(v) for v in col if str(v) not in ("", "None")) if isinstance(col, tuple) else str(col) for col in wide.columns]

    def wcol(metric, side):
        target = f"{metric}_{side}"
        if target in wide.columns:
            return target
        for c in wide.columns:
            if metric in str(c) and side in str(c):
                return c
        return None

    ce_close = wcol("close", "CE")
    pe_close = wcol("close", "PE")
    if ce_close is None or pe_close is None:
        return pd.DataFrame()
    wide = wide.dropna(subset=[ce_close, pe_close])
    if wide.empty:
        return pd.DataFrame()

    spot_key = x.groupby(key, as_index=False)["nifty_spot"].first()
    wide = wide.merge(spot_key, on=key, how="left")
    wide["atm_dist"] = (wide["strike_num"] - wide["nifty_spot"]).abs()
    atm = wide.sort_values(["timestamp", "atm_dist"]).drop_duplicates("timestamp", keep="first").copy()
    if atm.empty:
        return pd.DataFrame()

    oi_all = x.pivot_table(index=["timestamp", "expiry_dt"], columns="side", values="oi", aggfunc="sum")
    atm_index = pd.MultiIndex.from_frame(atm[["timestamp", "expiry_dt"]])
    oi_all = oi_all.reindex(atm_index)
    ce_oi = oi_all["CE"].to_numpy() if "CE" in oi_all.columns else np.full(len(atm), np.nan)
    pe_oi = oi_all["PE"].to_numpy() if "PE" in oi_all.columns else np.full(len(atm), np.nan)
    pcr = np.divide(pe_oi, ce_oi, out=np.full(len(atm), np.nan), where=np.isfinite(ce_oi) & (ce_oi != 0))

    def arr(metric, side):
        c = wcol(metric, side)
        return atm[c].to_numpy() if c else np.full(len(atm), np.nan)

    f = pd.DataFrame({
        "timestamp": atm["timestamp"].to_numpy(),
        "nifty_spot": atm["nifty_spot"].to_numpy(),
        "atm_strike": atm["strike_num"].to_numpy(),
        "ce_close": arr("close", "CE"), "pe_close": arr("close", "PE"),
        "ce_iv": arr("iv", "CE"), "pe_iv": arr("iv", "PE"),
        "ce_oi": arr("oi", "CE"), "pe_oi": arr("oi", "PE"),
        "ce_volume": arr("volume", "CE"), "pe_volume": arr("volume", "PE"),
        "pcr_oi": pcr,
    })
    vix_by_ts = x.groupby("timestamp")["vix_close"].last() if "vix_close" in x else pd.Series(dtype=float)
    f["vix_close"] = pd.to_numeric(vix_by_ts.reindex(f["timestamp"]).to_numpy(), errors="coerce")
    f["straddle"] = f["ce_close"] + f["pe_close"]
    f["atm_iv"] = pd.concat([f["ce_iv"], f["pe_iv"]], axis=1).mean(axis=1)
    f = f.sort_values("timestamp").reset_index(drop=True)

    f["spot_ret_1"] = f["nifty_spot"].pct_change()
    f["spot_ret_4"] = f["nifty_spot"].pct_change(4)
    f["spot_ret_16"] = f["nifty_spot"].pct_change(16)
    f["spot_vol_8"] = f["spot_ret_1"].rolling(8).std()
    f["spot_ma_8"] = f["nifty_spot"].rolling(8).mean()
    f["spot_ma_32"] = f["nifty_spot"].rolling(32).mean()
    f["spot_trend"] = f["spot_ma_8"] - f["spot_ma_32"]
    f["straddle_change"] = f["straddle"].diff()
    f["straddle_ret"] = f["straddle"].pct_change()
    f["iv_change"] = f["atm_iv"].diff()
    f["vix_change"] = f["vix_close"].diff()
    f["vix_ret"] = f["vix_close"].pct_change()
    f["pcr_change"] = f["pcr_oi"].diff()
    f["forward_spot_4"] = f["nifty_spot"].shift(-4) / f["nifty_spot"] - 1
    f["forward_spot_16"] = f["nifty_spot"].shift(-16) / f["nifty_spot"] - 1
    f["forward_straddle_4"] = f["straddle"].shift(-4) / f["straddle"] - 1
    f["forward_straddle_16"] = f["straddle"].shift(-16) / f["straddle"] - 1
    return f


def discover_patterns(f):
    rules = [
        ("IV rising + spot flat", (f["iv_change"] > 0) & (f["spot_ret_4"].abs() < 0.001)),
        ("IV falling + spot flat", (f["iv_change"] < 0) & (f["spot_ret_4"].abs() < 0.001)),
        ("PCR rising", f["pcr_change"] > 0), ("PCR falling", f["pcr_change"] < 0),
        ("VIX rising", f["vix_change"] > 0), ("VIX falling", f["vix_change"] < 0),
        ("Straddle expanding", f["straddle_change"] > 0), ("Straddle contracting", f["straddle_change"] < 0),
        ("Spot uptrend", f["spot_trend"] > 0), ("Spot downtrend", f["spot_trend"] < 0),
    ]
    rows = []
    for name, mask in rules:
        d = f.loc[mask].dropna(subset=["forward_spot_4", "forward_straddle_4"])
        if len(d) >= 10:
            rows.append({
                "pattern": name, "observations": len(d),
                "avg_next_4_spot_pct": d["forward_spot_4"].mean(),
                "avg_next_16_spot_pct": d["forward_spot_16"].mean(),
                "avg_next_4_straddle_pct": d["forward_straddle_4"].mean(),
                "avg_next_16_straddle_pct": d["forward_straddle_16"].mean(),
                "next_4_spot_up_rate": (d["forward_spot_4"] > 0).mean(),
                "next_4_straddle_up_rate": (d["forward_straddle_4"] > 0).mean(),
            })
    return pd.DataFrame(rows).sort_values("observations", ascending=False) if rows else pd.DataFrame()


def diagnostics(o, s, v, m, f):
    return pd.DataFrame([
        ["Option rows", len(o)], ["Spot rows", len(s)], ["VIX rows", len(v)], ["Synchronized rows", len(m)], ["ATM observations", len(f)],
        ["CE rows", int((o["side"] == "CE").sum()) if not o.empty else 0], ["PE rows", int((o["side"] == "PE").sum()) if not o.empty else 0],
        ["Option start", str(o["timestamp"].min()) if not o.empty else "N/A"], ["Option end", str(o["timestamp"].max()) if not o.empty else "N/A"],
        ["Spot start", str(s["timestamp"].min()) if not s.empty else "N/A"], ["Spot end", str(s["timestamp"].max()) if not s.empty else "N/A"],
    ], columns=["Check", "Value"])


def render_app():
    st.set_page_config(page_title="FRIDAY", page_icon="📊", layout="wide")
    st.title("FRIDAY — OPTION PATTERN RESEARCH")
    st.caption("Standalone build — Options + NIFTY Spot + India VIX. Futures are excluded.")

    with st.sidebar:
        st.header("FRIDAY")
        st.text_input("Dhan Client ID", DEFAULT_CLIENT_ID, disabled=True)
        st.info("Futures are currently outside the design.")

    a, b, c = st.columns(3)
    with a:
        opt_files = st.file_uploader("Q1 Option Data", type=["csv"], accept_multiple_files=True, key="q1_options")
    with b:
        spot_files = st.file_uploader("Q1 NIFTY Spot Data", type=["csv"], accept_multiple_files=True, key="q1_spot")
    with c:
        vix_files = st.file_uploader("Q1 India VIX Data", type=["csv"], accept_multiple_files=True, key="q1_vix")

    if not opt_files or not spot_files:
        st.info("Upload Q1 Option + Spot data. VIX is optional but recommended.")
        return

    if not st.button("ANALYZE PATTERNS", type="primary", use_container_width=True):
        return

    progress = st.progress(0, text="FRIDAY: 0%")
    status = st.empty()

    try:
        progress.progress(10, text="FRIDAY: 10% — Reading files")
        status.info("Reading CSV files…")
        options_raw = read_csvs(opt_files)
        spot_raw = read_csvs(spot_files)
        vix_raw = read_csvs(vix_files) if vix_files else pd.DataFrame()

        progress.progress(25, text="FRIDAY: 25% — Normalizing Options")
        options = normalize_options(options_raw)

        progress.progress(40, text="FRIDAY: 40% — Normalizing NIFTY Spot")
        spot = normalize_spot(spot_raw)

        progress.progress(50, text="FRIDAY: 50% — Normalizing India VIX")
        vix = normalize_vix(vix_raw) if not vix_raw.empty else pd.DataFrame(columns=["timestamp", "vix_close"])

        progress.progress(65, text="FRIDAY: 65% — Synchronizing")
        status.info("Matching option timestamps with Spot/VIX…")
        merged = synchronize(options, spot, vix)
        if merged.empty:
            raise ValueError("No Option/Spot timestamps overlap within 20 minutes. Check the file date ranges.")

        progress.progress(82, text="FRIDAY: 82% — Building ATM features")
        status.info("Finding matching CE + PE ATM observations…")
        features = build_features(merged)
        if features.empty:
            raise ValueError("Spot synchronization succeeded, but no valid CE + PE ATM pair was found. Check strike, option-type and expiry columns.")

        progress.progress(92, text="FRIDAY: 92% — Discovering patterns")
        patterns = discover_patterns(features)
        progress.progress(100, text="FRIDAY: 100% ✅")
        status.success(f"Analysis complete — {len(features):,} ATM observations.")

        st.subheader("Input Diagnostics")
        st.dataframe(diagnostics(options, spot, vix, merged, features), use_container_width=True, hide_index=True)

        if patterns.empty:
            st.warning("No pattern had at least 10 usable forward observations.")
        else:
            st.subheader("Pattern Summary")
            st.dataframe(patterns, use_container_width=True, hide_index=True)

        st.subheader("Feature Data")
        st.dataframe(features.tail(500), use_container_width=True, hide_index=True)

        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("FRIDAY_features.csv", features.to_csv(index=False))
            zf.writestr("FRIDAY_pattern_summary.csv", patterns.to_csv(index=False))
            zf.writestr("FRIDAY_diagnostics.csv", diagnostics(options, spot, vix, merged, features).to_csv(index=False))
        st.download_button("DOWNLOAD ANALYSIS ZIP", package.getvalue(), "FRIDAY_Q1_ANALYSIS.zip", "application/zip", use_container_width=True)

    except Exception as exc:
        progress.progress(100, text="FRIDAY: stopped safely")
        status.error(f"❌ FRIDAY stopped safely: {exc}")
        st.exception(exc)


render_app()
