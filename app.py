
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="ALPHA ANALYZER", page_icon="α", layout="wide")

API = "https://api.dhan.co/v2"

# -----------------------------
# Session credentials
# -----------------------------
if "client_id" not in st.session_state:
    st.session_state.client_id = ""
if "access_token" not in st.session_state:
    st.session_state.access_token = ""
if "api_log" not in st.session_state:
    st.session_state.api_log = []

with st.sidebar:
    st.title("ALPHA ANALYZER")
    st.session_state.client_id = st.text_input(
        "Client Code",
        value=st.session_state.client_id,
    ).strip()
    st.session_state.access_token = st.text_input(
        "Access Token",
        value=st.session_state.access_token,
        type="password",
    ).strip()

    auto = st.checkbox("Auto Refresh", True)
    page = st.radio(
        "Module",
        [
            "Market Overview",
            "NSE Intraday P&F",
            "NSE Positional P&F",
            "MCX Intraday",
            "MCX Positional",
            "Diagnostics",
        ],
    )

def headers():
    if not st.session_state.client_id or not st.session_state.access_token:
        raise RuntimeError("Enter Client Code and Access Token.")
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
            "time": datetime.now().strftime("%H:%M:%S"),
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
    """Sector confirmation from the already-scanned P&F universe; no extra API calls."""
    sec = sector_of(symbol)
    if sec == "Other":
        return {"sector": sec, "breadth": np.nan, "star": False}
    x = df[df["Sector"] == sec].copy()
    valid = x[x["Bias"].isin(["Bullish", "Bearish"])]
    if valid.empty:
        return {"sector": sec, "breadth": np.nan, "star": False}
    if direction == "LONG":
        pct = 100.0 * (valid["Bias"] == "Bullish").mean()
        return {"sector": sec, "breadth": pct, "star": pct >= 50.0}
    if direction == "SHORT":
        pct = 100.0 * (valid["Bias"] == "Bearish").mean()
        return {"sector": sec, "breadth": pct, "star": pct >= 50.0}
    return {"sector": sec, "breadth": np.nan, "star": False}



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

# Auto refresh is deliberately longer than the initial full-universe scan.
# The full NSE scan can take longer than 1 minute, so a 1-minute rerun would
# interrupt the scan and start it again from stock 1.
if auto:
    try:
        from streamlit_autorefresh import st_autorefresh
        mins = 1 if page == "NSE Intraday P&F" else 1 if page == "MCX Intraday" else 15
        if page == "Market Overview":
            mins = 3
        st_autorefresh(interval=mins * 60 * 1000, key=f"refresh_{page}")
        if page == "NSE Intraday P&F":
            st.caption("NSE Intraday auto-refresh: 1 minute. Daily P&F filter reduces the intraday scan universe before 1-minute analysis.")
    except Exception:
        pass

if page == "Market Overview":
    nse = future_universe(master, "NSE")
    mcx = future_universe(master, "MCX")
    st.title("ALPHA ANALYZER")
    a,b,c = st.columns(3)
    a.metric("NSE F&O stocks", len(nse))
    b.metric("MCX futures", len(mcx))
    c.metric("Instrument rows", len(master))

    if not nse.empty:
        sample = nse.head(min(20, len(nse)))
        try:
            spot = batch_ltp("NSE_EQ", sample["underlying_security_id"].dropna().astype(int).tolist())
            st.metric("Live NSE cash LTPs returned", f"{len(spot)}/{len(sample)}")
        except Exception as e:
            st.error(f"Spot LTP test: {e}")

