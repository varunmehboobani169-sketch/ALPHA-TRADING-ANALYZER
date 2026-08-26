
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

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

st.set_page_config(
    page_title="FRIDAY • AI Option Strategist",
    page_icon="🤖",
    layout="wide",
)

API = "https://api.dhan.co/v2"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
NIFTY_ID = 13
VIX_ID_FALLBACK = 26

STRATEGIES = [
    "BUY CE",
    "SELL PE",
    "BULL CALL SPREAD",
    "BULL PUT SPREAD",
    "BUY PE",
    "SELL CE",
    "BEAR PUT SPREAD",
    "BEAR CALL SPREAD",
    "SHORT STRADDLE",
    "SHORT STRANGLE",
    "IRON CONDOR",
    "NO TRADE",
]

REGIMES = ["BULLISH", "BEARISH", "SIDEWAYS", "UNCLEAR"]

RATE_LIMIT_SECONDS = 3.2


def now_ist():
    return datetime.now(LOCAL_TZ)


def init_state():
    defaults = {
        "client_id": "",
        "access_token": "",
        "model": None,
        "label_encoder": None,
        "imputer": None,
        "feature_columns": [],
        "training_summary": {},
        "model_status": "NOT TRAINED",
        "live_features": {},
        "last_training_rows": 0,
        "last_api_call": 0.0,
        "uploaded_option_files": [],
        "uploaded_future_files": [],
        "uploaded_spot_files": [],
        "uploaded_vix_files": [],
        "prepared_training_data": None,
        "prepared_data_summary": {},
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

    response = requests.post(
        API + path,
        headers=headers(),
        json=payload,
        timeout=45,
    )
    st.session_state.last_api_call = time.monotonic()

    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}

    if response.status_code == 429:
        retry = min(10.0, 2.0)
        time.sleep(retry)
        response = requests.post(
            API + path,
            headers=headers(),
            json=payload,
            timeout=45,
        )
        st.session_state.last_api_call = time.monotonic()
        body = response.json() if response.content else {"raw": response.text}

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


@st.cache_data(ttl=21600, show_spinner=False)
def load_master():
    url = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    df = pd.read_csv(url, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]
    rename = {
        "EXCH_ID": "exchange",
        "SEGMENT": "segment",
        "INSTRUMENT": "instrument",
        "SECURITY_ID": "security_id",
        "UNDERLYING_SECURITY_ID": "underlying_security_id",
        "UNDERLYING_SYMBOL": "underlying_symbol",
        "SYMBOL_NAME": "symbol_name",
        "SEM_TRADING_SYMBOL": "trading_symbol",
        "DISPLAY_NAME": "display_name",
    }
    df = df.rename(columns={c: rename.get(c, c) for c in df.columns})
    if "security_id" not in df.columns:
        for c in ["SM_SECURITY_ID", "SEM_SECURITY_ID"]:
            if c in df.columns:
                df["security_id"] = df[c]
                break
    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    return df.dropna(subset=["security_id"]).copy()


def resolve_vix_id(master):
    for col in ["underlying_symbol", "symbol_name", "trading_symbol", "display_name"]:
        if col not in master.columns:
            continue
        s = master[col].astype(str).str.upper().str.replace(" ", "", regex=False)
        mask = s.str.contains("INDIAVIX", regex=False, na=False) | s.eq("VIX")
        rows = master[mask]
        ids = pd.to_numeric(rows["security_id"], errors="coerce").dropna()
        if not ids.empty:
            return int(ids.iloc[0])
    return VIX_ID_FALLBACK


def parse_chart_response(body):
    data = parse_data(body)
    if not isinstance(data, dict) or not data.get("timestamp"):
        return pd.DataFrame()

    ts = pd.to_numeric(pd.Series(data["timestamp"]), errors="coerce")
    dt = pd.to_datetime(ts, unit="s", utc=True, errors="coerce").dt.tz_convert(LOCAL_TZ)
    out = pd.DataFrame({"datetime": dt})
    for src, dst in [
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("close", "close"),
        ("volume", "volume"),
        ("oi", "oi"),
        ("open_interest", "oi"),
    ]:
        values = data.get(src)
        if values is not None and len(values) == len(out):
            out[dst] = pd.to_numeric(pd.Series(values), errors="coerce")
    return out.dropna(subset=["datetime"]).sort_values("datetime").drop_duplicates("datetime")


@st.cache_data(ttl=30, show_spinner=False)
def live_index_features(vix_id):
    result = {}

    vix_body = api_post(
        "/marketfeed/ltp",
        {"IDX_I": [int(vix_id)]},
        "FRIDAY India VIX",
    )
    vix_data = parse_data(vix_body).get("IDX_I", {})
    vix_row = vix_data.get(str(vix_id)) or vix_data.get(vix_id) or {}
    result["vix_ltp"] = pd.to_numeric(vix_row.get("last_price"), errors="coerce")

    nifty_body = api_post(
        "/marketfeed/ltp",
        {"IDX_I": [NIFTY_ID]},
        "FRIDAY NIFTY",
    )
    nifty_data = parse_data(nifty_body).get("IDX_I", {})
    nifty_row = nifty_data.get(str(NIFTY_ID)) or nifty_data.get(NIFTY_ID) or {}
    result["nifty_ltp"] = pd.to_numeric(nifty_row.get("last_price"), errors="coerce")

    return result


