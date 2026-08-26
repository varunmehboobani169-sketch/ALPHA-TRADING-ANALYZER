import io
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st

DEFAULT_CLIENT_ID = "1113195747"

st.set_page_config(page_title="FRIDAY", layout="wide")


def parse_datetime(values):
    s = pd.Series(values)
    if s.empty:
        return pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        if med >= 1e18:
            unit = "ns"
        elif med >= 1e15:
            unit = "us"
        elif med >= 1e12:
            unit = "ms"
        elif med >= 1e9:
            unit = "s"
        else:
            unit = None
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
    for norm, original in lookup.items():
        if any(name.lower().replace(" ", "_") in norm for name in names):
            return original
    return None


def num(df, names):
    c = find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def read_files(files):
    parts = []
    for f in files or []:
        d = pd.read_csv(f, low_memory=False)
        if not d.empty:
            parts.append(d)
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
    s = pd.Series(values)
    dt = pd.to_datetime(s, errors="coerce")
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        if med >= 1e12:
            dt = pd.to_datetime(n, unit="ms", errors="coerce")
        elif med >= 1e9:
            dt = pd.to_datetime(n, unit="s", errors="coerce")
    return dt.dt.date


def normalize_options(df):
    x = normalize_time(df)
    strike = find_col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = find_col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike is None or side is None:
        raise ValueError(f"Options require strike and CE/PE columns. Found: {list(df.columns)}")
    r = pd.DataFrame({
        "timestamp": x.timestamp,
        "strike": pd.to_numeric(x[strike], errors="coerce"),
        "side": x[side].astype(str).str.upper().str.strip(),
    })
    r["side"] = r.side.replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
    r["expiry"] = normalize_expiry(x[expiry]) if expiry else pd.Series(pd.NaT, index=x.index)
    for names, target in [
        (["close", "ltp", "last_price", "price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["volume", "vol", "traded_volume"], "volume"),
    ]:
        v = num(x, names)
        r[target] = v if v is not None else float("nan")
    r = r.dropna(subset=["timestamp", "strike"])
    r = r[r.side.isin(["CE", "PE"])]
    return r.drop_duplicates(["timestamp", "expiry", "strike", "side"], keep="last").reset_index(drop=True)


def normalize_spot(df):
    x = normalize_time(df)
    p = num(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if p is None:
        raise ValueError("NIFTY Spot has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "nifty_spot": p}).dropna().drop_duplicates("timestamp", keep="last").reset_index(drop=True)


def normalize_vix(df):
    x = normalize_time(df)
    p = num(x, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if p is None:
        raise ValueError("India VIX has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "vix_close": p}).dropna().drop_duplicates("timestamp", keep="last").reset_index(drop=True)


def synchronize(options, spot, vix):
    if options.empty or spot.empty:
        return pd.DataFrame()
    o = options.sort_values("timestamp").copy()
    s = spot.sort_values("timestamp").copy()
    o.timestamp = o.timestamp.astype("datetime64[ns]")
    s.timestamp = s.timestamp.astype("datetime64[ns]")
    m = pd.merge_asof(o, s, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20))
    m = m.dropna(subset=["nifty_spot"])
    if not vix.empty and not m.empty:
        vx = vix.sort_values("timestamp").copy()
        vx.timestamp = vx.timestamp.astype("datetime64[ns]")
        m = pd.merge_asof(m.sort_values("timestamp"), vx, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20))
    return m.sort_values("timestamp").reset_index(drop=True)


def build_features(m):
    if m.empty:
        return pd.DataFrame()
    x = m.copy()
    x["strike_num"] = pd.to_numeric(x.strike, errors="coerce")
    x = x.dropna(subset=["strike_num", "nifty_spot"])
    if x.empty:
        return pd.DataFrame()

    x["expiry_dt"] = pd.to_datetime(x.expiry, errors="coerce")
    day = x.timestamp.dt.normalize()
    x = x[x.expiry_dt.isna() | (x.expiry_dt.dt.normalize() >= day)].copy()
    if x.empty:
        return pd.DataFrame()
    nearest = x.groupby("timestamp")["expiry_dt"].transform("min")
    x = x[x.expiry_dt.isna() | (x.expiry_dt == nearest)].copy()
    if x.empty:
        return pd.DataFrame()

    key = ["timestamp", "expiry_dt", "strike_num"]
    wide = x.pivot_table(index=key, columns="side", values=["close", "iv", "oi", "volume"], aggfunc="last").reset_index()
    if not isinstance(wide.columns, pd.MultiIndex):
        return pd.DataFrame()
    wide.columns = ["_".join(str(v) for v in c if str(v) not in ("", "None")) if isinstance(c, tuple) else str(c) for c in wide.columns]

    def wcol(metric, side):
        exact = f"{metric}_{side}"
        if exact in wide.columns:
            return exact
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
    wide["atm_dist"] = (wide.strike_num - wide.nifty_spot).abs()
    atm = wide.sort_values(["timestamp", "atm_dist"]).drop_duplicates("timestamp", keep="first")
    if atm.empty:
        return pd.DataFrame()

    def arr(metric, side):
        c = wcol(metric, side)
        return atm[c].to_numpy() if c else [float("nan")] * len(atm)

    oi_all = x.pivot_table(index=["timestamp", "expiry_dt"], columns="side", values="oi", aggfunc="sum")
    atm_idx = pd.MultiIndex.from_frame(atm[["timestamp", "expiry_dt"]])
    oi_all = oi_all.reindex(atm_idx)
    ce_oi = oi_all["CE"] if "CE" in oi_all.columns else pd.Series(index=atm_idx, dtype=float)
    pe_oi = oi_all["PE"] if "PE" in oi_all.columns else pd.Series(index=atm_idx, dtype=float)
    pcr = pe_oi.reset_index(drop=True) / ce_oi.reset_index(drop=True)

    f = pd.DataFrame({
        "timestamp": atm.timestamp.to_numpy(),
        "nifty_spot": atm.nifty_spot.to_numpy(),
        "atm_strike": atm.strike_num.to_numpy(),
        "ce_close": arr("close", "CE"),
        "pe_close": arr("close", "PE"),
        "ce_iv": arr("iv", "CE"),
        "pe_iv": arr("iv", "PE"),
        "ce_oi": arr("oi", "CE"),
        "pe_oi": arr("oi", "PE"),
        "ce_volume": arr("volume", "CE"),
        "pe_volume": arr("volume", "PE"),
        "pcr_oi": pcr.to_numpy(),
    })
    if "vix_close" in x:
        vix_ts = x.groupby("timestamp").vix_close.last()
        f["vix_close"] = pd.to_numeric(vix_ts.reindex(f.timestamp).to_numpy(), errors="coerce")
    else:
        f["vix_close"] = float("nan")

    f["straddle"] = f.ce_close + f.pe_close
    f["atm_iv"] = pd.concat([f.ce_iv, f.pe_iv], axis=1).mean(axis=1)
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


def diagnostics(o, s, v, m, f):
    return pd.DataFrame({"Check": ["Option rows", "Spot rows", "VIX rows", "Synchronized rows", "ATM observations", "CE rows", "PE rows", "Option start", "Option end", "Spot start", "Spot end"], "Value": [len(o), len(s), len(v), len(m), len(f), int((o.side == "CE").sum()) if not o.empty else 0, int((o.side == "PE").sum()) if not o.empty else 0, str(o.timestamp.min()) if not o.empty else "N/A", str(o.timestamp.max()) if not o.empty else "N/A", str(s.timestamp.min()) if not s.empty else "N/A", str(s.timestamp.max()) if not s.empty else "N/A"]})


st.title("FRIDAY — OPTION PATTERN RESEARCH")
st.caption("Options + NIFTY Spot + India VIX only. Futures are excluded.")

with st.sidebar:
    st.subheader("FRIDAY")
    client_id = st.text_input("Dhan Client ID", value=DEFAULT_CLIENT_ID)
    st.caption(f"Client ID: {client_id or DEFAULT_CLIENT_ID}")

opt_files = st.file_uploader("Q1 Option Data (CSV)", type=["csv"], accept_multiple_files=True)
spot_files = st.file_uploader("Q1 NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True)
vix_files = st.file_uploader("Q1 India VIX Data (CSV)", type=["csv"], accept_multiple_files=True)

if not opt_files or not spot_files:
    st.info("Upload the Q1 Option and NIFTY Spot CSV files. India VIX is optional.")
else:
    if st.button("ANALYZE PATTERNS", use_container_width=True):
        bar = st.progress(0, text="FRIDAY: 0%")
        status = st.empty()
        try:
            status.info("10% — Reading CSV files")
            bar.progress(10, text="FRIDAY: 10% — Reading files")
            o_raw = read_files(opt_files)
            s_raw = read_files(spot_files)
            v_raw = read_files(vix_files) if vix_files else pd.DataFrame()

            status.info("30% — Normalizing data")
            bar.progress(30, text="FRIDAY: 30% — Normalizing Options / Spot / VIX")
            o = normalize_options(o_raw)
            s = normalize_spot(s_raw)
            v = normalize_vix(v_raw) if not v_raw.empty else pd.DataFrame()

            status.info("50% — Synchronizing timestamps")
            bar.progress(50, text="FRIDAY: 50% — Synchronizing")
            m = synchronize(o, s, v)
            if m.empty:
                raise ValueError("No Option/Spot timestamps overlap within 20 minutes.")

            status.info("70% — Finding ATM CE/PE pairs")
            bar.progress(70, text="FRIDAY: 70% — Finding ATM")
            f = build_features(m)
            if f.empty:
                raise ValueError("Spot synchronization worked, but no valid CE+PE ATM pair was found.")

            status.info("90% — Measuring historical patterns")
            bar.progress(90, text="FRIDAY: 90% — Measuring patterns")
            patterns = discover_patterns(f)

            bar.progress(100, text="FRIDAY: 100% ✅")
            status.success(f"Analysis complete — {len(f):,} ATM observations")

            st.subheader("Input Diagnostics")
            st.dataframe(diagnostics(o, s, v, m, f), use_container_width=True, hide_index=True)
            st.subheader("Pattern Summary")
            if patterns.empty:
                st.warning("No pattern produced at least 10 usable forward observations.")
            else:
                st.dataframe(patterns, use_container_width=True, hide_index=True)
            st.subheader("Feature Data")
            st.dataframe(f.tail(500), use_container_width=True, hide_index=True)

            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv", f.to_csv(index=False))
                z.writestr("FRIDAY_patterns.csv", patterns.to_csv(index=False))
                z.writestr("FRIDAY_diagnostics.csv", diagnostics(o, s, v, m, f).to_csv(index=False))
            st.download_button("DOWNLOAD ANALYSIS ZIP", out.getvalue(), "FRIDAY_Q1_ANALYSIS.zip", "application/zip", use_container_width=True)
        except Exception as exc:
            status.error(f"Processing stopped: {exc}")
            st.exception(exc)
