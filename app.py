
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
import requests
import numpy as np
import pandas as pd
import streamlit as st
from zoneinfo import ZoneInfo

try:
    import plotly.graph_objects as go
except Exception:
    go = None

st.set_page_config(
    page_title="JARVIS • Option Seller Environment",
    page_icon="🤖",
    layout="wide",
)

API = "https://api.dhan.co/v2"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
NIFTY_ID = 13
INDIA_VIX_ID = 26

VIX_ALLOWED_TIMEFRAMES = {
    "Day": {"mode": "daily", "interval": None},
    "1 Min": {"mode": "intraday", "interval": 1},
    "5 Min": {"mode": "intraday", "interval": 5},
    "15 Min": {"mode": "intraday", "interval": 15},
    "60 Min": {"mode": "intraday", "interval": 60},
}

DEFAULT_BOX = 0.25
DEFAULT_REVERSAL = 3
HISTORY_SESSIONS = 30
HISTORY_CALENDAR_DAYS = 60
INTRADAY_CHUNK_DAYS = 90

API_MIN_INTERVAL_SECONDS = 3.2
API_MAX_RETRIES = 4


def local_now():
    return datetime.now(LOCAL_TZ)


def ensure_state():
    st.session_state.setdefault("client_id", "")
    st.session_state.setdefault("access_token", "")
    st.session_state.setdefault("api_log", [])


def headers():
    if not st.session_state.client_id or not st.session_state.access_token:
        raise RuntimeError("Enter your Dhan Client ID and Access Token.")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": st.session_state.access_token,
        "client-id": st.session_state.client_id,
    }