@st.cache_data(ttl=120, show_spinner=False)
def current_option_features():
    expiry_body = api_post(
        "/optionchain/expirylist",
        {
            "UnderlyingScrip": NIFTY_ID,
            "UnderlyingSeg": "IDX_I",
        },
        "FRIDAY NIFTY expiry list",
    )
    data = parse_data(expiry_body)
    expiries = []
    for v in data.get("data", []) if isinstance(data, dict) else []:
        try:
            expiries.append(pd.Timestamp(str(v)).date())
        except Exception:
            pass
    future = sorted([d for d in expiries if d >= now_ist().date()])
    if not future:
        return {}

    expiry = future[0]
    chain_body = api_post(
        "/optionchain",
        {
            "UnderlyingScrip": NIFTY_ID,
            "UnderlyingSeg": "IDX_I",
            "Expiry": expiry.strftime("%Y-%m-%d"),
        },
        "FRIDAY NIFTY option chain",
    )
    data = parse_data(chain_body)
    spot = pd.to_numeric(data.get("last_price"), errors="coerce")

    rows = []
    for strike_raw, pair in (data.get("oc") or {}).items():
        try:
            strike = float(strike_raw)
        except Exception:
            continue
        for key, side in [("ce", "CE"), ("pe", "PE")]:
            leg = pair.get(key) if isinstance(pair, dict) else None
            if not isinstance(leg, dict):
                continue
            rows.append({
                "strike": strike,
                "side": side,
                "ltp": pd.to_numeric(leg.get("last_price"), errors="coerce"),
                "iv": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                "oi": pd.to_numeric(leg.get("oi"), errors="coerce"),
                "volume": pd.to_numeric(leg.get("volume"), errors="coerce"),
            })

    chain = pd.DataFrame(rows)
    if chain.empty or pd.isna(spot):
        return {}

    strikes = sorted(chain["strike"].dropna().unique())
    atm = min(strikes, key=lambda x: abs(float(x) - float(spot)))
    ce = chain[(chain.side == "CE") & np.isclose(chain.strike, atm)]
    pe = chain[(chain.side == "PE") & np.isclose(chain.strike, atm)]
    if ce.empty or pe.empty:
        return {}

    ce = ce.iloc[-1]
    pe = pe.iloc[-1]
    ce_iv = float(ce.iv) if pd.notna(ce.iv) else np.nan
    pe_iv = float(pe.iv) if pd.notna(pe.iv) else np.nan
    avg_iv = np.nanmean([ce_iv, pe_iv])

    call_oi = chain.loc[chain.side == "CE", "oi"].sum(min_count=1)
    put_oi = chain.loc[chain.side == "PE", "oi"].sum(min_count=1)
    pcr_oi = put_oi / call_oi if pd.notna(call_oi) and call_oi else np.nan

    return {
        "expiry": expiry,
        "spot": float(spot),
        "atm": float(atm),
        "ce_iv": ce_iv,
        "pe_iv": pe_iv,
        "atm_iv": float(avg_iv) if pd.notna(avg_iv) else np.nan,
        "ce_ltp": float(ce.ltp) if pd.notna(ce.ltp) else np.nan,
        "pe_ltp": float(pe.ltp) if pd.notna(pe.ltp) else np.nan,
        "straddle": float(ce.ltp + pe.ltp) if pd.notna(ce.ltp) and pd.notna(pe.ltp) else np.nan,
        "pcr_oi": float(pcr_oi) if pd.notna(pcr_oi) else np.nan,
        "call_oi": float(call_oi) if pd.notna(call_oi) else np.nan,
        "put_oi": float(put_oi) if pd.notna(put_oi) else np.nan,
    }


def derive_features(raw):
    """Normalize flexible training/live fields into model features."""
    aliases = {
        "nifty_ltp": ["nifty_ltp", "nifty", "spot", "nifty_spot"],
        "nifty_return_15m": ["nifty_return_15m", "spot_return_15m", "return_15m"],
        "fut_return_15m": ["fut_return_15m", "future_return_15m", "nifty_fut_return_15m"],
        "vix_ltp": ["vix_ltp", "india_vix", "vix"],
        "vix_change": ["vix_change", "vix_return", "vix_pct_change"],
        "atm_iv": ["atm_iv", "avg_atm_iv", "atm_avg_iv"],
        "atm_iv_change": ["atm_iv_change", "iv_change", "atm_iv_return"],
        "ce_iv": ["ce_iv", "atm_ce_iv"],
        "pe_iv": ["pe_iv", "atm_pe_iv"],
        "straddle": ["straddle", "atm_straddle", "atm_straddle_premium"],
        "pcr_oi": ["pcr_oi", "pcr"],
        "matrix_total_performance": ["matrix_total_performance", "total_performance"],
        "matrix_net_performance": ["matrix_net_performance", "net_performance"],
        "price_performance": ["price_performance"],
        "rs_performance": ["rs_performance"],
        "price_ranking": ["price_ranking"],
        "rs_ranking": ["rs_ranking"],
        "vix_pnf_state": ["vix_pnf_state", "vix_state"],
        "vix_pnf_boxes": ["vix_pnf_boxes", "vix_column_boxes"],
        "hour": ["hour"],
        "minute": ["minute"],
        "dte": ["dte", "days_to_expiry"],
    }

    out = pd.DataFrame(index=raw.index)
    lower = {str(c).strip().lower(): c for c in raw.columns}

    for target, possible in aliases.items():
        found = None
        for candidate in possible:
            if candidate in lower:
                found = lower[candidate]
                break
        if found is not None:
            if target == "vix_pnf_state":
                s = raw[found].astype(str).str.upper().map({
                    "BULLISH": 1,
                    "ACTIVE LONG": 1,
                    "X": 1,
                    "BEARISH": -1,
                    "ACTIVE SELL": -1,
                    "O": -1,
                }).fillna(0)
                out[target] = s
            else:
                out[target] = pd.to_numeric(raw[found], errors="coerce")
        else:
            out[target] = np.nan

    return out



