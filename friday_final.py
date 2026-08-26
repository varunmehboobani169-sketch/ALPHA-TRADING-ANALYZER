import io
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st

TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_CLIENT_ID = "1113195747"


def parse_dt(s):
    s = pd.Series(s)
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(s, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt.dt.tz_convert(TZ).dt.tz_localize(None).astype("datetime64[ns]")


def col(df, names):
    m = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for n in names:
        k = n.lower().replace(" ", "_")
        if k in m:
            return m[k]
    for k, v in m.items():
        if any(n.lower().replace(" ", "_") in k for n in names):
            return v
    return None


def num(df, names):
    c = col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def normalize_time(df):
    if df.empty:
        return df
    c = col(df, ["timestamp", "datetime", "date_time", "timestamp_ist", "datetime_ist", "exchange_timestamp", "trade_time", "time", "date"])
    if c is None:
        raise ValueError(f"No timestamp column found. Columns: {list(df.columns)}")
    out = df.copy()
    out["timestamp"] = parse_dt(out[c])
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def read_files(files):
    frames = []
    for f in files or []:
        d = pd.read_csv(f, low_memory=False)
        if not d.empty:
            frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_spot(df):
    x = normalize_time(df)
    p = num(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if p is None:
        raise ValueError("NIFTY Spot: no close/price column found.")
    return pd.DataFrame({"timestamp": x.timestamp, "nifty_spot": p}).dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    x = normalize_time(df)
    p = num(x, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if p is None:
        raise ValueError("India VIX: no close/price column found.")
    return pd.DataFrame({"timestamp": x.timestamp, "vix_close": p}).dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def normalize_expiry(s):
    raw = pd.Series(s)
    dt = pd.to_datetime(raw, errors="coerce")
    n = pd.to_numeric(raw, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        if med >= 1e12:
            dt = pd.to_datetime(n, unit="ms", errors="coerce")
        elif med >= 1e9:
            dt = pd.to_datetime(n, unit="s", errors="coerce")
    return dt.dt.date


def normalize_options(df):
    x = normalize_time(df)
    strike = col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike is None or side is None:
        raise ValueError(f"Options need strike + CE/PE column. Columns: {list(df.columns)}")
    r = pd.DataFrame({"timestamp": x.timestamp, "strike": pd.to_numeric(x[strike], errors="coerce"), "side": x[side].astype(str).str.upper().str.strip()})
    r["side"] = r.side.replace({"C":"CE", "CALL":"CE", "P":"PE", "PUT":"PE"})
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


def synchronize(options, spot, vix):
    o = options.sort_values("timestamp")
    s = spot.sort_values("timestamp")
    m = pd.merge_asof(o, s, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20)).dropna(subset=["nifty_spot"])
    if not vix.empty and not m.empty:
        m = pd.merge_asof(m.sort_values("timestamp"), vix.sort_values("timestamp"), on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20))
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
    d = x.timestamp.dt.normalize()
    x = x[x.expiry_dt.isna() | (x.expiry_dt.dt.normalize() >= d)].copy()
    if x.empty:
        return pd.DataFrame()
    # Select the nearest valid expiry per timestamp without Python row loops.
    minexp = x.groupby("timestamp")["expiry_dt"].transform("min")
    x = x[x.expiry_dt.isna() | (x.expiry_dt == minexp)].copy()
    # CE/PE pairing by timestamp + expiry + strike.
    key = ["timestamp", "expiry_dt", "strike_num"]
    wide = x.pivot_table(index=key, columns="side", values=["close", "iv", "oi", "volume"], aggfunc="last")
    if not isinstance(wide.columns, pd.MultiIndex):
        return pd.DataFrame()
    wide = wide.reset_index()
    wide.columns = ["_".join([str(a), str(b)]) if isinstance(a, tuple) and b != "" else str(a) for a, b in wide.columns]
    # Pandas can name the flattened columns slightly differently; locate them robustly.
    def wcol(metric, side):
        for c in wide.columns:
            if str(metric) in str(c) and str(side) in str(c):
                return c
        return None
    c_close, p_close = wcol("close", "CE"), wcol("close", "PE")
    if not c_close or not p_close:
        return pd.DataFrame()
    wide = wide.dropna(subset=[c_close, p_close])
    if wide.empty:
        return pd.DataFrame()
    spot_key = x.groupby(key, dropna=False).nifty_spot.first().reset_index()
    wide = wide.merge(spot_key, on=key, how="left")
    wide["atm_dist"] = (wide.strike_num - wide.nifty_spot).abs()
    atm = wide.sort_values(["timestamp", "atm_dist"]).drop_duplicates("timestamp", keep="first")
    if atm.empty:
        return pd.DataFrame()

    def value(metric, side, default=np.nan):
        c = wcol(metric, side)
        return atm[c].to_numpy() if c else np.full(len(atm), default)

    oi_all = x.pivot_table(index="timestamp", columns="side", values="oi", aggfunc="sum").reindex(atm.timestamp)
    ceoi = oi_all["CE"].to_numpy() if "CE" in oi_all else np.full(len(atm), np.nan)
    peoi = oi_all["PE"].to_numpy() if "PE" in oi_all else np.full(len(atm), np.nan)
    f = pd.DataFrame({
        "timestamp": atm.timestamp.to_numpy(), "nifty_spot": atm.nifty_spot.to_numpy(), "atm_strike": atm.strike_num.to_numpy(),
        "ce_close": value("close", "CE"), "pe_close": value("close", "PE"),
        "ce_iv": value("iv", "CE"), "pe_iv": value("iv", "PE"),
        "ce_oi": value("oi", "CE"), "pe_oi": value("oi", "PE"),
        "ce_volume": value("volume", "CE"), "pe_volume": value("volume", "PE"),
        "pcr_oi": np.divide(peoi, ceoi, out=np.full(len(peoi), np.nan), where=np.isfinite(ceoi) & (ceoi != 0)),
    })
    vix_by_ts = x.groupby("timestamp").vix_close.last() if "vix_close" in x else pd.Series(dtype=float)
    f["vix_close"] = pd.to_numeric(vix_by_ts.reindex(f.timestamp).to_numpy(), errors="coerce")
    f["straddle"] = f.ce_close + f.pe_close
    f["atm_iv"] = pd.concat([f.ce_iv, f.pe_iv], axis=1).mean(axis=1)
    f = f.sort_values("timestamp").reset_index(drop=True)
    f["spot_ret_1"] = f.nifty_spot.pct_change(); f["spot_ret_4"] = f.nifty_spot.pct_change(4); f["spot_ret_16"] = f.nifty_spot.pct_change(16)
    f["spot_vol_8"] = f.spot_ret_1.rolling(8).std(); f["spot_ma_8"] = f.nifty_spot.rolling(8).mean(); f["spot_ma_32"] = f.nifty_spot.rolling(32).mean(); f["spot_trend"] = f.spot_ma_8 - f.spot_ma_32
    f["straddle_change"] = f.straddle.diff(); f["straddle_ret"] = f.straddle.pct_change(); f["iv_change"] = f.atm_iv.diff(); f["vix_change"] = f.vix_close.diff(); f["vix_ret"] = f.vix_close.pct_change(); f["pcr_change"] = f.pcr_oi.diff()
    f["forward_spot_4"] = f.nifty_spot.shift(-4) / f.nifty_spot - 1; f["forward_spot_16"] = f.nifty_spot.shift(-16) / f.nifty_spot - 1
    f["forward_straddle_4"] = f.straddle.shift(-4) / f.straddle - 1; f["forward_straddle_16"] = f.straddle.shift(-16) / f.straddle - 1
    return f


def discover_patterns(f):
    rules = [
        ("IV rising + spot flat", (f.iv_change > 0) & (f.spot_ret_4.abs() < .001)), ("IV falling + spot flat", (f.iv_change < 0) & (f.spot_ret_4.abs() < .001)),
        ("PCR rising", f.pcr_change > 0), ("PCR falling", f.pcr_change < 0), ("VIX rising", f.vix_change > 0), ("VIX falling", f.vix_change < 0),
        ("Straddle expanding", f.straddle_change > 0), ("Straddle contracting", f.straddle_change < 0), ("Spot uptrend", f.spot_trend > 0), ("Spot downtrend", f.spot_trend < 0),
    ]
    out = []
    for name, mask in rules:
        d = f.loc[mask].dropna(subset=["forward_spot_4", "forward_straddle_4"])
        if len(d) >= 10:
            out.append({"pattern": name, "observations": len(d), "avg_next_4_spot_pct": d.forward_spot_4.mean(), "avg_next_16_spot_pct": d.forward_spot_16.mean(), "avg_next_4_straddle_pct": d.forward_straddle_4.mean(), "avg_next_16_straddle_pct": d.forward_straddle_16.mean(), "next_4_spot_up_rate": (d.forward_spot_4 > 0).mean(), "next_4_straddle_up_rate": (d.forward_straddle_4 > 0).mean()})
    return pd.DataFrame(out).sort_values("observations", ascending=False) if out else pd.DataFrame()


def diagnostics(o, s, v, m, f):
    return pd.DataFrame([
        ["Option rows", len(o)], ["Spot rows", len(s)], ["VIX rows", len(v)], ["Synchronized rows", len(m)], ["ATM observations", len(f)],
        ["CE rows", int((o.side == "CE").sum()) if not o.empty else 0], ["PE rows", int((o.side == "PE").sum()) if not o.empty else 0],
        ["Option start", str(o.timestamp.min()) if not o.empty else "N/A"], ["Option end", str(o.timestamp.max()) if not o.empty else "N/A"],
        ["Spot start", str(s.timestamp.min()) if not s.empty else "N/A"], ["Spot end", str(s.timestamp.max()) if not s.empty else "N/A"],
    ], columns=["Check", "Value"])


def analysis_ui():
    st.title("FRIDAY — OPTION PATTERN RESEARCH")
    st.caption("Options + NIFTY Spot + India VIX. Futures are intentionally excluded.")
    opt = st.file_uploader("Option Data (CSV)", type=["csv"], accept_multiple_files=True)
    spot = st.file_uploader("NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True)
    vix = st.file_uploader("India VIX Data (CSV)", type=["csv"], accept_multiple_files=True)
    if not opt or not spot:
        st.info("Upload Q1 Option + Spot data. VIX is optional but recommended.")
        return
    if st.button("ANALYZE PATTERNS", use_container_width=True):
        bar = st.progress(0, text="FRIDAY: 0%")
        status = st.empty()
        try:
            status.info("10% — Reading files"); bar.progress(10, text="FRIDAY: 10% — Reading files")
            o = normalize_options(read_files(opt)); s = normalize_spot(read_files(spot)); v = normalize_vix(read_files(vix)) if vix else pd.DataFrame()
            status.info("40% — Synchronizing timestamps"); bar.progress(40, text="FRIDAY: 40% — Synchronizing")
            m = synchronize(o, s, v)
            if m.empty: raise ValueError("No Option/Spot timestamps overlap within 20 minutes.")
            status.info("70% — Finding ATM CE/PE pairs"); bar.progress(70, text="FRIDAY: 70% — Building ATM")
            f = build_features(m)
            if f.empty: raise ValueError("Spot synchronization worked, but no valid CE+PE ATM pair was found. Check option type/strike/expiry columns.")
            status.info("90% — Discovering patterns"); bar.progress(90, text="FRIDAY: 90% — Discovering patterns")
            p = discover_patterns(f)
            bar.progress(100, text="FRIDAY: 100% ✅")
            status.success(f"Complete — {len(f):,} ATM observations")
            st.subheader("Input Diagnostics"); st.dataframe(diagnostics(o, s, v, m, f), use_container_width=True, hide_index=True)
            if p.empty: st.warning("No pattern had 10+ usable observations.")
            else: st.subheader("Pattern Summary"); st.dataframe(p, use_container_width=True, hide_index=True)
            st.subheader("Feature Data"); st.dataframe(f.tail(500), use_container_width=True, hide_index=True)
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv", f.to_csv(index=False)); z.writestr("FRIDAY_pattern_summary.csv", p.to_csv(index=False)); z.writestr("FRIDAY_diagnostics.csv", diagnostics(o,s,v,m,f).to_csv(index=False))
            st.download_button("DOWNLOAD ANALYSIS ZIP", b.getvalue(), "FRIDAY_Q1_PATTERN_ANALYSIS.zip", "application/zip", use_container_width=True)
        except Exception as e:
            bar.progress(0, text="FRIDAY: stopped")
            status.error(f"Processing stopped: {e}")


def main():
    st.session_state.setdefault("client_id", DEFAULT_CLIENT_ID)
    st.session_state.setdefault("access_token", "")
    st.session_state.setdefault("last_api_call", 0.0)
    with st.sidebar:
        st.title("FRIDAY")
        st.caption("Option Pattern Research Engine")
        st.session_state.client_id = st.text_input("Dhan Client ID", st.session_state.client_id).strip()
        st.session_state.access_token = st.text_input("Dhan Access Token", st.session_state.access_token, type="password").strip()
    analysis_ui()

main()
