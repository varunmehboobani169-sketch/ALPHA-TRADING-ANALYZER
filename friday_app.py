import math
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO

import numpy as np
import pandas as pd
import requests
import streamlit as st
from zoneinfo import ZoneInfo

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import LabelEncoder
    from sklearn.impute import SimpleImputer
except Exception:
    RandomForestClassifier = None
    LabelEncoder = None
    SimpleImputer = None

st.set_page_config(page_title="FRIDAY • AI Option Strategist", page_icon="🤖", layout="wide")
API = "https://api.dhan.co/v2"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
NIFTY_ID = 13
VIX_ID_FALLBACK = 26
STRATEGIES = ["BUY CE", "SELL PE", "BULL CALL SPREAD", "BULL PUT SPREAD", "BUY PE", "SELL CE", "BEAR PUT SPREAD", "BEAR CALL SPREAD", "SHORT STRADDLE", "SHORT STRANGLE", "IRON CONDOR", "NO TRADE"]
REGIMES = ["BULLISH", "BEARISH", "SIDEWAYS", "UNCLEAR"]
RATE_LIMIT_SECONDS = 3.2


def now_ist():
    return datetime.now(LOCAL_TZ)


def init_state():
    defaults = {
        "client_id": "", "access_token": "", "model": None,
        "label_encoder": None, "imputer": None, "feature_columns": [],
        "training_summary": {}, "model_status": "NOT TRAINED", "live_features": {},
        "last_training_rows": 0, "last_api_call": 0.0,
        "uploaded_option_files": [], "uploaded_future_files": [],
        "uploaded_spot_files": [], "uploaded_vix_files": [],
        "prepared_training_data": None, "prepared_data_summary": {},
        "quarterly_reports": {}, "friday_master_review": None,
        "friday_vault_cache": {}
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
    response = requests.post(API + path, headers=headers(), json=payload, timeout=45)
    st.session_state.last_api_call = time.monotonic()
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    if response.status_code == 429:
        time.sleep(3.5)
        response = requests.post(API + path, headers=headers(), json=payload, timeout=45)
        st.session_state.last_api_call = time.monotonic()
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
    if not response.ok:
        raise RuntimeError(
            f"{label}: HTTP {response.status_code}: "
            f"{body.get('errorMessage') or body.get('remarks') or body.get('message') or str(body)[:500]}"
        )
    return body


def parse_data(body):
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


def parse_chart_response(body):
    d = parse_data(body)
    if not isinstance(d, dict):
        return pd.DataFrame()
    timestamps = d.get("timestamp")
    if not timestamps:
        return pd.DataFrame()
    ts = pd.to_datetime(
        pd.to_numeric(pd.Series(timestamps), errors="coerce"),
        unit="s", utc=True, errors="coerce"
    ).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    n = len(ts)
    out = pd.DataFrame({"datetime": ts})
    for src, dst in [
        ("open", "open"), ("high", "high"), ("low", "low"),
        ("close", "close"), ("volume", "volume"),
    ]:
        vals = d.get(src)
        if vals is not None and len(vals) == n:
            out[dst] = pd.to_numeric(pd.Series(vals), errors="coerce")
    return out.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)


@st.cache_data(ttl=21600, show_spinner=False)
def load_master():
    df = pd.read_csv(
        "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        low_memory=False
    )
    df.columns = [str(c).strip() for c in df.columns]
    rename = {
        "EXCH_ID": "exchange", "SEGMENT": "segment", "INSTRUMENT": "instrument",
        "SECURITY_ID": "security_id", "UNDERLYING_SECURITY_ID": "underlying_security_id",
        "UNDERLYING_SYMBOL": "underlying_symbol", "SYMBOL_NAME": "symbol_name",
        "SEM_TRADING_SYMBOL": "trading_symbol", "DISPLAY_NAME": "display_name",
        "EXPIRY_DATE": "expiry_date"
    }
    df = df.rename(columns={c: rename.get(c, c) for c in df.columns})
    if "security_id" not in df.columns:
        for c in ["SM_SECURITY_ID", "SEM_SECURITY_ID"]:
            if c in df.columns:
                df["security_id"] = df[c]
                break
    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")
    return df.dropna(subset=["security_id"]).copy()