elif page in ("NSE Intraday P&F", "NSE Positional P&F"):
    mode = "Intraday" if page == "NSE Intraday P&F" else "Positional"
    st.title(page)

    fut = future_universe(master, "NSE")
    if fut.empty:
        st.error("No NSE FUTSTK universe found.")
        st.stop()

    if mode == "Positional":
        st.caption("Positional P&F: cash/spot | 0.25% box | 3-box reversal | daily close.")
        candidates = fut.copy()
    else:
        st.caption(
            "Intraday: daily last-column Anchor (>15 boxes) filter once per day → "
            "0.15% / 3-box / 1-minute P&F + intraday 10-SMA. No OI. No sector analysis."
        )
        daily_map = get_morning_daily_filter(fut)
        daily_df = pd.DataFrame([
            {"Symbol": sym, "Daily Bias": info["bias"], "Daily Anchor": info["anchor_ok"]}
            for sym, info in daily_map.items()
        ])
        allowed_symbols = set(daily_df.loc[daily_df["Daily Anchor"], "Symbol"])
        candidates = fut[fut["underlying_symbol"].isin(allowed_symbols)].copy()

        st.info(
            f"Morning daily filter retained {len(candidates)} of {len(fut)} F&O stocks. "
            f"Filter date: {st.session_state.intraday_filter_date}"
        )
        if st.button("Rebuild Daily Filter", key="rebuild_daily_filter"):
            st.session_state.intraday_daily_filter = build_morning_daily_filter(fut)
            st.session_state.intraday_filter_date = datetime.now().date().isoformat()
            st.rerun()

    ids = candidates["underlying_security_id"].astype(int).tolist() if not candidates.empty else []
    spot_map = batch_ltp("NSE_EQ", ids) if ids else {}

    rows = []
    prog = st.progress(0, text=f"Scanning {len(candidates)} NSE stocks...")
    for i, (_, r) in enumerate(candidates.iterrows(), 1):
        symbol = str(r["underlying_symbol"])
        sid = int(r["underlying_security_id"])
        ltp = spot_map.get(sid, np.nan)

        try:
            h = cached_cash_intraday(sid) if mode == "Intraday" else cached_cash_daily(sid)
            p = analyze_new_pattern(
                h,
                0.0015 if mode == "Intraday" else 0.0025,
                anchor_min=15,
                pullback_max=5,
            )

            if mode == "Intraday":
                daily_bias = daily_df.loc[daily_df["Symbol"] == symbol, "Daily Bias"].iloc[0]
                sma10 = intraday_sma10(h)
                above_sma = pd.notna(ltp) and pd.notna(sma10) and float(ltp) > float(sma10)
                below_sma = pd.notna(ltp) and pd.notna(sma10) and float(ltp) < float(sma10)

                if p["dtb"] and daily_bias == "Bullish" and above_sma:
                    rec = "🟢 BUY"
                elif p["dbs"] and daily_bias == "Bearish" and below_sma:
                    rec = "🔴 SELL"
                elif p["prospective"] and (
                    (daily_bias == "Bullish" and p["bias"] == "Bullish" and above_sma) or
                    (daily_bias == "Bearish" and p["bias"] == "Bearish" and below_sma)
                ):
                    rec = "🟡 SETUP"
                else:
                    rec = "NO TRADE"
                bias = daily_bias
                sl = p.get("sl", np.nan)
            else:
                rec = (
                    "🟢 BUY" if p["dtb"] else
                    "🔴 SELL" if p["dbs"] else
                    "🟡 SETUP" if p["prospective"] else
                    "NO TRADE"
                )
                bias = p["bias"]
                sl = p.get("sl", np.nan)

            # Ensure SL is displayed for BUY/SELL/SETUP. If pattern does not
            # yet have a pullback-based SL, derive a structural fallback from
            # the last three P&F columns.
            if pd.isna(sl):
                try:
                    cols = build_pnf(h["close"], 0.0015 if mode == "Intraday" else 0.0025, 3)
                    if len(cols) >= 2:
                        last = cols[-1]
                        prev = cols[-2]
                        if bias == "Bullish" and prev["type"] == "O":
                            sl = prev["low"]
                        elif bias == "Bearish" and prev["type"] == "X":
                            sl = prev["high"]
                except Exception:
                    pass

            rows.append({
                "Script": symbol,
                "LTP": ltp,
                "Bias": bias,
                "Entry": p.get("entry_level", np.nan),
                "SL": sl,
                "Recommendation": rec,
            })
        except Exception as e:
            rows.append({
                "Script": symbol,
                "LTP": ltp,
                "Bias": "UNAVAILABLE",
                "Entry": np.nan,
                "SL": np.nan,
                "Recommendation": "DATA ERROR",
            })

        prog.progress(i / max(len(candidates), 1), text=f"Scanning {i}/{max(len(candidates), 1)}")
    prog.empty()

    res = pd.DataFrame(rows)

    long_df = res[res["Recommendation"] == "🟢 BUY"].copy()
    short_df = res[res["Recommendation"] == "🔴 SELL"].copy()
    setup_df = res[res["Recommendation"] == "🟡 SETUP"].copy()

    st.markdown("## 🟢 BULLISH / LONG TRADES")
    if long_df.empty:
        st.info("No bullish trades currently.")
    else:
        st.dataframe(
            long_df[["Script", "LTP", "Bias", "SL", "Recommendation"]],
            use_container_width=True, hide_index=True
        )

    st.markdown("## 🔴 BEARISH / SHORT TRADES")
    if short_df.empty:
        st.info("No bearish trades currently.")
    else:
        st.dataframe(
            short_df[["Script", "LTP", "Bias", "SL", "Recommendation"]],
            use_container_width=True, hide_index=True
        )

    st.markdown("## 🟡 SETUPS FORMING")
    if setup_df.empty:
        st.info("No setups forming currently.")
    else:
        st.dataframe(
            setup_df[["Script", "LTP", "Bias", "SL", "Recommendation"]],
            use_container_width=True, hide_index=True
        )

    st.markdown("## All Scanned")
    st.dataframe(
        res[["Script", "LTP", "Bias", "SL", "Recommendation"]],
        use_container_width=True, hide_index=True
    )

