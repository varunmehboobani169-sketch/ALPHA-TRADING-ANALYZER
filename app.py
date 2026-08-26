
import math
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
    response = requests.post(
        API + path,
        headers=headers(),
        json=payload,
        timeout=45,
    )
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

    if not response.ok:
        msg = (
            body.get("remarks")
            or body.get("message")
            or body.get("error")
            or str(body)[:500]
        )
        raise RuntimeError(f"{label}: HTTP {response.status_code}: {msg}")

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


@st.cache_data(ttl=5, show_spinner=False)
def live_vix():
    body = api_post(
        "/marketfeed/ltp",
        {"IDX_I": [INDIA_VIX_ID]},
        "India VIX LTP",
    )
    data = parse_data(body)
    segment = data.get("IDX_I", {}) if isinstance(data, dict) else {}
    row = segment.get(str(INDIA_VIX_ID)) or segment.get(INDIA_VIX_ID) or {}
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


@st.cache_data(ttl=60, show_spinner=False)
def vix_history(timeframe, from_date, to_date):
    cfg = VIX_ALLOWED_TIMEFRAMES[timeframe]
    frames = []

    if cfg["mode"] == "daily":
        body = api_post(
            "/charts/historical",
            {
                "securityId": str(INDIA_VIX_ID),
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
                "securityId": str(INDIA_VIX_ID),
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


@st.cache_data(ttl=300, show_spinner=False)
def historical_atm_iv_sessions():
    """
    Fetch roughly 60 calendar days of weekly ATM CE/PE 1-minute IV, then
    reduce to the last 30 completed trading sessions.
    """
    end = local_now().date() + timedelta(days=1)
    start = local_now().date() - timedelta(days=HISTORY_CALENDAR_DAYS)
    frames = []

    for chunk_start, chunk_end in _date_chunks(
        start, end, 30
    ):
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
    raw = raw.dropna(subset=["iv"])

    # Per day: first available IV and last available IV for each side.
    piv = (
        raw.sort_values("datetime")
        .groupby(["date", "side"], as_index=False)
        .agg(
            open_iv=("iv", "first"),
            close_iv=("iv", "last"),
        )
    )

    wide = piv.pivot(index="date", columns="side", values=["open_iv", "close_iv"]).reset_index()
    wide.columns = [
        "_".join([str(x) for x in col if str(x) != ""])
        if isinstance(col, tuple) else str(col)
        for col in wide.columns
    ]

    rename = {
        "CALL_open_iv": "ce_open_iv",
        "PUT_open_iv": "pe_open_iv",
        "CALL_close_iv": "ce_close_iv",
        "PUT_close_iv": "pe_close_iv",
    }
    wide = wide.rename(columns=rename)

    for col in ["ce_open_iv", "pe_open_iv", "ce_close_iv", "pe_close_iv"]:
        if col not in wide.columns:
            wide[col] = np.nan

    wide["open_iv"] = wide[["ce_open_iv", "pe_open_iv"]].mean(axis=1)
    wide["close_iv"] = wide[["ce_close_iv", "pe_close_iv"]].mean(axis=1)
    wide["iv_change"] = wide["close_iv"] - wide["open_iv"]

    # Only completed sessions for the baseline.
    today = local_now().date()
    wide = wide[wide["date"] < today].copy()
    wide = wide.dropna(subset=["open_iv", "close_iv"]).sort_values("date")
    return wide.tail(HISTORY_SESSIONS).reset_index(drop=True)


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

    auto = st.checkbox("Auto Refresh", value=True)
    if st.button("🔄 Refresh Now"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.caption("Single-purpose volatility environment dashboard.")
    st.caption("Source: same Dhan API / instrument-master pipeline.")

if auto:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=60000, key="jarvis_refresh")
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
    vix_ltp, vix_ltt = live_vix()

    hist_end = local_now().date() + timedelta(days=1)
    hist_start = local_now().date() - timedelta(days=120 if vix_tf == "Day" else 60)
    vix_hist = vix_history(vix_tf, hist_start, hist_end)
    vix_state = vix_pnf_state(vix_hist, vix_box, vix_rev)

    iv_today = current_atm_iv()
    iv_hist = historical_atm_iv_sessions()

    if not iv_hist.empty:
        hist_avg_change = float(iv_hist["iv_change"].mean())
        hist_std_change = float(iv_hist["iv_change"].std(ddof=1)) if len(iv_hist) > 1 else np.nan
    else:
        hist_avg_change = np.nan
        hist_std_change = np.nan

    today_current_iv = iv_today.get("avg_iv", np.nan)
    today_date = local_now().date()

    if not iv_hist.empty and pd.notna(today_current_iv):
        # Use the first recorded IV of the current session when available.
        # If today's session is not returned by rolling history yet, use live
        # current IV as the current reading and leave open IV unavailable.
        current_hist = None
        if "date" in iv_hist.columns:
            current_hist = iv_hist[iv_hist["date"] == today_date]
        if current_hist is not None and not current_hist.empty:
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
        f'<div class="metric-value">{vix_ltp:.2f}' if pd.notna(vix_ltp)
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
    d1.metric("Expiry", iv_today.get("expiry", "—").strftime("%d-%b-%Y") if iv_today else "—")
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