def resolve_vix_id(master):
    for col in ["underlying_symbol", "symbol_name", "trading_symbol", "display_name"]:
        if col in master.columns:
            s = master[col].astype(str).str.upper().str.replace(" ", "", regex=False)
            rows = master[s.str.contains("INDIAVIX", regex=False, na=False) | s.eq("VIX")]
            ids = pd.to_numeric(rows["security_id"], errors="coerce").dropna()
            if not ids.empty:
                return int(ids.iloc[0])
    return VIX_ID_FALLBACK


def _find_column(df, candidates):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for c in candidates:
        k = c.lower().replace(" ", "_")
        if k in lookup:
            return lookup[k]
    for norm, orig in lookup.items():
        if any(c.lower().replace(" ", "_") in norm for c in candidates):
            return orig
    return None


def _read_uploaded_csvs(files, label):
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        df = pd.read_csv(f, low_memory=False)
        if not df.empty:
            df["_source_file"] = getattr(f, "name", label)
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _normalize_time_column(df):
    if df.empty:
        return df
    c = _find_column(df, ["timestamp", "datetime", "date_time", "date", "time", "timestamp_ist", "datetime_ist"])
    if c is None:
        return df
    out = df.copy()
    dt = pd.to_datetime(out[c], errors="coerce")
    try:
        if getattr(dt.dt, "tz", None) is not None:
            dt = dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception:
        pass
    out["timestamp"] = dt
    return out.dropna(subset=["timestamp"]).sort_values("timestamp")