def api_post(path, payload, label):
    last_error = None

    for attempt in range(API_MAX_RETRIES):
        # Simple per-session throttle to stay comfortably below Dhan rate limits.
        last_call = st.session_state.get("_jarvis_last_api_call", 0.0)
        wait_for = API_MIN_INTERVAL_SECONDS - (time.monotonic() - last_call)
        if wait_for > 0:
            time.sleep(wait_for)

        try:
            response = requests.post(
                API + path,
                headers=headers(),
                json=payload,
                timeout=45,
            )
        except requests.RequestException as exc:
            last_error = RuntimeError(f"{label}: network error: {exc}")
            if attempt < API_MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_error

        st.session_state["_jarvis_last_api_call"] = time.monotonic()

        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        st.session_state.api_log.append({
            "time": local_now().strftime("%H:%M:%S IST"),
            "label": label,
            "endpoint": path,
            "status": response.status_code,
        })

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = max(float(retry_after), 2.0)
            except Exception:
                delay = min(8.0, 2.0 ** attempt)
            last_error = RuntimeError(
                f"{label}: Dhan rate limit (DH-904). Retrying in {delay:.1f}s."
            )
            if attempt < API_MAX_RETRIES - 1:
                time.sleep(delay)
                continue
            raise last_error

        if not response.ok:
            msg = (
                body.get("remarks")
                or body.get("message")
                or body.get("errorMessage")
                or body.get("error")
                or str(body)[:500]
            )
            raise RuntimeError(f"{label}: HTTP {response.status_code}: {msg}")

        return body

    raise last_error or RuntimeError(f"{label}: request failed")


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
        "SEM_SMST_SECURITY_ID": "security_id",
        "UNDERLYING_SECURITY_ID": "underlying_security_id",
        "UNDERLYING_SYMBOL": "underlying_symbol",
        "SYMBOL_NAME": "symbol_name",
        "SEM_TRADING_SYMBOL": "trading_symbol",
        "DISPLAY_NAME": "display_name",
        "EXPIRY_DATE": "expiry_date",
    }
    df = df.rename(columns={c: rename.get(c, c) for c in df.columns})

    if "security_id" not in df.columns:
        for col in ["SM_SECURITY_ID", "SEM_SECURITY_ID"]:
            if col in df.columns:
                df["security_id"] = df[col]
                break

    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    for col in ["exchange", "segment", "instrument", "trading_symbol",
                "underlying_symbol", "symbol_name", "display_name"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.upper().str.strip()
    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")

    return df.dropna(subset=["security_id"]).copy()


@st.cache_data(ttl=21600, show_spinner=False)
def resolve_india_vix_security_id(master):
    """
    Resolve India VIX from the same Dhan instrument master used by the app.
    Avoids depending on a hard-coded security ID.
    """
    x = master.copy()
    text_cols = [
        c for c in ["underlying_symbol", "symbol_name",
                    "trading_symbol", "display_name"]
        if c in x.columns
    ]
    if not text_cols:
        return 26

    masks = []
    for c in text_cols:
        s = x[c].astype(str).str.upper().str.replace(" ", "", regex=False)
        masks.append(
            s.str.contains("INDIAVIX", regex=False, na=False)
            | s.str.fullmatch("VIX", na=False)
            | s.str.contains("INDIAVIX", regex=False, na=False)
        )
    mask = masks[0]
    for m in masks[1:]:
        mask = mask | m

    vix = x[mask].copy()
    if not vix.empty:
        ids = pd.to_numeric(vix["security_id"], errors="coerce").dropna()
        if not ids.empty:
            return int(ids.iloc[0])

    return 26



@st.cache_data(ttl=5, show_spinner=False)
def live_vix(vix_id):
    body = api_post(
        "/marketfeed/ltp",
        {"IDX_I": [int(vix_id)]},
        "India VIX LTP",
    )
    data = parse_data(body)
    segment = data.get("IDX_I", {}) if isinstance(data, dict) else {}
    row = segment.get(str(vix_id)) or segment.get(vix_id) or {}
    ltp = pd.to_numeric(row.get("last_price"), errors="coerce")
    ltt = pd.to_numeric(row.get("last_trade_time"), errors="coerce")
    return (
        float(ltp) if pd.notna(ltp) else np.nan,
        float(ltt) if pd.notna(ltt) else np.nan,
    )


def _parse_arrays(data, include_oi=False):
    if not isinstance(data, dict):
        return pd.DataFrame()

    d = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(d, dict) or not d.get("timestamp"):
        return pd.DataFrame()

    timestamps = pd.to_numeric(pd.Series(d["timestamp"]), errors="coerce")
    # Dhan chart endpoints return Unix epoch seconds.
    dt = pd.to_datetime(
        timestamps,
        unit="s",
        utc=True,
        errors="coerce",
    ).dt.tz_convert(LOCAL_TZ)

    out = pd.DataFrame({"datetime": dt})

    for source, target in [
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("close", "close"),
        ("volume", "volume"),
        ("open_interest", "oi"),
        ("oi", "oi"),
        ("iv", "iv"),
        ("strike", "strike"),
        ("spot", "spot"),
    ]:
        values = d.get(source)
        if values is not None and len(values) == len(out):
            out[target] = pd.to_numeric(pd.Series(values), errors="coerce")

    if include_oi and "oi" not in out.columns:
        out["oi"] = np.nan

    return (
        out.dropna(subset=["datetime"])
        .sort_values("datetime")
        .drop_duplicates("datetime")
        .reset_index(drop=True)
    )


def _date_chunks(start_date, end_date, max_days):
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    current = start
    while current < end:
        nxt = min(current + pd.Timedelta(days=max_days), end)
        yield current.date(), nxt.date()
        current = nxt


@st.cache_data(ttl=600, show_spinner=False)
def vix_history(timeframe, from_date, to_date, vix_id):
    cfg = VIX_ALLOWED_TIMEFRAMES[timeframe]
    frames = []

    if cfg["mode"] == "daily":
        body = api_post(
            "/charts/historical",
            {
                "securityId": str(vix_id),
                "exchangeSegment": "IDX_I",
                "instrument": "INDEX",
                "expiryCode": 0,
                "oi": False,
                "fromDate": pd.Timestamp(from_date).strftime("%Y-%m-%d"),
                "toDate": pd.Timestamp(to_date).strftime("%Y-%m-%d"),
            },
            "India VIX daily history",
        )
        return _parse_arrays(body)

    for chunk_start, chunk_end in _date_chunks(
        from_date, to_date, INTRADAY_CHUNK_DAYS
    ):
        body = api_post(
            "/charts/intraday",
            {
                "securityId": str(vix_id),
                "exchangeSegment": "IDX_I",
                "instrument": "INDEX",
                "interval": str(cfg["interval"]),
                "oi": False,
                "fromDate": f"{chunk_start} 00:00:00",
                "toDate": f"{chunk_end} 00:00:00",
            },
            f"India VIX {timeframe} history",
        )
        part = _parse_arrays(body)
        if not part.empty:
            frames.append(part)

    if not frames:
        return pd.DataFrame()

    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )


@st.cache_data(ttl=10, show_spinner=False)
def current_nifty_option_chain():
    body = api_post(
        "/optionchain",
        {
            "UnderlyingScrip": NIFTY_ID,
            "UnderlyingSeg": "IDX_I",
            "Expiry": "",
        },
        "NIFTY option chain",
    )
    return body


def expiry_list():
    body = api_post(
        "/optionchain/expirylist",
        {
            "UnderlyingScrip": NIFTY_ID,
            "UnderlyingSeg": "IDX_I",
        },
        "NIFTY expiry list",
    )
    data = parse_data(body)
    values = data.get("data") if isinstance(data, dict) else []
    dates = []
    for value in values if isinstance(values, list) else []:
        try:
            dates.append(pd.Timestamp(str(value)).date())
        except Exception:
            pass
    return sorted(set(dates))


def option_chain_for_expiry(expiry):
    return api_post(
        "/optionchain",
        {
            "UnderlyingScrip": NIFTY_ID,
            "UnderlyingSeg": "IDX_I",
            "Expiry": expiry.strftime("%Y-%m-%d"),
        },
        "NIFTY option chain for expiry",
    )


def parse_option_chain(body):
    data = parse_data(body)
    rows = []
    if not isinstance(data, dict):
        return pd.DataFrame()

    spot = pd.to_numeric(data.get("last_price"), errors="coerce")

    for strike_raw, pair in (data.get("oc") or {}).items():
        try:
            strike = float(strike_raw)
        except Exception:
            continue
        if not isinstance(pair, dict):
            continue

        for key, side in [("ce", "CE"), ("pe", "PE")]:
            leg = pair.get(key)
            if not isinstance(leg, dict):
                continue
            rows.append({
                "strike": strike,
                "side": side,
                "ltp": pd.to_numeric(leg.get("last_price"), errors="coerce"),
                "iv": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                "oi": pd.to_numeric(leg.get("oi"), errors="coerce"),
                "security_id": pd.to_numeric(leg.get("security_id"), errors="coerce"),
            })

    return pd.DataFrame(rows), float(spot) if pd.notna(spot) else np.nan


@st.cache_data(ttl=120, show_spinner=False)
def current_atm_iv():
    expiries = expiry_list()
    today = local_now().date()
    future = [d for d in expiries if d >= today]
    if not future:
        return {}

    expiry = future[0]
    raw = option_chain_for_expiry(expiry)
    chain, spot = parse_option_chain(raw)
    if chain.empty or pd.isna(spot):
        return {}

    strikes = sorted(chain["strike"].dropna().unique())
    atm = min(strikes, key=lambda x: abs(float(x) - float(spot)))

    ce = chain[(chain["side"] == "CE") & np.isclose(chain["strike"], atm)]
    pe = chain[(chain["side"] == "PE") & np.isclose(chain["strike"], atm)]

    ce_iv = float(ce.iloc[-1]["iv"]) if not ce.empty and pd.notna(ce.iloc[-1]["iv"]) else np.nan
    pe_iv = float(pe.iloc[-1]["iv"]) if not pe.empty and pd.notna(pe.iloc[-1]["iv"]) else np.nan
    avg_iv = float(np.nanmean([ce_iv, pe_iv])) if not (pd.isna(ce_iv) and pd.isna(pe_iv)) else np.nan

    return {
        "expiry": expiry,
        "spot": spot,
        "atm": float(atm),
        "ce_iv": ce_iv,
        "pe_iv": pe_iv,
        "avg_iv": avg_iv,
    }