def _find_column(df, candidates):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for candidate in candidates:
        key = candidate.lower().replace(" ", "_")
        if key in lookup:
            return lookup[key]
    # loose contains fallback
    for norm, original in lookup.items():
        if any(candidate.lower().replace(" ", "_") in norm for candidate in candidates):
            return original
    return None


def _read_uploaded_csvs(files, label):
    """Read many uploaded CSVs, attach source file name and preserve all rows."""
    if not files:
        return pd.DataFrame()

    frames = []
    errors = []
    for f in files:
        try:
            df = pd.read_csv(f, low_memory=False)
            if df.empty:
                continue
            df["_source_file"] = getattr(f, "name", label)
            frames.append(df)
        except Exception as exc:
            errors.append(f"{getattr(f, 'name', 'file')}: {exc}")

    if errors:
        raise ValueError(" | ".join(errors))

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def _normalize_time_column(df):
    if df.empty:
        return df

    ts_col = _find_column(
        df,
        [
            "timestamp", "datetime", "date_time", "date",
            "time", "timestamp_ist", "datetime_ist",
        ],
    )
    if ts_col is None:
        return df

    out = df.copy()
    dt = pd.to_datetime(out[ts_col], errors="coerce", utc=False)

    # If timezone-aware, convert to IST. If naive, assume IST.
    try:
        if getattr(dt.dt, "tz", None) is not None:
            dt = dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception:
        pass

    out["timestamp"] = dt
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp")
    return out


def _pick_numeric(df, candidates):
    col = _find_column(df, candidates)
    if col is None:
        return None
    return pd.to_numeric(df[col], errors="coerce")


def _normalize_spot(df, label="spot"):
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "nifty_spot"])

    out = _normalize_time_column(df)
    price = _pick_numeric(
        out,
        ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"],
    )
    if price is None:
        raise ValueError(f"{label} file has no recognizable price/close column.")

    out = out[["timestamp"]].copy()
    out["nifty_spot"] = price.reindex(out.index).values
    return out.dropna(subset=["timestamp", "nifty_spot"])


def _normalize_future(df):
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "future_open", "future_high", "future_low", "future_close"])

    out = _normalize_time_column(df)
    result = pd.DataFrame({"timestamp": out["timestamp"]})
    for src_names, target in [
        (["open"], "future_open"),
        (["high"], "future_high"),
        (["low"], "future_low"),
        (["close", "ltp", "last_price"], "future_close"),
    ]:
        val = _pick_numeric(out, src_names)
        result[target] = val.reindex(out.index).values if val is not None else np.nan
    return result.dropna(subset=["timestamp"])


def _normalize_vix(df):
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "vix_open", "vix_high", "vix_low", "vix_close"])

    out = _normalize_time_column(df)
    result = pd.DataFrame({"timestamp": out["timestamp"]})
    for src_names, target in [
        (["open"], "vix_open"),
        (["high"], "vix_high"),
        (["low"], "vix_low"),
        (["close", "ltp", "last_price"], "vix_close"),
    ]:
        val = _pick_numeric(out, src_names)
        result[target] = val.reindex(out.index).values if val is not None else np.nan
    return result.dropna(subset=["timestamp"])


def _normalize_options(df):
    """
    Normalize flexible option files. Expected concepts:
    timestamp, expiry, strike, CE/PE, OHLC, IV, OI, volume.
    """
    if df.empty:
        return pd.DataFrame()

    out = _normalize_time_column(df)

    strike_col = _find_column(out, ["strike", "strike_price", "strikeprice"])
    side_col = _find_column(out, ["side", "option_type", "type", "cp", "ce_pe"])
    expiry_col = _find_column(out, ["expiry", "expiry_date", "expirydate", "exp_date"])
    iv_col = _find_column(out, ["iv", "implied_volatility", "impliedvolatility"])
    oi_col = _find_column(out, ["oi", "open_interest", "openinterest"])
    vol_col = _find_column(out, ["volume", "vol"])

    if strike_col is None or side_col is None:
        raise ValueError(
            "Options file must contain recognizable Strike and CE/PE/Option Type columns."
        )

    result = pd.DataFrame({
        "timestamp": out["timestamp"],
        "strike": pd.to_numeric(out[strike_col], errors="coerce"),
        "side": out[side_col].astype(str).str.upper().str.strip(),
    })

    result["side"] = result["side"].replace({
        "C": "CE", "CALL": "CE", "CE_OPTION": "CE",
        "P": "PE", "PUT": "PE", "PE_OPTION": "PE",
    })

    if expiry_col is not None:
        result["expiry"] = pd.to_datetime(out[expiry_col], errors="coerce").dt.date
    else:
        result["expiry"] = pd.NaT

    result["iv"] = (
        pd.to_numeric(out[iv_col], errors="coerce")
        if iv_col is not None else np.nan
    )
    result["oi"] = (
        pd.to_numeric(out[oi_col], errors="coerce")
        if oi_col is not None else np.nan
    )
    result["volume"] = (
        pd.to_numeric(out[vol_col], errors="coerce")
        if vol_col is not None else np.nan
    )

    close = _pick_numeric(out, ["close", "ltp", "last_price"])
    result["close"] = close.reindex(out.index).values if close is not None else np.nan
    return result.dropna(subset=["timestamp", "strike"])