def _pick_numeric(df, candidates):
    c = _find_column(df, candidates)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def _normalize_spot(df):
    out = _normalize_time_column(df)
    p = _pick_numeric(out, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if out.empty or p is None:
        return pd.DataFrame(columns=["timestamp", "nifty_spot"])
    r = pd.DataFrame({"timestamp": out["timestamp"].values, "nifty_spot": p.values})
    return r.dropna()


def _normalize_future(df):
    out = _normalize_time_column(df)
    r = pd.DataFrame({"timestamp": out.get("timestamp", pd.Series(dtype="datetime64[ns]"))})
    if out.empty:
        return pd.DataFrame(columns=["timestamp", "future_open", "future_high", "future_low", "future_close"])
    for names, target in [
        (["open"], "future_open"), (["high"], "future_high"),
        (["low"], "future_low"), (["close", "ltp", "last_price"], "future_close")
    ]:
        v = _pick_numeric(out, names)
        r[target] = v.values if v is not None else np.nan
    return r.dropna(subset=["timestamp"])


def _normalize_vix(df):
    out = _normalize_time_column(df)
    r = pd.DataFrame({"timestamp": out.get("timestamp", pd.Series(dtype="datetime64[ns]"))})
    if out.empty:
        return pd.DataFrame(columns=["timestamp", "vix_open", "vix_high", "vix_low", "vix_close"])
    for names, target in [
        (["open"], "vix_open"), (["high"], "vix_high"),
        (["low"], "vix_low"), (["close", "ltp", "last_price"], "vix_close")
    ]:
        v = _pick_numeric(out, names)
        r[target] = v.values if v is not None else np.nan
    return r.dropna(subset=["timestamp"])


def _normalize_options(df):
    out = _normalize_time_column(df)
    sc = _find_column(out, ["strike", "strike_price", "strikeprice"])
    sidec = _find_column(out, ["side", "option_type", "type", "cp", "ce_pe"])
    expc = _find_column(out, ["expiry", "expiry_date", "expirydate", "exp_date"])
    ivc = _find_column(out, ["iv", "implied_volatility", "impliedvolatility"])
    oic = _find_column(out, ["oi", "open_interest", "openinterest"])
    volc = _find_column(out, ["volume", "vol"])
    if out.empty or sc is None or sidec is None:
        raise ValueError("Options file must contain recognizable timestamp, strike and CE/PE/option type columns.")
    r = pd.DataFrame({
        "timestamp": out["timestamp"].values,
        "strike": pd.to_numeric(out[sc], errors="coerce").values,
        "side": out[sidec].astype(str).str.upper().str.strip().values
    })
    r["side"] = r["side"].replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
    r["expiry"] = pd.to_datetime(out[expc], errors="coerce").dt.date if expc else pd.NaT
    r["iv"] = pd.to_numeric(out[ivc], errors="coerce").values if ivc else np.nan
    r["oi"] = pd.to_numeric(out[oic], errors="coerce").values if oic else np.nan
    r["volume"] = pd.to_numeric(out[volc], errors="coerce").values if volc else np.nan
    close = _pick_numeric(out, ["close", "ltp", "last_price"])
    r["close"] = close.values if close is not None else np.nan
    return r.dropna(subset=["timestamp", "strike"])


def _build_option_features(options, spot):
    if options.empty or spot.empty:
        return pd.DataFrame()
    opt = options.sort_values("timestamp")
    sp = spot.sort_values("timestamp")[["timestamp", "nifty_spot"]]
    opt = pd.merge_asof(
        opt, sp, on="timestamp", direction="backward", tolerance=pd.Timedelta("10min")
    ).dropna(subset=["nifty_spot", "strike", "side"])
    rows = []
    for ts, g in opt.groupby("timestamp", sort=True):
        if g["expiry"].notna().any():
            exps = [e for e in g["expiry"].dropna().unique() if e >= ts.date()]
            if exps:
                g = g[g["expiry"] == min(exps)]
        if g.empty:
            continue
        target = float(g.iloc[0].nifty_spot)
        strikes = g.strike.dropna().unique()
        atm = min(strikes, key=lambda x: abs(float(x) - target))
        a = g[np.isclose(g.strike.astype(float), float(atm))]
        ce = a[a.side == "CE"]
        pe = a[a.side == "PE"]
        if ce.empty or pe.empty:
            continue
        ce = ce.iloc[-1]
        pe = pe.iloc[-1]
        call_oi = g.loc[g.side == "CE", "oi"].sum(min_count=1)
        put_oi = g.loc[g.side == "PE", "oi"].sum(min_count=1)
        pcr = put_oi / call_oi if pd.notna(put_oi) and pd.notna(call_oi) and call_oi != 0 else np.nan
        rows.append({
            "timestamp": ts, "nifty_spot": target, "atm_strike": float(atm),
            "ce_iv": ce.iv, "pe_iv": pe.iv, "atm_iv": np.nanmean([ce.iv, pe.iv]),
            "ce_close": ce.close, "pe_close": pe.close,
            "straddle": ce.close + pe.close if pd.notna(ce.close) and pd.notna(pe.close) else np.nan,
            "pcr_oi": pcr
        })
    return pd.DataFrame(rows).sort_values("timestamp") if rows else pd.DataFrame()


def _build_quarterly_reports(feature):
    reports = {}
    if feature is None or feature.empty or "timestamp" not in feature.columns:
        return reports
    x = feature.copy()
    x["timestamp"] = pd.to_datetime(x.timestamp, errors="coerce")
    x = x.dropna(subset=["timestamp"]).sort_values("timestamp")
    x["quarter"] = x.timestamp.map(lambda t: f"{t.year} Q{t.quarter}")
    for q, qdf in x.groupby("quarter", sort=True):
        summary = {
            "quarter": q, "rows": len(qdf), "start": str(qdf.timestamp.min()),
            "end": str(qdf.timestamp.max()),
            "avg_atm_iv": float(qdf.atm_iv.mean()) if "atm_iv" in qdf else np.nan,
            "avg_pcr_oi": float(qdf.pcr_oi.mean()) if "pcr_oi" in qdf else np.nan
        }
        daily = qdf.assign(date=qdf.timestamp.dt.date).groupby("date", as_index=False).agg(
            nifty_open=("nifty_spot", "first"), nifty_close=("nifty_spot", "last"),
            atm_iv_open=("atm_iv", "first"), atm_iv_close=("atm_iv", "last"),
            avg_pcr_oi=("pcr_oi", "mean"), avg_straddle=("straddle", "mean")
        )
        daily["nifty_change_pct"] = daily.nifty_close / daily.nifty_open - 1
        daily["iv_change"] = daily.atm_iv_close - daily.atm_iv_open
        reports[q] = {"summary": pd.DataFrame([summary]), "daily": daily, "features": qdf.copy()}
    return reports


def _quarterly_report_zip(reports, selected=None):
    if not reports:
        return None
    b = BytesIO()
    rows = []
    with zipfile.ZipFile(b, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for q in selected or sorted(reports):
            if q not in reports:
                continue
            p = reports[q]
            safe = q.replace(" ", "_")
            z.writestr(f"{safe}/quarterly_summary.csv", p["summary"].to_csv(index=False))
            z.writestr(f"{safe}/daily_analysis.csv", p["daily"].to_csv(index=False))
            z.writestr(f"{safe}/decision_features.csv", p["features"].to_csv(index=False))
            rows.append(p["summary"])
        if rows:
            z.writestr("MASTER_quarterly_summary.csv", pd.concat(rows, ignore_index=True).to_csv(index=False))
    b.seek(0)
    return b


def _read_quarterly_zip_files(files):
    rows = []
    for f in files:
        with zipfile.ZipFile(f) as z:
            for name in z.namelist():
                if name.endswith("/quarterly_summary.csv") or name == "MASTER_quarterly_summary.csv":
                    with z.open(name) as fh:
                        rows.append(pd.read_csv(fh))
    if not rows:
        raise ValueError("No quarterly_summary.csv found in uploaded ZIPs.")
    return pd.concat(rows, ignore_index=True).drop_duplicates()


def _resolve_historical_nifty_future(master, year, quarter):
    """Legacy single-contract resolver retained for compatibility; returns a candidate only."""
    x = master.copy()
    if "exchange" in x.columns:
        x = x[x.exchange.astype(str).str.upper().eq("NSE")]
    if "instrument" in x.columns:
        x = x[x.instrument.astype(str).str.upper().str.contains("FUTIDX", regex=False, na=False)]
    mask = pd.Series(False, index=x.index)
    for c in ["underlying_symbol", "symbol_name", "trading_symbol", "display_name"]:
        if c in x.columns:
            s = x[c].astype(str).str.upper().str.strip()
            mask |= s.eq("NIFTY") | s.str.startswith("NIFTY", na=False)
    x = x[mask].copy()
    if x.empty or "expiry_date" not in x.columns:
        return None
    x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")
    x = x.dropna(subset=["expiry_date", "security_id"])
    x["security_id"] = pd.to_numeric(x.security_id, errors="coerce")
    x = x.dropna(subset=["security_id"])
    q = pd.Period(f"{year}-Q{quarter}")
    q_end = q.end_time
    after = x[x.expiry_date >= q_end].sort_values("expiry_date")
    if not after.empty:
        row = after.iloc[0]
    else:
        prior = x[x.expiry_date <= q_end].sort_values("expiry_date")
        if prior.empty:
            return None
        row = prior.iloc[-1]
    return int(row.security_id), "NSE_FNO", "FUTIDX"


def _resolve_basic_instrument(master, dataset):
    if dataset == "NIFTY Spot":
        return (13, "IDX_I", "INDEX")
    if dataset == "India VIX":
        return (resolve_vix_id(master), "IDX_I", "INDEX")
    return None


def _friday_master_contracts(master, year, quarter):
    """Find all actual NIFTY index-futures contracts relevant to the selected quarter."""
    x = master.copy()
    if "security_id" not in x.columns:
        return pd.DataFrame()
    x["security_id"] = pd.to_numeric(x["security_id"], errors="coerce")
    x = x.dropna(subset=["security_id"]).copy()
    if "expiry_date" not in x.columns:
        return pd.DataFrame()
    x["_expiry"] = pd.to_datetime(x["expiry_date"], errors="coerce")
    x = x.dropna(subset=["_expiry"])

    text_cols = [c for c in x.columns if x[c].dtype == "object"]
    blob = x[text_cols].fillna("").astype(str).agg(" ".join, axis=1).str.upper() if text_cols else pd.Series("", index=x.index)
    nifty = blob.str.contains("NIFTY", regex=False, na=False)
    fut = blob.str.contains("FUTIDX|FUTURE", regex=True, na=False)
    y = x[nifty & fut].copy()

    if y.empty:
        mask = pd.Series(False, index=x.index)
        for c in ["underlying_symbol", "symbol_name", "trading_symbol", "display_name"]:
            if c in x.columns:
                s = x[c].fillna("").astype(str).str.upper().str.strip()
                mask |= s.eq("NIFTY") | s.str.startswith("NIFTY", na=False)
        if "instrument" in x.columns:
            ins = x["instrument"].fillna("").astype(str).str.upper()
            mask &= ins.eq("FUTIDX")
        y = x[mask].copy()

    if y.empty:
        return pd.DataFrame()

    q = pd.Period(f"{int(year)}-Q{int(quarter)}")
    qs, qe = pd.Timestamp(q.start_time.date()), pd.Timestamp(q.end_time.date())
    lo, hi = qs - pd.Timedelta(days=45), qe + pd.Timedelta(days=45)
    y = y[(y["_expiry"].dt.normalize() >= lo) & (y["_expiry"].dt.normalize() <= hi)].copy()
    if y.empty:
        return pd.DataFrame()

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
    """Attempt Dhan intraday OHLC for one exact futures contract."""
    interval_map = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60}
    interval = interval_map.get(timeframe, 15)
    chunks, cur, end = [], pd.Timestamp(start_dt), pd.Timestamp(end_dt)
    while cur <= end:
        chunk_end = min(cur + pd.Timedelta(days=89), end)
        payload = {
            "securityId": str(int(security_id)),
            "exchangeSegment": "NSE_FNO",
            "instrument": "FUTIDX",
            "interval": interval,
            "oi": bool(include_oi),
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


def _download_basic_dataset(sid, dataset, timeframe, year, quarter):
    q = pd.Period(f"{year}-Q{quarter}")
    start_dt, end_dt = pd.Timestamp(q.start_time), pd.Timestamp(q.end_time)
    segment, instrument = "IDX_I", "INDEX"
    if timeframe == "Daily":
        body = api_post(
            "/charts/historical",
            {
                "securityId": str(int(sid)), "exchangeSegment": segment,
                "instrument": instrument, "expiryCode": 0, "oi": False,
                "fromDate": start_dt.strftime("%Y-%m-%d"), "toDate": end_dt.strftime("%Y-%m-%d")
            },
            f"{dataset} {year} Q{quarter}"
        )
        return parse_chart_response(body)

    interval = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60}.get(timeframe, 15)
    frames = []
    cur = start_dt
    while cur <= end_dt:
        ce = min(cur + pd.Timedelta(days=89), end_dt)
        body = api_post(
            "/charts/intraday",
            {
                "securityId": str(int(sid)), "exchangeSegment": segment,
                "instrument": instrument, "interval": interval, "oi": False,
                "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": ce.strftime("%Y-%m-%d %H:%M:%S")
            },
            f"{dataset} {year} Q{quarter}"
        )
        part = parse_chart_response(body)
        if not part.empty:
            frames.append(part)
        cur = ce + pd.Timedelta(seconds=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)


def render_data_vault():
    st.markdown(
        '<div class="friday-hero"><div class="friday-title">DATA VAULT</div>'
        '<div class="friday-sub">Contract-wise NIFTY Futures / Spot / India VIX historical data</div></div>',
        unsafe_allow_html=True
    )
    master = load_master()
    c1, c2, c3 = st.columns(3)
    with c1:
        dataset = st.selectbox("Dataset", ["NIFTY Futures", "NIFTY Spot", "India VIX"], key="vault_dataset")
    with c2:
        years = list(range(2020, now_ist().year + 1))
        default_year = years.index(2024) if 2024 in years else len(years) - 1
        year = st.selectbox("Year", years, index=default_year, key="vault_year")
    with c3:
        quarter = st.selectbox("Quarter", [1, 2, 3, 4], index=0, key="vault_quarter")

    timeframe = st.selectbox(
        "Timeframe", ["15-minute", "1-minute", "5-minute", "25-minute", "60-minute", "Daily"],
        index=0, key="vault_tf"
    )
    # User requirement: futures downloader is OHLC-only. OI is intentionally not requested or saved.

    if dataset == "NIFTY Futures":
        st.info(
            "Futures are resolved contract-by-contract from Dhan's instrument master. "
            "FRIDAY never substitutes a current contract for an expired contract. "
            "The downloader asks Dhan for 15-minute OHLC only."
        )
        contracts = _friday_master_contracts(master, year, quarter)
        if contracts.empty:
            st.error(f"No NIFTY Futures contracts were found in the Dhan instrument master for {year} Q{quarter}.")
            return

        show_cols = [c for c in ["contract", "expiry", "security_id", "status"] if c in contracts.columns]
        st.subheader("Detected NIFTY Futures Contracts")
        st.dataframe(contracts[show_cols], use_container_width=True, hide_index=True)

        choices = [
            f"{r.contract} | Expiry {r.expiry} | ID {int(r.security_id)} | {r.status}"
            for r in contracts.itertuples()
        ]
        selected = st.multiselect("Contracts to download", choices, default=choices, key="vault_contracts")
        selected_rows = [contracts.iloc[choices.index(choice)] for choice in selected]

        if st.button("⬇️ DOWNLOAD SELECTED FUTURES", use_container_width=True):
            if not selected_rows:
                st.warning("Select at least one contract.")
                return

            q = pd.Period(f"{year}-Q{quarter}")
            zip_buffer = BytesIO()
            manifest = []
            successful = 0
            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for row in selected_rows:
                    sid = int(row.security_id)
                    expiry = pd.Timestamp(row._expiry)
                    start_dt = max(pd.Timestamp(q.start_time), expiry - pd.Timedelta(days=120))
                    end_dt = min(pd.Timestamp(q.end_time), expiry + pd.Timedelta(days=1))
                    df, err = _friday_futures_download(
                        sid, timeframe, start_dt, end_dt, False, str(row.contract)
                    ) if timeframe != "Daily" else (pd.DataFrame(), "")

                    if timeframe == "Daily":
                        try:
                            body = api_post(
                                "/charts/historical",
                                {
                                    "securityId": str(sid), "exchangeSegment": "NSE_FNO", "instrument": "FUTIDX",
                                    "expiryCode": 0, "oi": False,
                                    "fromDate": start_dt.strftime("%Y-%m-%d"), "toDate": end_dt.strftime("%Y-%m-%d")
                                },
                                f"{row.contract} daily"
                            )
                            df = parse_chart_response(body)
                            err = "" if not df.empty else "Dhan returned no daily candles."
                        except Exception as exc:
                            df, err = pd.DataFrame(), str(exc)

                    fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row.contract))
                    fname += f"_{year}_Q{quarter}_{timeframe.replace('-', '')}.csv"
                    if not df.empty:
                        df.insert(1, "contract", str(row.contract))
                        df.insert(2, "expiry", row.expiry)
                        df.insert(3, "security_id", sid)
                        z.writestr(fname, df.to_csv(index=False).encode("utf-8"))
                        successful += 1
                        manifest.append({
                            "contract": str(row.contract), "expiry": row.expiry,
                            "security_id": sid, "status": "DOWNLOADED", "rows": len(df), "error": ""
                        })
                    else:
                        manifest.append({
                            "contract": str(row.contract), "expiry": row.expiry,
                            "security_id": sid, "status": "UNAVAILABLE", "rows": 0, "error": err
                        })

                z.writestr(
                    f"NIFTY_FUTURES_{year}_Q{quarter}_MANIFEST.csv",
                    pd.DataFrame(manifest).to_csv(index=False).encode("utf-8")
                )
            zip_buffer.seek(0)
            st.dataframe(pd.DataFrame(manifest), use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ DOWNLOAD CONTRACT-WISE ZIP",
                data=zip_buffer.getvalue(),
                file_name=f"FRIDAY_NIFTY_FUTURES_{year}_Q{quarter}_{timeframe}.zip",
                mime="application/zip",
                use_container_width=True
            )
            if successful == 0:
                st.warning(
                    "Dhan returned no Futures candles for the selected historical contract(s). "
                    "The manifest preserves the exact Security IDs and errors. No substitute contract is used."
                )
        return

    st.info("Spot/VIX can still be downloaded quarter-wise from Dhan. These are also OHLC-only.")
    if st.button("⬇️ DOWNLOAD QUARTER", use_container_width=True, key="vault_basic_download"):
        try:
            sid = NIFTY_ID if dataset == "NIFTY Spot" else resolve_vix_id(master)
            df = _download_basic_dataset(sid, dataset, timeframe, year, quarter)
            if df.empty:
                st.error("No data returned for the requested quarter.")
            else:
                st.success(f"Downloaded {len(df):,} rows.")
                st.download_button(
                    "⬇️ DOWNLOAD CSV",
                    data=df.to_csv(index=False).encode("utf-8"),
                    file_name=f"FRIDAY_{dataset.replace(' ','_')}_{year}_Q{quarter}_{timeframe}.csv",
                    mime="text/csv", use_container_width=True
                )
                st.dataframe(df.head(300), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(str(exc))


def _safe_numeric_series(s):
    return pd.to_numeric(s, errors="coerce")


def _technical_feature_frame(df):
    """Build broad pattern/market features from whatever data is supplied."""
    x = df.copy().sort_values("timestamp").reset_index(drop=True)
    if x.empty:
        return x
    if "nifty_spot" in x:
        x["spot_return_1"] = x["nifty_spot"].pct_change()
        x["spot_return_4"] = x["nifty_spot"].pct_change(4)
        x["spot_return_16"] = x["nifty_spot"].pct_change(16)
        x["spot_range"] = x["nifty_spot"].pct_change().rolling(8).std()
        x["spot_ma_8"] = x["nifty_spot"].rolling(8).mean()
        x["spot_ma_32"] = x["nifty_spot"].rolling(32).mean()
        x["spot_trend"] = x["spot_ma_8"] - x["spot_ma_32"]
    if "ce_close" in x and "pe_close" in x:
        x["synthetic_straddle"] = x["ce_close"] + x["pe_close"]
        x["straddle_return"] = x["synthetic_straddle"].pct_change()
        x["straddle_change"] = x["synthetic_straddle"].diff()
    if "atm_iv" in x:
        x["iv_change"] = x["atm_iv"].diff()
        x["iv_return"] = x["atm_iv"].pct_change()
        x["iv_ma"] = x["atm_iv"].rolling(16).mean()
    if "vix_close" in x:
        x["vix_change"] = x["vix_close"].diff()
        x["vix_return"] = x["vix_close"].pct_change()
    return x


def _infer_regime(row):
    trend = row.get("spot_trend", np.nan)
    vol = row.get("spot_range", np.nan)
    ivch = row.get("iv_change", np.nan)
    if pd.isna(trend):
        return "UNCLEAR"
    if trend > 0 and (pd.isna(vol) or pd.isna(ivch) or ivch <= 0):
        return "BULLISH"
    if trend < 0 and (pd.isna(vol) or pd.isna(ivch) or ivch <= 0):
        return "BEARISH"
    if abs(trend) < max(abs(row.get("nifty_spot", 0)) * 0.0003, 1e-9):
        return "SIDEWAYS"
    return "UNCLEAR"


def _prepare_fallback_research(option_files, spot_files):
    """Fallback mode: learn patterns from options + spot when Futures/VIX are absent."""
    options = _normalize_options(_read_uploaded_csvs(option_files, "options"))
    spot = _normalize_spot(_read_uploaded_csvs(spot_files, "spot"))
    if options.empty or spot.empty:
        raise ValueError("Fallback mode needs both option data and NIFTY Spot data.")
    features = _build_option_features(options, spot)
    if features.empty:
        raise ValueError("Could not build synchronized option + spot features from the uploaded files.")
    features = _technical_feature_frame(features)
    features["regime"] = features.apply(_infer_regime, axis=1)

    # Purely descriptive forward labels; this does not pretend to reproduce an executable strategy outcome.
    if "synthetic_straddle" in features:
        fwd = features["synthetic_straddle"].shift(-4) / features["synthetic_straddle"] - 1
        features["forward_straddle_return"] = fwd
        features["pattern_label"] = np.select(
            [fwd > 0.01, fwd < -0.01],
            ["STRADDLE_PREMIUM_EXPANSION", "STRADDLE_PREMIUM_CONTRACTION"],
            default="NEUTRAL"
        )
    return features


def render_ai_strategist():
    st.markdown('<div class="section">AI STRATEGIST</div>', unsafe_allow_html=True)
    st.write(
        "FRIDAY can now work in two modes: a full market-context mode when Futures/VIX are available, "
        "and a fallback research mode using only Options + NIFTY Spot. In fallback mode FRIDAY searches for "
        "repeatable price/volatility/straddle patterns and does not manufacture Futures data."
    )

    opt_files = st.file_uploader(
        "Option Data (CSV)", type=["csv"], accept_multiple_files=True, key="friday_options_upload"
    )
    spot_files = st.file_uploader(
        "NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True, key="friday_spot_upload"
    )
    future_files = st.file_uploader(
        "Optional NIFTY Futures Data (CSV)", type=["csv"], accept_multiple_files=True, key="friday_future_upload"
    )
    vix_files = st.file_uploader(
        "Optional India VIX Data (CSV)", type=["csv"], accept_multiple_files=True, key="friday_vix_upload"
    )

    mode = "OPTIONS + SPOT FALLBACK" if opt_files and spot_files and not future_files else "FULL MARKET CONTEXT"
    st.metric("FRIDAY INPUT MODE", mode)

    if mode == "OPTIONS + SPOT FALLBACK":
        st.info(
            "Futures data is optional. FRIDAY will analyze option behaviour, ATM/straddle behaviour, "
            "spot trend, volatility, recurring patterns and forward relationships from the supplied datasets."
        )
        if st.button("🔎 ANALYZE PATTERNS", use_container_width=True):
            try:
                features = _prepare_fallback_research(opt_files, spot_files)
                st.session_state.prepared_training_data = features
                st.session_state.model_status = "PATTERN RESEARCH"
                st.success(f"Analyzed {len(features):,} synchronized timestamps.")
                st.dataframe(features.tail(500), use_container_width=True, hide_index=True)
                if "pattern_label" in features:
                    st.subheader("Observed Forward Pattern Distribution")
                    dist = features["pattern_label"].value_counts(dropna=False).rename_axis("pattern").reset_index(name="rows")
                    dist["share"] = dist["rows"] / len(features)
                    st.dataframe(dist, use_container_width=True, hide_index=True)
            except Exception as exc:
                st.error(str(exc))
        return

    st.caption(
        "Full-context training remains available when Futures/VIX files are supplied. "
        "FRIDAY does not label itself trained simply because files were uploaded."
    )



def inject_css():
    st.markdown(
        "<style>.stApp{background:linear-gradient(180deg,#05080d,#080d15)} "
        ".block-container{max-width:1500px;padding-top:1rem} "
        ".friday-hero{background:linear-gradient(135deg,#0f1722,#07101a);border:1px solid rgba(45,180,255,.24);border-radius:18px;padding:24px 28px} "
        ".friday-title{font-size:40px;font-weight:900;color:#7edcff;letter-spacing:3px} "
        ".friday-sub{color:#8da1b5}.section{font-size:18px;font-weight:800;color:#e6f2ff;margin:18px 0 8px}</style>",
        unsafe_allow_html=True
    )


init_state()
inject_css()

with st.sidebar:
    st.markdown(
        '<div style="font-size:42px;font-weight:900;color:#7edcff;letter-spacing:3px;">FRIDAY</div>'
        '<div style="color:#8da1b5;margin-bottom:20px;">AI OPTION STRATEGIST</div>',
        unsafe_allow_html=True
    )
    st.session_state.client_id = st.text_input("Dhan Client ID", value=st.session_state.client_id).strip()
    st.session_state.access_token = st.text_input(
        "Dhan Access Token", value=st.session_state.access_token, type="password"
    ).strip()

friday_view = st.radio("FRIDAY MODULE", ["AI Strategist", "Data Vault"], horizontal=True, key="friday_view")

if friday_view == "Data Vault":
    render_data_vault()
    st.stop()

st.markdown(
    '<div class="friday-hero"><div class="friday-title">FRIDAY</div>'
    '<div class="friday-sub">AI OPTION STRATEGY & PATTERN RESEARCH ENGINE</div></div>',
    unsafe_allow_html=True
)
render_ai_strategist()