@st.cache_data(ttl=900, show_spinner=False)
@st.cache_data(ttl=900, show_spinner=False)
def historical_atm_iv_sessions():
    """
    Fetch enough rolling weekly ATM CE/PE IV history to obtain:
      - today's Open/Close IV where available
      - the previous 30 completed trading sessions
    Today's row is intentionally retained here; the dashboard excludes it
    when calculating the 30-session baseline.
    """
    end_date = local_now().date() + timedelta(days=1)
    start_date = local_now().date() - timedelta(days=60)
    frames = []

    for chunk_start, chunk_end in _date_chunks(start_date, end_date, 30):
        for side in ["CALL", "PUT"]:
            body = api_post(
                "/charts/rollingoption",
                {
                    "exchangeSegment": "NSE_FNO",
                    "interval": "1",
                    "securityId": NIFTY_ID,
                    "instrument": "OPTIDX",
                    "expiryFlag": "WEEK",
                    "expiryCode": 0,
                    "strike": "ATM",
                    "drvOptionType": side,
                    "requiredData": [
                        "open",
                        "high",
                        "low",
                        "close",
                        "iv",
                        "volume",
                        "strike",
                        "oi",
                        "spot",
                    ],
                    "fromDate": chunk_start.strftime("%Y-%m-%d"),
                    "toDate": chunk_end.strftime("%Y-%m-%d"),
                },
                f"Historical ATM {side} IV",
            )

            data = parse_data(body)
            key = "ce" if side == "CALL" else "pe"
            leg = data.get(key) if isinstance(data, dict) else None
            if not isinstance(leg, dict):
                continue

            df = _parse_arrays(leg)
            if df.empty or "iv" not in df.columns:
                continue

            df["side"] = side
            frames.append(df[["datetime", "iv", "side"]])

    if not frames:
        return pd.DataFrame()

    raw = pd.concat(frames, ignore_index=True)
    raw["date"] = raw["datetime"].dt.date
    raw = raw.dropna(subset=["iv"]).sort_values("datetime")

    # Keep first and last available IV in every session for each side.
    piv = (
        raw.groupby(["date", "side"], as_index=False)
        .agg(
            open_iv=("iv", "first"),
            close_iv=("iv", "last"),
        )
    )

    wide = (
        piv.pivot(index="date", columns="side", values=["open_iv", "close_iv"])
        .reset_index()
    )
    wide.columns = [
        "_".join([str(x) for x in col if str(x) != ""])
        if isinstance(col, tuple) else str(col)
        for col in wide.columns
    ]
    wide = wide.rename(columns={
        "CALL_open_iv": "ce_open_iv",
        "PUT_open_iv": "pe_open_iv",
        "CALL_close_iv": "ce_close_iv",
        "PUT_close_iv": "pe_close_iv",
    })

    for col in ["ce_open_iv", "pe_open_iv", "ce_close_iv", "pe_close_iv"]:
        if col not in wide.columns:
            wide[col] = np.nan

    wide["open_iv"] = wide[["ce_open_iv", "pe_open_iv"]].mean(axis=1)
    wide["close_iv"] = wide[["ce_close_iv", "pe_close_iv"]].mean(axis=1)
    wide["iv_change"] = wide["close_iv"] - wide["open_iv"]

    # Keep only the latest 31 sessions: today + previous 30 completed.
    today = local_now().date()
    wide = wide.dropna(subset=["open_iv", "close_iv"]).sort_values("date")
    historical = wide[wide["date"] < today].tail(HISTORY_SESSIONS)
    today_row = wide[wide["date"] == today]

    return pd.concat([historical, today_row], ignore_index=True).sort_values("date").reset_index(drop=True)


def build_pnf(closes, box_pct, reversal):
    vals = [
        float(v) for v in pd.to_numeric(pd.Series(closes), errors="coerce").dropna()
        if float(v) > 0
    ]
    if len(vals) < 2:
        return []

    log_box = math.log1p(float(box_pct) / 100.0)
    level0 = math.floor(math.log(vals[0]) / log_box)
    direction = None
    high = level0
    low = level0
    columns = []

    for value in vals[1:]:
        level = math.floor(math.log(value) / log_box)

        if direction is None:
            if level >= high + 1:
                direction = "X"
                high = level
                columns.append({"type": "X", "top": high, "bottom": high})
            elif level <= low - 1:
                direction = "O"
                low = level
                columns.append({"type": "O", "top": low, "bottom": low})
            continue

        if direction == "X":
            if level > high:
                high = level
                columns[-1]["top"] = high
            elif level <= high - reversal:
                previous_high = high
                direction = "O"
                low = level
                columns.append({
                    "type": "O",
                    "top": previous_high - 1,
                    "bottom": low,
                })
        else:
            if level < low:
                low = level
                columns[-1]["bottom"] = low
            elif level >= low + reversal:
                previous_low = low
                direction = "X"
                high = level
                columns.append({
                    "type": "X",
                    "top": high,
                    "bottom": previous_low + 1,
                })

    return columns


def vix_pnf_state(df, box_pct, reversal):
    if df.empty or "close" not in df.columns:
        return {
            "state": "UNAVAILABLE",
            "direction": None,
            "columns": [],
        }

    cols = build_pnf(df["close"], box_pct, reversal)
    if not cols:
        return {
            "state": "SIDEWAYS / NEUTRAL",
            "direction": None,
            "columns": [],
        }

    last = cols[-1]
    if last["type"] == "O":
        state = "ACTIVE SELL"
        direction = "BEARISH"
    else:
        state = "ACTIVE LONG"
        direction = "BULLISH"

    return {
        "state": state,
        "direction": direction,
        "columns": cols,
    }