def _resample_to_common_minute(df, prefix=""):
    if df.empty:
        return df
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp")
    return out.drop_duplicates("timestamp")


def _build_option_features(options, spot):
    """
    Build ATM option features at each timestamp from raw option rows.
    Uses nearest strike to spot and nearest available expiry when expiry exists.
    """
    if options.empty or spot.empty:
        return pd.DataFrame()

    opt = options.copy()
    sp = spot.copy()

    # As-of join to attach spot to every option row.
    sp = sp.sort_values("timestamp")[["timestamp", "nifty_spot"]]
    opt = opt.sort_values("timestamp")
    opt = pd.merge_asof(
        opt,
        sp,
        on="timestamp",
        direction="backward",
        tolerance=pd.Timedelta("10min"),
    )
    opt = opt.dropna(subset=["nifty_spot", "strike", "side"])

    rows = []
    for ts, g in opt.groupby("timestamp", sort=True):
        # Use expiry nearest to current timestamp if expiry is available.
        if g["expiry"].notna().any():
            today = ts.date()
            expiries = pd.Series(g["expiry"].dropna().unique())
            future_exp = [e for e in expiries if e >= today]
            selected_expiry = min(future_exp) if future_exp else min(expiries)
            g = g[g["expiry"] == selected_expiry]

        if g.empty:
            continue

        target = float(g.iloc[0]["nifty_spot"])
        strikes = g["strike"].dropna().unique()
        if len(strikes) == 0:
            continue

        atm_strike = min(strikes, key=lambda x: abs(float(x) - target))
        atm = g[np.isclose(g["strike"].astype(float), float(atm_strike))]

        ce = atm[atm["side"] == "CE"]
        pe = atm[atm["side"] == "PE"]
        if ce.empty or pe.empty:
            continue

        ce = ce.iloc[-1]
        pe = pe.iloc[-1]

        call_oi = g.loc[g["side"] == "CE", "oi"].sum(min_count=1)
        put_oi = g.loc[g["side"] == "PE", "oi"].sum(min_count=1)
        pcr = (
            put_oi / call_oi
            if pd.notna(put_oi) and pd.notna(call_oi) and call_oi not in [0, np.nan]
            else np.nan
        )

        rows.append({
            "timestamp": ts,
            "nifty_spot": target,
            "atm_strike": float(atm_strike),
            "ce_iv": ce["iv"],
            "pe_iv": pe["iv"],
            "atm_iv": np.nanmean([ce["iv"], pe["iv"]]),
            "ce_close": ce["close"],
            "pe_close": pe["close"],
            "straddle": (
                ce["close"] + pe["close"]
                if pd.notna(ce["close"]) and pd.notna(pe["close"])
                else np.nan
            ),
            "pcr_oi": pcr,
        })

    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)



def _infer_median_minutes(df):
    if df.empty or "timestamp" not in df.columns:
        return np.nan
    t = pd.to_datetime(df["timestamp"], errors="coerce").dropna().sort_values()
    if len(t) < 3:
        return np.nan
    diffs = t.diff().dt.total_seconds().div(60).dropna()
    diffs = diffs[(diffs > 0) & (diffs < 1440)]
    return float(diffs.median()) if not diffs.empty else np.nan


def _timeframe_warning(df, expected_minutes, label):
    median = _infer_median_minutes(df)
    if pd.isna(median):
        return f"{label}: timeframe could not be inferred."
    if abs(median - expected_minutes) > max(1.0, expected_minutes * 0.20):
        return (
            f"{label}: detected median candle spacing ≈ {median:.1f} min, "
            f"but FRIDAY expects {expected_minutes}-minute data."
        )
    return ""


def _timeframe_summary(df, label):
    median = _infer_median_minutes(df)
    return {
        "label": label,
        "median_minutes": None if pd.isna(median) else round(float(median), 2),
        "rows": int(len(df)),
    }


