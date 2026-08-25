
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st
from zoneinfo import ZoneInfo

st.set_page_config(page_title="ALPHA ANALYZER V9", page_icon="α", layout="wide")

API = "https://api.dhan.co/v2"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")


def _trade_entry_window(module_name, mode):
    """Return allowed NEW-entry and forced-exit windows in IST."""
    now = local_now().time()
    if module_name == "NSE":
        return (
            datetime.strptime("09:15", "%H:%M").time(),
            datetime.strptime("15:40", "%H:%M").time(),
        )
    if module_name == "MCX":
        return (
            datetime.strptime("09:00", "%H:%M").time(),
            datetime.strptime("23:30", "%H:%M").time(),
        )
    return (
        datetime.strptime("00:00", "%H:%M").time(),
        datetime.strptime("23:59", "%H:%M").time(),
    )


def _trade_entry_allowed(module_name, mode):
    """
    Strict NEW-TRADE gate.
    NSE Intraday: 09:15 through 15:40.
    NSE Positional: 09:15 through 15:40 for a fresh signal as well.
    MCX: its normal live window.
    """
    now = local_now().time()

    if module_name == "NSE":
        start = datetime.strptime("09:15", "%H:%M").time()
        end = datetime.strptime("15:40", "%H:%M").time()
        return start <= now <= end

    if module_name == "MCX":
        start = datetime.strptime("09:00", "%H:%M").time()
        end = datetime.strptime("23:30", "%H:%M").time()
        return start <= now <= end

    return True


def local_now():
    """Application display time: India Standard Time (IST)."""
    return datetime.now(LOCAL_TZ)


# -----------------------------
# Session credentials
# -----------------------------
if "client_id" not in st.session_state:
    st.session_state.client_id = ""
if "access_token" not in st.session_state:
    st.session_state.access_token = ""
if "api_log" not in st.session_state:
    st.session_state.api_log = []

# Per-session trade book. It survives Streamlit reruns and is downloadable.
if "trade_book" not in st.session_state:
    st.session_state.trade_book = {}

if "trade_sequence" not in st.session_state:
    st.session_state.trade_sequence = 0
if "fresh_trade_log" not in st.session_state:
    st.session_state.fresh_trade_log = []
if "fresh_trade_loaded_date" not in st.session_state:
    st.session_state.fresh_trade_loaded_date = None