def make_pnf_figure(df, box_pct, reversal):
    if go is None or df.empty:
        return None

    cols = build_pnf(df["close"], box_pct, reversal)
    if not cols:
        return None

    log_box = math.log1p(float(box_pct) / 100.0)
    xs, ys, texts, labels = [], [], [], []

    for col_idx, col in enumerate(cols):
        start = col["bottom"]
        end = col["top"]
        for level in range(int(start), int(end) + 1):
            xs.append(col_idx)
            ys.append(math.exp(level * log_box))
            if col["type"] == "X":
                texts.append("X")
                labels.append("bullish")
            else:
                texts.append("O")
                labels.append("bearish")

    fig = go.Figure()
    for kind, marker_symbol, color in [
        ("bullish", "x", "#39ff70"),
        ("bearish", "circle-open", "#ff3b30"),
    ]:
        idx = [i for i, label in enumerate(labels) if label == kind]
        if not idx:
            continue
        fig.add_trace(go.Scatter(
            x=[xs[i] for i in idx],
            y=[ys[i] for i in idx],
            mode="markers+text",
            text=[texts[i] for i in idx],
            textposition="middle center",
            marker={
                "symbol": marker_symbol,
                "size": 10,
                "color": color,
                "line": {"width": 2, "color": color},
            },
            hovertemplate="Column %{x}<br>Level %{y:.2f}<extra></extra>",
            name="X" if kind == "bullish" else "O",
        ))

    fig.update_layout(
        height=390,
        margin=dict(l=10, r=10, t=20, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(4,10,18,0.55)",
        xaxis=dict(
            title="P&F Columns",
            showgrid=True,
            gridcolor="rgba(255,255,255,0.08)",
            zeroline=False,
        ),
        yaxis=dict(
            title="India VIX",
            showgrid=True,
            gridcolor="rgba(255,255,255,0.08)",
            zeroline=False,
        ),
        legend=dict(orientation="h", y=1.06, x=0),
    )
    return fig


def environment_assessment(vix_state, iv_change, avg_change, std_change):
    if pd.isna(iv_change):
        iv_class = "UNAVAILABLE"
    else:
        z = (
            (iv_change - avg_change) / std_change
            if pd.notna(std_change) and std_change > 0 else 0
        )
        if z >= 2:
            iv_class = "EXPANDING SHARPLY"
        elif z >= 0.5 or iv_change > 0:
            iv_class = "EXPANDING"
        elif z <= -0.5 or iv_change < 0:
            iv_class = "CONTRACTING"
        else:
            iv_class = "STABLE"

    if vix_state == "ACTIVE SELL":
        if iv_class in {"CONTRACTING", "STABLE"}:
            overall = "FAVOURABLE"
            reason = "VIX is falling/soft while ATM IV is not expanding."
        elif iv_class == "EXPANDING":
            overall = "CAUTION"
            reason = "VIX is favourable, but ATM IV is expanding."
        else:
            overall = "CAUTION"
            reason = "VIX is favourable, but IV conditions are not fully confirmed."
    elif vix_state == "ACTIVE LONG":
        if iv_class == "CONTRACTING":
            overall = "CAUTION"
            reason = "VIX is rising even though ATM IV is contracting."
        else:
            overall = "NOT FAVOURABLE"
            reason = "VIX is rising; short-volatility conditions are weak."
    else:
        overall = "CAUTION"
        reason = "VIX has no clear directional P&F state."

    return overall, iv_class, reason



def fmt_num(value, digits=2, signed=False):
    """Safe numeric formatter that never applies numeric formatting to tuples."""
    try:
        if isinstance(value, (tuple, list, dict)):
            return "—"
        value = float(value)
        if not np.isfinite(value):
            return "—"
        spec = f"+.{digits}f" if signed else f".{digits}f"
        return format(value, spec)
    except (TypeError, ValueError):
        return "—"


def safe_call(cache_key, fn, *args):
    """Return fresh data or last successful session result on transient API/rate errors."""
    try:
        value = fn(*args)
        st.session_state[cache_key] = value
        st.session_state.pop(f"{cache_key}_error", None)
        return value, None
    except Exception as exc:
        stale = st.session_state.get(cache_key)
        st.session_state[f"{cache_key}_error"] = str(exc)
        return stale, str(exc)


def inject_css():
    st.markdown(
        """
        <style>
        .stApp{
            background:
              radial-gradient(circle at 75% 0%, rgba(255,30,30,.10), transparent 30%),
              linear-gradient(180deg,#050912 0%,#070b12 100%);
        }
        .block-container{max-width:1550px;padding-top:1rem;}
        .jarvis-hero{
            background:linear-gradient(135deg,#111a27,#07101c);
            border:1px solid rgba(255,70,50,.28);
            border-radius:18px;
            padding:24px 28px;
            box-shadow:0 0 40px rgba(255,40,20,.08);
            margin-bottom:14px;
        }
        .jarvis-title{font-size:38px;font-weight:800;color:#ff453a;letter-spacing:2px;}
        .jarvis-sub{font-size:15px;color:#91a1b5;}
        .jarvis-card{
            background:rgba(10,19,31,.82);
            border:1px solid rgba(94,160,220,.18);
            border-radius:16px;
            padding:18px;
            min-height:120px;
            box-shadow:0 8px 30px rgba(0,0,0,.18);
        }
        .metric-label{font-size:12px;color:#8fa0b5;text-transform:uppercase;letter-spacing:1px;}
        .metric-value{font-size:32px;font-weight:750;color:#f5f7fb;margin-top:5px;}
        .good{color:#36f06a !important;}
        .warn{color:#ffc436 !important;}
        .bad{color:#ff4a48 !important;}
        .section-title{font-size:18px;font-weight:750;color:#dfe8f2;margin-bottom:8px;}
        .jarvis-banner{
            border-radius:14px;padding:16px 20px;margin:10px 0 16px 0;
            font-size:21px;font-weight:750;border:1px solid rgba(255,255,255,.08);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


ensure_state()
inject_css()

with st.sidebar:
    st.markdown('\n<div style="text-align:center;margin-bottom:10px;">\n  <img src="data:image/png;base64,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" style="width:110px;height:110px;object-fit:cover;border-radius:18px;box-shadow:0 0 20px rgba(255,45,45,.35);">\n</div>\n', unsafe_allow_html=True)
    st.markdown(
        """
        <div style="font-size:34px;font-weight:800;color:#ff453a;letter-spacing:2px;">JARVIS</div>
        <div style="color:#93a1b4;margin-bottom:18px;">Option Seller Environment</div>
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

    auto = st.checkbox("Auto Refresh (Every 5 Min)", value=True)
    if st.button("🔄 Refresh Now"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.caption("Single-purpose volatility environment dashboard.")
    st.caption("Source: same Dhan API / instrument-master pipeline.")

if auto:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=300000, key="jarvis_refresh")
    except Exception:
        pass

st.markdown(
    """
    <div class="jarvis-hero">
      <div class="jarvis-title">JARVIS</div>
      <div class="jarvis-sub">OPTION SELLER ENVIRONMENT</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# VIX controls
left, right = st.columns([2.2, 1])
with left:
    st.markdown('<div class="section-title">1. INDIA VIX ENVIRONMENT</div>', unsafe_allow_html=True)
with right:
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        vix_tf = st.selectbox("VIX Timeframe", list(VIX_ALLOWED_TIMEFRAMES.keys()), index=0, key="jarvis_vix_tf")
    with col_b:
        vix_box = st.number_input("VIX Box %", min_value=0.05, max_value=10.0, value=DEFAULT_BOX, step=0.05, format="%.2f", key="jarvis_vix_box")
    with col_c:
        vix_rev = st.number_input("Reversal", min_value=1, max_value=10, value=DEFAULT_REVERSAL, step=1, key="jarvis_vix_rev")

try:
    master = load_master()
    vix_id = resolve_india_vix_security_id(master)

    live_vix_result, live_vix_error = safe_call("jarvis_live_vix", live_vix, vix_id)
    if isinstance(live_vix_result, tuple) and len(live_vix_result) == 2:
        vix_ltp, vix_ltt = live_vix_result
    else:
        vix_ltp, vix_ltt = np.nan, np.nan

    hist_end = local_now().date() + timedelta(days=1)
    hist_start = local_now().date() - timedelta(days=120 if vix_tf == "Day" else 60)
    vix_hist, vix_hist_error = safe_call(
        "jarvis_vix_history",
        vix_history,
        vix_tf,
        hist_start,
        hist_end,
        vix_id,
    )
    if vix_hist is None:
        vix_hist = pd.DataFrame()

    vix_state = vix_pnf_state(vix_hist, vix_box, vix_rev)

    iv_today, iv_today_error = safe_call("jarvis_iv_today", current_atm_iv)
    iv_hist, iv_hist_error = safe_call("jarvis_iv_history", historical_atm_iv_sessions)
    if iv_hist is None:
        iv_hist = pd.DataFrame()
    if iv_today is None:
        iv_today = {}

    today_current_iv = iv_today.get("avg_iv", np.nan) if isinstance(iv_today, dict) else np.nan
    today_date = local_now().date()

    # Historical baseline = previous 30 completed sessions only.
    if not iv_hist.empty:
        baseline = iv_hist[iv_hist["date"] < today_date].tail(HISTORY_SESSIONS)
    else:
        baseline = pd.DataFrame()

    if not baseline.empty:
        hist_avg_change = float(baseline["iv_change"].mean())
        hist_std_change = (
            float(baseline["iv_change"].std(ddof=1))
            if len(baseline) > 1 else np.nan
        )
    else:
        hist_avg_change = np.nan
        hist_std_change = np.nan

    # Today's Open IV comes from today's retained historical row.
    if not iv_hist.empty and pd.notna(today_current_iv):
        current_hist = iv_hist[iv_hist["date"] == today_date]
        if not current_hist.empty and pd.notna(current_hist.iloc[-1]["open_iv"]):
            today_open_iv = float(current_hist.iloc[-1]["open_iv"])
            today_change = today_current_iv - today_open_iv
        else:
            today_open_iv = np.nan
            today_change = np.nan
    else:
        today_open_iv = np.nan
        today_change = np.nan

    overall, iv_class, reason = environment_assessment(
        vix_state["state"],
        today_change,
        hist_avg_change,
        hist_std_change,
    )

    errors = [e for e in [live_vix_error, vix_hist_error, iv_today_error, iv_hist_error] if e]
    if errors:
        st.warning(
            "Some live requests were rate-limited. JARVIS is showing the last successful cached data "
            "where available and will retry automatically."
        )

    # Top status
    status_cls = "good" if overall == "FAVOURABLE" else "warn" if overall == "CAUTION" else "bad"
    st.markdown(
        f'<div class="jarvis-banner"><span class="{status_cls}">OVERALL ENVIRONMENT: {overall}</span>'
        f' <span style="color:#93a1b4;font-size:15px;font-weight:500;">— {reason}</span></div>',
        unsafe_allow_html=True,
    )

    # VIX cards
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(
        f'<div class="jarvis-card"><div class="metric-label">India VIX</div>'
        f'<div class="metric-value">{fmt_num(vix_ltp)}' if pd.notna(vix_ltp)
        else '<div class="jarvis-card"><div class="metric-label">India VIX</div><div class="metric-value">—',
        unsafe_allow_html=True,
    )
    c1.markdown("</div>", unsafe_allow_html=True)

    state_cls = "good" if vix_state["state"] == "ACTIVE SELL" else "bad" if vix_state["state"] == "ACTIVE LONG" else "warn"
    c2.markdown(
        f'<div class="jarvis-card"><div class="metric-label">VIX P&F STATE</div>'
        f'<div class="metric-value {state_cls}" style="font-size:24px">{vix_state["state"]}</div>'
        f'<div style="color:#8fa0b5">{vix_state["direction"] or "NEUTRAL"} • {vix_tf} • {vix_box:.2f}% • {int(vix_rev)}R</div></div>',
        unsafe_allow_html=True,
    )

    c3.markdown(
        f'<div class="jarvis-card"><div class="metric-label">ATM AVG IV</div>'
        f'<div class="metric-value">{today_current_iv:.2f}' if pd.notna(today_current_iv)
        else '<div class="jarvis-card"><div class="metric-label">ATM AVG IV</div><div class="metric-value">—',
        unsafe_allow_html=True,
    )
    c3.markdown("</div>", unsafe_allow_html=True)

    iv_cls = "good" if iv_class == "CONTRACTING" else "bad" if "EXPANDING" in iv_class else "warn"
    c4.markdown(
        f'<div class="jarvis-card"><div class="metric-label">IV BEHAVIOUR</div>'
        f'<div class="metric-value {iv_cls}" style="font-size:24px">{iv_class}</div>'
        f'<div style="color:#8fa0b5">Today vs 30-session baseline</div></div>',
        unsafe_allow_html=True,
    )

    # VIX P&F chart
    st.markdown("#### India VIX • P&F", unsafe_allow_html=True)
    if vix_hist.empty:
        st.warning("India VIX historical data is currently unavailable.")
    elif go is not None:
        fig = make_pnf_figure(vix_hist, vix_box, int(vix_rev))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.info("Plotly is not installed; the VIX state is still calculated.")

    st.caption(
        f"VIX state calculated using: {vix_box:.2f}% box • {int(vix_rev)} box reversal • "
        f"{vix_tf} timeframe • close only."
    )

    # Current ATM IV details
    st.markdown("#### Current NIFTY ATM IV", unsafe_allow_html=True)
    d1, d2, d3, d4 = st.columns(4)
    expiry_value = iv_today.get("expiry") if isinstance(iv_today, dict) else None
    d1.metric(
        "Expiry",
        expiry_value.strftime("%d-%b-%Y") if hasattr(expiry_value, "strftime") else "—",
    )
    d2.metric("ATM Strike", f"{iv_today.get('atm'):.0f}" if iv_today and pd.notna(iv_today.get("atm")) else "—")
    d3.metric("ATM CE IV", f"{iv_today.get('ce_iv'):.2f}" if iv_today and pd.notna(iv_today.get("ce_iv")) else "—")
    d4.metric("ATM PE IV", f"{iv_today.get('pe_iv'):.2f}" if iv_today and pd.notna(iv_today.get("pe_iv")) else "—")

    # IV baseline comparison
    st.markdown("#### Today's IV vs Last 30 Completed Sessions", unsafe_allow_html=True)

    vals = st.columns(6)
    vals[0].metric(
        "Today's Open IV",
        f"{today_open_iv:.2f}" if pd.notna(today_open_iv) else "—",
    )
    vals[1].metric(
        "Current IV",
        f"{today_current_iv:.2f}" if pd.notna(today_current_iv) else "—",
    )
    vals[2].metric(
        "Today's IV Change",
        f"{today_change:+.2f}" if pd.notna(today_change) else "—",
    )
    vals[3].metric(
        "30D Avg Change",
        f"{hist_avg_change:+.2f}" if pd.notna(hist_avg_change) else "—",
    )

    z_score = (
        (today_change - hist_avg_change) / hist_std_change
        if pd.notna(today_change) and pd.notna(hist_avg_change)
        and pd.notna(hist_std_change) and hist_std_change > 0
        else np.nan
    )
    deviation = (
        today_change - hist_avg_change
        if pd.notna(today_change) and pd.notna(hist_avg_change)
        else np.nan
    )
    ratio = (
        abs(today_change) / abs(hist_avg_change)
        if pd.notna(today_change) and pd.notna(hist_avg_change) and abs(hist_avg_change) > 1e-9
        else np.nan
    )

    vals[4].metric("Difference vs 30D Avg", f"{deviation:+.2f}" if pd.notna(deviation) else "—")
    vals[5].metric("Z-Score", f"{z_score:+.2f}" if pd.notna(z_score) else "—")

    # Last 30 session table
    if not iv_hist.empty:
        table = iv_hist.copy()
        table["date"] = pd.to_datetime(table["date"]).dt.strftime("%d-%b-%Y")
        table["iv_change"] = table["iv_change"].map(lambda x: f"{x:+.2f}")
        table = table.rename(columns={
            "date": "Date",
            "open_iv": "Open IV",
            "close_iv": "Close IV",
            "iv_change": "IV Change",
        })[["Date", "Open IV", "Close IV", "IV Change"]]

        st.dataframe(
            table.sort_values("Date", ascending=False),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown(
        f"""
        <div class="jarvis-card">
          <div class="metric-label">JARVIS CONCLUSION</div>
          <div style="font-size:24px;font-weight:800;margin:8px 0;" class="{status_cls}">{overall}</div>
          <div style="color:#c8d2de">{reason}</div>
          <div style="color:#718197;margin-top:8px;font-size:13px;">
            This module measures the volatility environment only. It does not generate a CE, PE,
            straddle or strangle trade by itself.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

except Exception as exc:
    st.error("JARVIS could not complete the latest environment calculation.")
    st.code(str(exc))
    st.info("Check the Dhan Client ID / Access Token and refresh. The dashboard does not place orders.")