def build_friday_feature_base(option_files, future_files, spot_files, vix_files):
    """Merge the four user-supplied yearly file groups into one ML feature base."""
    options_raw = _read_uploaded_csvs(option_files, "options")
    futures_raw = _read_uploaded_csvs(future_files, "futures")
    spot_raw = _read_uploaded_csvs(spot_files, "spot")
    vix_raw = _read_uploaded_csvs(vix_files, "vix")

    spot = _normalize_spot(spot_raw, "NIFTY Spot")
    fut = _normalize_future(futures_raw)
    vix = _normalize_vix(vix_raw)
    options = _normalize_options(options_raw)

    if spot.empty:
        raise ValueError("NIFTY Spot data could not be prepared.")
    if options.empty:
        raise ValueError("Option data could not be prepared.")

    warnings = []
    spot_warning = _timeframe_warning(spot, 15, "NIFTY Spot")
    if spot_warning:
        warnings.append(spot_warning)

    if not fut.empty:
        fut_warning = _timeframe_warning(fut, 15, "NIFTY Futures")
        if fut_warning:
            warnings.append(fut_warning)

    # Options are expected to be 1-minute for FRIDAY.
    opt_warning = _timeframe_warning(options, 1, "NIFTY Options")
    if opt_warning:
        warnings.append(opt_warning)

    st.session_state["friday_timeframe_warnings"] = warnings
    st.session_state["friday_timeframe_summary"] = [
        _timeframe_summary(options, "NIFTY Options"),
        _timeframe_summary(fut, "NIFTY Futures"),
        _timeframe_summary(spot, "NIFTY Spot"),
        _timeframe_summary(vix, "India VIX"),
    ]

    opt_features = _build_option_features(options, spot)

    # Merge NIFTY spot/futures/VIX into option decision timestamps.
    feature = opt_features.sort_values("timestamp")

    if not fut.empty:
        feature = pd.merge_asof(
            feature,
            fut.sort_values("timestamp"),
            on="timestamp",
            direction="backward",
            tolerance=pd.Timedelta("10min"),
        )

    if not vix.empty:
        feature = pd.merge_asof(
            feature,
            vix.sort_values("timestamp"),
            on="timestamp",
            direction="backward",
            tolerance=pd.Timedelta("10min"),
        )

    feature["date"] = feature["timestamp"].dt.date
    feature["hour"] = feature["timestamp"].dt.hour
    feature["minute"] = feature["timestamp"].dt.minute

    # Basic derived market features.
    # Because NIFTY and Futures are 15-minute data, their natural changes
    # are calculated per 15-minute bar. The option stream remains 1-minute.
    feature["nifty_return_15m"] = feature["nifty_spot"].pct_change(15)
    if "future_close" in feature.columns:
        feature["fut_return_15m"] = feature["future_close"].pct_change(15)
        feature["spot_future_spread"] = (
            feature["nifty_spot"] - feature["future_close"]
        )
    else:
        feature["fut_return_15m"] = np.nan
        feature["spot_future_spread"] = np.nan

    if "vix_close" in feature.columns:
        feature["vix_change"] = feature["vix_close"].pct_change(15)
    else:
        feature["vix_change"] = np.nan

    feature["atm_iv_change"] = feature["atm_iv"].diff(15)
    feature["atm_iv_change_pct"] = feature["atm_iv"].pct_change(15)
    feature["straddle_change"] = feature["straddle"].diff(15)
    feature["straddle_change_pct"] = feature["straddle"].pct_change(15)

    # Drop rows with no usable option signal.
    feature = feature.dropna(subset=["timestamp", "nifty_spot", "atm_iv"]).reset_index(drop=True)
    return feature


def store_uploaded_files(option_files, future_files, spot_files, vix_files):
    for key, files in [
        ("uploaded_option_files", option_files),
        ("uploaded_future_files", future_files),
        ("uploaded_spot_files", spot_files),
        ("uploaded_vix_files", vix_files),
    ]:
        if files:
            # Store bytes so selections survive reruns.
            stored = []
            for f in files:
                stored.append((f.name, f.getvalue()))
            st.session_state[key] = stored


def restore_uploaded_files(key):
    """Restore stored upload bytes as lightweight file-like objects."""
    import io
    return [
        type("UploadedMemoryFile", (), {
            "name": name,
            "getvalue": lambda self, b=content: b,
        })()
        for name, content in st.session_state.get(key, [])
    ]


def prepare_uploaded_feature_base():
    option_files = restore_uploaded_files("uploaded_option_files")
    future_files = restore_uploaded_files("uploaded_future_files")
    spot_files = restore_uploaded_files("uploaded_spot_files")
    vix_files = restore_uploaded_files("uploaded_vix_files")

    if not all([option_files, future_files, spot_files, vix_files]):
        raise ValueError(
            "Upload at least one CSV in each of the four groups: "
            "Options, NIFTY Futures, NIFTY Spot and India VIX."
        )

    feature = build_friday_feature_base(
        option_files, future_files, spot_files, vix_files
    )
    st.session_state.prepared_training_data = feature
    st.session_state.prepared_data_summary = {
        "options_files": len(option_files),
        "future_files": len(future_files),
        "spot_files": len(spot_files),
        "vix_files": len(vix_files),
        "rows": len(feature),
        "from": str(feature["timestamp"].min()),
        "to": str(feature["timestamp"].max()),
    }
    return feature


