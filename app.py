
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
            "Option Seller",
            "Intraday",
            "Positional",
            "MCX Futures",
            "Market Overview",
        ],
    )

render_notification_panel()

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
# Notification Panel
# -----------------------------
def record_trade_notifications(module_name, mode, symbols):
    """
    Store the latest new-trade timestamps for the client notification panel.
    Keeps a small rolling history per module/mode.
    """
    now_text = datetime.now().strftime("%d-%b-%Y %H:%M:%S")

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
        key=lambda x: x["time"],
        reverse=True,
    )[:10]

    with st.sidebar:
        st.markdown("### 🔔 Notifications")

        if not history:
            st.caption("No trades yet.")
            return

        for item in history[:5]:
            st.markdown(
                f"**{item['symbol']}**  \n"
                f"{item['module']} • {item['mode']}  \n"
                f"`{item['time']}`"
            )
            st.divider()


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
        if page == "Intraday":
            mins = 1
        elif page == "Option Seller":
            mins = 1 if st.session_state.get("option_horizon", "Intraday") == "Intraday" else 3
        elif page == "MCX Futures":
            mins = 1 if st.session_state.get("mcx_futures_mode", "Intraday") == "Intraday" else 15
        else:
            mins = 15
        if page == "Market Overview":
            mins = 3
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

if page == "Market Overview":
    st.title("MARKET OVERVIEW")
    st.caption("Current directional bias")

    def market_bias_from_history(sec_id, segment, instrument):
        try:
            h = historical(sec_id, segment, instrument, "Positional")
            if h.empty:
                return "UNAVAILABLE"

            cols = build_pnf(h["close"], 0.0025, 3)
            if not cols:
                return "SIDEWAYS"

            last = cols[-1]

            if last["type"] == "X":
                return "BULLISH"
            if last["type"] == "O":
                return "BEARISH"
            return "SIDEWAYS"

        except Exception:
            return "UNAVAILABLE"

    overview = []

    for name, sid in [("NIFTY", 13), ("BANKNIFTY", 25)]:
        overview.append({
            "Market": name,
            "Bias": market_bias_from_history(
                sid,
                "NSE_IDX",
                "INDEX",
            ),
        })

    mcx = mcx_futures_universe(master)

    for commodity in ["GOLD", "SILVER", "CRUDEOIL"]:
        match = mcx[
            mcx["underlying_symbol"] == commodity
        ]

        if match.empty:
            bias = "UNAVAILABLE"
        else:
            bias = market_bias_from_history(
                int(match.iloc[0]["security_id"]),
                "MCX_COMM",
                "FUTCOM",
            )

        overview.append({
            "Market": commodity,
            "Bias": bias,
        })

    cols = st.columns(5)

    for col, item in zip(cols, overview):
        bias = item["Bias"]

        if bias == "BULLISH":
            icon = "🟢"
        elif bias == "BEARISH":
            icon = "🔴"
        elif bias == "SIDEWAYS":
            icon = "🟡"
        else:
            icon = "⚪"

        col.metric(
            item["Market"],
            f"{icon} {bias}",
        )

    st.caption(
        "Display only. No trade generation or OI confirmation runs here."
    )