with st.sidebar:
    st.markdown(
        """
        <div class="a-brand">
            <div class="a-mark">A</div>
            <div>
                <div class="a-brand-name">ALPHA ANALYZER</div>
                <div class="a-brand-sub">Professional Market Dashboard</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="a-side-head">Account</div>', unsafe_allow_html=True)
    st.session_state.client_id = st.text_input(
        "User Name",
        value=st.session_state.client_id,
    ).strip()
    st.session_state.access_token = st.text_input(
        "Password",
        value=st.session_state.access_token,
        type="password",
    ).strip()
    auto = st.checkbox("Auto Refresh", True)
    page = st.radio(
        "Module",
        [
            "Market Overview",
            "Fresh Trades",
            "Trade Logs",
            "Option Seller",
            "Momentum",
            "Positional",
            "MCX Futures",
            "Sector Analysis",
            "RS Matrix",
        ],
    )


# -----------------------------
# Client UI Theme
# -----------------------------
st.markdown(
    """
    <div class="alpha-statusbar">
        <span class="alpha-live-dot"></span>
        <span>ALPHA ANALYZER</span>
        <span class="alpha-divider">•</span>
        <span class="alpha-status-text">LIVE MARKET DASHBOARD</span>
    </div>
    """,
    unsafe_allow_html=True,
)



# -----------------------------
# ALPHA ANALYZER — REFERENCE DASHBOARD UI
# -----------------------------
st.markdown(
    """
    <style>
    :root{
        --bg:#070d16;
        --panel:#0c1522;
        --line:rgba(255,255,255,.075);
        --text:#f4f7fb;
        --muted:#7f8ca0;
        --green:#22d77c;
        --red:#ff5364;
        --yellow:#f3c95c;
        --blue:#3d8dff;
    }

    .stApp{
        background:
            radial-gradient(circle at 82% -5%, rgba(40,105,194,.15), transparent 27%),
            linear-gradient(180deg,#060b14 0%,#07101a 100%);
    }

    .block-container{
        max-width:1580px;
        padding-top:1rem;
        padding-left:.9rem;
        padding-right:.9rem;
        padding-bottom:2rem;
    }

    section[data-testid="stSidebar"]{
        background:linear-gradient(180deg,#09111c 0%,#060c15 100%);
        border-right:1px solid var(--line);
    }

    section[data-testid="stSidebar"] .block-container{
        padding:.75rem .70rem 1rem .70rem;
    }

    .a-brand{
        display:flex;
        align-items:center;
        gap:10px;
        margin:3px 3px 11px;
    }

    .a-mark{
        width:38px;
        height:38px;
        border-radius:10px;
        display:flex;
        align-items:center;
        justify-content:center;
        background:linear-gradient(145deg,#1f7ef0,#7188ff);
        color:#fff;
        font-size:18px;
        font-weight:900;
        box-shadow:0 8px 22px rgba(31,126,240,.25);
    }

    .a-brand-name{
        font-size:1.03rem;
        font-weight:900;
        letter-spacing:.03em;
    }

    .a-brand-sub{
        font-size:.62rem;
        color:var(--muted);
        margin-top:2px;
    }

    .a-side-head{
        margin:12px 3px 6px;
        color:var(--muted);
        font-size:.60rem;
        font-weight:800;
        letter-spacing:.10em;
        text-transform:uppercase;
    }

    .a-account{
        border:1px solid var(--line);
        border-radius:11px;
        padding:9px 10px;
        margin-bottom:8px;
        background:rgba(255,255,255,.018);
    }

    .a-account-label{
        color:var(--muted);
        font-size:.60rem;
        letter-spacing:.07em;
        text-transform:uppercase;
    }

    .a-account-value{
        font-weight:800;
        font-size:.84rem;
        margin-top:2px;
    }

    .a-topbar{
        display:flex;
        justify-content:space-between;
        align-items:center;
        padding:8px 12px;
        margin-bottom:11px;
        border:1px solid var(--line);
        border-radius:11px;
        background:rgba(255,255,255,.016);
    }

    .a-top-left{
        display:flex;
        align-items:center;
        gap:8px;
        font-size:.73rem;
        font-weight:820;
        letter-spacing:.04em;
    }

    .a-live-dot{
        width:7px;
        height:7px;
        border-radius:50%;
        background:var(--green);
        box-shadow:0 0 12px rgba(34,215,124,.65);
    }

    .a-top-time{
        color:var(--muted);
        font-size:.66rem;
    }

    .alpha-hero{
        border:1px solid var(--line);
        border-radius:17px;
        padding:16px 18px;
        margin-bottom:13px;
        background:
            linear-gradient(135deg,rgba(42,112,205,.11),rgba(255,255,255,.014)),
            var(--panel);
        box-shadow:0 12px 32px rgba(0,0,0,.15);
    }

    .alpha-hero-title{
        font-size:1.55rem;
        font-weight:900;
        letter-spacing:-.03em;
        line-height:1.05;
    }

    .alpha-hero-sub{
        margin-top:4px;
        color:var(--muted);
        font-size:.75rem;
    }

    .alpha-badge{
        display:inline-block;
        margin-top:9px;
        padding:4px 8px;
        border-radius:999px;
        font-size:.60rem;
        font-weight:850;
        letter-spacing:.06em;
        color:#8df1bc;
        border:1px solid rgba(34,215,124,.17);
        background:rgba(34,215,124,.07);
    }

    .alpha-kpi{
        border:1px solid var(--line);
        border-radius:12px;
        padding:11px 12px;
        background:linear-gradient(145deg,rgba(255,255,255,.032),rgba(255,255,255,.012));
    }

    .alpha-section{
        display:flex;
        align-items:center;
        gap:8px;
        margin:15px 0 8px;
        font-size:1rem;
        font-weight:880;
    }

    .alpha-section-dot{
        width:10px;
        height:10px;
        border-radius:50%;
    }

    .alpha-alert{
        border:1px solid var(--line);
        border-radius:10px;
        padding:8px 9px;
        margin-bottom:6px;
        background:rgba(255,255,255,.017);
    }

    .alpha-alert-time{
        font-size:.58rem;
        color:var(--muted);
    }

    .alpha-alert-symbol{
        margin-top:2px;
        font-size:.76rem;
        font-weight:850;
    }

    .alpha-alert-meta{
        margin-top:2px;
        font-size:.59rem;
        color:var(--muted);
    }

    div[data-testid="stDataFrame"]{
        border:1px solid var(--line);
        border-radius:13px;
        overflow:hidden;
        box-shadow:0 10px 24px rgba(0,0,0,.11);
    }

    div[data-testid="stMetric"]{
        border:1px solid var(--line);
        border-radius:12px;
        background:rgba(255,255,255,.02);
    }

    .stButton > button,
    .stDownloadButton > button{
        min-height:2.3rem;
        border-radius:9px;
        font-weight:800;
        border:1px solid rgba(255,255,255,.07);
    }

    .stButton > button{
        background:linear-gradient(135deg,#2f89ff,#4f67db);
        box-shadow:0 7px 18px rgba(47,137,255,.14);
    }

    .stDownloadButton > button{
        background:#101b2b;
    }

    div[data-baseweb="input"] > div,
    div[data-baseweb="select"] > div{
        border-radius:9px !important;
        background:rgba(255,255,255,.018) !important;
        border-color:var(--line) !important;
    }

    div[data-testid="stAlert"],
    div[data-testid="stExpander"]{
        border-radius:11px;
    }

    div[data-testid="stExpander"]{
        border:1px solid var(--line);
        background:rgba(255,255,255,.014);
    }

    hr{border-color:var(--line);}

    .market-dashboard-hero{display:flex;justify-content:space-between;align-items:center;border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:15px 18px;margin-bottom:13px;background:linear-gradient(135deg,rgba(40,105,194,.10),rgba(255,255,255,.018));}
    .market-dashboard-title{font-size:1.45rem;font-weight:950;letter-spacing:-.03em;}
    .market-dashboard-sub{font-size:.70rem;color:#f3c95c;font-weight:800;margin-top:3px;}
    .market-dashboard-live{text-align:right;color:#22d77c;font-size:.70rem;font-weight:900;line-height:1.6;}
    .market-dashboard-live span{color:#7f8ca0;font-weight:600;font-size:.60rem;}
    .market-dashboard-card{border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:12px;background:linear-gradient(145deg,rgba(255,255,255,.045),rgba(255,255,255,.012));min-height:96px;}
    .market-dashboard-card .name{font-size:.67rem;color:#7f8ca0;font-weight:850;}
    .market-dashboard-card .bias{font-size:1.08rem;font-weight:950;margin-top:7px;}
    .market-dashboard-card .sub{font-size:.61rem;color:#7f8ca0;margin-top:4px;}

    @media(max-width:900px){
        .a-top-time{display:none;}
        .alpha-hero-title{font-size:1.35rem;}
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    f"""
    <div class="a-topbar">
        <div class="a-top-left">
            <span class="a-live-dot"></span>
            <span>ALPHA ANALYZER</span>
            <span style="opacity:.30;">•</span>
            <span style="color:#7f8ca0;font-weight:650;">LIVE MARKET DASHBOARD</span>
        </div>
        <div class="a-top-time">
            {local_now().strftime("%d-%b-%Y %H:%M:%S IST")}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


def headers():
    if not st.session_state.client_id or not st.session_state.access_token:
        raise RuntimeError("Enter your login credentials.")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": st.session_state.access_token,
        "client-id": st.session_state.client_id,
    }

def api_post(path, payload, label):
    r = requests.post(API + path, headers=headers(), json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    st.session_state.api_log.append(
        {
            "time": local_now().strftime("%H:%M:%S IST"),
            "label": label,
            "endpoint": path,
            "status": r.status_code,
        }
    )
    if not r.ok:
        raise RuntimeError(
            f"{label}: HTTP {r.status_code}: "
            f"{body.get('remarks') or body.get('message') or str(body)[:400]}"
        )
    return body

def parse_data(body):
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}

# -----------------------------
# Instrument master
# -----------------------------
@st.cache_data(ttl=21600, show_spinner=False)
def load_master():
    # Detailed instrument master: direct underlying-security mapping where available.
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

    # Handle column variants.
    if "security_id" not in df.columns:
        for c in ["SM_SECURITY_ID", "SEM_SECURITY_ID"]:
            if c in df.columns:
                df["security_id"] = df[c]
                break

    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    if "underlying_security_id" in df.columns:
        df["underlying_security_id"] = pd.to_numeric(
            df["underlying_security_id"], errors="coerce"
        )

    for c in ["exchange", "segment", "instrument", "trading_symbol",
              "underlying_symbol", "symbol_name", "display_name"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.upper().str.strip()

    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")

    # Remove test symbols.
    bad = pd.Series(False, index=df.index)
    for c in ["trading_symbol", "underlying_symbol", "symbol_name", "display_name"]:
        if c in df.columns:
            bad |= df[c].str.contains("NSETEST", na=False)
    return df.loc[~bad].copy()

def future_universe(master, exchange="NSE"):
    """
    Return ONE nearest active futures contract per underlying.
    2nd/3rd expiries are never scanned.

    NSE:
      exchange=NSE, instrument=FUTSTK
    MCX:
      exchange=MCX, instrument=FUTCOM

    If expiry is unavailable, we keep one contract per underlying in
    deterministic instrument-master order rather than crashing.
    """
    if exchange == "NSE":
        x = master[
            (master["exchange"] == "NSE") &
            (master["instrument"] == "FUTSTK")
        ].copy()
    else:
        x = master[
            (master["exchange"] == "MCX") &
            (master["instrument"] == "FUTCOM")
        ].copy()

    if x.empty:
        return x

    # Ensure required fields.
    x = x.dropna(subset=["security_id"]).copy()

    if "expiry_date" in x.columns:
        x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")
    else:
        x["expiry_date"] = pd.NaT

    # Only remove contracts that are definitely expired.
    now = pd.Timestamp.now()
    if x["expiry_date"].notna().any():
        x = x[x["expiry_date"].isna() | (x["expiry_date"] >= now)].copy()

    # Build underlying symbol if missing.
    if "underlying_symbol" not in x.columns:
        if "trading_symbol" in x.columns:
            x["underlying_symbol"] = (
                x["trading_symbol"].astype(str)
                .str.split("-", n=1)
                .str[0]
                .str.upper()
                .str.strip()
            )
        else:
            return pd.DataFrame()

    x["underlying_symbol"] = (
        x["underlying_symbol"].astype(str).str.upper().str.strip()
    )
    x = x[~x["underlying_symbol"].isin(["", "NAN", "NONE"])].copy()

    # Sort by expiry first so the first row for each symbol is the nearest contract.
    # Stable fallback by security_id prevents random selection if expiry is missing.
    x = x.sort_values(
        ["underlying_symbol", "expiry_date", "security_id"],
        ascending=[True, True, True],
        na_position="last",
    )

    # ONLY the first/nearest active contract per underlying.
    x = x.drop_duplicates(subset=["underlying_symbol"], keep="first").reset_index(drop=True)

    return x

# -----------------------------
# Live LTP
# -----------------------------

@st.cache_data(ttl=5, show_spinner=False)
def batch_quote(segment, ids):
    body = api_post(
        "/marketfeed/quote",
        {segment: [int(x) for x in ids]},
        f"{segment} Quote",
    )
    data = parse_data(body).get(segment, {})
    out = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        price = pd.to_numeric(v.get("last_price"), errors="coerce")
        ltt = pd.to_numeric(v.get("last_trade_time"), errors="coerce")
        out[int(k)] = {
            "last_price": float(price) if pd.notna(price) else np.nan,
            "last_trade_time": float(ltt) if pd.notna(ltt) else np.nan,
        }
    return out


def exchange_time_from_ltt(ltt):
    """Convert Dhan Last Trade Time (epoch seconds) to IST."""
    if pd.isna(ltt):
        return None
    try:
        return datetime.fromtimestamp(float(ltt), tz=LOCAL_TZ).strftime(
            "%d-%b-%Y %H:%M:%S IST"
        )
    except Exception:
        return None


@st.cache_data(ttl=5, show_spinner=False)
def batch_ltp(segment, ids):
    body = api_post(
        "/marketfeed/ltp",
        {segment: [int(x) for x in ids]},
        f"{segment} LTP",
    )
    data = parse_data(body).get(segment, {})
    return {
        int(k): float(v.get("last_price"))
        for k, v in data.items()
        if isinstance(v, dict) and v.get("last_price") is not None
    }

# -----------------------------
# Historical cash data
# -----------------------------
def historical(sec_id, segment, instrument, mode):
    if mode == "Positional":
        payload = {
            "securityId": str(int(sec_id)),
            "exchangeSegment": segment,
            "instrument": instrument,
            "expiryCode": 0,
            "oi": False,
            "fromDate": str(datetime.now().date() - timedelta(days=220)),
            "toDate": str(datetime.now().date() + timedelta(days=1)),
        }

        # Dhan uses IDX_I + INDEX for indices. Some Data API deployments
        # also validate the daily timeframe explicitly.
        if segment == "IDX_I" and instrument == "INDEX":
            payload["timeframe"] = "1D"
        body = api_post("/charts/historical", payload, "daily history")
    else:
        payload = {
            "securityId": str(int(sec_id)),
            "exchangeSegment": segment,
            "instrument": instrument,
            "interval": "1",
            "oi": False,
            "fromDate": (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        body = api_post("/charts/intraday", payload, "1-minute history")

    data = parse_data(body)
    if not isinstance(data, dict) or "close" not in data:
        raise RuntimeError(f"No historical close data returned: {str(body)[:350]}")

    n = len(data["close"])
    out = pd.DataFrame({
        "close": data["close"],
        "timestamp": data.get("timestamp", [None] * n),
    })
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    unit = "ms" if pd.to_numeric(out["timestamp"], errors="coerce").dropna().median() > 10**12 else "s"
    out["datetime"] = pd.to_datetime(out["timestamp"], unit=unit, errors="coerce")
    out = out.dropna(subset=["close"]).sort_values("datetime").reset_index(drop=True)

    if mode == "Intraday":
        out = out[out["datetime"] < pd.Timestamp.now().floor("min")].copy()

    return out


@st.cache_data(ttl=180, show_spinner=False)
def cached_cash_intraday(sec_id, days=5):
    return historical(sec_id, "NSE_EQ", "EQUITY", "Intraday")

@st.cache_data(ttl=180, show_spinner=False)
def cached_cash_daily(sec_id):
    return historical(sec_id, "NSE_EQ", "EQUITY", "Positional")

# -----------------------------
# P&F engine
# -----------------------------
def build_pnf(closes, box_pct, reversal=3):
    p = pd.Series(closes).dropna().astype(float).tolist()
    if len(p) < 3:
        return []

    direction = None
    high = low = p[0]
    boxes = 0
    cols = []

    def save():
        if direction and boxes > 0:
            cols.append({"type": direction, "boxes": boxes, "high": high, "low": low})

    for price in p[1:]:
        if direction is None:
            if price >= high * (1 + box_pct):
                direction = "X"
                boxes = 0
                while price >= high * (1 + box_pct):
                    high *= (1 + box_pct)
                    boxes += 1
                low = high / ((1 + box_pct) ** boxes)
            elif price <= low * (1 - box_pct):
                direction = "O"
                boxes = 0
                while price <= low * (1 - box_pct):
                    low *= (1 - box_pct)
                    boxes += 1
                high = low / ((1 - box_pct) ** boxes)
            continue

        if direction == "X":
            while price >= high * (1 + box_pct):
                high *= (1 + box_pct)
                boxes += 1
            if price <= high * ((1 - box_pct) ** reversal):
                save()
                direction = "O"
                low = high
                boxes = 0
                while price <= low * (1 - box_pct):
                    low *= (1 - box_pct)
                    boxes += 1
        else:
            while price <= low * (1 - box_pct):
                low *= (1 - box_pct)
                boxes += 1
            if price >= low * ((1 + box_pct) ** reversal):
                save()
                direction = "X"
                high = low
                boxes = 0
                while price >= high * (1 + box_pct):
                    high *= (1 + box_pct)
                    boxes += 1

    save()
    return cols

def analyze_new_pattern(df, box_pct, anchor_min=15, pullback_max=5):
    base = {
        "bias": "NO DATA", "pattern": "—", "prospective": False,
        "dtb": False, "dbs": False, "anchor_boxes": 0,
        "pullback_boxes": 0, "entry_level": np.nan, "sl": np.nan,
        "signal": None, "reason": "No data"
    }
    if df.empty:
        return base
    cols = build_pnf(df["close"], box_pct)
    if len(cols) < 3:
        base["bias"] = "NO P&F"
        base["reason"] = f"Only {len(cols)} columns"
        return base

    c1, c2, c3 = cols[-3:]
    base["bias"] = "Bullish" if c3["type"] == "X" else "Bearish"

    if c1["type"] == "X" and c2["type"] == "O" and c3["type"] == "X":
        base["anchor_boxes"] = c1["boxes"]
        base["pullback_boxes"] = c2["boxes"]
        base["entry_level"] = c1["high"]
        base["anchor_valid"] = c1["boxes"] > anchor_min
        pull_valid = 1 <= c2["boxes"] <= pullback_max
        if base["anchor_valid"] and pull_valid:
            base["prospective"] = True
            if c3["high"] > c1["high"]:
                base["dtb"] = True
                base["signal"] = "LONG"
                base["pattern"] = "DTB"
                base["sl"] = c2["low"]
                base["reason"] = "3-column Anchor + 1–5 pullback + DTB"
            else:
                base["pattern"] = "NEW PATTERN"
                base["reason"] = "3-column bullish setup forming; waiting for DTB"
        else:
            base["reason"] = "3-column X-O-X exists but Anchor/pullback limits fail"

    elif c1["type"] == "O" and c2["type"] == "X" and c3["type"] == "O":
        base["anchor_boxes"] = c1["boxes"]
        base["pullback_boxes"] = c2["boxes"]
        base["entry_level"] = c1["low"]
        base["anchor_valid"] = c1["boxes"] > anchor_min
        pull_valid = 1 <= c2["boxes"] <= pullback_max
        if base["anchor_valid"] and pull_valid:
            base["prospective"] = True
            if c3["low"] < c1["low"]:
                base["dbs"] = True
                base["signal"] = "SHORT"
                base["pattern"] = "DBS"
                base["sl"] = c2["high"]
                base["reason"] = "3-column Anchor + 1–5 pullback + DBS"
            else:
                base["pattern"] = "NEW PATTERN"
                base["reason"] = "3-column bearish setup forming; waiting for DBS"
        else:
            base["reason"] = "3-column O-X-O exists but Anchor/pullback limits fail"
    else:
        base["reason"] = f"Latest columns {c1['type']}-{c2['type']}-{c3['type']}"

    return base


# -----------------------------
# Optional confirmation modules
# -----------------------------
SECTOR_MAP = {
    "HDFCBANK":"Banking","ICICIBANK":"Banking","SBIN":"Banking","AXISBANK":"Banking","KOTAKBANK":"Banking",
    "INDUSINDBK":"Banking","BANKBARODA":"Banking","PNB":"Banking","FEDERALBNK":"Banking","IDFCFIRSTB":"Banking",
    "BAJFINANCE":"Financials","BAJAJFINSV":"Financials","SHRIRAMFIN":"Financials","CHOLAFIN":"Financials","MUTHOOTFIN":"Financials",
    "RELIANCE":"Energy","ONGC":"Energy","COALINDIA":"Energy","IOC":"Energy","BPCL":"Energy","GAIL":"Energy",
    "TCS":"IT","INFY":"IT","HCLTECH":"IT","WIPRO":"IT","TECHM":"IT","LTIM":"IT","MPHASIS":"IT","COFORGE":"IT",
    "MARUTI":"Auto","M&M":"Auto","TATAMOTORS":"Auto","HEROMOTOCO":"Auto","EICHERMOT":"Auto","BAJAJ-AUTO":"Auto","TVSMOTOR":"Auto","ASHOKLEY":"Auto",
    "SUNPHARMA":"Pharma","CIPLA":"Pharma","DRREDDY":"Pharma","DIVISLAB":"Pharma","APOLLOHOSP":"Pharma","LUPIN":"Pharma","AUROPHARMA":"Pharma","TORNTPHARM":"Pharma",
    "TATASTEEL":"Metals","JSWSTEEL":"Metals","HINDALCO":"Metals","SAIL":"Metals","JINDALSTEL":"Metals","NATIONALUM":"Metals","VEDL":"Metals",
    "ITC":"FMCG","HINDUNILVR":"FMCG","NESTLEIND":"FMCG","BRITANNIA":"FMCG","TATACONSUM":"FMCG","DABUR":"FMCG","MARICO":"FMCG","COLPAL":"FMCG",
    "LT":"Capital Goods","BEL":"Capital Goods","BHEL":"Capital Goods","SIEMENS":"Capital Goods","ABB":"Capital Goods","CUMMINSIND":"Capital Goods",
    "DLF":"Realty","GODREJPROP":"Realty","OBEROIRLTY":"Realty","LODHA":"Realty","PRESTIGE":"Realty","PHOENIXLTD":"Realty",
    "BHARTIARTL":"Telecom","INDUSTOWER":"Telecom","IDEA":"Telecom",
    "TRENT":"Consumer","TITAN":"Consumer","DMART":"Consumer","KALYANKJIL":"Consumer","JUBLFOOD":"Consumer",
}
SECTOR_BASKETS = {}
for _s, _sec in SECTOR_MAP.items():
    SECTOR_BASKETS.setdefault(_sec, []).append(_s)

def sector_of(symbol):
    return SECTOR_MAP.get(str(symbol).upper(), "Other")

def future_quote_map(fut):
    """Current futures LTP/Quote for the exact active contract."""
    if fut.empty:
        return {}
    try:
        ids = fut["security_id"].dropna().astype(int).tolist()
        body = api_post("/marketfeed/quote", {"NSE_FNO": ids}, "NSE futures quote")
        data = parse_data(body).get("NSE_FNO", {})
        return {int(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        return {}

@st.cache_data(ttl=60, show_spinner=False)
def futures_oi_history(sec_id, lookback_days=7):
    """
    Fetch OI history for the SAME futures contract.
    This avoids comparing one expiry's OI to another expiry's OI.
    """
    end = datetime.now().date()
    start = end - timedelta(days=lookback_days)
    payload = {
        "securityId": str(int(sec_id)),
        "exchangeSegment": "NSE_FNO",
        "instrument": "FUTSTK",
        "expiryCode": 0,
        "oi": True,
        "fromDate": str(start),
        "toDate": str(end + timedelta(days=1)),
    }
    body = api_post("/charts/historical", payload, "same-contract futures OI")
    data = parse_data(body)

    if not isinstance(data, dict):
        return pd.DataFrame()

    oi = pd.to_numeric(pd.Series(data.get("open_interest", [])), errors="coerce")
    close = pd.to_numeric(pd.Series(data.get("close", [])), errors="coerce")
    ts = pd.to_numeric(pd.Series(data.get("timestamp", [])), errors="coerce")

    n = min(len(oi), len(close), len(ts))
    if n == 0:
        return pd.DataFrame()

    out = pd.DataFrame({
        "timestamp": ts.iloc[:n].to_numpy(),
        "close": close.iloc[:n].to_numpy(),
        "oi": oi.iloc[:n].to_numpy(),
    })

    unit = "ms" if out["timestamp"].dropna().median() > 10**12 else "s"
    out["datetime"] = pd.to_datetime(out["timestamp"], unit=unit, errors="coerce")
    out = out.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    return out

def classify_futures_oi(sec_id, direction):
    """
    Same active contract:
    current OI - previous OI = ΔOI
    current futures close - previous futures close = price direction

    Long:  price up + OI up
    Short: price down + OI up
    Short covering: price up + OI down
    Long unwinding: price down + OI down
    """
    try:
        h = futures_oi_history(sec_id)
        if len(h) < 2:
            return {
                "state": "UNAVAILABLE",
                "current_oi": np.nan,
                "previous_oi": np.nan,
                "oi_change": np.nan,
                "oi_change_pct": np.nan,
                "price_change_pct": np.nan,
                "star": False,
            }

        a = h.iloc[-2]
        b = h.iloc[-1]

        prev_oi = float(a["oi"]) if pd.notna(a["oi"]) else np.nan
        cur_oi = float(b["oi"]) if pd.notna(b["oi"]) else np.nan

        prev_price = float(a["close"]) if pd.notna(a["close"]) else np.nan
        cur_price = float(b["close"]) if pd.notna(b["close"]) else np.nan

        if any(pd.isna(x) for x in [prev_oi, cur_oi, prev_price, cur_price]):
            return {
                "state": "UNAVAILABLE",
                "current_oi": cur_oi,
                "previous_oi": prev_oi,
                "oi_change": np.nan,
                "oi_change_pct": np.nan,
                "price_change_pct": np.nan,
                "star": False,
            }

        oi_change = cur_oi - prev_oi
        oi_change_pct = (oi_change / prev_oi * 100.0) if prev_oi else np.nan
        price_change_pct = ((cur_price / prev_price) - 1.0) * 100.0 if prev_price else np.nan

        if price_change_pct > 0 and oi_change > 0:
            state = "LONG BUILDUP"
            star = direction == "LONG"
        elif price_change_pct < 0 and oi_change > 0:
            state = "SHORT BUILDUP"
            star = direction == "SHORT"
        elif price_change_pct > 0 and oi_change < 0:
            state = "SHORT COVERING"
            star = False
        elif price_change_pct < 0 and oi_change < 0:
            state = "LONG UNWINDING"
            star = False
        else:
            state = "NEUTRAL"
            star = False

        return {
            "state": state,
            "current_oi": cur_oi,
            "previous_oi": prev_oi,
            "oi_change": oi_change,
            "oi_change_pct": oi_change_pct,
            "price_change_pct": price_change_pct,
            "star": star,
        }
    except Exception:
        return {
            "state": "UNAVAILABLE",
            "current_oi": np.nan,
            "previous_oi": np.nan,
            "oi_change": np.nan,
            "oi_change_pct": np.nan,
            "price_change_pct": np.nan,
            "star": False,
        }


def sector_breadth_star(df, symbol, direction):
    """
    Sector confirmation from the already-scanned NSE P&F universe.

    Backend-only:
    - Uses the sector assigned to each scanned stock.
    - Uses only valid Bullish/Bearish directional rows.
    - >=50% same-direction breadth confirms the trade.
    """
    try:
        sec = sector_of(symbol)

        if sec == "Other" or df is None or df.empty:
            return {
                "sector": sec,
                "breadth": np.nan,
                "star": False,
            }

        x = df.copy()

        # The client-safe positional table keeps sector as "_Sector".
        # Older code may pass "Sector", so support both.
        sector_col = (
            "_Sector"
            if "_Sector" in x.columns
            else "Sector"
            if "Sector" in x.columns
            else None
        )

        if sector_col is None or "Bias" not in x.columns:
            return {
                "sector": sec,
                "breadth": np.nan,
                "star": False,
            }

        x[sector_col] = x[sector_col].astype(str).str.strip()
        x = x[x[sector_col] == sec].copy()

        valid = x[
            x["Bias"].isin(["Bullish", "Bearish"])
        ].copy()

        if valid.empty:
            return {
                "sector": sec,
                "breadth": np.nan,
                "star": False,
            }

        if direction == "LONG":
            pct = 100.0 * (
                valid["Bias"] == "Bullish"
            ).mean()
            return {
                "sector": sec,
                "breadth": pct,
                "star": pct >= 50.0,
            }

        if direction == "SHORT":
            pct = 100.0 * (
                valid["Bias"] == "Bearish"
            ).mean()
            return {
                "sector": sec,
                "breadth": pct,
                "star": pct >= 50.0,
            }

        return {
            "sector": sec,
            "breadth": np.nan,
            "star": False,
        }

    except Exception:
        return {
            "sector": sector_of(symbol),
            "breadth": np.nan,
            "star": False,
        }



@st.cache_data(ttl=900, show_spinner=False)

def intraday_sma10(df):
    """Calculate 10-period SMA on completed 1-minute cash closes."""
    if df is None or df.empty or "close" not in df.columns:
        return np.nan
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(c) < 10:
        return np.nan
    return float(c.rolling(10).mean().iloc[-1])

def daily_direction_filter(sec_id, anchor_min=15):
    """
    Higher-timeframe filter:
    0.25% box, 3-box reversal, daily cash closes.
    Returns Bullish / Bearish / Sideways.
    """
    try:
        h = cached_cash_daily(sec_id)
        if h.empty:
            return "UNAVAILABLE", "No daily data"
        p = analyze_new_pattern(h, 0.0025, anchor_min=anchor_min, pullback_max=5)
        if p["bias"] == "Bullish":
            return "Bullish", p["reason"]
        if p["bias"] == "Bearish":
            return "Bearish", p["reason"]
        return "Sideways", p["reason"]
    except Exception as e:
        return "UNAVAILABLE", str(e)[:200]



# -----------------------------
# Intraday daily-filter state
# -----------------------------
if "intraday_daily_filter" not in st.session_state:
    st.session_state.intraday_daily_filter = {}
if "intraday_filter_date" not in st.session_state:
    st.session_state.intraday_filter_date = None

def build_morning_daily_filter(fut):
    result = {}
    prog = st.progress(0, text=f"Building daily filter for {len(fut)} stocks...")
    for i, (_, r) in enumerate(fut.iterrows(), 1):
        symbol = str(r["underlying_symbol"])
        try:
            h = cached_cash_daily(int(r["underlying_security_id"]))
            cols = build_pnf(h["close"], 0.0025, 3)
            if cols:
                last = cols[-1]
                if last["type"] == "X" and last["boxes"] > 15:
                    bias, anchor_ok = "Bullish", True
                elif last["type"] == "O" and last["boxes"] > 15:
                    bias, anchor_ok = "Bearish", True
                else:
                    bias, anchor_ok = "Sideways", False
            else:
                bias, anchor_ok = "Unavailable", False
        except Exception:
            bias, anchor_ok = "Unavailable", False
        result[symbol] = {"bias": bias, "anchor_ok": anchor_ok}
        prog.progress(i / max(len(fut), 1), text=f"Daily filter {i}/{len(fut)}")
    prog.empty()
    return result

def get_morning_daily_filter(fut):
    today = datetime.now().date().isoformat()
    if (
        st.session_state.intraday_filter_date != today
        or not st.session_state.intraday_daily_filter
    ):
        st.session_state.intraday_daily_filter = build_morning_daily_filter(fut)
        st.session_state.intraday_filter_date = today
    return st.session_state.intraday_daily_filter




def daily_running_pnf_bias(sec_id):
    """
    Intraday eligibility gate:
    - Daily close-only data
    - 0.25% box
    - 3-box reversal
    - Latest completed daily column must itself be an Anchor >15 boxes.
      X >15 boxes = Bullish candidate.
      O >15 boxes = Bearish candidate.
    """
    try:
        h = cached_cash_daily(sec_id)
        if h.empty:
            return "UNAVAILABLE"

        cols = build_pnf(h["close"], 0.0025, 3)
        if not cols:
            return "UNAVAILABLE"

        last = cols[-1]

        if last["type"] == "X" and last["boxes"] > 15:
            return "Bullish"

        if last["type"] == "O" and last["boxes"] > 15:
            return "Bearish"

        return "UNAVAILABLE"
    except Exception:
        return "UNAVAILABLE"




def positional_active_pattern(sec_id):
    """
    STRICT positional classification.

    Bullish ONLY if:
      latest 3 completed daily columns are X-O-X
      AND the latest X breaks the prior X high -> ACTIVE DTB.

    Bearish ONLY if:
      latest 3 completed daily columns are O-X-O
      AND the latest O breaks the prior O low -> ACTIVE DBS.

    Everything else = SIDEWAYS / NO POSITION.

    Entry:
      DTB = prior X high
      DBS = prior O low

    SL:
      DTB = pullback O low
      DBS = pullback X high
    """
    result = {
        "bias": "Sideways",
        "recommendation": "NO POSITION",
        "entry": np.nan,
        "sl": np.nan,
        "active_dtb": False,
        "active_dbs": False,
        "pattern": "Sideways",
        "reason": "No active directional breakout",
    }

    try:
        h = cached_cash_daily(sec_id)
        if h.empty:
            result["reason"] = "No daily data"
            return result

        cols = build_pnf(h["close"], 0.0025, 3)

        if len(cols) < 3:
            result["reason"] = "No completed 3-column structure"
            return result

        c1, c2, c3 = cols[-3:]

        # Active bullish pattern: X -> O -> X and new X breaks
        # the high of the prior X column.
        if c1["type"] == "X" and c2["type"] == "O" and c3["type"] == "X":
            if c3["high"] > c1["high"]:
                result.update({
                    "bias": "Bullish",
                    "recommendation": "🟢 LONG",
                    "entry": float(c1["high"]),
                    "sl": float(c2["low"]),
                    "active_dtb": True,
                    "pattern": "DTB",
                    "reason": "Active DTB with latest column X",
                })
                return result

            result["reason"] = "X-O-X present, but Anchor high not broken"
            return result

        # Active bearish pattern: O -> X -> O and new O breaks
        # the low of the prior O column.
        if c1["type"] == "O" and c2["type"] == "X" and c3["type"] == "O":
            if c3["low"] < c1["low"]:
                result.update({
                    "bias": "Bearish",
                    "recommendation": "🔴 SHORT",
                    "entry": float(c1["low"]),
                    "sl": float(c2["high"]),
                    "active_dbs": True,
                    "pattern": "DBS",
                    "reason": "Active DBS with latest column O",
                })
                return result

            result["reason"] = "O-X-O present, but Anchor low not broken"
            return result

        result["reason"] = f"No active DTB/DBS in {c1['type']}-{c2['type']}-{c3['type']}"
        return result

    except Exception as exc:
        result["reason"] = f"Pattern data error: {str(exc)[:120]}"
        return result


# -----------------------------
# Option Seller Analyzer
# -----------------------------


def filter_atm_strike_window(chain_df, spot, strikes_each_side=20):
    """
    Restrict all option analysis to ATM +/- N strikes.

    ATM is selected from the live spot using the nearest available strike.
    The returned chain contains:
      - 20 strikes below ATM
      - ATM
      - 20 strikes above ATM
    wherever those strikes exist in the live chain.
    """
    if chain_df is None or chain_df.empty or pd.isna(spot):
        return chain_df.copy() if chain_df is not None else pd.DataFrame()

    x = chain_df.copy()
    x["Strike"] = pd.to_numeric(x["Strike"], errors="coerce")
    x = x.dropna(subset=["Strike"])

    strikes = sorted(x["Strike"].unique())
    if not strikes:
        return x

    atm = min(strikes, key=lambda s: abs(float(s) - float(spot)))
    atm_idx = strikes.index(atm)

    lo = max(0, atm_idx - strikes_each_side)
    hi = min(len(strikes), atm_idx + strikes_each_side + 1)

    allowed = set(strikes[lo:hi])
    return x[x["Strike"].isin(allowed)].copy()

# -----------------------------
# Dhan v2 Option Seller Analyzer
# -----------------------------
INDEX_NAMES = ["NIFTY", "BANKNIFTY", "SENSEX"]


def resolve_index_instrument(master, index_name):
    """
    Dhan-documented Index Security IDs:
      NIFTY    = 13
      BANKNIFTY = 25
      SENSEX   = 51
    Dhan uses IDX_I for Index Value.
    """
    ids = {
        "NIFTY": 13,
        "BANKNIFTY": 25,
        "SENSEX": 51,
    }

    key = str(index_name).upper().strip()
    if key not in ids:
        raise RuntimeError(f"Unsupported index: {index_name}")

    return ids[key]


@st.cache_data(ttl=60, show_spinner=False)
def option_expiry_list_v2(index_security_id):
    body = api_post(
        "/optionchain/expirylist",
        {
            "UnderlyingScrip": int(index_security_id),
            "UnderlyingSeg": "IDX_I",
        },
        "Option expiry list",
    )

    data = parse_data(body)
    expiries = data.get("data") if isinstance(data, dict) else None

    if not isinstance(expiries, list):
        raise RuntimeError("No active option expiries returned")

    out = []
    for value in expiries:
        try:
            out.append(pd.Timestamp(str(value)).date())
        except Exception:
            pass

    out = sorted(set(out))
    if not out:
        raise RuntimeError("No valid option expiries returned")

    return out


def select_option_expiry_v2(expiries, horizon):
    today = datetime.now().date()
    future = [d for d in expiries if d >= today]

    if not future:
        raise RuntimeError("No future expiry available")

    if horizon == "Intraday":
        return future[0]

    # Positional: prefer the final active expiry in the current month.
    current_month = [d for d in future if d.year == today.year and d.month == today.month]
    if current_month:
        return max(current_month)

    # Otherwise use the final expiry in the nearest available month.
    ym = sorted({(d.year, d.month) for d in future})
    y, m = ym[0]
    same_month = [d for d in future if (d.year, d.month) == (y, m)]
    return max(same_month)


@st.cache_data(ttl=3, show_spinner=False)
def option_chain_request_v2(index_security_id, expiry_date):
    return api_post(
        "/optionchain",
        {
            "UnderlyingScrip": int(index_security_id),
            "UnderlyingSeg": "IDX_I",
            "Expiry": expiry_date.strftime("%Y-%m-%d"),
        },
        "Option chain",
    )


def parse_option_chain_v2(body):
    data = parse_data(body)
    if not isinstance(data, dict) or not isinstance(data.get("oc"), dict):
        raise RuntimeError("Option chain strikes are unavailable")

    rows = []
    for strike_raw, pair in data["oc"].items():
        try:
            strike = float(strike_raw)
        except Exception:
            continue

        if not isinstance(pair, dict):
            continue

        for key, side in (("ce", "CE"), ("pe", "PE")):
            leg = pair.get(key)
            if not isinstance(leg, dict):
                continue

            oi = pd.to_numeric(leg.get("oi"), errors="coerce")
            prev_oi = pd.to_numeric(leg.get("previous_oi"), errors="coerce")
            change_oi = oi - prev_oi if pd.notna(oi) and pd.notna(prev_oi) else np.nan

            greeks = leg.get("greeks") or {}

            rows.append({
                "Strike": strike,
                "Side": side,
                "LTP": pd.to_numeric(leg.get("last_price"), errors="coerce"),
                "IV": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                "OI": oi,
                "Previous OI": prev_oi,
                "Change OI": change_oi,
                "Volume": pd.to_numeric(leg.get("volume"), errors="coerce"),
                "Delta": pd.to_numeric(greeks.get("delta"), errors="coerce"),
                "Security ID": pd.to_numeric(leg.get("security_id"), errors="coerce"),
                "Previous Close": pd.to_numeric(
                    leg.get("previous_close_price"), errors="coerce"
                ),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No option-chain rows returned")

    return df


def option_seller_analysis_v2(chain_df, spot, horizon):
    empty = {
        "recommendation": "WAIT",
        "reason": "Option data is currently unavailable.",
        "atm": np.nan,
        "ce_ltp": np.nan,
        "pe_ltp": np.nan,
        "atm_iv": np.nan,
        "expected_move": np.nan,
        "support": np.nan,
        "resistance": np.nan,
        "support_oi": np.nan,
        "resistance_oi": np.nan,
    }

    if chain_df.empty or pd.isna(spot):
        return empty

    x = chain_df.copy()
    x["Strike"] = pd.to_numeric(x["Strike"], errors="coerce")
    x["LTP"] = pd.to_numeric(x["LTP"], errors="coerce")
    x["IV"] = pd.to_numeric(x["IV"], errors="coerce")
    x["OI"] = pd.to_numeric(x["OI"], errors="coerce")
    x["Change OI"] = pd.to_numeric(x["Change OI"], errors="coerce")
    x = x.dropna(subset=["Strike"])

    strikes = sorted(x["Strike"].unique())
    if not strikes:
        return empty

    atm = min(strikes, key=lambda s: abs(float(s) - float(spot)))

    ce = x[(x["Side"] == "CE") & (x["Strike"] == atm)]
    pe = x[(x["Side"] == "PE") & (x["Strike"] == atm)]

    if ce.empty or pe.empty:
        empty["atm"] = atm
        empty["reason"] = "ATM call/put data is unavailable."
        return empty

    ce = ce.iloc[-1]
    pe = pe.iloc[-1]

    ce_ltp = ce["LTP"]
    pe_ltp = pe["LTP"]
    ce_iv = ce["IV"]
    pe_iv = pe["IV"]

    atm_iv = (
        np.nanmean([ce_iv, pe_iv])
        if not (pd.isna(ce_iv) and pd.isna(pe_iv))
        else np.nan
    )

    expected_move = (
        ce_ltp + pe_ltp
        if pd.notna(ce_ltp) and pd.notna(pe_ltp)
        else np.nan
    )

    calls = x[x["Side"] == "CE"]
    puts = x[x["Side"] == "PE"]

    resistance = np.nan
    support = np.nan
    resistance_oi = np.nan
    support_oi = np.nan

    if not calls.empty and calls["OI"].notna().any():
        idx = calls["OI"].idxmax()
        resistance = float(calls.loc[idx, "Strike"])
        resistance_oi = float(calls.loc[idx, "OI"])

    if not puts.empty and puts["OI"].notna().any():
        idx = puts["OI"].idxmax()
        support = float(puts.loc[idx, "Strike"])
        support_oi = float(puts.loc[idx, "OI"])

    range_ok = (
        pd.notna(expected_move)
        and pd.notna(support)
        and pd.notna(resistance)
        and float(spot) - expected_move >= support * 0.995
        and float(spot) + expected_move <= resistance * 1.005
    )

    if pd.notna(expected_move) and pd.notna(atm_iv) and (range_ok or horizon == "Positional"):
        recommendation = "🟢 SELL STRADDLE"
        reason = "Premium and current market range are supportive for premium selling."
    elif pd.notna(expected_move) and pd.notna(atm_iv):
        recommendation = "🟡 CAUTION"
        reason = "Premium is available, but the current range is not comfortably contained."
    else:
        recommendation = "🔴 DON'T SELL"
        reason = "Insufficient premium or volatility information."

    return {
        "recommendation": recommendation,
        "reason": reason,
        "atm": atm,
        "ce_ltp": ce_ltp,
        "pe_ltp": pe_ltp,
        "atm_iv": atm_iv,
        "expected_move": expected_move,
        "support": support,
        "resistance": resistance,
        "support_oi": support_oi,
        "resistance_oi": resistance_oi,
    }


def option_session_state(index_name, expiry_date, atm_iv):
    key = f"{index_name}|{expiry_date}"

    if "option_session" not in st.session_state:
        st.session_state.option_session = {}

    state = st.session_state.option_session.setdefault(
        key,
        {
            "open_iv": np.nan,
            "last_iv": np.nan,
            "iv_alert": False,
            "oi_alert": False,
        },
    )

    now = datetime.now().time()

    # The first successfully fetched IV after normal market open is recorded
    # as the opening baseline for this Streamlit session.
    if pd.notna(atm_iv) and now >= datetime.strptime("09:15", "%H:%M").time():
        if pd.isna(state["open_iv"]):
            state["open_iv"] = float(atm_iv)

    state["last_iv"] = float(atm_iv) if pd.notna(atm_iv) else state["last_iv"]
    return state


def option_oi_risk(chain_df, spot):
    if chain_df.empty or pd.isna(spot):
        return {
            "alert": False,
            "side": None,
            "text": "No OI risk alert.",
        }

    x = chain_df.copy()
    strikes = sorted(x["Strike"].dropna().unique())
    if len(strikes) < 2:
        return {
            "alert": False,
            "side": None,
            "text": "No OI risk alert.",
        }

    step = np.nanmedian(np.diff(strikes))
    if pd.isna(step) or step <= 0:
        return {
            "alert": False,
            "side": None,
            "text": "No OI risk alert.",
        }

    atm = min(strikes, key=lambda s: abs(float(s) - float(spot)))
    band = 5 * step

    y = x[
        x["Strike"].between(atm - band, atm + band)
        & (x["Change OI"] > 0)
    ]

    ce_add = y.loc[y["Side"] == "CE", "Change OI"].sum()
    pe_add = y.loc[y["Side"] == "PE", "Change OI"].sum()

    total = ce_add + pe_add
    if total <= 0:
        return {
            "alert": False,
            "side": None,
            "text": "No meaningful one-sided OI buildup.",
        }

    ce_share = ce_add / total
    pe_share = pe_add / total

    if ce_share >= 0.65:
        return {
            "alert": True,
            "side": "CALL",
            "text": "Heavy call-side positioning buildup detected.",
        }

    if pe_share >= 0.65:
        return {
            "alert": True,
            "side": "PUT",
            "text": "Heavy put-side positioning buildup detected.",
        }

    return {
        "alert": False,
        "side": None,
        "text": "No meaningful one-sided OI buildup.",
    }


def option_iv_risk(atm_iv, state):
    if pd.isna(atm_iv) or pd.isna(state.get("open_iv")):
        return {
            "alert": False,
            "text": "Opening volatility baseline is being recorded.",
        }

    increase = float(atm_iv) - float(state["open_iv"])

    if increase >= 2.5:
        return {
            "alert": True,
            "level": "HIGH",
            "text": "Implied volatility is expanding sharply.",
        }

    if increase >= 1.5:
        return {
            "alert": True,
            "level": "MEDIUM",
            "text": "Implied volatility is rising.",
        }

    return {
        "alert": False,
        "level": "NORMAL",
        "text": "Implied volatility is stable.",
    }


def speak_option_alert(message):
    safe = str(message).replace("\\", "\\\\").replace('"', '\\"')
    st.components.v1.html(
        f"""
        <script>
        try {{
            if ("speechSynthesis" in window) {{
                window.speechSynthesis.cancel();
                const u = new SpeechSynthesisUtterance("{safe}");
                u.rate = 0.95;
                u.volume = 1.0;
                window.speechSynthesis.speak(u);
            }}
        }} catch(e) {{}}
        </script>
        """,
        height=0,
    )



def option_strategy_suggestion(
    index_name,
    horizon,
    recommendation,
    spot,
    atm,
    expected_move,
    support,
    resistance,
    atm_iv,
    open_iv,
    oi_alert,
    chain_df,
):
    """Suggest strategy + exact option strike(s) from the live chain."""

    empty = {
        "strategy": "WAIT",
        "reason": "Live option data is incomplete.",
        "legs": "—",
        "ce_strike": np.nan,
        "pe_strike": np.nan,
        "ce_premium": np.nan,
        "pe_premium": np.nan,
    }

    if pd.isna(spot) or pd.isna(atm) or chain_df.empty:
        return empty

    iv_move = 0.0
    if pd.notna(atm_iv) and pd.notna(open_iv):
        iv_move = float(atm_iv) - float(open_iv)

    if iv_move >= 2.5:
        return {
            **empty,
            "strategy": "NO FRESH SELL",
            "reason": "Implied volatility is expanding sharply; wait for stabilization.",
        }

    x = chain_df.copy()
    x["Strike"] = pd.to_numeric(x["Strike"], errors="coerce")
    x["LTP"] = pd.to_numeric(x["LTP"], errors="coerce")
    x["OI"] = pd.to_numeric(x["OI"], errors="coerce")
    x = x.dropna(subset=["Strike"])

    strikes = sorted(x["Strike"].unique())
    if not strikes:
        return empty

    # Sell STRADDLE: current ATM CE + ATM PE.
    if recommendation == "🟢 SELL STRADDLE":
        ce = x[(x["Side"] == "CE") & (x["Strike"] == atm)]
        pe = x[(x["Side"] == "PE") & (x["Strike"] == atm)]

        ce_p = float(ce["LTP"].iloc[-1]) if not ce.empty and pd.notna(ce["LTP"].iloc[-1]) else np.nan
        pe_p = float(pe["LTP"].iloc[-1]) if not pe.empty and pd.notna(pe["LTP"].iloc[-1]) else np.nan

        return {
            "strategy": "SELL STRADDLE",
            "reason": "Premium and the implied range are supportive for an ATM premium sale.",
            "legs": f"SELL {index_name} {int(atm)} CE + {int(atm)} PE",
            "ce_strike": atm,
            "pe_strike": atm,
            "ce_premium": ce_p,
            "pe_premium": pe_p,
        }

    # Heavy call-side OI -> consider selling PUT at/below the strongest
    # nearby put-support strike.
    if oi_alert.get("alert") and oi_alert.get("side") == "CALL":
        puts = x[
            (x["Side"] == "PE")
            & (x["Strike"] <= float(support))
        ].copy()

        if not puts.empty:
            # Highest strike below/equal to support, so we stay just below support.
            strike = float(puts["Strike"].max())
            prem = float(
                puts.loc[puts["Strike"] == strike, "LTP"].iloc[-1]
            )
            return {
                "strategy": "SELL PUT",
                "reason": "Call-side positioning is concentrated while downside support remains intact.",
                "legs": f"SELL {index_name} {int(strike)} PE",
                "ce_strike": np.nan,
                "pe_strike": strike,
                "ce_premium": np.nan,
                "pe_premium": prem,
            }

    # Heavy put-side OI -> consider selling CALL at/above the strongest
    # nearby call-resistance strike.
    if oi_alert.get("alert") and oi_alert.get("side") == "PUT":
        calls = x[
            (x["Side"] == "CE")
            & (x["Strike"] >= float(resistance))
        ].copy()

        if not calls.empty:
            # Lowest strike above/equal to resistance, so we stay just above resistance.
            strike = float(calls["Strike"].min())
            prem = float(
                calls.loc[calls["Strike"] == strike, "LTP"].iloc[-1]
            )
            return {
                "strategy": "SELL CALL",
                "reason": "Put-side positioning is concentrated while upside resistance remains intact.",
                "legs": f"SELL {index_name} {int(strike)} CE",
                "ce_strike": strike,
                "pe_strike": np.nan,
                "ce_premium": prem,
                "pe_premium": np.nan,
            }

    if recommendation == "🟡 CAUTION":
        return {
            **empty,
            "strategy": "WAIT",
            "reason": "Premium exists, but the current range is not sufficiently contained.",
        }

    return {
        **empty,
        "strategy": "WAIT",
        "reason": "Current conditions do not justify a fresh premium-selling position.",
    }




def simple_option_decision(index_name, recommendation, spot, atm, expected_move,
                           support, resistance, atm_iv, open_iv, oi_alert):
    """Simple client-facing decision engine."""
    if pd.isna(spot) or pd.isna(atm):
        return "WAIT", "Live option data is incomplete."

    if pd.notna(atm_iv) and pd.notna(open_iv):
        iv_move = float(atm_iv) - float(open_iv)
        if iv_move >= 2.5:
            return "WAIT", "Volatility is rising sharply."

    if recommendation == "🟢 SELL STRADDLE":
        return "SELL STRADDLE", "IV and the expected range are supportive."

    if oi_alert.get("alert"):
        if oi_alert.get("side") == "CALL":
            return "SELL PUT", "Call-side OI buildup is strong and downside support is holding."
        if oi_alert.get("side") == "PUT":
            return "SELL CALL", "Put-side OI buildup is strong and upside resistance is holding."

    if pd.notna(expected_move) and pd.notna(support) and pd.notna(resistance):
        if float(spot) - float(expected_move) >= float(support) * 0.995 and \
           float(spot) + float(expected_move) <= float(resistance) * 1.005:
            return "SELL STRADDLE", "The expected range fits inside the current support/resistance zone."

    return "WAIT", "Current conditions are not strong enough for a fresh option sale."



def oi_confirmation_for_trade(sec_id, trade_direction):
    """
    Use nearest active NSE F&O futures price + OI behavior as confirmation.

    LONG:
      LONG BUILDUP     -> Strong Long
      NEUTRAL          -> Long
      SHORT COVERING   -> Weak Long
      SHORT BUILDUP    -> Conflict

    SHORT:
      SHORT BUILDUP    -> Strong Short
      NEUTRAL          -> Short
      LONG UNWINDING   -> Weak Short
      LONG BUILDUP     -> Conflict
    """
    if trade_direction not in ("LONG", "SHORT"):
        return {
            "label": "—",
            "state": "—",
            "oi_change_pct": np.nan,
            "price_change_pct": np.nan,
            "rank": 0,
        }

    x = classify_futures_oi(sec_id, trade_direction)
    state = x.get("state", "UNAVAILABLE")

    if state == "UNAVAILABLE":
        return {
            "label": "OI unavailable",
            "state": state,
            "oi_change_pct": x.get("oi_change_pct", np.nan),
            "price_change_pct": x.get("price_change_pct", np.nan),
            "rank": 0,
        }

    if trade_direction == "LONG":
        mapping = {
            "LONG BUILDUP": ("🟢 STRONG LONG", 3),
            "NEUTRAL": ("🟢 LONG", 2),
            "SHORT COVERING": ("🟡 WEAK LONG", 1),
            "SHORT BUILDUP": ("⚠️ LONG CONFLICT", 0),
            "LONG UNWINDING": ("⚠️ LONG WEAK", 1),
        }
    else:
        mapping = {
            "SHORT BUILDUP": ("🔴 STRONG SHORT", 3),
            "NEUTRAL": ("🔴 SHORT", 2),
            "LONG UNWINDING": ("🟡 WEAK SHORT", 1),
            "LONG BUILDUP": ("⚠️ SHORT CONFLICT", 0),
            "SHORT COVERING": ("⚠️ SHORT WEAK", 1),
        }

    label, rank = mapping.get(state, ("—", 0))

    return {
        "label": label,
        "state": state,
        "oi_change_pct": x.get("oi_change_pct", np.nan),
        "price_change_pct": x.get("price_change_pct", np.nan),
        "rank": rank,
    }



# -----------------------------
# MCX Futures Trading
# -----------------------------
MCX_FUTURE_SYMBOLS = [
    "GOLD",
    "SILVER",
    "COPPER",
    "CRUDEOIL",
    "NATURALGAS",
    "ZINC",
    "LEAD",
    "NICKEL",
    "ALUMINIUM",
]

def mcx_futures_universe(master):
    x=master.copy()
    if "exchange" in x.columns:
        x=x[x["exchange"].astype(str).str.upper().eq("MCX")].copy()
    if "instrument" in x.columns:
        x=x[x["instrument"].astype(str).str.upper().eq("FUTCOM")].copy()
    if x.empty: return x

    expiry_col=next((c for c in [
        "expiry_date","expiryDate","expiry","EXCH_EXPIRY_DATE","expiry_date_time"
    ] if c in x.columns),None)
    if expiry_col:
        x["_expiry"]=pd.to_datetime(x[expiry_col],errors="coerce")
        x=x[x["_expiry"].isna() | (x["_expiry"]>=pd.Timestamp.now())].copy()
    else:
        x["_expiry"]=pd.NaT

    symbol_col=next((c for c in [
        "underlying_symbol","underlyingSymbol","symbol_name","trading_symbol","tradingSymbol"
    ] if c in x.columns),None)
    if not symbol_col: return pd.DataFrame()

    raw=x[symbol_col].astype(str).str.upper().str.strip()
    def root(v):
        for r in MCX_FUTURE_SYMBOLS:
            if v==r or v.startswith(r+"-") or v.startswith(r+"_") or v.startswith(r+" "):
                return r
        return None

    x["underlying_symbol"]=raw.map(root)
    x=x[x["underlying_symbol"].notna()].copy()
    if x.empty: return x

    sort_cols=["underlying_symbol","_expiry"]
    if "security_id" in x.columns: sort_cols.append("security_id")
    x=x.sort_values(sort_cols,na_position="last")
    return x.drop_duplicates("underlying_symbol",keep="first").reset_index(drop=True)


def positional_active_pattern_mcx(sec_id):
    result={"bias":"Sideways","recommendation":"NO POSITION","entry":np.nan,"sl":np.nan}
    try:
        h=historical(sec_id,"MCX_COMM","FUTCOM","Positional")
        if h.empty: return result
        cols=build_pnf(h["close"],0.0025,3)
        if len(cols)<3: return result
        c1,c2,c3=cols[-3:]
        if c1["type"]=="X" and c2["type"]=="O" and c3["type"]=="X" and c3["high"]>c1["high"]:
            return {"bias":"Bullish","recommendation":"🟢 LONG","entry":float(c1["high"]),"sl":float(c2["low"])}
        if c1["type"]=="O" and c2["type"]=="X" and c3["type"]=="O" and c3["low"]<c1["low"]:
            return {"bias":"Bearish","recommendation":"🔴 SHORT","entry":float(c1["low"]),"sl":float(c2["high"])}
    except Exception:
        pass
    return result


@st.cache_data(ttl=60, show_spinner=False)
def futures_oi_history_mcx(sec_id, lookback_days=7):
    end=datetime.now().date()
    start=end-timedelta(days=lookback_days)
    body=api_post(
        "/charts/historical",
        {
            "securityId":str(int(sec_id)),
            "exchangeSegment":"MCX_COMM",
            "instrument":"FUTCOM",
            "expiryCode":0,
            "oi":True,
            "fromDate":str(start),
            "toDate":str(end+timedelta(days=1)),
        },
        "MCX futures OI",
    )
    data=parse_data(body)
    if not isinstance(data,dict): return pd.DataFrame()
    oi=pd.to_numeric(pd.Series(data.get("open_interest",[])),errors="coerce")
    close=pd.to_numeric(pd.Series(data.get("close",[])),errors="coerce")
    ts=pd.to_numeric(pd.Series(data.get("timestamp",[])),errors="coerce")
    n=min(len(oi),len(close),len(ts))
    if n<2: return pd.DataFrame()
    df=pd.DataFrame({"timestamp":ts.iloc[:n].to_numpy(),"close":close.iloc[:n].to_numpy(),"oi":oi.iloc[:n].to_numpy()})
    unit="ms" if df["timestamp"].dropna().median()>10**12 else "s"
    df["datetime"]=pd.to_datetime(df["timestamp"],unit=unit,errors="coerce")
    return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)


def mcx_oi_star(sec_id,direction):
    try:
        h=futures_oi_history_mcx(sec_id)
        if len(h)<2: return False
        a,b=h.iloc[-2],h.iloc[-1]
        oi_up=float(b["oi"])>float(a["oi"])
        price_up=float(b["close"])>float(a["close"])
        return (
            (direction=="LONG" and price_up and oi_up)
            or (direction=="SHORT" and (not price_up) and oi_up)
        )
    except Exception:
        return False


def mcx_intraday_daily_eligibility(sec_id):
    try:
        h=historical(sec_id,"MCX_COMM","FUTCOM","Positional")
        if h.empty: return None
        cols=build_pnf(h["close"],0.0025,3)
        if not cols: return None
        last=cols[-1]
        if last["boxes"]>15 and last["type"]=="X": return "Bullish"
        if last["boxes"]>15 and last["type"]=="O": return "Bearish"
    except Exception:
        pass
    return None



# -----------------------------
# Client Alerts
# -----------------------------
def speak_client_alert(message, prefix="New trade alert"):
    spoken = f"{prefix}. {message}"
    safe = spoken.replace("\\", "\\\\").replace('"', '\\"')

    st.components.v1.html(
        f"""
        <script>
        try {{
            if ("speechSynthesis" in window) {{
                window.speechSynthesis.cancel();
                const u = new SpeechSynthesisUtterance("{safe}");
                u.rate = 0.95;
                u.volume = 1.0;
                window.speechSynthesis.speak(u);
            }}
        }} catch(e) {{}}
        </script>
        """,
        height=0,
    )


def notify_new_trades(module_name, mode, current_symbols):
    """Popup + voice + persistent notification entry for new trades."""
    clean = {
        str(s).replace("★ ", "").strip()
        for s in current_symbols
        if str(s).strip()
    }

    state_key = f"{module_name}|{mode}"

    if "trade_alert_previous" not in st.session_state:
        st.session_state.trade_alert_previous = {}

    previous = set(
        st.session_state.trade_alert_previous.get(state_key, set())
    )

    new_trades = sorted(clean - previous)

    if new_trades:
        record_trade_notifications(
            module_name,
            mode,
            new_trades,
        )

    for symbol in new_trades:
        st.toast(
            f"🆕 New {module_name} {mode} trade: {symbol}",
            icon="🔔",
        )
        speak_client_alert(
            f"{module_name} {mode} trade in {symbol}",
            prefix="New trade alert",
        )

    st.session_state.trade_alert_previous[state_key] = clean


def notify_option_warning(module_name, warning_key, message, active):
    """Popup + warning voice only when a risk warning turns ON."""
    state_key = f"{module_name}|{warning_key}"

    if "option_warning_states" not in st.session_state:
        st.session_state.option_warning_states = {}

    previous = bool(
        st.session_state.option_warning_states.get(state_key, False)
    )
    current = bool(active)

    if current and not previous:
        st.toast(
            f"⚠️ {module_name}: {message}",
            icon="⚠️",
        )
        speak_client_alert(
            message,
            prefix=f"Warning. {module_name}",
        )

    st.session_state.option_warning_states[state_key] = current





# -----------------------------
# Fresh Trade Ledger / Persistent Trade History
# -----------------------------
FRESH_TRADE_DIR = Path(os.getenv("ALPHA_TRADE_DATA_DIR", "alpha_data"))
FRESH_TRADE_DIR.mkdir(parents=True, exist_ok=True)


def _fresh_trade_path(day=None):
    day = day or local_now().date()
    return FRESH_TRADE_DIR / f"fresh_trades_{day.isoformat()}.csv"


def _fresh_trade_columns():
    return [
        "Trade ID", "Date", "Entry Time", "Module", "Mode", "Symbol",
        "Direction", "Trade Price", "LTP", "Signal Entry", "Entry",
        "Initial SL", "SL", "Current", "Exit", "Status", "Exit Reason",
        "Points P&L", "P&L %", "Closed", "Duration (min)",
        "SL Trails", "Last SL Update", "First Logged",
    ]


def _dedupe_trade_rows(rows):
    """
    Keep exactly one Fresh Trade per SYMBOL per DAY.

    The user wants the ledger to represent the actual trade event, not every
    Streamlit refresh. If duplicates already exist, prefer:
      1) ACTIVE row over CLOSED row
      2) earliest immutable Entry Time
    """
    if not rows:
        return []

    df = pd.DataFrame(rows)

    for col in _fresh_trade_columns():
        if col not in df.columns:
            df[col] = np.nan

    def _clean_time(row):
        for key in ("Entry Time", "First Logged", "Opened"):
            value = row.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text and text.lower() not in {"nan", "none", "nat"}:
                return text
        return ""

    df["Entry Time"] = df.apply(_clean_time, axis=1)

    df["_date_key"] = df["Date"].astype(str).str.strip()
    df["_symbol_key"] = (
        df["Symbol"].astype(str).str.upper().str.strip()
    )

    # Prefer the active record. If several exist, keep the earliest real
    # exchange timestamp.
    df["_active_rank"] = (
        df["Status"].astype(str).str.upper().eq("ACTIVE").astype(int)
    )
    df["_entry_sort"] = pd.to_datetime(
        df["Entry Time"],
        format="%d-%b-%Y %H:%M:%S IST",
        errors="coerce",
    )

    df = df.sort_values(
        ["_date_key", "_symbol_key", "_active_rank", "_entry_sort"],
        ascending=[True, True, False, True],
        na_position="last",
    )

    df = df.drop_duplicates(
        subset=["_date_key", "_symbol_key"],
        keep="first",
    )

    return df[_fresh_trade_columns()].to_dict("records")



def _load_day_trade_rows(day):
    path = _fresh_trade_path(day)
    if not path.exists():
        return []

    try:
        df = pd.read_csv(path)

        for col in _fresh_trade_columns():
            if col not in df.columns:
                df[col] = np.nan

        rows = df[_fresh_trade_columns()].to_dict("records")

        # Remove impossible NSE entries created outside the actual market window.
        # This fixes historical bogus rows such as 16:40 for NSE Intraday.
        clean_rows = []
        for row in rows:
            module = str(row.get("Module", "")).upper().strip()
            mode = str(row.get("Mode", "")).upper().strip()
            if module == "NSE":
                raw_time = str(row.get("Entry Time", "")).strip()
                parsed_time = pd.to_datetime(
                    raw_time,
                    format="%d-%b-%Y %H:%M:%S IST",
                    errors="coerce",
                )
                if mode == "INTRADAY" and pd.notna(parsed_time):
                    t = parsed_time.time()
                    if not (
                        datetime.strptime("09:15", "%H:%M").time()
                        <= t
                        <= datetime.strptime("15:40", "%H:%M").time()
                    ):
                        continue
                # Positional fresh entries are also only created in the live window.
                if mode == "POSITIONAL" and pd.notna(parsed_time):
                    t = parsed_time.time()
                    if not (
                        datetime.strptime("09:15", "%H:%M").time()
                        <= t
                        <= datetime.strptime("15:40", "%H:%M").time()
                    ):
                        continue
            clean_rows.append(row)

        cleaned = _dedupe_trade_rows(clean_rows)

        # Persist cleanup so old duplicate/invalid entries disappear permanently.
        pd.DataFrame(
            cleaned,
            columns=_fresh_trade_columns(),
        ).to_csv(path, index=False)

        return cleaned

    except Exception:
        return []



def _load_fresh_trades_today():
    today = local_now().date()
    if st.session_state.get("fresh_trade_loaded_date") == today:
        return
    st.session_state.fresh_trade_log = _dedupe_trade_rows(
        _load_day_trade_rows(today)
    )
    st.session_state.fresh_trade_loaded_date = today
    try:
        pd.DataFrame(
            st.session_state.fresh_trade_log,
            columns=_fresh_trade_columns(),
        ).to_csv(_fresh_trade_path(today), index=False)
    except Exception:
        pass


def _save_fresh_trades_today():
    _load_fresh_trades_today()
    try:
        pd.DataFrame(
            st.session_state.fresh_trade_log,
            columns=_fresh_trade_columns(),
        ).to_csv(_fresh_trade_path(local_now().date()), index=False)
    except Exception:
        pass


def _canonicalize_ledger_df(df):
    """Normalize old ledgers and enforce one actual trade per symbol/day."""
    if df is None or df.empty:
        return pd.DataFrame(columns=_fresh_trade_columns())

    x = df.copy()

    for col in _fresh_trade_columns():
        if col not in x.columns:
            x[col] = np.nan

    # Recover a missing Entry Time from First Logged only when it represents
    # a valid market-hours timestamp. Never manufacture an after-hours
    # timestamp from a refresh.
    def valid_entry_time(row):
        raw_entry = str(row.get("Entry Time", "")).strip()
        raw_first = str(row.get("First Logged", "")).strip()

        for raw in (raw_entry, raw_first):
            if not raw or raw.lower() in {"nan", "none", "nat"}:
                continue
            parsed = pd.to_datetime(
                raw,
                format="%d-%b-%Y %H:%M:%S IST",
                errors="coerce",
            )
            if pd.isna(parsed):
                continue

            module = str(row.get("Module", "")).upper().strip()
            if module == "NSE":
                t = parsed.time()
                if not (
                    datetime.strptime("09:15", "%H:%M").time()
                    <= t
                    <= datetime.strptime("15:40", "%H:%M").time()
                ):
                    continue

            return raw

        return "TIME UNAVAILABLE"

    x["Entry Time"] = x.apply(valid_entry_time, axis=1)

    if "LTP" not in x.columns:
        x["LTP"] = x["Current"]
    else:
        x["LTP"] = pd.to_numeric(x["LTP"], errors="coerce")
        x["LTP"] = x["LTP"].fillna(pd.to_numeric(x["Current"], errors="coerce"))

    # Historical report rows from the broken versions may contain entries
    # generated after NSE close. Remove them completely.
    def is_valid_row(row):
        module = str(row.get("Module", "")).upper().strip()

        if module != "NSE":
            parsed = pd.to_datetime(
                str(row.get("Entry Time", "")),
                format="%d-%b-%Y %H:%M:%S IST",
                errors="coerce",
            )
            return bool(
                pd.notna(parsed)
                and pd.notna(
                    pd.to_numeric(
                        row.get("Initial SL"),
                        errors="coerce",
                    )
                )
            )

        parsed = pd.to_datetime(
            str(row.get("Entry Time", "")),
            format="%d-%b-%Y %H:%M:%S IST",
            errors="coerce",
        )
        if pd.isna(parsed):
            return False

        t = parsed.time()
        if not (
            datetime.strptime("09:15", "%H:%M").time()
            <= t
            <= datetime.strptime("15:40", "%H:%M").time()
        ):
            return False

        # A genuine NSE trade must have an initial SL.
        return pd.notna(
            pd.to_numeric(row.get("Initial SL"), errors="coerce")
        )

    x = x[x.apply(is_valid_row, axis=1)].copy()

    # One actual trade = one row per symbol/day.
    x["_date_key"] = x["Date"].astype(str).str.strip()
    x["_symbol_key"] = x["Symbol"].astype(str).str.upper().str.strip()

    x["_active_rank"] = (
        x["Status"].astype(str).str.upper().eq("ACTIVE").astype(int)
    )
    x["_time_sort"] = pd.to_datetime(
        x["Entry Time"],
        format="%d-%b-%Y %H:%M:%S IST",
        errors="coerce",
    )

    x = x.sort_values(
        ["_date_key", "_symbol_key", "_active_rank", "_time_sort"],
        ascending=[True, True, False, True],
        na_position="last",
    )

    # Prefer the earliest legitimate record for the one real trade.
    x = x.drop_duplicates(
        subset=["_date_key", "_symbol_key"],
        keep="first",
    )

    return x[_fresh_trade_columns()].reset_index(drop=True)


def _all_trade_ledger_dataframe():
    frames = []

    for path in sorted(FRESH_TRADE_DIR.glob("fresh_trades_*.csv")):
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=_fresh_trade_columns())

    out = pd.concat(frames, ignore_index=True)
    out = _canonicalize_ledger_df(out)

    # Persist canonical cleanup for each day file. This makes the downloaded
    # report clean from then on as well.
    if not out.empty and "Date" in out.columns:
        for day_value in sorted(out["Date"].astype(str).unique()):
            day_path = FRESH_TRADE_DIR / (
                f"fresh_trades_{pd.to_datetime(day_value, format='%d-%b-%Y', errors='coerce').strftime('%Y-%m-%d')}.csv"
            )
            if str(day_path).endswith("NaT.csv"):
                continue
            day_df = out[out["Date"].astype(str) == day_value].copy()
            try:
                day_df.to_csv(day_path, index=False)
            except Exception:
                pass

    return out.reset_index(drop=True)



def _update_trade_ledger_row(trade):
    trade_id = str(trade.get("Trade ID", "")).strip()
    if not trade_id:
        return

    for path in sorted(FRESH_TRADE_DIR.glob("fresh_trades_*.csv")):
        try:
            df = pd.read_csv(path)
        except Exception:
            continue

        if "Trade ID" not in df.columns:
            continue

        df["Trade ID"] = df["Trade ID"].astype(str)
        matches = df.index[df["Trade ID"] == trade_id].tolist()
        if not matches:
            continue

        idx = matches[0]
        for col in _fresh_trade_columns():
            if col in trade:
                df.loc[idx, col] = trade.get(col)

        for col in _fresh_trade_columns():
            if col not in df.columns:
                df[col] = np.nan

        df[_fresh_trade_columns()].to_csv(path, index=False)
        return


def _record_fresh_trade(trade, trade_price):
    _load_fresh_trades_today()

    symbol_key = str(trade.get("Symbol", "")).upper().strip()
    date_key = str(
        trade.get("Date", local_now().strftime("%d-%b-%Y"))
    ).strip()

    if not symbol_key:
        return

    # Idempotent symbol/day guard. This catches duplicate Trade IDs as well
    # as duplicate trades generated by repeated reruns.
    for existing in st.session_state.get("fresh_trade_log", []):
        if (
            str(existing.get("Date", "")).strip() == date_key
            and str(existing.get("Symbol", "")).upper().strip() == symbol_key
        ):
            return

    trade_id = str(trade.get("Trade ID", "")).strip()
    if not trade_id:
        return

    entry_time = str(
        trade.get("Entry Time")
        or ""
    ).strip()

    # Never replace a missing exchange timestamp with a dashboard refresh time.
    if not entry_time or entry_time.lower() in {"nan", "none", "nat"}:
        entry_time = "TIME UNAVAILABLE"

    row = {
        "Trade ID": trade_id,
        "Date": date_key,
        "Entry Time": entry_time,
        "Module": trade.get("Module", ""),
        "Mode": trade.get("Mode", ""),
        "Symbol": trade.get("Symbol", ""),
        "Direction": trade.get("Direction", ""),
        "Trade Price": float(trade.get("Entry", trade_price)) if pd.notna(trade.get("Entry", trade_price)) else np.nan,
        "LTP": float(trade.get("LTP", trade.get("Current", trade_price))) if pd.notna(trade.get("LTP", trade.get("Current", trade_price))) else np.nan,
        "Signal Entry": trade.get("Signal Entry", np.nan),
        "Entry": trade.get("Entry", np.nan),
        "Initial SL": trade.get("Initial SL", np.nan),
        "SL": trade.get("SL", np.nan),
        "Current": trade.get("Current", trade_price),
        "Exit": trade.get("Exit", np.nan),
        "Status": trade.get("Status", "ACTIVE"),
        "Exit Reason": trade.get("Exit Reason", ""),
        "Points P&L": trade.get("Points P&L", np.nan),
        "P&L %": trade.get("P&L %", np.nan),
        "Closed": trade.get("Closed", ""),
        "Duration (min)": trade.get("Duration (min)", np.nan),
        "SL Trails": trade.get("SL Trails", 0),
        "Last SL Update": trade.get("Last SL Update", ""),
        "First Logged": entry_time,
    }

    st.session_state.fresh_trade_log.insert(0, row)

    # Always normalize the ledger after insertion.
    st.session_state.fresh_trade_log = _dedupe_trade_rows(
        st.session_state.fresh_trade_log
    )
    _save_fresh_trades_today()



def _sync_fresh_trade_status():
    _load_fresh_trades_today()
    current = {
        str(t.get("Trade ID")): t
        for t in st.session_state.trade_book.values()
    }

    changed = False
    for row in st.session_state.fresh_trade_log:
        trade = current.get(str(row.get("Trade ID")))
        if trade is None:
            continue

        new_status = (
            "ACTIVE"
            if trade.get("Status", trade.get("status")) == "ACTIVE"
            else "CLOSED"
        )
        if row.get("Status") != new_status:
            row["Status"] = new_status
            changed = True

    if changed:
        _save_fresh_trades_today()


def fresh_trades_dataframe():
    _sync_fresh_trade_status()
    df = pd.DataFrame(
        st.session_state.fresh_trade_log,
        columns=_fresh_trade_columns(),
    )
    if df.empty:
        return df

    # Immutable Entry Time: never use the page-refresh time as a substitute.
    for col in ("Entry Time", "First Logged"):
        if col not in df.columns:
            df[col] = ""

    def _time_value(row):
        for key in ("Entry Time", "First Logged"):
            text = str(row.get(key, "")).strip()
            if text and text.lower() not in {"nan", "none", "nat"}:
                return text
        return "TIME UNAVAILABLE (LEGACY)"

    df["Entry Time"] = df.apply(_time_value, axis=1)

    # One ledger row per symbol per module/mode/day.
    df = pd.DataFrame(
        _dedupe_trade_rows(df.to_dict("records")),
        columns=_fresh_trade_columns(),
    )

    for col in [
        "Trade Price", "Signal Entry", "Entry", "Initial SL", "SL",
        "Current", "Exit", "Points P&L", "P&L %",
        "Duration (min)", "SL Trails",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.reset_index(drop=True)



def trade_report_dataframe(selected_date=None):
    df = _canonicalize_ledger_df(_all_trade_ledger_dataframe())
    if df.empty:
        return df

    parsed = pd.to_datetime(
        df["Date"],
        format="%d-%b-%Y",
        errors="coerce",
    )

    if selected_date is not None:
        df = df[parsed.dt.date == selected_date].copy()

    sort_time = pd.to_datetime(
        df["Entry Time"],
        format="%d-%b-%Y %H:%M:%S IST",
        errors="coerce",
    )
    if sort_time.notna().any():
        df = df.assign(_sort=sort_time)
        df = df.sort_values("_sort", ascending=False).drop(columns="_sort")

    return df.reset_index(drop=True)


def _available_trade_dates():
    df = _all_trade_ledger_dataframe()
    if df.empty:
        return []

    dates = pd.to_datetime(
        df["Date"],
        format="%d-%b-%Y",
        errors="coerce",
    ).dropna().dt.date
    return sorted(set(dates), reverse=True)


def _restore_active_trades_from_ledger():
    """Restore persistent active trades after a Streamlit session restart."""
    if st.session_state.get("_trade_book_restored"):
        return

    df = _all_trade_ledger_dataframe()
    if df.empty:
        st.session_state._trade_book_restored = True
        return

    today = local_now().date()

    for _, row in df.iterrows():
        if str(row.get("Status", "")) != "ACTIVE":
            continue

        module = str(row.get("Module", ""))
        mode = str(row.get("Mode", ""))

        parsed_date = pd.to_datetime(
            str(row.get("Date", "")),
            format="%d-%b-%Y",
            errors="coerce",
        )

        # Intraday trades should not leak into the next trading day.
        # Positional trades may remain overnight, but only valid live-market
        # entry timestamps are eligible to be restored.
        if (
            mode == "Intraday"
            and (
                pd.isna(parsed_date)
                or parsed_date.date() != today
            )
        ):
            continue

        symbol = str(row.get("Symbol", "")).strip()
        if not symbol:
            continue

        key = _trade_key(module, mode, symbol)
        if key in st.session_state.trade_book:
            continue

        entry_time = str(
            row.get("Entry Time")
            or row.get("First Logged")
            or row.get("Opened")
            or ""
        )
        trade = {
            col: row.get(col, np.nan)
            for col in [
                "Trade ID", "Date", "Module", "Mode", "Symbol", "Direction",
                "Signal Entry", "Entry", "Initial SL", "SL", "Current",
                "Exit", "Status", "Exit Reason", "Points P&L", "P&L %",
                "Closed", "Duration (min)", "SL Trails", "Last SL Update",
            ]
        }
        trade["Entry Time"] = entry_time
        trade["Opened"] = entry_time
        trade["LTP"] = pd.to_numeric(row.get("LTP"), errors="coerce")
        if pd.isna(trade["LTP"]):
            trade["LTP"] = pd.to_numeric(row.get("Current"), errors="coerce")

        st.session_state.trade_book[key] = trade

    st.session_state._trade_book_restored = True


def render_fresh_trades_module():
    df = fresh_trades_dataframe()

    st.markdown(
        '<div class="alpha-hero"><div class="alpha-hero-title">FRESH TRADES</div>'
        '<div class="alpha-hero-sub">New trades detected today • exact signal-detection time and actual entry price</div>'
        '<span class="alpha-badge">AUTO LOGGED</span></div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Date", local_now().strftime("%d-%b-%Y"))
    c2.metric("Fresh Trades", len(df))
    c3.metric(
        "Active",
        int((df["Status"] == "ACTIVE").sum()) if not df.empty else 0,
    )
    c4.metric(
        "Closed",
        int((df["Status"] == "CLOSED").sum()) if not df.empty else 0,
    )

    st.markdown(
        "<div class=\"alpha-section\"><span class=\"alpha-section-dot\" "
        "style=\"background:#22d77c;\"></span>TODAY'S TRADE LEDGER</div>",
        unsafe_allow_html=True,
    )

    if df.empty:
        st.info("No fresh trades detected today.")
        st.caption(
            "A row is created only when the live analyzer opens a new trade."
        )
        return

    display = df[[
        "Entry Time", "Symbol", "Mode", "Direction",
        "Trade Price", "LTP", "Initial SL", "SL", "Status",
    ]].copy()

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Trade Price": st.column_config.NumberColumn(
                "Entry Price", format="%.2f"
            ),
            "LTP": st.column_config.NumberColumn(
                "LTP", format="%.2f"
            ),
            "Initial SL": st.column_config.NumberColumn(
                "Initial SL", format="%.2f"
            ),
            "SL": st.column_config.NumberColumn(
                "Dynamic SL", format="%.2f"
            ),
            "Exit": st.column_config.NumberColumn(
                "Exit", format="%.2f"
            ),
        },
    )

    st.download_button(
        "⬇️ Download Today's Fresh Trades",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"alpha_fresh_trades_{local_now().strftime('%Y-%m-%d')}.csv",
        mime="text/csv",
        key="download_fresh_trades_today",
    )


def render_trade_logs_module():
    dates = _available_trade_dates()

    st.markdown(
        '<div class="alpha-hero"><div class="alpha-hero-title">TRADE LOGS</div>'
        '<div class="alpha-hero-sub">Day-wise setup performance and trade history</div>'
        '<span class="alpha-badge">HISTORICAL</span></div>',
        unsafe_allow_html=True,
    )

    if not dates:
        st.info("No persistent trade history is available yet.")
        return

    selected_date = st.date_input(
        "Select trading date",
        value=dates[0],
        min_value=min(dates),
        max_value=max(dates),
        key="trade_log_selected_date",
    )

    df = trade_report_dataframe(selected_date)
    closed = df[df["Status"] == "CLOSED"].copy()
    active = df[df["Status"] == "ACTIVE"].copy()

    pnl = pd.to_numeric(closed["Points P&L"], errors="coerce")
    wins = int((pnl > 0).sum()) if not closed.empty else 0
    losses = int((pnl < 0).sum()) if not closed.empty else 0
    net_points = float(pnl.sum()) if not closed.empty else 0.0
    win_rate = 100.0 * wins / len(closed) if not closed.empty else 0.0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Trades", len(df))
    c2.metric("Closed", len(closed))
    c3.metric("Active", len(active))
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Net Points", f"{net_points:.2f}")

    st.markdown("### Trade History")

    if df.empty:
        st.info("No trades were recorded on the selected date.")
        return

    # Exactly the client-facing fields requested.
    display = df[
        [
            "Entry Time",
            "Symbol",
            "Mode",
            "Direction",
            "Trade Price",
            "LTP",
            "Initial SL",
            "SL",
            "Status",
        ]
    ].copy()

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Trade Price": st.column_config.NumberColumn(
                "Trade Price", format="%.2f"
            ),
            "Initial SL": st.column_config.NumberColumn(
                "Initial SL", format="%.2f"
            ),
            "SL": st.column_config.NumberColumn(
                "Dynamic SL", format="%.2f"
            ),
        },
    )

    st.markdown("### Day Performance")
    perf = pd.DataFrame([{
        "Date": selected_date.strftime("%d-%b-%Y"),
        "Trades": len(df),
        "Closed": len(closed),
        "Active": len(active),
        "Wins": wins,
        "Losses": losses,
        "Win Rate %": win_rate,
        "Net Points": net_points,
        "Stop / Dynamic SL Exits": (
            int(
                closed["Exit Reason"].astype(str).isin(
                    ["Stop Loss", "Dynamic SL"]
                ).sum()
            )
            if not closed.empty else 0
        ),
        "Dynamic SL Trails": (
            int(pd.to_numeric(df["SL Trails"], errors="coerce").sum())
            if not df.empty else 0
        ),
    }])
    st.dataframe(perf, use_container_width=True, hide_index=True)

    # Detailed columns stay in the downloadable report only.
    st.download_button(
        "⬇️ Download Selected Day Report",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"alpha_trade_report_{selected_date.strftime('%Y-%m-%d')}.csv",
        mime="text/csv",
        key=f"trade_report_selected_{selected_date.isoformat()}",
    )




# -----------------------------
# Trade Book / Active Trade Manager
# -----------------------------
def _trade_key(module_name, mode, symbol):
    return f"{module_name}|{mode}|{symbol}"


def _trade_points_pnl(direction, entry, exit_price):
    if pd.isna(entry) or pd.isna(exit_price):
        return np.nan
    if direction == "LONG":
        return float(exit_price) - float(entry)
    return float(entry) - float(exit_price)


def _trade_pct_pnl(direction, entry, exit_price):
    if pd.isna(entry) or pd.isna(exit_price) or float(entry) == 0:
        return np.nan
    points = _trade_points_pnl(direction, entry, exit_price)
    return 100.0 * points / float(entry)


def _trade_duration_minutes(opened_at, closed_at=None):
    try:
        end = closed_at or datetime.now()
        return round((end - opened_at).total_seconds() / 60.0, 1)
    except Exception:
        return np.nan



def _trade_key(module_name, mode, symbol):
    return f"{module_name}|{mode}|{symbol}"


def _trade_points_pnl(direction, entry, exit_price):
    if pd.isna(entry) or pd.isna(exit_price):
        return np.nan
    return (
        float(exit_price) - float(entry)
        if direction == "LONG"
        else float(entry) - float(exit_price)
    )


def _trade_pct_pnl(direction, entry, exit_price):
    if pd.isna(entry) or pd.isna(exit_price) or float(entry) == 0:
        return np.nan
    return 100.0 * _trade_points_pnl(
        direction, entry, exit_price
    ) / abs(float(entry))


def _trade_duration_minutes(opened_at, closed_at=None):
    try:
        end = closed_at or local_now()
        return round((end - opened_at).total_seconds() / 60.0, 1)
    except Exception:
        return np.nan


def _valid_nse_market_timestamp(value):
    """Return True only for a real NSE market timestamp in the trade window."""
    text = _normalize_market_time(value)
    if not text:
        return False
    try:
        parsed = datetime.strptime(text, "%d-%b-%Y %H:%M:%S IST")
    except Exception:
        return False
    return (
        datetime.strptime("09:15", "%H:%M").time()
        <= parsed.time()
        <= datetime.strptime("15:40", "%H:%M").time()
    )


def _valid_trade_timestamp(module_name, value):
    if module_name == "NSE":
        return _valid_nse_market_timestamp(value)
    return bool(_normalize_market_time(value))


def _normalize_market_time(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    return text


def _upsert_trade_record(
    module_name,
    mode,
    symbol,
    direction,
    signal_entry,
    actual_entry,
    sl,
    market_time=None,
):
    key = _trade_key(module_name, mode, symbol)
    book = st.session_state.trade_book

    existing = book.get(key)
    if existing and existing.get("Status", existing.get("status")) == "ACTIVE":
        return existing, False

    # Hard validation: a Fresh Trade is created only when all required
    # live-market fields are present.
    market_time = _normalize_market_time(market_time)
    if not _valid_trade_timestamp(module_name, market_time):
        return None, False

    actual_entry = (
        float(actual_entry)
        if pd.notna(actual_entry)
        else np.nan
    )
    initial_sl = (
        float(sl)
        if pd.notna(sl)
        else np.nan
    )

    if not pd.notna(actual_entry):
        return None, False

    # We do not log a complete trade without an initial structural SL.
    if not pd.notna(initial_sl):
        return None, False

    opened = local_now()
    st.session_state.trade_sequence += 1

    trade_id = (
        f"T{opened.strftime('%Y%m%d%H%M%S')}-"
        f"{st.session_state.trade_sequence:03d}"
    )

    signal_entry = (
        float(signal_entry)
        if pd.notna(signal_entry)
        else np.nan
    )

    trade = {
        "Trade ID": trade_id,
        "Date": opened.strftime("%d-%b-%Y"),
        "Module": module_name,
        "Mode": mode,
        "Symbol": symbol,
        "Direction": direction,
        "Signal Entry": signal_entry,
        "Entry": actual_entry,
        "Initial SL": initial_sl,
        "SL": initial_sl,
        "Current": actual_entry,
        "LTP": actual_entry,
        "Exit": np.nan,
        "Status": "ACTIVE",
        "Exit Reason": "",
        "Points P&L": np.nan,
        "P&L %": np.nan,
        "Entry Time": market_time,
        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"),
        "Closed": "",
        "Duration (min)": np.nan,
        "SL Trails": 0,
        "Last SL Update": "",
    }

    book[key] = trade
    return trade, True



def _close_trade_record(trade, exit_price, reason):
    now = local_now()

    trade["Exit"] = (
        float(exit_price) if pd.notna(exit_price) else np.nan
    )
    trade["Current"] = trade["Exit"]
    trade["Status"] = "CLOSED"
    trade["Exit Reason"] = str(reason)

    trade["Points P&L"] = _trade_points_pnl(
        trade["Direction"],
        trade["Entry"],
        trade["Exit"],
    )
    trade["P&L %"] = _trade_pct_pnl(
        trade["Direction"],
        trade["Entry"],
        trade["Exit"],
    )
    trade["Closed"] = now.strftime("%d-%b-%Y %H:%M:%S IST")

    try:
        opened_text = str(trade.get("Entry Time", "")).replace(" IST", "")
        opened_dt = datetime.strptime(
            opened_text,
            "%d-%b-%Y %H:%M:%S",
        ).replace(tzinfo=LOCAL_TZ)
        trade["Duration (min)"] = _trade_duration_minutes(
            opened_dt,
            now,
        )
    except Exception:
        trade["Duration (min)"] = np.nan

    _update_trade_ledger_row(trade)


def _can_create_fresh_trade(module_name, mode):
    now_t = local_now().time()

    if module_name == "NSE":
        return (
            datetime.strptime("09:15", "%H:%M").time()
            <= now_t
            <= datetime.strptime("15:40", "%H:%M").time()
        )

    if module_name == "MCX":
        return (
            datetime.strptime("09:00", "%H:%M").time()
            <= now_t
            <= datetime.strptime("23:30", "%H:%M").time()
        )

    return True

def _trade_already_logged_today(module_name, mode, symbol):
    """
    One actual trade per symbol per trading day.

    This is intentionally independent of module/mode so the same underlying
    stock cannot be logged repeatedly as a fresh trade by repeated refreshes
    or by another dashboard module.
    """
    _load_fresh_trades_today()

    today = local_now().strftime("%d-%b-%Y")
    target_symbol = str(symbol).upper().strip()

    # Check persistent ledger first.
    for row in st.session_state.get("fresh_trade_log", []):
        row_date = str(row.get("Date", "")).strip()
        row_symbol = str(row.get("Symbol", "")).upper().strip()
        if row_date == today and row_symbol == target_symbol:
            return True

    # Also check the live trade book in case the current trade was just
    # created in this same run and has not yet been persisted.
    for trade in st.session_state.get("trade_book", {}).values():
        if (
            str(trade.get("Date", "")).strip() == today
            and str(trade.get("Symbol", "")).upper().strip() == target_symbol
        ):
            return True

    return False




def _sticky_active_rows_for_display(module_name, mode, res):
    """
    Return active logged trades that should remain visible even when the
    current signal scanner has become neutral.

    A logged trade is sticky until the trade manager records an exit.
    """
    today = local_now().strftime("%d-%b-%Y")
    existing = set()

    if res is not None and not res.empty and "Script" in res.columns:
        existing = {
            str(x).replace("★★ ", "").replace("★ ", "").strip().upper()
            for x in res["Script"].tolist()
        }

    rows = []

    for trade in st.session_state.get("trade_book", {}).values():
        if (
            str(trade.get("Module", "")).upper().strip() != str(module_name).upper().strip()
            or str(trade.get("Mode", "")).upper().strip() != str(mode).upper().strip()
            or str(trade.get("Status", "")).upper().strip() != "ACTIVE"
            or str(trade.get("Date", "")).strip() != today
        ):
            continue

        symbol = str(trade.get("Symbol", "")).upper().strip()
        if not symbol or symbol in existing:
            continue

        direction = str(trade.get("Direction", "")).upper().strip()
        rec = "🟢 BUY" if direction == "LONG" else "🔴 SELL"

        rows.append({
            "Script": symbol,
            "LTP": trade.get("LTP", trade.get("Current", np.nan)),
            "Bias": "ACTIVE TRADE",
            "Pattern": "LOGGED",
            "OI Confirmation": "ACTIVE TRADE",
            "OI Δ%": np.nan,
            "Entry": trade.get("Entry", np.nan),
            "SL": trade.get("SL", np.nan),
            "Recommendation": rec,
            "_OI Rank": 9999,
            "_Sector Confirmed": False,
            "_Sector": sector_of(symbol),
            "_Sector Breadth %": np.nan,
        })

    return pd.DataFrame(rows)


def _is_active_logged_trade(module_name, mode, symbol):
    today = local_now().strftime("%d-%b-%Y")
    target = str(symbol).upper().strip()
    try:
        df = _all_trade_ledger_dataframe()
        if df.empty:
            return False
        hit = df[
            (df["Date"].astype(str).str.strip() == today)
            & (df["Module"].astype(str).str.upper().str.strip() == str(module_name).upper().strip())
            & (df["Mode"].astype(str).str.upper().str.strip() == str(mode).upper().strip())
            & (df["Symbol"].astype(str).str.upper().str.strip() == target)
            & (df["Status"].astype(str).str.upper().str.strip() == "ACTIVE")
        ]
        return not hit.empty
    except Exception:
        return False


def manage_trade_book(module_name, mode, signal_rows):
    """
    Persistent NSE/MCX trade manager.

    Universal P&F structural dynamic SL:
      LONG  -> SL moves upward only.
      SHORT -> SL moves downward only.

    Entry Time and actual Entry Price are immutable after trade creation.
    """
    opened = []
    exited = []

    rows = (
        signal_rows.copy()
        if signal_rows is not None
        else pd.DataFrame()
    )

    for _, row in rows.iterrows():
        symbol = (
            str(row.get("Script", ""))
            .replace("★★ ", "")
            .replace("★ ", "")
            .strip()
        )
        if not symbol:
            continue

        rec = str(row.get("Recommendation", ""))
        ltp = pd.to_numeric(row.get("LTP"), errors="coerce")
        signal_entry = pd.to_numeric(
            row.get("Entry"),
            errors="coerce",
        )
        structural_sl = pd.to_numeric(
            row.get("SL"),
            errors="coerce",
        )

        if rec in ("🟢 BUY", "🟢 LONG"):
            direction = "LONG"
        elif rec in ("🔴 SELL", "🔴 SHORT"):
            direction = "SHORT"
        else:
            direction = None

        key = _trade_key(module_name, mode, symbol)
        active = st.session_state.trade_book.get(key)

        if active is None and _is_active_logged_trade(
            module_name, mode, symbol
        ):
            _restore_active_trades_from_ledger()
            active = st.session_state.trade_book.get(key)

        # ---------------------------
        # EXISTING ACTIVE TRADE
        # ---------------------------
        if active and active.get(
            "Status",
            active.get("status"),
        ) == "ACTIVE":

            if pd.notna(ltp):
                active["Current"] = float(ltp)
                active["LTP"] = float(ltp)
                _update_trade_ledger_row(active)

            trail_moved = False

            # Universal dynamic structural stop:
            # LONG: only a higher structural SL is accepted.
            # SHORT: only a lower structural SL is accepted.
            if (
                pd.notna(structural_sl)
                and pd.notna(active.get("SL"))
            ):
                old_sl = float(active["SL"])
                new_sl = float(structural_sl)

                should_trail = (
                    (
                        active["Direction"] == "LONG"
                        and new_sl > old_sl
                    )
                    or
                    (
                        active["Direction"] == "SHORT"
                        and new_sl < old_sl
                    )
                )

                if should_trail:
                    active["SL"] = new_sl
                    active["SL Trails"] = int(
                        active.get("SL Trails", 0)
                    ) + 1
                    active["Last SL Update"] = local_now().strftime(
                        "%d-%b-%Y %H:%M:%S IST"
                    )
                    _update_trade_ledger_row(active)
                    trail_moved = True

                    st.toast(
                        f"🔒 {symbol} dynamic SL moved to {new_sl:.2f}",
                        icon="🔒",
                    )
                    speak_client_alert(
                        f"{symbol} dynamic stop moved to {new_sl:.2f}",
                        prefix="Dynamic stop",
                    )

            exit_reason = None

            # Current dynamic SL.
            if pd.notna(ltp) and pd.notna(active.get("SL")):
                if (
                    active["Direction"] == "LONG"
                    and float(ltp) <= float(active["SL"])
                ):
                    exit_reason = (
                        "Dynamic SL"
                        if (
                            trail_moved
                            or int(active.get("SL Trails", 0)) > 0
                        )
                        else "Stop Loss"
                    )

                elif (
                    active["Direction"] == "SHORT"
                    and float(ltp) >= float(active["SL"])
                ):
                    exit_reason = (
                        "Dynamic SL"
                        if (
                            trail_moved
                            or int(active.get("SL Trails", 0)) > 0
                        )
                        else "Stop Loss"
                    )

            # Reverse signal.
            if (
                exit_reason is None
                and direction is not None
                and direction != active["Direction"]
            ):
                exit_reason = "Reverse Signal"

            # NSE intraday end-of-day.
            if (
                exit_reason is None
                and module_name == "NSE"
                and mode == "Intraday"
                and local_now().time()
                >= datetime.strptime(
                    "15:40", "%H:%M"
                ).time()
                and pd.notna(ltp)
            ):
                exit_reason = "End of Day"

            # MCX intraday end-of-day.
            if (
                exit_reason is None
                and module_name == "MCX"
                and mode == "Intraday"
                and local_now().time()
                >= datetime.strptime(
                    "23:30", "%H:%M"
                ).time()
                and pd.notna(ltp)
            ):
                exit_reason = "End of Day"

            if exit_reason:
                _close_trade_record(
                    active,
                    ltp,
                    exit_reason,
                )
                exited.append(active.copy())
            else:
                _update_trade_ledger_row(active)

            continue

        # ---------------------------
        # NEW TRADE
        # ---------------------------
        market_time = _normalize_market_time(row.get("Market Time"))

        if (
            direction is not None
            and pd.notna(ltp)
            and pd.notna(structural_sl)
            and _can_create_fresh_trade(module_name, mode)
            and _valid_trade_timestamp(module_name, market_time)
            and not _trade_already_logged_today(
                module_name,
                mode,
                symbol,
            )
        ):
            trade, created = _upsert_trade_record(
                module_name,
                mode,
                symbol,
                direction,
                signal_entry,
                ltp,
                structural_sl,
                market_time=market_time,
            )

            if created and trade is not None:
                _record_fresh_trade(
                    trade,
                    ltp,
                )
                opened.append(trade.copy())

    return opened, exited



def render_trade_monitor(module_name=None, mode=None):
    """Current-day active/completed monitor + report download."""
    df = trade_report_dataframe(local_now().date())

    if module_name is not None:
        df = df[df["Module"] == module_name]
    if mode is not None:
        df = df[df["Mode"] == mode]

    if df.empty:
        return

    active = df[df["Status"] == "ACTIVE"].copy()
    closed = df[df["Status"] == "CLOSED"].copy()

    st.markdown("---")
    st.markdown("### Trade Monitor")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Active", int(len(active)))
    c2.metric("Completed", int(len(closed)))

    if not closed.empty:
        pnl = pd.to_numeric(
            closed["Points P&L"],
            errors="coerce",
        )
        winners = int((pnl > 0).sum())
        win_rate = 100.0 * winners / len(closed)
        total_points = float(pnl.sum())
    else:
        win_rate = 0.0
        total_points = 0.0

    c3.metric("Win Rate", f"{win_rate:.1f}%")
    c4.metric("Net Points", f"{total_points:.2f}")

    if not active.empty:
        st.markdown("#### Active Trades")
        st.dataframe(
            active[
                [
                    "Symbol", "Direction", "Entry Time",
                    "Trade Price", "Signal Entry", "Entry",
                    "Initial SL", "SL", "Current", "Status",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

    if not closed.empty:
        st.markdown("#### Recent Closed Trades")
        st.dataframe(
            closed.head(20)[
                [
                    "Symbol", "Direction", "Entry Time",
                    "Entry", "Exit", "Points P&L",
                    "P&L %", "Exit Reason", "Closed", "SL Trails",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.download_button(
        "⬇️ Download Today's Trade Report",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=(
            f"alpha_trades_{local_now().strftime('%Y-%m-%d')}"
            f"_{module_name or 'ALL'}_{mode or 'ALL'}.csv"
        ),
        mime="text/csv",
        key=f"trade_report_{module_name}_{mode}",
    )


def notify_trade_exits(exited_trades):
    for trade in exited_trades:
        symbol = trade["Symbol"]
        reason = trade["Exit Reason"]
        st.toast(
            f"Trade exit: {symbol} • {reason}",
            icon="✅" if reason != "Stop Loss" else "⚠️",
        )
        speak_client_alert(
            f"{symbol} trade exited because {reason}",
            prefix="Trade exit",
        )


# -----------------------------
# Notification Panel
# -----------------------------
def record_trade_notifications(module_name, mode, symbols):
    """
    Store the latest new-trade timestamps for the client notification panel.
    Keeps a small rolling history per module/mode.
    """
    now_local = local_now()
    now_text = now_local.strftime("%d-%b-%Y %H:%M:%S IST")
    now_sort = now_local.isoformat()

    if "notification_history" not in st.session_state:
        st.session_state.notification_history = {}

    key = f"{module_name}|{mode}"
    history = st.session_state.notification_history.setdefault(key, [])

    known = {item["symbol"] for item in history}

    for symbol in symbols:
        clean = str(symbol).replace("★ ", "").strip()
        if not clean or clean in known:
            continue

        history.insert(
            0,
            {
                "time": now_text,
                "sort_time": now_sort,
                "module": module_name,
                "mode": mode,
                "symbol": clean,
            },
        )

    st.session_state.notification_history[key] = history[:10]


def render_notification_panel():
    """Top-right client notification panel."""
    history = []

    for items in st.session_state.get("notification_history", {}).values():
        history.extend(items)

    history = sorted(
        history,
        key=lambda x: x.get("sort_time", ""),
        reverse=True,
    )[:10]

    with st.sidebar:
        st.markdown("### 🔔 Recent Alerts")

        if not history:
            st.caption("No trades yet.")
            return

        for item in history[:5]:
            st.markdown(
                f"**{item['symbol']}**  \\n"
                f"{item['module']} • {item['mode']}  \\n"
                f"🕒 {item['time']}"
            )
            st.divider()



def positional_sector_confirmation(df, symbol, direction):
    """
    Backend-only sector confirmation for already-valid positional P&F trades.
    A sector confirms when at least 50% of its valid directional members agree.
    """
    try:
        result = sector_breadth_star(df, symbol, direction)
        return {
            "confirmed": bool(result.get("star", False)),
            "breadth": result.get("breadth", np.nan),
            "sector": result.get("sector", "Other"),
        }
    except Exception:
        return {
            "confirmed": False,
            "breadth": np.nan,
            "sector": "Other",
        }



# -----------------------------
# Manual Sector Analysis + RS Matrix
# -----------------------------
@st.cache_data(ttl=900, show_spinner=False)
def manual_daily_close(sec_id):
    return cached_cash_daily(int(sec_id))

@st.cache_data(ttl=900, show_spinner=False)
def manual_index_daily(sec_id):
    return historical(int(sec_id), "IDX_I", "INDEX", "Positional")

def pnf_direction_from_close(close_series, box_pct):
    s = pd.to_numeric(close_series, errors="coerce").dropna()
    if len(s) < 3:
        return "UNAVAILABLE"
    cols = build_pnf(s, box_pct, 3)
    if not cols:
        return "SIDEWAYS"
    return "BULLISH" if cols[-1]["type"] == "X" else "BEARISH"

def run_sector_analysis_manual(fut):
    """
    Sector Analysis:
    - NSE F&O universe
    - Daily timeframe
    - Close-only
    - 1% P&F box
    - 3-box reversal

    Returns a sector summary. The stock-level calculation is also retained
    in session state by the page so an empty sector summary can be diagnosed.
    """
    rows = []
    progress = st.progress(0, text="Building sector analysis...")
    total = len(fut)

    for i, (_, r) in enumerate(fut.iterrows(), 1):
        symbol = str(r.get("underlying_symbol", "")).strip()

        try:
            sec_id = int(r["underlying_security_id"])
        except Exception:
            progress.progress(i / max(total, 1), text=f"Sector: {symbol}")
            continue

        # Use the existing daily cash history loader. Do not rely on a
        # particular column name beyond close/datetime/date.
        try:
            h = manual_daily_close(sec_id)
        except Exception:
            h = pd.DataFrame()

        if h is None or h.empty or "close" not in h.columns:
            progress.progress(i / max(total, 1), text=f"Sector: {symbol}")
            continue

        close = pd.to_numeric(h["close"], errors="coerce")
        close = close.dropna()

        if len(close) < 10:
            progress.progress(i / max(total, 1), text=f"Sector: {symbol}")
            continue

        # Existing P&F builder is close-only. 1% box, 3-box reversal.
        try:
            bias = pnf_direction_from_close(close, 0.01)
        except Exception:
            bias = "UNAVAILABLE"

        if bias not in ("BULLISH", "BEARISH"):
            progress.progress(i / max(total, 1), text=f"Sector: {symbol}")
            continue

        # Always retain the stock. If the existing sector map has no entry,
        # put it into "Other" rather than dropping it silently.
        try:
            sector = str(sector_of(symbol)).strip()
        except Exception:
            sector = "Other"

        if not sector or sector.lower() in ("none", "nan"):
            sector = "Other"

        rows.append({
            "Sector": sector,
            "Stock": symbol,
            "Bias": bias,
        })

        progress.progress(i / max(total, 1), text=f"Sector: {symbol}")

    progress.empty()

    stock_df = pd.DataFrame(rows)

    if stock_df.empty:
        return pd.DataFrame(), stock_df

    result = []

    for sector, g in stock_df.groupby("Sector", dropna=False):
        stocks = len(g)
        bullish = int((g["Bias"] == "BULLISH").sum())
        bearish = int((g["Bias"] == "BEARISH").sum())

        bull_pct = 100.0 * bullish / stocks if stocks else 0.0
        bear_pct = 100.0 * bearish / stocks if stocks else 0.0

        if bull_pct >= 50:
            bias = "🟢 BULLISH"
        elif bear_pct >= 50:
            bias = "🔴 BEARISH"
        else:
            bias = "🟡 SIDEWAYS"

        result.append({
            "Sector": sector,
            "Bias": bias,
            "Bullish": bullish,
            "Bearish": bearish,
            "Stocks": stocks,
            "Bullish %": round(bull_pct, 1),
            "Bearish %": round(bear_pct, 1),
        })

    summary = pd.DataFrame(result).sort_values(
        ["Bullish %", "Bearish %", "Stocks"],
        ascending=[False, True, False],
    ).reset_index(drop=True)

    return summary, stock_df


def run_rs_matrix_manual(fut):
    """
    Relative Strength Matrix versus NIFTY 50.

    RS = Stock daily close / NIFTY 50 daily close.
    Apply P&F independently to the raw ratio at 3%, 2%, 1%, 0.25%.
    NIFTY 50 itself is not converted to P&F first.
    """
    nifty_id = resolve_nse_index_security_id(master, "NIFTY")
    if nifty_id is None:
        return pd.DataFrame()

    nifty = manual_index_daily(nifty_id)
    if nifty.empty or "close" not in nifty.columns:
        return pd.DataFrame()

    nifty = nifty[["datetime", "close"]].copy()
    nifty["date"] = pd.to_datetime(nifty["datetime"], errors="coerce").dt.date
    nifty["close"] = pd.to_numeric(nifty["close"], errors="coerce")
    nifty = nifty.dropna(subset=["date", "close"])
    nifty = nifty.groupby("date")["close"].last()

    rows = []
    progress = st.progress(0, text="Building RS Matrix vs NIFTY 50...")
    total = len(fut)

    for i, (_, r) in enumerate(fut.iterrows(), 1):
        symbol = str(r["underlying_symbol"])
        h = manual_daily_close(int(r["underlying_security_id"]))

        if not h.empty and "close" in h.columns:
            stock = h[["datetime", "close"]].copy()
            stock["date"] = pd.to_datetime(stock["datetime"], errors="coerce").dt.date
            stock["close"] = pd.to_numeric(stock["close"], errors="coerce")
            stock = stock.dropna(subset=["date", "close"])
            stock = stock.groupby("date")["close"].last()

            common = stock.index.intersection(nifty.index)
            if len(common) >= 3:
                ratio = (
                    stock.loc[common].astype(float)
                    / nifty.loc[common].astype(float)
                ).dropna()

                row = {"Stock": symbol}

                for label, box in [
                    ("3%", 0.03),
                    ("2%", 0.02),
                    ("1%", 0.01),
                    ("0.25%", 0.0025),
                ]:
                    d = pnf_direction_from_close(ratio, box)
                    row[label] = (
                        "🟢 OUTPERFORM"
                        if d == "BULLISH"
                        else "🔴 UNDERPERFORM"
                        if d == "BEARISH"
                        else "🟡 SIDEWAYS"
                        if d == "SIDEWAYS"
                        else "⚪ N/A"
                    )

                row["_score"] = sum(
                    1 if row[k] == "🟢 OUTPERFORM"
                    else -1 if row[k] == "🔴 UNDERPERFORM"
                    else 0
                    for k in ["3%", "2%", "1%", "0.25%"]
                )
                rows.append(row)

        progress.progress(
            i / max(total, 1),
            text=f"RS Matrix: {symbol}",
        )

    progress.empty()

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows).sort_values(
        ["_score", "Stock"],
        ascending=[False, True],
    ).reset_index(drop=True)



# -----------------------------
# Dhan Index Resolver
# -----------------------------
def resolve_nse_index_security_id(master, symbol="NIFTY"):
    """
    Resolve an NSE index security ID from Dhan's detailed instrument master.
    The master uses SEGMENT='I' for the IDX_I segment.
    """
    try:
        x = master.copy()

        if "exchange" in x.columns:
            x = x[x["exchange"].astype(str).str.upper().eq("NSE")]

        if "segment" in x.columns:
            x = x[x["segment"].astype(str).str.upper().eq("I")]

        if "instrument" in x.columns:
            x = x[x["instrument"].astype(str).str.upper().eq("INDEX")]

        target = str(symbol).upper().strip()

        for col in ["underlying_symbol", "symbol_name", "display_name", "trading_symbol"]:
            if col not in x.columns:
                continue

            m = x[x[col].astype(str).str.upper().str.strip().eq(target)]
            if not m.empty:
                sid = pd.to_numeric(
                    m.iloc[0]["security_id"],
                    errors="coerce",
                )
                if pd.notna(sid):
                    return int(sid)

        return None

    except Exception:
        return None



@st.cache_data(ttl=900, show_spinner=False)
def sector_confirmation_1pct_map(fut):
    """
    Positional sector confirmation must come from the standalone Sector
    Analysis logic: daily close-only, 1% box, 3-box reversal.

    Returns:
        {
            sector: {
                bullish_pct: ...,
                bearish_pct: ...,
                bias: "BULLISH"/"BEARISH"/"SIDEWAYS",
                stocks: ...,
            }
        }
    """
    counts = {}

    for _, r in fut.iterrows():
        symbol = str(r.get("underlying_symbol", "")).strip()
        sector = sector_of(symbol)

        if sector not in counts:
            counts[sector] = {
                "bullish": 0,
                "bearish": 0,
                "stocks": 0,
            }

        try:
            sid = int(r["underlying_security_id"])
            h = manual_daily_close(sid)

            if h is None or h.empty or "close" not in h.columns:
                continue

            close = pd.to_numeric(
                h["close"],
                errors="coerce",
            ).dropna()

            if len(close) < 10:
                continue

            direction = pnf_direction_from_close(
                close,
                0.01,
            )

            if direction not in ("BULLISH", "BEARISH"):
                continue

            counts[sector]["stocks"] += 1

            if direction == "BULLISH":
                counts[sector]["bullish"] += 1
            else:
                counts[sector]["bearish"] += 1

        except Exception:
            continue

    result = {}

    for sector, c in counts.items():
        n = c["stocks"]

        if n <= 0:
            result[sector] = {
                "bullish_pct": np.nan,
                "bearish_pct": np.nan,
                "bias": "UNAVAILABLE",
                "stocks": 0,
            }
            continue

        bull_pct = 100.0 * c["bullish"] / n
        bear_pct = 100.0 * c["bearish"] / n

        if bull_pct >= 50.0:
            bias = "BULLISH"
        elif bear_pct >= 50.0:
            bias = "BEARISH"
        else:
            bias = "SIDEWAYS"

        result[sector] = {
            "bullish_pct": bull_pct,
            "bearish_pct": bear_pct,
            "bias": bias,
            "stocks": n,
        }

    return result



# -----------------------------
# PCR Positioning Trend
# -----------------------------
def option_pcr_snapshot(chain_df, spot, strikes_each_side=20):
    if chain_df is None or chain_df.empty or pd.isna(spot):
        return np.nan
    x=chain_df.copy()
    x["Strike"]=pd.to_numeric(x["Strike"],errors="coerce")
    x["OI"]=pd.to_numeric(x["OI"],errors="coerce")
    x=x.dropna(subset=["Strike","OI"])
    if x.empty:
        return np.nan
    strikes=sorted(x["Strike"].unique())
    if not strikes:
        return np.nan
    atm=min(strikes,key=lambda s: abs(float(s)-float(spot)))
    i=strikes.index(atm)
    x=x[x["Strike"].isin(set(strikes[max(0,i-strikes_each_side):min(len(strikes),i+strikes_each_side+1)]))]
    puts=float(x.loc[x["Side"]=="PE","OI"].sum())
    calls=float(x.loc[x["Side"]=="CE","OI"].sum())
    return puts/calls if calls>0 else np.nan

def pcr_trend_columns(values):
    vals=pd.Series(values).dropna()
    return build_pnf(vals,0.05,3) if len(vals)>=3 else []

def render_pcr_positioning_chart(values):
    vals=[float(v) for v in values if pd.notna(v)]
    if not vals:
        st.info("Positioning trend will appear after option data refreshes.")
        return
    cols=pcr_trend_columns(vals)
    if not cols:
        st.info(f"Positioning trend is building • Current reading: {vals[-1]:.2f}")
        return
    recent=cols[-12:]
    height=max(150,min(290,90+max(4,max(int(c["boxes"]) for c in recent))*11))
    parts=[]
    for c in recent:
        glyph="▮" if c["type"]=="X" else "▯"
        parts.append(
            "<div style='display:flex;flex-direction:column;justify-content:flex-end;"
            "align-items:center;min-width:20px;height:100%;gap:2px;'>"
            + "".join(
                f"<span style='font-size:13px;line-height:11px'>{glyph}</span>"
                for _ in range(max(1,int(c["boxes"])))
            )
            + "</div>"
        )
    html=f"""<div style="border:1px solid rgba(255,255,255,.10);border-radius:16px;padding:16px;
    background:linear-gradient(145deg,rgba(255,255,255,.055),rgba(255,255,255,.018));
    box-shadow:0 8px 24px rgba(0,0,0,.16);">
    <div style="font-size:1.05rem;font-weight:800;margin-bottom:4px;">Positioning Trend</div>
    <div style="font-size:.76rem;opacity:.55;margin-bottom:12px;">Current reading: {vals[-1]:.2f}</div>
    <div style="height:{height-55}px;display:flex;align-items:flex-end;gap:12px;overflow:hidden;
    border-bottom:1px solid rgba(255,255,255,.10);padding:8px 4px;">{''.join(parts)}</div></div>"""
    st.components.v1.html(html,height=height,scrolling=False)


# -----------------------------
# Main
# -----------------------------
if not st.session_state.client_id or not st.session_state.access_token:
    st.warning("Enter Client Code and Access Token.")
    st.stop()

try:
    master = load_master()
except Exception as e:
    st.error(f"Instrument master failed: {e}")
    st.stop()

_load_fresh_trades_today()
render_notification_panel()

# The report button is rendered only after trade-report helpers are defined.
if st.session_state.get("trade_book"):
    report_all = trade_report_dataframe(local_now().date())
    with st.sidebar:
        st.markdown(
            '<div class="a-side-head">Reports</div>',
            unsafe_allow_html=True,
        )
        st.download_button(
            "⬇️ Download Daily Report",
            data=report_all.to_csv(index=False).encode("utf-8"),
            file_name=f"alpha_trades_{local_now().strftime('%Y-%m-%d')}.csv",
            mime="text/csv",
            key="sidebar_daily_report",
        )


# -----------------------------
# Global Option Seller Warning Monitor
# -----------------------------
def _global_option_warning_snapshot():
    """
    Lightweight option-risk monitor for the three supported NSE indices.
    It is intentionally independent of the selected page/module.
    """
    out = []

    for index_name in INDEX_NAMES:
        try:
            index_sid = resolve_index_instrument(master, index_name)
            expiries = option_expiry_list_v2(index_sid)

            if not expiries:
                continue

            selected_expiry = select_option_expiry_v2(expiries, "Intraday")
            raw_chain = option_chain_request_v2(index_sid, selected_expiry)
            raw_data = parse_data(raw_chain)

            spot = (
                pd.to_numeric(
                    raw_data.get("last_price"),
                    errors="coerce",
                )
                if isinstance(raw_data, dict)
                else np.nan
            )

            chain_df = parse_option_chain_v2(raw_chain)
            chain_df = filter_atm_strike_window(
                chain_df,
                spot,
                strikes_each_side=20,
            )

            analysis = option_seller_analysis_v2(
                chain_df,
                spot,
                "Intraday",
            )

            session_state = option_session_state(
                index_name,
                selected_expiry,
                analysis["atm_iv"],
            )

            iv_risk = option_iv_risk(
                analysis["atm_iv"],
                session_state,
            )
            oi_risk = option_oi_risk(
                chain_df,
                spot,
            )

            if bool(iv_risk.get("alert")):
                out.append({
                    "index": index_name,
                    "key": "iv_expansion",
                    "message": (
                        f"{index_name} IV is expanding sharply."
                        if iv_risk.get("level") == "HIGH"
                        else f"{index_name} IV is rising."
                    ),
                    "severity": "HIGH"
                    if iv_risk.get("level") == "HIGH"
                    else "MEDIUM",
                })

            if bool(oi_risk.get("alert")):
                side_word = (
                    "call"
                    if oi_risk.get("side") == "CALL"
                    else "put"
                )
                out.append({
                    "index": index_name,
                    "key": "oi_buildup",
                    "message": (
                        f"{index_name} heavy {side_word} side "
                        "positioning buildup."
                    ),
                    "severity": "HIGH",
                })

        except Exception:
            # Global monitor must never interrupt the client's selected module.
            continue

    return out


def render_global_option_warning_monitor():
    """
    Runs independently from the selected module using Streamlit fragments.
    This keeps option warnings alive even while the user is on NSE/MCX/RS/etc.
    """
    try:
        fragment = getattr(st, "fragment", None)
        if fragment is None:
            return

        @fragment(run_every="60s")
        def _warning_fragment():
            warnings = _global_option_warning_snapshot()

            active_keys = {
                f"{w['index']}|{w['key']}"
                for w in warnings
            }

            if "global_option_warning_states" not in st.session_state:
                st.session_state.global_option_warning_states = {}

            previous_states = st.session_state.global_option_warning_states

            for warning in warnings:
                state_key = (
                    f"{warning['index']}|{warning['key']}"
                )
                was_active = bool(
                    previous_states.get(state_key, False)
                )

                if not was_active:
                    st.toast(
                        f"⚠️ Option warning: {warning['message']}",
                        icon="⚠️",
                    )
                    speak_option_alert(
                        f"Warning. {warning['message']}"
                    )

                previous_states[state_key] = True

            # Clear warnings that are no longer active.
            for key in list(previous_states):
                if key not in active_keys:
                    previous_states[key] = False

            with st.sidebar:
                st.markdown(
                    '<div class="a-side-head">Option Seller Risk</div>',
                    unsafe_allow_html=True,
                )

                if not warnings:
                    st.markdown(
                        """
                        <div class="alpha-alert">
                            <div class="alpha-alert-symbol">✓ Option risk normal</div>
                            <div class="alpha-alert-meta">
                                Monitoring continues in the background
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                else:
                    for warning in warnings:
                        color = "#ff5364"
                        st.markdown(
                            f"""
                            <div class="alpha-alert"
                                 style="border-color:rgba(255,83,100,.30);
                                        background:rgba(255,83,100,.06);">
                                <div class="alpha-alert-time">
                                    ⚠ {warning["severity"]}
                                </div>
                                <div class="alpha-alert-symbol">
                                    {warning["index"]} • OPTION WARNING
                                </div>
                                <div class="alpha-alert-meta">
                                    {warning["message"]}
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

        _warning_fragment()

    except Exception:
        # Never break the selected page because the background warning monitor
        # is unavailable.
        pass


render_global_option_warning_monitor()


# Auto refresh is deliberately longer than the initial full-universe scan.
# The full NSE scan can take longer than 1 minute, so a 1-minute rerun would
# interrupt the scan and start it again from stock 1.
if auto:
    try:
        from streamlit_autorefresh import st_autorefresh
        if page in ("Intraday", "Fresh Trades", "Trade Logs"):
            mins = 1
        elif page == "Option Seller":
            mins = 1 if st.session_state.get("option_horizon", "Intraday") == "Intraday" else 3
        elif page == "MCX Futures":
            mins = 1 if st.session_state.get("mcx_futures_mode", "Intraday") == "Intraday" else 15
        elif page in ("Sector Analysis", "RS Matrix"):
            mins = None
        else:
            mins = 15
        if page == "Market Overview":
            mins = 3
        if mins is not None:
            st_autorefresh(interval=mins * 60 * 1000, key=f"refresh_{page}")
        if page == "Intraday":
            st.caption("Live intraday monitor — updates every minute.")
        elif page == "Option Seller":
            st.caption(
                "Live option monitor — "
                + ("updates every minute." if mins == 1 else "updates every 3 minutes.")
            )
    except Exception:
        pass

if not st.session_state.get("_trade_book_restored"):
    _restore_active_trades_from_ledger()

if page == "Fresh Trades":
    render_fresh_trades_module()

elif page == "Trade Logs":
    render_trade_logs_module()

elif page == "Market Overview":
    st.markdown(
        f"""
        <div class="market-dashboard-hero">
          <div>
            <div class="market-dashboard-title">ALPHA ANALYZER</div>
            <div class="market-dashboard-sub">Smart View • Clear Signals • Better Trades</div>
          </div>
          <div class="market-dashboard-live">● LIVE MARKET<br><span>{local_now().strftime('%d-%b-%Y %H:%M:%S IST')}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption("Market Pulse • P&F Direction • Fresh Trade Flow")

    def market_bias_from_history(sec_id, segment, instrument):
        try:
            h=historical(sec_id,segment,instrument,"Positional")
            if h.empty:
                return "UNAVAILABLE"
            cols=build_pnf(h["close"],0.0025,3)
            if len(cols)<3:
                return "SIDEWAYS"
            c1,c2,c3=cols[-3:]
            if c1["type"]=="X" and c2["type"]=="O" and c3["type"]=="X" and c3["high"]>c1["high"]:
                return "BULLISH"
            if c1["type"]=="O" and c2["type"]=="X" and c3["type"]=="O" and c3["low"]<c1["low"]:
                return "BEARISH"
            return "SIDEWAYS"
        except Exception:
            return "UNAVAILABLE"

    overview=[]
    for name in ["NIFTY","BANKNIFTY"]:
        sid=resolve_nse_index_security_id(master,name)
        overview.append({"Market":name,"Bias":market_bias_from_history(sid,"IDX_I","INDEX") if sid is not None else "UNAVAILABLE"})

    mcx=mcx_futures_universe(master)
    for commodity in ["GOLD","SILVER","CRUDEOIL"]:
        match=mcx[mcx["underlying_symbol"]==commodity]
        bias="UNAVAILABLE"
        if not match.empty:
            bias=market_bias_from_history(int(match.iloc[0]["security_id"]),"MCX_COMM","FUTCOM")
        overview.append({"Market":commodity,"Bias":bias})

    st.markdown("""
    <style>
    .market-card{border:1px solid rgba(255,255,255,.10);border-radius:16px;padding:15px;min-height:105px;
    background:linear-gradient(145deg,rgba(255,255,255,.055),rgba(255,255,255,.018));box-shadow:0 8px 24px rgba(0,0,0,.16);}
    .market-name{font-size:.82rem;font-weight:700;opacity:.75;margin-bottom:10px;}
    .market-bias{font-size:1.18rem;font-weight:800;}
    .market-sub{margin-top:7px;font-size:.70rem;opacity:.50;}
    
    .alpha-statusbar {
        display:flex;
        align-items:center;
        gap:9px;
        padding:9px 13px;
        margin:0 0 18px 0;
        border-radius:12px;
        background:rgba(255,255,255,.028);
        border:1px solid rgba(255,255,255,.06);
        color:rgba(255,255,255,.78);
        font-size:.78rem;
        font-weight:750;
        letter-spacing:.04em;
    }

    .alpha-live-dot {
        width:8px;
        height:8px;
        border-radius:50%;
        background:#47d18c;
        box-shadow:0 0 12px rgba(71,209,140,.55);
        flex:0 0 auto;
    }

    .alpha-divider {
        opacity:.35;
    }

    .alpha-status-text {
        opacity:.55;
        font-weight:650;
        letter-spacing:.03em;
    }
</style>
    """,unsafe_allow_html=True)

    def _market_parts(bias):
        if bias=="BULLISH": return "🟢","BULLISH","#47d18c"
        if bias=="BEARISH": return "🔴","BEARISH","#ff5c69"
        if bias=="SIDEWAYS": return "🟡","SIDEWAYS","#ffd15c"
        return "⚪","UNAVAILABLE","#a9adb7"

    cards=st.columns(5,gap="small")
    for col,item in zip(cards,overview):
        icon,label,dot=_market_parts(item["Bias"])
        with col:
            st.markdown(
                f"""<div class="market-dashboard-card"><div class="name">{item["Market"]}</div>
                <div class="bias" style="color:{dot};">{icon} {label}</div>
                <div class="sub">Current market state</div></div>""",
                unsafe_allow_html=True)

    st.markdown("### Dashboard Signals")
    ft=fresh_trades_dataframe()
    a,b,c=st.columns([1.25,1.25,1.5],gap="small")
    with a:
        bull=sum(1 for x in overview if x["Bias"]=="BULLISH")
        bear=sum(1 for x in overview if x["Bias"]=="BEARISH")
        st.metric("Bullish Markets",bull)
    with b:
        st.metric("Bearish Markets",bear)
    with c:
        st.metric("Fresh Trades Today",len(ft))

    if not ft.empty:
        st.markdown("#### Latest Fresh Trades")
        st.dataframe(
            ft.head(8)[["Entry Time","Symbol","Direction","Trade Price","Module","Mode","Status"]],
            use_container_width=True, hide_index=True,
            column_config={"Trade Price": st.column_config.NumberColumn("Price",format="%.2f")},
        )
    st.caption("Market information only.")


elif page in ("Momentum", "Positional"):
    mode = "Intraday" if page == "Momentum" else "Positional"
    st.markdown(
        f"""
        <div class="alpha-hero">
            <div class="alpha-hero-title">NSE {"Momentum" if page == "Momentum" else mode}</div>
            <div class="alpha-hero-sub">Live trade monitoring & signal tracking</div>
            <span class="alpha-badge">LIVE</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    

    fut = future_universe(master, "NSE")
    if fut.empty:
        st.error("No NSE F&O instruments available.")
        st.stop()

    # ---------------------------------------------------------
    # INTRADAY: daily Anchor eligibility is calculated once/day
    # ---------------------------------------------------------
    if mode == "Intraday":
        if "intraday_daily_filter" not in st.session_state:
            st.session_state.intraday_daily_filter = {}
        if "intraday_filter_date" not in st.session_state:
            st.session_state.intraday_filter_date = None

        today = datetime.now().date().isoformat()

        if (
            st.session_state.intraday_filter_date != today
            or not st.session_state.intraday_daily_filter
        ):
            eligible = {}

            prog = st.progress(
                0,
                text=f"Building today's opportunity universe: {len(fut)} instruments..."
            )

            for i, (_, r) in enumerate(fut.iterrows(), 1):
                try:
                    h = cached_cash_daily(int(r["underlying_security_id"]))
                    cols = build_pnf(h["close"], 0.0025, 3)

                    if cols:
                        last = cols[-1]

                        if last["type"] == "X" and last["boxes"] > 15:
                            eligible[str(r["underlying_symbol"])] = "Bullish"
                        elif last["type"] == "O" and last["boxes"] > 15:
                            eligible[str(r["underlying_symbol"])] = "Bearish"
                        else:
                            eligible[str(r["underlying_symbol"])] = None
                    else:
                        eligible[str(r["underlying_symbol"])] = None

                except Exception:
                    eligible[str(r["underlying_symbol"])] = None

                prog.progress(
                    i / max(len(fut), 1),
                    text=f"Universe {i}/{len(fut)}"
                )

            prog.empty()

            st.session_state.intraday_daily_filter = eligible
            st.session_state.intraday_filter_date = today

        daily_map = st.session_state.intraday_daily_filter

        allowed_symbols = {
            symbol
            for symbol, bias in daily_map.items()
            if bias in ("Bullish", "Bearish")
        }

        candidates = fut[
            fut["underlying_symbol"].isin(allowed_symbols)
        ].copy()

        st.info(
            f"{len(candidates)} instruments currently eligible for intraday monitoring."
        )

        if st.button(
            "Refresh Eligible Universe",
            key="refresh_intraday_universe"
        ):
            st.session_state.intraday_daily_filter = {}
            st.session_state.intraday_filter_date = None
            st.rerun()

    # ---------------------------------------------------------
    # POSITIONAL: show the currently running daily direction
    # ---------------------------------------------------------
    else:
        candidates = fut.copy()

    ids = (
        candidates["underlying_security_id"].astype(int).tolist()
        if not candidates.empty else []
    )

    spot_map = batch_ltp("NSE_EQ", ids) if ids else {}

    rows = []

    prog = st.progress(
        0,
        text=f"Scanning {len(candidates)} instruments..."
    )

    for i, (_, r) in enumerate(candidates.iterrows(), 1):
        symbol = str(r["underlying_symbol"])
        sid = int(r["underlying_security_id"])
        fut_sid = int(r["security_id"])
        ltp = spot_map.get(sid, np.nan)
        market_quote = {}
        try:
            market_quote = batch_quote("NSE_EQ", [sid]).get(sid, {})
        except Exception:
            market_quote = {}
        market_time = exchange_time_from_ltt(
            market_quote.get("last_trade_time", np.nan)
        )
        quote_price = pd.to_numeric(
            market_quote.get("last_price"),
            errors="coerce",
        )
        if pd.notna(quote_price):
            ltp = float(quote_price)

        try:
            oi_conf = {
                "label": "—",
                "state": "—",
                "oi_change_pct": np.nan,
                "price_change_pct": np.nan,
                "rank": 0,
            }
            sector_conf = {
                "confirmed": False,
                "breadth": np.nan,
                "sector": "Other",
            }

            if mode == "Intraday":
                # Only eligible stocks reach this point.
                h = cached_cash_intraday(sid)

                ip = analyze_new_pattern(
                    h,
                    0.0015,
                    anchor_min=15,
                    pullback_max=5,
                )

                positional_bias = daily_map[symbol]

                above_trend = (
                    pd.notna(ltp)
                    and pd.notna(intraday_sma10(h))
                    and float(ltp) > float(intraday_sma10(h))
                )
                below_trend = (
                    pd.notna(ltp)
                    and pd.notna(intraday_sma10(h))
                    and float(ltp) < float(intraday_sma10(h))
                )

                if (
                    positional_bias == "Bullish"
                    and ip["dtb"]
                    and above_trend
                ):
                    rec = "🟢 BUY"

                elif (
                    positional_bias == "Bearish"
                    and ip["dbs"]
                    and below_trend
                ):
                    rec = "🔴 SELL"

                elif (
                    positional_bias == "Bullish"
                    and ip["prospective"]
                    and ip["bias"] == "Bullish"
                    and above_trend
                ):
                    rec = "🟡 SETUP"

                elif (
                    positional_bias == "Bearish"
                    and ip["prospective"]
                    and ip["bias"] == "Bearish"
                    and below_trend
                ):
                    rec = "🟡 SETUP"

                else:
                    rec = "NO TRADE"

                bias = positional_bias
                entry = ip.get("entry_level", np.nan)
                sl = ip.get("sl", np.nan)

            else:
                # Positional = ACTIVE DTB/DBS only.
                p = positional_active_pattern(sid)

                bias = p["bias"]
                rec = p["recommendation"]
                entry = p["entry"]
                sl = p["sl"]

            # F&O OI confirmation runs ONLY for positional trades.
            # Intraday remains lightweight and does not query futures OI.
            if mode == "Positional":
                trade_direction = None
                if rec == "🟢 LONG":
                    trade_direction = "LONG"
                elif rec == "🔴 SHORT":
                    trade_direction = "SHORT"

                if trade_direction is not None:
                    oi_conf = oi_confirmation_for_trade(
                        fut_sid,
                        trade_direction,
                    )

                    # Sector confirmation is additive only.
                    # It never removes or changes the P&F trade.
                    sector_direction = trade_direction

            # A star means: valid positional P&F trade + strong OI confirmation.
            # It does not create the trade.
            superior = (
                mode == "Positional"
                and oi_conf.get("state") in ("LONG BUILDUP", "SHORT BUILDUP")
                and oi_conf.get("rank", 0) >= 3
                and rec in ("🟢 LONG", "🔴 SHORT")
            )
            display_symbol = f"★ {symbol}" if superior else symbol

            rows.append({
                "Script": display_symbol,
                "LTP": ltp,
                "Market Time": market_time,
                "Bias": bias,
                "Pattern": p.get("pattern", "—") if mode == "Positional" else "—",
                "OI Confirmation": oi_conf["label"],
                "OI Δ%": oi_conf["oi_change_pct"],
                "Entry": entry,
                "SL": sl,
                "Recommendation": rec,
                "_OI Rank": oi_conf["rank"],
                "_Sector Confirmed": False,
                "_Sector": sector_of(symbol),
                "_Sector Breadth %": np.nan,
            })

        except Exception:
            rows.append({
                "Script": symbol,
                "LTP": ltp,
                "Market Time": market_time,
                "Bias": "UNAVAILABLE",
                "Pattern": "—",
                "OI Confirmation": "DATA ERROR",
                "OI Δ%": np.nan,
                "Entry": np.nan,
                "SL": np.nan,
                "Recommendation": "DATA ERROR",
                "_OI Rank": 0,
                "_Sector Confirmed": False,
                "_Sector": sector_of(symbol),
                "_Sector Breadth %": np.nan,
            })

        prog.progress(
            i / max(len(candidates), 1),
            text=f"Scanning {i}/{max(len(candidates), 1)}"
        )

    prog.empty()

    if mode == "Positional":
        st.session_state["positional_oi_confirmed_count"] = int(
            sum(1 for x in rows if str(x.get("Script", "")).startswith("★ "))
        )

    res = pd.DataFrame(rows)

    if mode == "Positional" and not res.empty:
        # IMPORTANT:
        # Sector confirmation is based on the same 1% / 3-box daily
        # Sector Analysis module, NOT the individual stock's 0.25% P&F.
        fut_for_sector = future_universe(master, "NSE")
        sector_map_1pct = sector_confirmation_1pct_map(
            fut_for_sector
        )

        for idx_row, trade_row in res.iterrows():
            rec = str(
                trade_row.get("Recommendation", "")
            )

            if rec not in ("🟢 LONG", "🔴 SHORT"):
                continue

            symbol_clean = (
                str(trade_row["Script"])
                .replace("★★ ", "")
                .replace("★ ", "")
                .strip()
            )

            direction = (
                "LONG"
                if rec == "🟢 LONG"
                else "SHORT"
            )

            sector_name = sector_of(symbol_clean)
            sector_info = sector_map_1pct.get(
                sector_name,
                {
                    "bullish_pct": np.nan,
                    "bearish_pct": np.nan,
                    "bias": "UNAVAILABLE",
                    "stocks": 0,
                },
            )

            sector_bias = sector_info["bias"]

            if direction == "LONG":
                sector_ok = sector_bias == "BULLISH"
                breadth = sector_info["bullish_pct"]
            else:
                sector_ok = sector_bias == "BEARISH"
                breadth = sector_info["bearish_pct"]

            res.at[idx_row, "_Sector Confirmed"] = bool(
                sector_ok
            )
            res.at[idx_row, "_Sector"] = sector_name
            res.at[idx_row, "_Sector Breadth %"] = breadth

            # ★ = one confirmation
            # ★★ = both OI + sector confirmation
            oi_ok = (
                int(trade_row.get("_OI Rank", 0)) >= 3
            )

            if oi_ok and sector_ok:
                res.at[idx_row, "Script"] = (
                    f"★★ {symbol_clean}"
                )
            elif oi_ok or sector_ok:
                res.at[idx_row, "Script"] = (
                    f"★ {symbol_clean}"
                )
            else:
                res.at[idx_row, "Script"] = symbol_clean


    opened_trades, exited_trades = manage_trade_book(
        "NSE",
        mode,
        res,
    )

    if opened_trades:
        notify_new_trades(
            "NSE",
            mode,
            [t["Symbol"] for t in opened_trades],
        )

    if exited_trades:
        notify_trade_exits(exited_trades)


    if mode == "Intraday":
        long_df = res[res["Recommendation"] == "🟢 BUY"].copy()
        short_df = res[res["Recommendation"] == "🔴 SELL"].copy()
        setup_df = res[res["Recommendation"] == "🟡 SETUP"].copy()

    # MOMENTUM STICKY TRADES:
    # Once a trade is logged, keep it visible until an actual exit is recorded.
    if mode == "Intraday":
        sticky = _sticky_active_rows_for_display("NSE", "Intraday", res)
        if not sticky.empty:
            long_df = pd.concat(
                [long_df, sticky[sticky["Recommendation"] == "🟢 BUY"]],
                ignore_index=True,
            )
            short_df = pd.concat(
                [short_df, sticky[sticky["Recommendation"] == "🔴 SELL"]],
                ignore_index=True,
            )
    else:
        long_df = res[res["Recommendation"] == "🟢 LONG"].copy()
        short_df = res[res["Recommendation"] == "🔴 SHORT"].copy()
        setup_df = res[res["Recommendation"] == "🟡 SETUP"].copy()

    long_df = long_df.sort_values("_OI Rank", ascending=False)
    short_df = short_df.sort_values("_OI Rank", ascending=False)

    def active_trade_style(row):
        rec = str(row.get("Recommendation", ""))
        oi = str(row.get("OI Confirmation", ""))

        if rec in ("🟢 BUY", "🟢 LONG"):
            base = "background-color: #d9f2d9; color: #0b5d1e; font-weight: 700"
        elif rec in ("🔴 SELL", "🔴 SHORT"):
            base = "background-color: #f8d7da; color: #8a1c1c; font-weight: 700"
        else:
            base = ""

        styles = [base] * len(row)

        try:
            oi_col = row.index.get_loc("OI Confirmation")
            if "STRONG LONG" in oi or "STRONG SHORT" in oi:
                styles[oi_col] = "background-color: #b7e4c7; color: #064420; font-weight: 800"
            elif "CONFLICT" in oi:
                styles[oi_col] = "background-color: #f8b4b4; color: #7a0000; font-weight: 800"
            elif "WEAK" in oi:
                styles[oi_col] = "background-color: #fff3cd; color: #7a5200; font-weight: 700"
        except Exception:
            pass

        return styles

    if mode == "Positional":
        st.caption("Active trades remain visible until the system records an exit.")


    st.markdown("---")
    st.markdown(
        '<div class="alpha-section"><span class="alpha-section-dot" style="background:#29d77a;"></span>BULLISH / LONG</div>',
        unsafe_allow_html=True,
    )
    if long_df.empty:
        st.info("No active bullish positions currently.")
    else:
        st.dataframe(
            long_df[
                [
                    "Script", "LTP", "Bias", "Entry", "SL", "Recommendation"
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown(
        '<div class="alpha-section"><span class="alpha-section-dot" style="background:#ff5362;"></span>BEARISH / SHORT</div>',
        unsafe_allow_html=True,
    )
    if short_df.empty:
        st.info("No active bearish positions currently.")
    else:
        st.dataframe(
            short_df[
                [
                    "Script", "LTP", "Bias", "Entry", "SL", "Recommendation"
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown(
        '<div class="alpha-section"><span class="alpha-section-dot" style="background:#ffd45c;"></span>OTHER</div>',
        unsafe_allow_html=True,
    )
    sideways_df = res[res["Recommendation"] == "NO POSITION"].copy()
    if sideways_df.empty:
        st.info("No sideways instruments currently.")
    else:
        st.dataframe(
            sideways_df[
                ["Script", "LTP", "Bias", "Entry", "SL", "Recommendation"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    render_trade_monitor("NSE", mode)



elif page == "MCX Futures":
    st.markdown(
        """
        <div class="alpha-hero">
            <div class="alpha-hero-title">MCX FUTURES</div>
            <div class="alpha-hero-sub">Live commodity futures monitoring</div>
            <span class="alpha-badge">LIVE</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    
    st.caption("GOLD • SILVER • COPPER • CRUDEOIL • NATURALGAS • ZINC • LEAD • NICKEL • ALUMINIUM")

    mode=st.radio("Horizon",["Intraday","Positional"],horizontal=True,key="mcx_futures_mode")
    fut=mcx_futures_universe(master)

    if fut.empty:
        st.error("No active MCX futures contracts available.")
        st.stop()

    if mode=="Intraday":
        if "mcx_intraday_daily_filter" not in st.session_state:
            st.session_state.mcx_intraday_daily_filter={}
        if "mcx_intraday_filter_date" not in st.session_state:
            st.session_state.mcx_intraday_filter_date=None

        today=datetime.now().date().isoformat()
        if (
            st.session_state.mcx_intraday_filter_date!=today
            or not st.session_state.mcx_intraday_daily_filter
        ):
            daily_map={}
            for _,row in fut.iterrows():
                daily_map[str(row["underlying_symbol"])]=mcx_intraday_daily_eligibility(int(row["security_id"]))
            st.session_state.mcx_intraday_daily_filter=daily_map
            st.session_state.mcx_intraday_filter_date=today

        daily_map=st.session_state.mcx_intraday_daily_filter
        candidates=fut[fut["underlying_symbol"].map(daily_map).isin(["Bullish","Bearish"])].copy()

        st.info(f"{len(candidates)} MCX instruments currently eligible for intraday monitoring.")
        if st.button("Refresh Eligible Universe",key="mcx_refresh_eligible"):
            st.session_state.mcx_intraday_daily_filter={}
            st.session_state.mcx_intraday_filter_date=None
            st.rerun()
    else:
        candidates=fut

    ids=candidates["security_id"].astype(int).tolist() if not candidates.empty else []
    ltp_map=batch_ltp("MCX_COMM",ids) if ids else {}
    rows=[]

    for _,row in candidates.iterrows():
        symbol=str(row["underlying_symbol"])
        sid=int(row["security_id"])
        ltp=ltp_map.get(sid,np.nan)
        market_quote = {}
        try:
            market_quote = batch_quote("MCX_COMM", [sid]).get(sid, {})
        except Exception:
            market_quote = {}
        market_time = exchange_time_from_ltt(
            market_quote.get("last_trade_time", np.nan)
        )
        quote_price = pd.to_numeric(
            market_quote.get("last_price"),
            errors="coerce",
        )
        if pd.notna(quote_price):
            ltp = float(quote_price)

        rec="DATA ERROR"; bias="UNAVAILABLE"; entry=np.nan; sl=np.nan; display_symbol=symbol

        try:
            if mode=="Positional":
                p=positional_active_pattern_mcx(sid)
                rec=p["recommendation"]; bias=p["bias"]; entry=p["entry"]; sl=p["sl"]

                # P&F decides the trade; OI only adds ★.
                if rec=="🟢 LONG" and mcx_oi_star(sid,"LONG"):
                    display_symbol=f"★ {symbol}"
                elif rec=="🔴 SHORT" and mcx_oi_star(sid,"SHORT"):
                    display_symbol=f"★ {symbol}"

            else:
                h=historical(sid,"MCX_COMM","FUTCOM","Intraday")
                ip=analyze_new_pattern(h,0.0015,anchor_min=15,pullback_max=5)
                positional_bias=st.session_state.mcx_intraday_daily_filter.get(symbol,"Sideways")
                sma=intraday_sma10(h)

                above=pd.notna(ltp) and pd.notna(sma) and float(ltp)>float(sma)
                below=pd.notna(ltp) and pd.notna(sma) and float(ltp)<float(sma)

                if positional_bias=="Bullish" and ip["dtb"] and above:
                    rec="🟢 BUY"
                elif positional_bias=="Bearish" and ip["dbs"] and below:
                    rec="🔴 SELL"
                elif (
                    (positional_bias=="Bullish" and ip["prospective"] and ip["bias"]=="Bullish" and above)
                    or
                    (positional_bias=="Bearish" and ip["prospective"] and ip["bias"]=="Bearish" and below)
                ):
                    rec="🟡 SETUP"
                else:
                    rec="NO TRADE"

                bias=positional_bias
                entry=ip.get("entry_level",np.nan)
                sl=ip.get("sl",np.nan)

        except Exception:
            pass

        rows.append({
            "Script":display_symbol,
            "LTP":ltp,
            "Market Time":market_time,
            "Bias":bias,
            "Entry":entry,
            "SL":sl,
            "Recommendation":rec,
        })

    res = pd.DataFrame(rows)

    opened_trades, exited_trades = manage_trade_book(
        "MCX",
        mode,
        res,
    )

    if opened_trades:
        notify_new_trades(
            "MCX",
            mode,
            [t["Symbol"] for t in opened_trades],
        )

    if exited_trades:
        notify_trade_exits(exited_trades)

    long_rec="🟢 LONG" if mode=="Positional" else "🟢 BUY"
    short_rec="🔴 SHORT" if mode=="Positional" else "🔴 SELL"

    long_df=res[res["Recommendation"]==long_rec].copy()
    short_df=res[res["Recommendation"]==short_rec].copy()

    if mode=="Intraday":
        sticky = _sticky_active_rows_for_display("MCX", "Intraday", res)
        if not sticky.empty:
            # MCX uses the same compact trade display, but preserve the current
            # table's recommendation labels.
            sticky["Recommendation"] = sticky["Recommendation"].replace({
                "🟢 BUY": "🟢 BUY",
                "🔴 SELL": "🔴 SELL",
            })
            long_df = pd.concat(
                [long_df, sticky[sticky["Recommendation"]=="🟢 BUY"]],
                ignore_index=True,
            )
            short_df = pd.concat(
                [short_df, sticky[sticky["Recommendation"]=="🔴 SELL"]],
                ignore_index=True,
            )

    def mcx_style(row):
        rec=str(row["Recommendation"])
        if rec in ("🟢 LONG","🟢 BUY"):
            return ["background-color:#d9f2d9;color:#0b5d1e;font-weight:700"]*len(row)
        if rec in ("🔴 SHORT","🔴 SELL"):
            return ["background-color:#f8d7da;color:#8a1c1c;font-weight:700"]*len(row)
        return [""]*len(row)

    st.markdown('<div class="alpha-section"><span class="alpha-section-dot" style="background:#22d77c;"></span>BULLISH / LONG</div>', unsafe_allow_html=True)
    if long_df.empty:
        st.info("No bullish/long MCX trades currently.")
    else:
        st.dataframe(
            long_df[["Script","LTP","Bias","Entry","SL","Recommendation"]].style.apply(mcx_style,axis=1),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown('<div class="alpha-section"><span class="alpha-section-dot" style="background:#ff5364;"></span>BEARISH / SHORT</div>', unsafe_allow_html=True)
    if short_df.empty:
        st.info("No bearish/short MCX trades currently.")
    else:
        st.dataframe(
            short_df[["Script","LTP","Bias","Entry","SL","Recommendation"]].style.apply(mcx_style,axis=1),
            use_container_width=True,
            hide_index=True,
        )


elif page == "Option Seller":
    st.markdown(
        """
        <div class="alpha-hero">
            <div class="alpha-hero-title">OPTION SELLER</div>
            <div class="alpha-hero-sub">Live option market monitoring</div>
            <span class="alpha-badge">LIVE</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    
    st.caption("NIFTY • BANKNIFTY • SENSEX")

    index_name = st.selectbox(
        "Index",
        INDEX_NAMES,
        key="option_index",
    )

    horizon = st.radio(
        "Horizon",
        ["Intraday", "Positional"],
        horizontal=True,
        key="option_horizon",
    )

    try:
        index_sid = resolve_index_instrument(master, index_name)

        # Dhan v2: fetch active expiries for the selected index.
        expiries = option_expiry_list_v2(index_sid)

        expiry_options = [
            d.strftime("%d-%b-%Y") for d in expiries
        ]

        default_expiry = select_option_expiry_v2(expiries, horizon)
        default_index = (
            expiries.index(default_expiry)
            if default_expiry in expiries else 0
        )

        selected_expiry_label = st.selectbox(
            "Expiry",
            expiry_options,
            index=default_index,
            key=f"expiry_{index_name}_{horizon}",
        )
        selected_expiry = expiries[expiry_options.index(selected_expiry_label)]

        # Dhan v2: fetch the selected expiry option chain.
        raw_chain = option_chain_request_v2(index_sid, selected_expiry)
        raw_data = parse_data(raw_chain)

        # Dhan option-chain response contains the underlying last_price.
        spot = (
            pd.to_numeric(raw_data.get("last_price"), errors="coerce")
            if isinstance(raw_data, dict)
            else np.nan
        )

        chain_df = parse_option_chain_v2(raw_chain)
        # All strategy, IV and OI analysis is restricted to ATM +/- 20 strikes.
        chain_df = filter_atm_strike_window(
            chain_df,
            spot,
            strikes_each_side=20,
        )


        analysis = option_seller_analysis_v2(
            chain_df,
            spot,
            horizon,
        )

        session_state = option_session_state(
            index_name,
            selected_expiry,
            analysis["atm_iv"],
        )

        iv_risk = option_iv_risk(
            analysis["atm_iv"],
            session_state,
        )

        oi_risk = option_oi_risk(
            chain_df,
            spot,
        )

        notify_option_warning(
            index_name,
            "iv_expansion",
            f"{index_name} IV is expanding sharply.",
            bool(iv_risk["alert"]),
        )
        notify_option_warning(
            index_name,
            "oi_buildup",
            f"{index_name} has unusual one-sided OI buildup.",
            bool(oi_risk["alert"]),
        )

        strategy_name, strategy_reason = simple_option_decision(
            index_name=index_name,
            recommendation=analysis["recommendation"],
            spot=spot,
            atm=analysis["atm"],
            expected_move=analysis["expected_move"],
            support=analysis["support"],
            resistance=analysis["resistance"],
            atm_iv=analysis["atm_iv"],
            open_iv=session_state["open_iv"],
            oi_alert=oi_risk,
        )

        a, b, c, d = st.columns(4)
        a.metric("Index", index_name)
        b.metric("Spot", f"{spot:,.2f}" if pd.notna(spot) else "—")
        c.metric(
            "ATM IV",
            f"{analysis['atm_iv']:.2f}%"
            if pd.notna(analysis["atm_iv"])
            else "—",
        )
        d.metric(
            "ATM Premium",
            f"{analysis['ce_ltp'] + analysis['pe_ltp']:.2f}"
            if pd.notna(analysis["ce_ltp"]) and pd.notna(analysis["pe_ltp"])
            else "—",
        )

        st.markdown("### Recommendation")
        st.subheader(analysis["recommendation"])
        st.write(analysis["reason"])

        if analysis["recommendation"] == "🟢 SELL STRADDLE":
            st.success(
                f"SELL {index_name} {int(analysis['atm'])} CE + "
                f"{int(analysis['atm'])} PE"
            )
        elif analysis["recommendation"] == "🟡 CAUTION":
            st.warning("Wait for better conditions.")
        else:
            st.error("Do not sell under current conditions.")

        st.markdown("### Strategy")
        if strategy_name == "SELL STRADDLE":
            st.success(f"SELL {index_name} ATM CE + ATM PE")
        elif strategy_name == "SELL PUT":
            st.success(f"SELL {index_name} PUT")
        elif strategy_name == "SELL CALL":
            st.success(f"SELL {index_name} CALL")
        else:
            st.warning("WAIT")

        st.caption(f"Why: {strategy_reason}")

        st.markdown("### Risk Alerts")
        risk_df = pd.DataFrame([
            {
                "Risk": "Volatility",
                "Status": "🔴 ALERT" if iv_risk["alert"] else "🟢 NORMAL",
                "Message": iv_risk["text"],
            },
            {
                "Risk": "Positioning",
                "Status": "🔴 ALERT" if oi_risk["alert"] else "🟢 NORMAL",
                "Message": oi_risk["text"],
            },
        ])
        st.dataframe(
            risk_df,
            use_container_width=True,
            hide_index=True,
        )

        if "option_alert_states" not in st.session_state:
            st.session_state.option_alert_states = {}

        alert_key = f"{index_name}|{horizon}|{selected_expiry}"
        old_state = st.session_state.option_alert_states.get(
            alert_key,
            {"iv": False, "oi": False},
        )
        new_state = {
            "iv": bool(iv_risk["alert"]),
            "oi": bool(oi_risk["alert"]),
        }

        if new_state["iv"] and not old_state["iv"]:
            warning_text = f"{index_name} implied volatility is rising."
            st.toast(
                f"⚠️ Option warning: {warning_text}",
                icon="⚠️",
            )
            speak_option_alert(
                f"Warning. {warning_text}"
            )

        if new_state["oi"] and not old_state["oi"]:
            side_word = "call" if oi_risk["side"] == "CALL" else "put"
            warning_text = (
                f"{index_name} heavy {side_word} side positioning buildup."
            )
            st.toast(
                f"⚠️ Option warning: {warning_text}",
                icon="⚠️",
            )
            speak_option_alert(
                f"Warning. {warning_text}"
            )

        st.session_state.option_alert_states[alert_key] = new_state

        st.markdown("### Selected Straddle")
        st.dataframe(
            pd.DataFrame([{
                "Index": index_name,
                "Expiry": selected_expiry.strftime("%d-%b-%Y"),
                "Call Strike": int(analysis["atm"]) if pd.notna(analysis["atm"]) else "—",
                "Call Premium": analysis["ce_ltp"],
                "Put Strike": int(analysis["atm"]) if pd.notna(analysis["atm"]) else "—",
                "Put Premium": analysis["pe_ltp"],
                "Combined Premium": (
                    analysis["ce_ltp"] + analysis["pe_ltp"]
                    if pd.notna(analysis["ce_ltp"]) and pd.notna(analysis["pe_ltp"])
                    else np.nan
                ),
            }]),
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("---")
        st.markdown("### Option Chain")

        # Dhan-style two-sided layout:
        # CALLS | STRIKE | PUTS
        chain_view = chain_df.copy()
        chain_view["Strike"] = pd.to_numeric(chain_view["Strike"], errors="coerce")
        chain_view = chain_view.dropna(subset=["Strike"])

        calls = chain_view[chain_view["Side"] == "CE"].set_index("Strike").sort_index()
        puts = chain_view[chain_view["Side"] == "PE"].set_index("Strike").sort_index()

        strikes = sorted(set(calls.index.tolist()) | set(puts.index.tolist()))

        rows = []
        for strike in strikes:
            ce = calls.loc[strike] if strike in calls.index else pd.Series(dtype=float)
            pe = puts.loc[strike] if strike in puts.index else pd.Series(dtype=float)

            def num(series, key):
                if key not in series.index:
                    return np.nan
                return pd.to_numeric(series[key], errors="coerce")

            rows.append({
                "CE OI": num(ce, "OI"),
                "CE ΔOI": num(ce, "Change OI"),
                "CE Volume": num(ce, "Volume"),
                "CE IV": num(ce, "IV"),
                "CE LTP": num(ce, "LTP"),
                "CE Δ": num(ce, "Delta"),
                "STRIKE": strike,
                "PE Δ": num(pe, "Delta"),
                "PE LTP": num(pe, "LTP"),
                "PE IV": num(pe, "IV"),
                "PE Volume": num(pe, "Volume"),
                "PE ΔOI": num(pe, "Change OI"),
                "PE OI": num(pe, "OI"),
            })

        dhan_chain = pd.DataFrame(rows)

        atm_strike = analysis["atm"]

        def highlight_atm(row):
            if pd.notna(atm_strike) and row["STRIKE"] == atm_strike:
                return [
                    "background-color: #174c38; color: white; font-weight: 700"
                ] * len(row)
            return [""] * len(row)

        st.dataframe(
            dhan_chain.style.apply(highlight_atm, axis=1),
            use_container_width=True,
            hide_index=True,
            column_config={
                "CE OI": st.column_config.NumberColumn("OI", format="%.0f"),
                "CE ΔOI": st.column_config.NumberColumn("Δ OI", format="%.0f"),
                "CE Volume": st.column_config.NumberColumn("Volume", format="%.0f"),
                "CE IV": st.column_config.NumberColumn("IV", format="%.2f"),
                "CE LTP": st.column_config.NumberColumn("LTP", format="%.2f"),
                "CE Δ": st.column_config.NumberColumn("Delta", format="%.2f"),
                "STRIKE": st.column_config.NumberColumn("STRIKE", format="%.0f"),
                "PE Δ": st.column_config.NumberColumn("Delta", format="%.2f"),
                "PE LTP": st.column_config.NumberColumn("LTP", format="%.2f"),
                "PE IV": st.column_config.NumberColumn("IV", format="%.2f"),
                "PE Volume": st.column_config.NumberColumn("Volume", format="%.0f"),
                "PE ΔOI": st.column_config.NumberColumn("Δ OI", format="%.0f"),
                "PE OI": st.column_config.NumberColumn("OI", format="%.0f"),
            },
        )

        st.caption(
            "Calls on the left, strike prices in the centre, puts on the right. "
            "ATM is highlighted."
        )

        pcr_now=option_pcr_snapshot(chain_df,spot,strikes_each_side=20)
        pcr_key=f"pcr_history|{index_name}|{selected_expiry.strftime('%Y-%m-%d')}|{horizon}"
        if "pcr_history" not in st.session_state:
            st.session_state.pcr_history={}
        pcr_history=st.session_state.pcr_history.setdefault(pcr_key,[])
        if pd.notna(pcr_now):
            now_ts=datetime.now()
            if not pcr_history or (now_ts-pcr_history[-1][0]).total_seconds()>=45:
                pcr_history.append((now_ts,float(pcr_now)))
        pcr_history[:]=pcr_history[-120:]
        st.markdown("### Positioning Trend")
        render_pcr_positioning_chart([v for _,v in pcr_history])


    except Exception:
        st.error("Option data is currently unavailable.")
        st.info("Please refresh or try again later.")


elif page == "Sector Analysis":
    st.markdown(
        """
        <div class="alpha-hero">
            <div class="alpha-hero-title">SECTOR ANALYSIS</div>
            <div class="alpha-hero-sub">Sector strength monitor</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    
    st.caption(
        "Sector market view • Manual refresh only"
    )

    fut = future_universe(master, "NSE")

    if fut.empty:
        st.error("No NSE F&O universe available.")
    else:
        if st.button(
            "🔄 Calculate / Refresh Sector Analysis",
            key="sector_manual_refresh",
        ):
            # Only clear the analysis caches. Do not destroy login/session data.
            try:
                st.cache_data.clear()
            except Exception:
                pass

            summary, stock_detail = run_sector_analysis_manual(fut)
            st.session_state.sector_analysis_result = summary
            st.session_state.sector_stock_detail = stock_detail

        result = st.session_state.get(
            "sector_analysis_result",
            pd.DataFrame(),
        )
        stock_detail = st.session_state.get(
            "sector_stock_detail",
            pd.DataFrame(),
        )

        if result.empty:
            st.info(
                "Press 'Calculate / Refresh Sector Analysis' to calculate "
                "sector strength."
            )

            if not stock_detail.empty:
                st.warning("Some sector data is currently unavailable. Please refresh.")
        else:
            st.subheader("Sector Strength")
            st.dataframe(
                result,
                use_container_width=True,
                hide_index=True,
            )

            with st.expander("Additional Details"):
                st.dataframe(
                    stock_detail.sort_values(
                        ["Sector", "Bias", "Stock"]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )


elif page == "RS Matrix":
    st.markdown(
        """
        <div class="alpha-hero">
            <div class="alpha-hero-title">RS MATRIX</div>
            <div class="alpha-hero-sub">Relative market strength monitor</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    
    st.caption(
        "Relative strength market view • Manual refresh only"
    )

    fut = future_universe(master, "NSE")

    if fut.empty:
        st.error("No NSE F&O universe available.")
    else:
        if st.button(
            "🔄 Calculate / Refresh RS Matrix",
            key="rs_manual_refresh",
        ):
            st.cache_data.clear()
            st.session_state.rs_matrix_result = run_rs_matrix_manual(fut)

        result = st.session_state.get(
            "rs_matrix_result",
            pd.DataFrame(),
        )

        if result.empty:
            st.info("Press the button above to calculate relative strength.")
        else:
            display_result = result.drop(
                columns=["_score"],
                errors="ignore",
            )

            st.dataframe(
                display_result,
                use_container_width=True,
                hide_index=True,
            )


else:
    st.title("System Status")
    st.info("System is running.")

st.caption(f"Last refresh: {local_now().strftime('%d-%b-%Y %H:%M:%S IST')}")