def train_friday(uploaded):
    if uploaded is None:
        raise ValueError("Upload a historical training CSV first.")
    raw = pd.read_csv(uploaded)
    if raw.empty:
        raise ValueError("Training CSV is empty.")

    label_candidates = ["best_strategy", "target_strategy", "strategy", "label"]
    label_col = next(
        (c for c in label_candidates if c in [str(x).lower() for x in raw.columns]),
        None
    )
    if label_col is None:
        raise ValueError(
            "Training CSV must contain one strategy outcome column: "
            "best_strategy, target_strategy, strategy, or label."
        )
    # Recover actual case-sensitive column name.
    actual = next(c for c in raw.columns if str(c).lower() == label_col)

    y = raw[actual].astype(str).str.upper().str.strip()
    x = derive_features(raw)

    # Keep only numeric columns with at least some information.
    x = x.apply(pd.to_numeric, errors="coerce")
    feature_cols = [c for c in x.columns if x[c].notna().sum() >= max(20, len(x) * 0.05)]
    x = x[feature_cols]

    valid = y.isin(STRATEGIES)
    x = x.loc[valid]
    y = y.loc[valid]

    if len(x) < 100:
        raise ValueError("At least 100 valid training rows are required.")
    if y.nunique() < 2:
        raise ValueError("Training data must contain at least two different strategy labels.")

    imputer = SimpleImputer(strategy="median")
    x_imp = imputer.fit_transform(x)

    encoder = LabelEncoder()
    y_enc = encoder.fit_transform(y)

    model = RandomForestClassifier(
        n_estimators=350,
        max_depth=10,
        min_samples_leaf=5,
        class_weight="balanced_subsample",
        random_state=42,
        n_jobs=-1,
    )
    model.fit(x_imp, y_enc)

    st.session_state.model = model
    st.session_state.label_encoder = encoder
    st.session_state.imputer = imputer
    st.session_state.feature_columns = feature_cols
    st.session_state.last_training_rows = len(x)
    st.session_state.model_status = "TRAINED"

    counts = y.value_counts().to_dict()
    st.session_state.training_summary = {
        "rows": len(x),
        "features": len(feature_cols),
        "classes": counts,
        "label_col": actual,
    }


def create_live_feature_frame(vix_features, option_features):
    f = {
        "nifty_ltp": vix_features.get("nifty_ltp", np.nan),
        "vix_ltp": vix_features.get("vix_ltp", np.nan),
        "atm_iv": option_features.get("atm_iv", np.nan),
        "ce_iv": option_features.get("ce_iv", np.nan),
        "pe_iv": option_features.get("pe_iv", np.nan),
        "straddle": option_features.get("straddle", np.nan),
        "pcr_oi": option_features.get("pcr_oi", np.nan),
        "hour": now_ist().hour,
        "minute": now_ist().minute,
        "dte": (
            (option_features.get("expiry") - now_ist().date()).days
            if option_features.get("expiry")
            else np.nan
        ),
    }
    return pd.DataFrame([f])


def predict_strategy(feature_frame):
    if st.session_state.model is None:
        return {
            "strategy": "MODEL NOT TRAINED",
            "confidence": np.nan,
            "regime": "UNAVAILABLE",
            "ranking": [],
        }

    x = feature_frame.reindex(columns=st.session_state.feature_columns)
    x_imp = st.session_state.imputer.transform(x)
    probs = st.session_state.model.predict_proba(x_imp)[0]
    idx = np.argsort(probs)[::-1]

    pairs = [
        (
            st.session_state.label_encoder.inverse_transform([i])[0],
            float(probs[i]),
        )
        for i in idx
    ]

    best = pairs[0]
    # Coarse regime derived only from model's recommended strategy class.
    strategy = best[0]
    if strategy in ["BUY CE", "SELL PE", "BULL CALL SPREAD", "BULL PUT SPREAD"]:
        regime = "BULLISH"
    elif strategy in ["BUY PE", "SELL CE", "BEAR PUT SPREAD", "BEAR CALL SPREAD"]:
        regime = "BEARISH"
    elif strategy in ["SHORT STRADDLE", "SHORT STRANGLE", "IRON CONDOR"]:
        regime = "SIDEWAYS"
    else:
        regime = "UNCLEAR"

    return {
        "strategy": strategy,
        "confidence": best[1],
        "regime": regime,
        "ranking": pairs[:5],
    }


def confidence_label(value):
    if not pd.notna(value):
        return "—"
    if value >= 0.75:
        return "HIGH"
    if value >= 0.55:
        return "MODERATE"
    return "LOW"


def inject_css():
    st.markdown(
        """
        <style>
        .stApp{background:linear-gradient(180deg,#05080d,#080d15);}
        .block-container{max-width:1500px;padding-top:1rem;}
        .friday-hero{background:linear-gradient(135deg,#0f1722,#07101a);
            border:1px solid rgba(45,180,255,.24);border-radius:18px;padding:24px 28px;
            box-shadow:0 0 36px rgba(0,180,255,.07);}
        .friday-title{font-size:40px;font-weight:900;color:#7edcff;letter-spacing:3px;}
        .friday-sub{color:#8da1b5;margin-top:3px;}
        .friday-card{background:rgba(10,18,29,.86);border:1px solid rgba(126,220,255,.15);
            border-radius:16px;padding:18px;min-height:122px;}
        .k-label{font-size:11px;color:#8799ad;text-transform:uppercase;letter-spacing:1px;}
        .k-value{font-size:28px;font-weight:800;color:#eef7ff;margin-top:6px;}
        .good{color:#3df07b}.warn{color:#ffd166}.bad{color:#ff5d5d}
        .section{font-size:18px;font-weight:800;color:#e6f2ff;margin:18px 0 8px}
        </style>
        """,
        unsafe_allow_html=True,
    )