elif page in ("Intraday", "Positional"):
    mode = page
    st.title(mode)
    st.caption("Live trade monitor.")

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

        try:
            oi_conf = {
                "label": "—",
                "state": "—",
                "oi_change_pct": np.nan,
                "price_change_pct": np.nan,
                "rank": 0,
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
                "Bias": bias,
                "Pattern": p.get("pattern", "—") if mode == "Positional" else "—",
                "OI Confirmation": oi_conf["label"],
                "OI Δ%": oi_conf["oi_change_pct"],
                "Entry": entry,
                "SL": sl,
                "Recommendation": rec,
                "_OI Rank": oi_conf["rank"],
            })

        except Exception:
            rows.append({
                "Script": symbol,
                "LTP": ltp,
                "Bias": "UNAVAILABLE",
                "Pattern": "—",
                "OI Confirmation": "DATA ERROR",
                "OI Δ%": np.nan,
                "Entry": np.nan,
                "SL": np.nan,
                "Recommendation": "DATA ERROR",
                "_OI Rank": 0,
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

    if mode == "Intraday":
        notify_new_trades(
            "NSE",
            "Intraday",
            res.loc[
                res["Recommendation"].isin(["🟢 BUY", "🔴 SELL"]),
                "Script",
            ].tolist(),
        )
    else:
        notify_new_trades(
            "NSE",
            "Positional",
            res.loc[
                res["Recommendation"].isin(["🟢 LONG", "🔴 SHORT"]),
                "Script",
            ].tolist(),
        )

    if mode == "Intraday":
        long_df = res[res["Recommendation"] == "🟢 BUY"].copy()
        short_df = res[res["Recommendation"] == "🔴 SELL"].copy()
        setup_df = res[res["Recommendation"] == "🟡 SETUP"].copy()
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
        st.caption("★ marks a higher-conviction positional trade.")

    st.markdown("## 🟢 BULLISH / LONG")
    if long_df.empty:
        st.info("No active bullish positions currently.")
    else:
        st.dataframe(
            long_df[
                ["Script", "LTP", "Bias", "Entry", "SL", "Recommendation"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("## 🔴 BEARISH / SHORT")
    if short_df.empty:
        st.info("No active bearish positions currently.")
    else:
        st.dataframe(
            short_df[
                ["Script", "LTP", "Bias", "Entry", "SL", "Recommendation"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("## ⚪ OTHER")
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



elif page == "MCX Futures":
    st.title("MCX FUTURES")
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
            "Bias":bias,
            "Entry":entry,
            "SL":sl,
            "Recommendation":rec,
        })

    res=pd.DataFrame(rows)

    if mode=="Intraday":
        notify_new_trades(
            "MCX",
            "Intraday",
            res.loc[
                res["Recommendation"].isin(["🟢 BUY", "🔴 SELL"]),
                "Script",
            ].tolist(),
        )
    else:
        notify_new_trades(
            "MCX",
            "Positional",
            res.loc[
                res["Recommendation"].isin(["🟢 LONG", "🔴 SHORT"]),
                "Script",
            ].tolist(),
        )

    long_rec="🟢 LONG" if mode=="Positional" else "🟢 BUY"
    short_rec="🔴 SHORT" if mode=="Positional" else "🔴 SELL"

    long_df=res[res["Recommendation"]==long_rec].copy()
    short_df=res[res["Recommendation"]==short_rec].copy()

    def mcx_style(row):
        rec=str(row["Recommendation"])
        if rec in ("🟢 LONG","🟢 BUY"):
            return ["background-color:#d9f2d9;color:#0b5d1e;font-weight:700"]*len(row)
        if rec in ("🔴 SHORT","🔴 SELL"):
            return ["background-color:#f8d7da;color:#8a1c1c;font-weight:700"]*len(row)
        return [""]*len(row)

    st.markdown("## 🟢 BULLISH / LONG")
    if long_df.empty:
        st.info("No bullish/long MCX trades currently.")
    else:
        st.dataframe(
            long_df[["Script","LTP","Bias","Entry","SL","Recommendation"]].style.apply(mcx_style,axis=1),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("## 🔴 BEARISH / SHORT")
    if short_df.empty:
        st.info("No bearish/short MCX trades currently.")
    else:
        st.dataframe(
            short_df[["Script","LTP","Bias","Entry","SL","Recommendation"]].style.apply(mcx_style,axis=1),
            use_container_width=True,
            hide_index=True,
        )


elif page == "Option Seller":
    st.title("OPTION SELLER")
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


    except Exception as exc:
        st.error("Option data is currently unavailable.")
        with st.expander("Data status"):
            st.code(str(exc), language="text")


else:
    st.title("System Status")
    st.info("System is running.")

st.caption(f"Last refresh: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')}")