elif page in ("MCX Intraday", "MCX Positional"):
    mode = "Intraday" if page == "MCX Intraday" else "Positional"
    st.title(page)

    fut = future_universe(master, "MCX")
    if fut.empty:
        st.error("No MCX FUTCOM universe found.")
        st.stop()

    if mode == "Intraday":
        st.caption(
            "MCX Intraday: daily 0.25% / 3-box last-column Anchor filter once per day → "
            "0.15% / 3-box / 1-minute P&F. P&F only."
        )
        if "mcx_daily_filter" not in st.session_state:
            st.session_state.mcx_daily_filter = {}
        if "mcx_filter_date" not in st.session_state:
            st.session_state.mcx_filter_date = None

        today = datetime.now().date().isoformat()
        if st.session_state.mcx_filter_date != today or not st.session_state.mcx_daily_filter:
            dmap = {}
            prog = st.progress(0, text=f"Building MCX daily filter for {len(fut)} commodities...")
            for i, (_, r) in enumerate(fut.iterrows(), 1):
                try:
                    h = historical(r["security_id"], "MCX_COMM", "FUTCOM", "Positional")
                    cols = build_pnf(h["close"], 0.0025, 3)
                    if cols:
                        last = cols[-1]
                        if last["type"] == "X" and last["boxes"] > 15:
                            dmap[str(r["underlying_symbol"])] = "Bullish"
                        elif last["type"] == "O" and last["boxes"] > 15:
                            dmap[str(r["underlying_symbol"])] = "Bearish"
                        else:
                            dmap[str(r["underlying_symbol"])] = "Sideways"
                    else:
                        dmap[str(r["underlying_symbol"])] = "Unavailable"
                except Exception:
                    dmap[str(r["underlying_symbol"])] = "Unavailable"
                prog.progress(i / max(len(fut), 1), text=f"Daily filter {i}/{len(fut)}")
            prog.empty()
            st.session_state.mcx_daily_filter = dmap
            st.session_state.mcx_filter_date = today

        daily_map = st.session_state.mcx_daily_filter
        allowed = {s for s, b in daily_map.items() if b in ("Bullish", "Bearish")}
        candidates = fut[fut["underlying_symbol"].isin(allowed)].copy()
        if st.button("Rebuild MCX Daily Filter", key="rebuild_mcx_daily_filter"):
            st.session_state.mcx_daily_filter = {}
            st.session_state.mcx_filter_date = None
            st.rerun()
    else:
        st.caption("MCX Positional: 0.25% / 3-box / daily close. P&F only.")
        candidates = fut.copy()

    ids = candidates["security_id"].astype(int).tolist() if not candidates.empty else []
    ltp_map = batch_ltp("MCX_COMM", ids) if ids else {}

    rows = []
    prog = st.progress(0, text=f"Scanning {len(candidates)} MCX commodities...")
    for i, (_, r) in enumerate(candidates.iterrows(), 1):
        symbol = str(r["underlying_symbol"])
        sid = int(r["security_id"])
        ltp = ltp_map.get(sid, np.nan)
        try:
            h = historical(
                sid, "MCX_COMM", "FUTCOM",
                "Intraday" if mode == "Intraday" else "Positional"
            )
            p = analyze_new_pattern(
                h,
                0.0015 if mode == "Intraday" else 0.0025,
                anchor_min=15,
                pullback_max=5,
            )

            if mode == "Intraday":
                daily_bias = daily_map.get(symbol, "Unavailable")
                if p["dtb"] and daily_bias == "Bullish":
                    rec = "🟢 BUY"
                elif p["dbs"] and daily_bias == "Bearish":
                    rec = "🔴 SELL"
                elif p["prospective"] and p["bias"] == daily_bias:
                    rec = "🟡 SETUP"
                else:
                    rec = "NO TRADE"
                bias = daily_bias
            else:
                bias = p["bias"]
                rec = (
                    "🟢 BUY" if p["dtb"] else
                    "🔴 SELL" if p["dbs"] else
                    "🟡 SETUP" if p["prospective"] else
                    "NO TRADE"
                )

            sl = p.get("sl", np.nan)
            if pd.isna(sl):
                try:
                    cols = build_pnf(h["close"], 0.0015 if mode == "Intraday" else 0.0025, 3)
                    if len(cols) >= 2:
                        prev = cols[-2]
                        if bias == "Bullish" and prev["type"] == "O":
                            sl = prev["low"]
                        elif bias == "Bearish" and prev["type"] == "X":
                            sl = prev["high"]
                except Exception:
                    pass

            rows.append({
                "Script": symbol,
                "LTP": ltp,
                "Bias": bias,
                "Entry": p.get("entry_level", np.nan),
                "SL": sl,
                "Recommendation": rec,
            })
        except Exception:
            rows.append({
                "Script": symbol,
                "LTP": ltp,
                "Bias": "UNAVAILABLE",
                "Entry": np.nan,
                "SL": np.nan,
                "Recommendation": "DATA ERROR",
            })
        prog.progress(i / max(len(candidates), 1), text=f"MCX scan {i}/{max(len(candidates), 1)}")
    prog.empty()

    res = pd.DataFrame(rows)
    long_df = res[res["Recommendation"] == "🟢 BUY"].copy()
    short_df = res[res["Recommendation"] == "🔴 SELL"].copy()
    setup_df = res[res["Recommendation"] == "🟡 SETUP"].copy()

    st.markdown("## 🟢 BULLISH / LONG TRADES")
    if long_df.empty:
        st.info("No bullish trades currently.")
    else:
        st.dataframe(long_df[["Script","LTP","Bias","SL","Recommendation"]],
                     use_container_width=True, hide_index=True)

    st.markdown("## 🔴 BEARISH / SHORT TRADES")
    if short_df.empty:
        st.info("No bearish trades currently.")
    else:
        st.dataframe(short_df[["Script","LTP","Bias","SL","Recommendation"]],
                     use_container_width=True, hide_index=True)

    st.markdown("## 🟡 SETUPS FORMING")
    if setup_df.empty:
        st.info("No setups forming currently.")
    else:
        st.dataframe(setup_df[["Script","LTP","Bias","SL","Recommendation"]],
                     use_container_width=True, hide_index=True)

    st.markdown("## All Scanned")
    st.dataframe(res[["Script","LTP","Bias","SL","Recommendation"]],
                 use_container_width=True, hide_index=True)

else:
    st.title("Diagnostics")
    st.json({
        "client_code_present": bool(st.session_state.client_id),
        "access_token_present": bool(st.session_state.access_token),
        "master_rows": len(master),
        "nse_futures": len(future_universe(master, "NSE")),
        "mcx_futures": len(future_universe(master, "MCX")),
    })
    if st.session_state.api_log:
        st.dataframe(pd.DataFrame(st.session_state.api_log).tail(50),
                     use_container_width=True, hide_index=True)
    else:
        st.info("No API calls yet.")

st.caption(f"Last refresh: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')}")