init_state()
inject_css()

with st.sidebar:
    st.markdown(
        """
        <div style="font-size:42px;font-weight:900;color:#7edcff;letter-spacing:3px;">FRIDAY</div>
        <div style="color:#8da1b5;margin-bottom:20px;">AI OPTION STRATEGIST</div>
        """,
        unsafe_allow_html=True,
    )
    st.session_state.client_id = st.text_input(
        "Dhan Client ID",
        value=st.session_state.client_id,
    ).strip()
    st.session_state.access_token = st.text_input(
        "Dhan Access Token",
        value=st.session_state.access_token,
        type="password",
    ).strip()

    st.markdown("---")
    st.markdown("### FRIDAY Historical Data")
    st.caption(
        "Upload year-wise files. You can select multiple years in each group."
    )

    option_uploads = st.file_uploader(
        "1) NIFTY Options — 1-minute CSVs",
        type=["csv"],
        accept_multiple_files=True,
        key="friday_options_uploads",
        help="Example: 2024 options, 2025 options, 2026 options — all can be selected together.",
    )
    future_uploads = st.file_uploader(
        "2) NIFTY Futures — 15-minute OHLC CSVs",
        type=["csv"],
        accept_multiple_files=True,
        key="friday_future_uploads",
    )
    spot_uploads = st.file_uploader(
        "3) NIFTY Spot — 15-minute OHLC CSVs",
        type=["csv"],
        accept_multiple_files=True,
        key="friday_spot_uploads",
    )
    vix_uploads = st.file_uploader(
        "4) India VIX CSVs",
        type=["csv"],
        accept_multiple_files=True,
        key="friday_vix_uploads",
    )

    if any([option_uploads, future_uploads, spot_uploads, vix_uploads]):
        store_uploaded_files(
            option_uploads,
            future_uploads,
            spot_uploads,
            vix_uploads,
        )

    summary = st.session_state.prepared_data_summary
    if summary:
        st.success(
            f"Prepared {summary['rows']:,} rows from "
            f"{summary['options_files']} option + "
            f"{summary['future_files']} futures + "
            f"{summary['spot_files']} spot + "
            f"{summary['vix_files']} VIX files."
        )

        warnings = st.session_state.get("friday_timeframe_warnings", [])
        if warnings:
            for warning in warnings:
                st.warning(warning)

        tf_summary = st.session_state.get("friday_timeframe_summary", [])
        if tf_summary:
            with st.expander("Detected Timeframes"):
                st.dataframe(
                    pd.DataFrame(tf_summary),
                    use_container_width=True,
                    hide_index=True,
                )

    if st.button("🔧 Prepare FRIDAY Dataset", use_container_width=True):
        try:
            with st.spinner("Reading, aligning and merging all uploaded years..."):
                feature = prepare_uploaded_feature_base()
            st.success(
                f"Dataset prepared: {len(feature):,} synchronized decision rows."
            )
        except Exception as exc:
            st.error(str(exc))

    st.markdown("### Train FRIDAY")
    training_label_file = st.file_uploader(
        "Optional strategy-outcome CSV",
        type=["csv"],
        key="friday_label_upload",
        help=(
            "Optional at this stage. If supplied, it must contain best_strategy / "
            "target_strategy / strategy / label. FRIDAY will train on it."
        ),
    )

    if st.button("🧠 Train / Refresh Model", use_container_width=True):
        if RandomForestClassifier is None:
            st.error("scikit-learn is not installed.")
        elif training_label_file is None:
            st.warning(
                "Prepare the four historical datasets first. A strategy-outcome label "
                "file is still required for supervised training."
            )
        else:
            try:
                with st.spinner("Training FRIDAY..."):
                    train_friday(training_label_file)
                st.success(
                    f"Model trained on {st.session_state.last_training_rows:,} labeled rows."
                )
            except Exception as exc:
                st.error(str(exc))


    st.markdown("---")
    st.caption(
        "FRIDAY follows the controlled AI strategy-selection design: "
        "market regime → strategy ranking → confidence → NO TRADE when appropriate."
    )

st.markdown(
    """
    <div class="friday-hero">
      <div class="friday-title">FRIDAY</div>
      <div class="friday-sub">AI OPTION STRATEGY SELECTION ENGINE</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Model status
s1, s2, s3, s4 = st.columns(4)
status = st.session_state.model_status
s1.markdown(
    f'<div class="friday-card"><div class="k-label">Model</div>'
    f'<div class="k-value {"good" if status=="TRAINED" else "warn"}">{status}</div></div>',
    unsafe_allow_html=True,
)
s2.markdown(
    f'<div class="friday-card"><div class="k-label">Training Rows</div>'
    f'<div class="k-value">{st.session_state.last_training_rows:,}</div></div>',
    unsafe_allow_html=True,
)
s3.markdown(
    f'<div class="friday-card"><div class="k-label">Features</div>'
    f'<div class="k-value">{len(st.session_state.feature_columns)}</div></div>',
    unsafe_allow_html=True,
)
s4.markdown(
    f'<div class="friday-card"><div class="k-label">Allowed Strategies</div>'
    f'<div class="k-value">{len(STRATEGIES)}</div></div>',
    unsafe_allow_html=True,
)


if st.session_state.prepared_training_data is not None:
    prep = st.session_state.prepared_data_summary
    st.markdown(
        f"""
        <div class="friday-card">
          <div class="k-label">HISTORICAL DATASET READY</div>
          <div class="k-value" style="font-size:22px">{prep["rows"]:,} synchronized rows</div>
          <div style="color:#91a4b8">
            {prep["from"]} → {prep["to"]} •
            {prep["options_files"]} options files •
            {prep["future_files"]} futures files •
            {prep["spot_files"]} spot files •
            {prep["vix_files"]} VIX files
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.expander("Preview prepared historical dataset"):
        st.dataframe(
            st.session_state.prepared_training_data.head(300),
            use_container_width=True,
            hide_index=True,
        )

st.markdown('<div class="section">LIVE MARKET CONTEXT</div>', unsafe_allow_html=True)

try:
    master = load_master()
    vix_id = resolve_vix_id(master)

    live = live_index_features(vix_id)
    options = current_option_features()
    live_frame = create_live_feature_frame(live, options)

    prediction = predict_strategy(live_frame)
    regime = prediction["regime"]
    strategy = prediction["strategy"]
    confidence = prediction["confidence"]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("NIFTY", f"{live.get('nifty_ltp', np.nan):.2f}" if pd.notna(live.get("nifty_ltp")) else "—")
    c2.metric("India VIX", f"{live.get('vix_ltp', np.nan):.2f}" if pd.notna(live.get("vix_ltp")) else "—")
    c3.metric("ATM IV", f"{options.get('atm_iv', np.nan):.2f}" if pd.notna(options.get("atm_iv")) else "—")
    c4.metric("ATM Straddle", f"{options.get('straddle', np.nan):.2f}" if pd.notna(options.get("straddle")) else "—")
    c5.metric("PCR (OI)", f"{options.get('pcr_oi', np.nan):.2f}" if pd.notna(options.get("pcr_oi")) else "—")

    st.markdown('<div class="section">FRIDAY DECISION</div>', unsafe_allow_html=True)

    r1, r2, r3, r4 = st.columns(4)
    regime_cls = "good" if regime == "BULLISH" else "bad" if regime == "BEARISH" else "warn"
    strategy_cls = "good" if strategy in ["SELL PE", "SELL CE", "SHORT STRADDLE", "SHORT STRANGLE", "BULL PUT SPREAD", "BEAR CALL SPREAD"] else "warn"

    r1.markdown(
        f'<div class="friday-card"><div class="k-label">Market Regime</div>'
        f'<div class="k-value {regime_cls}">{regime}</div></div>',
        unsafe_allow_html=True,
    )
    r2.markdown(
        f'<div class="friday-card"><div class="k-label">Preferred Strategy</div>'
        f'<div class="k-value {strategy_cls}" style="font-size:22px">{strategy}</div></div>',
        unsafe_allow_html=True,
    )
    r3.markdown(
        f'<div class="friday-card"><div class="k-label">Confidence</div>'
        f'<div class="k-value">{confidence_label(confidence)}</div>'
        f'<div style="color:#91a4b8">{confidence:.1%}' if pd.notna(confidence)
        else '<div class="friday-card"><div class="k-label">Confidence</div><div class="k-value">—',
        unsafe_allow_html=True,
    )
    r3.markdown("</div></div>", unsafe_allow_html=True)
    r4.markdown(
        f'<div class="friday-card"><div class="k-label">Model Permission</div>'
        f'<div class="k-value {"good" if status=="TRAINED" else "warn"}">'
        f'{"LIVE" if status=="TRAINED" else "TRAIN FIRST"}</div></div>',
        unsafe_allow_html=True,
    )

    if status != "TRAINED":
        st.warning(
            "FRIDAY is connected to the live market, but it is not allowed to make a learned strategy recommendation "
            "until a historical training dataset is supplied and the model is trained."
        )
    else:
        st.success(
            f"FRIDAY recommends **{strategy}** for the current learned regime **{regime}** "
            f"with {confidence:.1%} model probability."
        )

    st.markdown('<div class="section">TOP STRATEGY RANKING</div>', unsafe_allow_html=True)

    ranking = prediction.get("ranking", [])
    if ranking:
        rank_df = pd.DataFrame(ranking, columns=["Strategy", "Probability"])
        rank_df["Probability"] = rank_df["Probability"].map(lambda x: f"{x:.1%}")
        st.dataframe(rank_df, use_container_width=True, hide_index=True)
    else:
        st.info("Train the model to see strategy ranking.")

    st.markdown('<div class="section">LIVE FEATURES SENT TO FRIDAY</div>', unsafe_allow_html=True)
    feature_display = live_frame.T.reset_index()
    feature_display.columns = ["Feature", "Value"]
    feature_display["Value"] = feature_display["Value"].map(
        lambda x: f"{float(x):.4f}" if pd.notna(x) and isinstance(x, (int, float, np.number)) else str(x)
    )
    st.dataframe(feature_display, use_container_width=True, hide_index=True)

except Exception as exc:
    st.error("FRIDAY could not complete the live market read.")
    st.code(str(exc))

st.markdown("---")
st.caption(
    "FRIDAY is a research/decision-support dashboard. It does not place orders. "
    "A trained model must be validated out-of-sample and walk-forward before live use."
)
