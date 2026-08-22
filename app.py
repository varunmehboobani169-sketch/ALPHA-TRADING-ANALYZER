
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="ALPHA ANALYZER", page_icon="α", layout="wide")

BASE = "https://api.dhan.co/v2"

# ----------------------------
# Session / credentials
# ----------------------------
if "alpha_client_id" not in st.session_state:
    st.session_state.alpha_client_id = ""
if "alpha_access_token" not in st.session_state:
    st.session_state.alpha_access_token = ""
if "api_log" not in st.session_state:
    st.session_state.api_log = []
if "last_error" not in st.session_state:
    st.session_state.last_error = ""

def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.getenv(name, default)

secret_client = secret("ALPHA_CLIENT_ID", "")

with st.sidebar:
    st.header("ALPHA ANALYZER")
    client = st.text_input(
        "Client code",
        value=st.session_state.alpha_client_id or secret_client,
        key="client_input"
    )
    if client.strip():
        st.session_state.alpha_client_id = client.strip()

    token = st.text_input(
        "Access token",
        value=st.session_state.alpha_access_token,
        type="password",
        key="token_input"
    )
    if token.strip():
        st.session_state.alpha_access_token = token.strip()

    st.divider()
    auto = st.checkbox("Auto refresh", True)
    refresh_min = st.selectbox("Refresh interval", [1, 2, 3, 5], index=2)
    scan_all_fno = st.checkbox("Scan ALL NSE F&O stocks", True)
    max_scan = st.slider("F&O/MCX scan size (when ALL is off)", 20, 640, 100, step=20)
    anchor_boxes = st.number_input("Minimum anchor boxes", 5, 30, 15)
    sector_threshold = st.slider("Super sector breadth %", 50, 90, 70)
    require_oi = st.checkbox("Require OI confirmation", True)

    if st.button("Clear login"):
        st.session_state.alpha_client_id = ""
        st.session_state.alpha_access_token = ""
        st.rerun()

    st.caption("Credentials are kept in this browser session. Do not put the access token in GitHub.")

# ----------------------------
# API
# ----------------------------
def headers():
    if not st.session_state.alpha_client_id:
        raise RuntimeError("Enter Client Code.")
    if not st.session_state.alpha_access_token:
        raise RuntimeError("Enter Access Token.")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": st.session_state.alpha_access_token,
        "client-id": st.session_state.alpha_client_id,
    }

def api_post(path, payload, label):
    r = requests.post(BASE + path, headers=headers(), json=payload, timeout=25)
    ok = r.ok
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    st.session_state.api_log.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "endpoint": path,
        "label": label,
        "status": r.status_code,
        "ok": ok,
    })
    if not ok:
        msg = body.get("remarks") if isinstance(body, dict) else None
        if not msg:
            msg = body.get("message") if isinstance(body, dict) else None
        raise RuntimeError(f"{path} HTTP {r.status_code}: {msg or str(body)[:400]}")
    return body

# ----------------------------
# Instrument master
# ----------------------------
@st.cache_data(ttl=21600, show_spinner=False)
def load_instruments():
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    df = pd.read_csv(url, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]

    # Dhan compact master column names.
    aliases = {
        "SEM_EXM_EXCH_ID": "exchange",
        "SEM_SEGMENT": "segment_code",
        "SEM_SMST_SECURITY_ID": "security_id",
        "SEM_SECURITY_ID": "security_id",
        "SEM_INSTRUMENT_NAME": "instrument",
        "SEM_EXPIRY_DATE": "expiry_date",
        "SEM_TRADING_SYMBOL": "trading_symbol",
        "SEM_CUSTOM_SYMBOL": "custom_symbol",
        "SM_SYMBOL_NAME": "symbol_name",
        "SEM_STRIKE_PRICE": "strike",
        "SEM_OPTION_TYPE": "option_type",
    }
    df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})

    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    for c in ["exchange", "instrument", "trading_symbol", "custom_symbol", "symbol_name"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.upper().str.strip()

    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")

    # Reliable underlying symbol:
    # RELIANCE-Aug2026-FUT -> RELIANCE
    # SILVER-04Sep2026-FUT -> SILVER
    if "trading_symbol" in df.columns:
        df["underlying_symbol"] = (
            df["trading_symbol"]
            .astype(str)
            .str.split("-", n=1)
            .str[0]
            .str.upper()
            .str.strip()
        )

    return df

def nearest_contracts(df, exchange, instrument):
    x = df.copy()
    if x.empty:
        return x
    x = x[(x["exchange"] == exchange) & (x["instrument"] == instrument)].copy()
    if x.empty:
        return x
    symcol = "underlying_symbol" if "underlying_symbol" in x.columns else "symbol_name"
    x[symcol] = x[symcol].astype(str).str.upper().str.strip()
    x = x.dropna(subset=["security_id"])
    if "expiry_date" in x.columns:
        x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")
        x = x.sort_values("expiry_date")
    rows = []
    for sym, g in x.groupby(symcol, dropna=False):
        g = g.dropna(subset=["expiry_date"]) if "expiry_date" in g.columns else g
        if not g.empty:
            rows.append(g.iloc[[0]])
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()

# ----------------------------
# Historical data
# ----------------------------
def candles_to_df(data):
    if not isinstance(data, dict):
        return pd.DataFrame()
    keys = ["open", "high", "low", "close", "volume", "timestamp", "open_interest"]
    lengths = [len(data[k]) for k in keys if isinstance(data.get(k), list)]
    n = max(lengths or [0])
    if n == 0:
        return pd.DataFrame()

    out = {}
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            out[k] = v + [np.nan] * (n - len(v))
        else:
            out[k] = [np.nan] * n
    df = pd.DataFrame(out)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    # Dhan historical response uses epoch timestamps; handle seconds or milliseconds defensively.
    unit = "ms" if df["timestamp"].dropna().median() > 10**12 else "s"
    df["datetime"] = pd.to_datetime(df["timestamp"], unit=unit, errors="coerce")
    # Treat returned timestamps as exchange timestamps for filtering/sorting.
    df = df.sort_values("datetime").dropna(subset=["close"]).reset_index(drop=True)
    return df

@st.cache_data(ttl=180, show_spinner=False)
def get_daily(sec_id, lookback=180):
    end = datetime.now().date()
    start = end - timedelta(days=lookback)
    payload = {
        "securityId": str(int(sec_id)),
        "exchangeSegment": "NSE_FNO",
        "instrument": "FUTSTK",
        "expiryCode": 0,
        "oi": True,
        "fromDate": str(start),
        "toDate": str(end + timedelta(days=1)),
    }
    return candles_to_df(api_post("/charts/historical", payload, "daily"))

@st.cache_data(ttl=180, show_spinner=False)
def get_intraday(sec_id, segment, instrument, days=5):
    now = datetime.now()
    start = now - timedelta(days=days)
    payload = {
        "securityId": str(int(sec_id)),
        "exchangeSegment": segment,
        "instrument": instrument,
        "interval": "1",
        "oi": True,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return candles_to_df(api_post("/charts/intraday", payload, "intraday"))

@st.cache_data(ttl=180, show_spinner=False)
def get_mcx_daily(sec_id):
    now = datetime.now().date()
    start = now - timedelta(days=180)
    payload = {
        "securityId": str(int(sec_id)),
        "exchangeSegment": "MCX_COMM",
        "instrument": "FUTCOM",
        "expiryCode": 0,
        "oi": True,
        "fromDate": str(start),
        "toDate": str(now + timedelta(days=1)),
    }
    return candles_to_df(api_post("/charts/historical", payload, "mcx_daily"))

# ----------------------------
# Improved Point & Figure engine
# ----------------------------
def _box_up(price, pct):
    return price * (1.0 + pct)

def _box_down(price, pct):
    return price * (1.0 - pct)

def pnf_build_columns(closes, box_pct=0.0025, reversal=3):
    """
    Close-only percentage P&F.

    Rules:
    - Uses only completed closes supplied by the caller.
    - A box is a multiplicative percentage move.
    - Reversal requires `reversal` boxes from the current extreme.
    - Keeps full column history so Anchor -> retracement -> DTB/DBS
      can be evaluated structurally.
    """
    prices = pd.Series(closes).dropna().astype(float)
    prices = prices[prices > 0].tolist()
    if len(prices) < 3:
        return []

    cols = []
    direction = None
    start_price = prices[0]
    high = start_price
    low = start_price
    boxes = 0
    last_extreme = start_price

    def close_column():
        if direction is not None and boxes > 0:
            cols.append({
                "type": direction,
                "boxes": int(boxes),
                "high": float(high),
                "low": float(low),
            })

    for price in prices[1:]:
        if direction is None:
            up_level = _box_up(start_price, box_pct)
            down_level = _box_down(start_price, box_pct)

            if price >= up_level:
                direction = "X"
                high = start_price
                low = start_price
                boxes = 0
                while price >= _box_up(high, box_pct):
                    high = _box_up(high, box_pct)
                    boxes += 1
                last_extreme = high
            elif price <= down_level:
                direction = "O"
                high = start_price
                low = start_price
                boxes = 0
                while price <= _box_down(low, box_pct):
                    low = _box_down(low, box_pct)
                    boxes += 1
                last_extreme = low
            continue

        if direction == "X":
            while price >= _box_up(high, box_pct):
                high = _box_up(high, box_pct)
                boxes += 1
            last_extreme = high

            reversal_level = high * ((1.0 - box_pct) ** reversal)
            if price <= reversal_level:
                close_column()
                direction = "O"
                # Start the new O column from the X extreme and fill the
                # confirmed reversal distance.
                new_low = high
                new_boxes = 0
                while price <= _box_down(new_low, box_pct):
                    new_low = _box_down(new_low, box_pct)
                    new_boxes += 1
                low = new_low
                high = high
                boxes = max(reversal, new_boxes)
                last_extreme = low

        else:
            while price <= _box_down(low, box_pct):
                low = _box_down(low, box_pct)
                boxes += 1
            last_extreme = low

            reversal_level = low * ((1.0 + box_pct) ** reversal)
            if price >= reversal_level:
                close_column()
                direction = "X"
                new_high = low
                new_boxes = 0
                while price >= _box_up(new_high, box_pct):
                    new_high = _box_up(new_high, box_pct)
                    new_boxes += 1
                high = new_high
                low = low
                boxes = max(reversal, new_boxes)
                last_extreme = high

    if direction is not None and boxes > 0:
        cols.append({
            "type": direction,
            "boxes": int(boxes),
            "high": float(high),
            "low": float(low),
        })
    return cols


def pnf_analysis(df, box_pct, anchor_min=15, reversal=3):
    """
    Improved P&F signal definition:

    LONG:
      1. An earlier X-column is an Anchor (>= anchor_min boxes).
      2. That anchor is followed by at least one O-column.
      3. The current X-column breaks above the immediately prior X-column high.
      4. The current signal must be the latest completed X column.
      5. Structural SL = latest completed O-column low.

    SHORT:
      1. An earlier O-column is an Anchor (>= anchor_min boxes).
      2. That anchor is followed by at least one X-column.
      3. The current O-column breaks below the immediately prior O-column low.
      4. The current signal must be the latest completed O column.
      5. Structural SL = latest completed X-column high.

    This prevents a generic "bullish column" from being treated as a DTB.
    """
    empty = {
        "bias": "NO DATA", "anchor": False, "dtb": False, "dbs": False,
        "signal_side": None, "reason": "Insufficient price history",
        "sl": np.nan, "columns": 0, "anchor_boxes": 0,
        "signal_price": np.nan, "pattern": "—"
    }

    if df is None or df.empty or "close" not in df.columns:
        return empty.copy()

    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(closes) < 10:
        return empty.copy()

    cols = pnf_build_columns(closes, box_pct, reversal)
    out = empty.copy()
    out["columns"] = len(cols)
    if not cols:
        out["bias"] = "NO P&F"
        out["reason"] = "Could not build P&F columns"
        return out

    cur = cols[-1]
    out["bias"] = "Bullish" if cur["type"] == "X" else "Bearish"
    out["signal_price"] = float(closes.iloc[-1])

    # Need at least 4 columns for a clean anchor -> opposite -> signal structure.
    if len(cols) < 3:
        out["reason"] = f"P&F built ({len(cols)} columns); waiting for anchor/retest structure"
        return out

    if cur["type"] == "X":
        # The previous completed X column immediately before the retracement.
        prev_x_idx = None
        for i in range(len(cols) - 2, -1, -1):
            if cols[i]["type"] == "X":
                prev_x_idx = i
                break

        if prev_x_idx is None:
            out["reason"] = "No previous X-column"
            return out

        # Anchor must be earlier than the previous X column.
        anchor_idx = None
        for i in range(0, prev_x_idx):
            if cols[i]["type"] == "X" and cols[i]["boxes"] >= anchor_min:
                anchor_idx = i
        if anchor_idx is None:
            out["reason"] = f"No earlier X Anchor >= {anchor_min} boxes"
            return out

        # There must have been an O-column after the anchor.
        has_retrace = any(cols[j]["type"] == "O" for j in range(anchor_idx + 1, len(cols) - 1))
        out["anchor"] = True
        out["anchor_boxes"] = cols[anchor_idx]["boxes"]

        if not has_retrace:
            out["reason"] = f"Anchor found ({cols[anchor_idx]['boxes']} boxes); no O retracement yet"
            return out

        if cur["high"] > cols[prev_x_idx]["high"]:
            out["dtb"] = True
            out["signal_side"] = "LONG"
            out["pattern"] = "DTB"
            out["reason"] = (
                f"DTB after {cols[anchor_idx]['boxes']}-box X Anchor; "
                f"breaks prior X high"
            )
            # SL = latest completed O column before current X.
            latest_o = next((cols[j] for j in range(len(cols)-2, -1, -1)
                             if cols[j]["type"] == "O"), None)
            if latest_o:
                out["sl"] = latest_o["low"]
        else:
            out["reason"] = (
                f"Anchor {cols[anchor_idx]['boxes']} boxes; "
                f"current X has not broken prior X high"
            )

    else:
        prev_o_idx = None
        for i in range(len(cols) - 2, -1, -1):
            if cols[i]["type"] == "O":
                prev_o_idx = i
                break

        if prev_o_idx is None:
            out["reason"] = "No previous O-column"
            return out

        anchor_idx = None
        for i in range(0, prev_o_idx):
            if cols[i]["type"] == "O" and cols[i]["boxes"] >= anchor_min:
                anchor_idx = i
        if anchor_idx is None:
            out["reason"] = f"No earlier O Anchor >= {anchor_min} boxes"
            return out

        has_retrace = any(cols[j]["type"] == "X" for j in range(anchor_idx + 1, len(cols) - 1))
        out["anchor"] = True
        out["anchor_boxes"] = cols[anchor_idx]["boxes"]

        if not has_retrace:
            out["reason"] = f"Anchor found ({cols[anchor_idx]['boxes']} boxes); no X retracement yet"
            return out

        if cur["low"] < cols[prev_o_idx]["low"]:
            out["dbs"] = True
            out["signal_side"] = "SHORT"
            out["pattern"] = "DBS"
            out["reason"] = (
                f"DBS after {cols[anchor_idx]['boxes']}-box O Anchor; "
                f"breaks prior O low"
            )
            latest_x = next((cols[j] for j in range(len(cols)-2, -1, -1)
                             if cols[j]["type"] == "X"), None)
            if latest_x:
                out["sl"] = latest_x["high"]
        else:
            out["reason"] = (
                f"Anchor {cols[anchor_idx]['boxes']} boxes; "
                f"current O has not broken prior O low"
            )

    return out

# ----------------------------
# OI / sector
# ----------------------------
def oi_state(df):
    if df.empty or len(df) < 2 or "open_interest" not in df.columns:
        return "UNAVAILABLE", np.nan, np.nan
    x = df.dropna(subset=["close"]).copy()
    if len(x) < 2:
        return "UNAVAILABLE", np.nan, np.nan
    a, b = x.iloc[-2], x.iloc[-1]
    pchg = (float(b.close) / float(a.close) - 1) * 100 if float(a.close) else np.nan
    oi_a = pd.to_numeric(a.open_interest, errors="coerce")
    oi_b = pd.to_numeric(b.open_interest, errors="coerce")
    if pd.isna(oi_a) or pd.isna(oi_b):
        return "UNAVAILABLE", pchg, np.nan
    d_oi = float(oi_b - oi_a)
    if pchg > 0 and d_oi > 0:
        return "LONG BUILDUP", pchg, d_oi
    if pchg < 0 and d_oi > 0:
        return "SHORT BUILDUP", pchg, d_oi
    if pchg > 0 and d_oi < 0:
        return "SHORT COVERING", pchg, d_oi
    if pchg < 0 and d_oi < 0:
        return "LONG UNWINDING", pchg, d_oi
    return "NEUTRAL", pchg, d_oi

def system_result(pnf, oi, require_oi=True):
    # Both long and short are supported for the scanner.
    if pnf["signal_side"] == "LONG":
        if not require_oi:
            return "🟢 BUY", True
        if oi == "LONG BUILDUP":
            return "🟢 BUY", True
        return "WAIT - OI", False
    if pnf["signal_side"] == "SHORT":
        if not require_oi:
            return "🔴 SELL", True
        if oi == "SHORT BUILDUP":
            return "🔴 SELL", True
        return "WAIT - OI", False
    if pnf["bias"] == "NO DATA":
        return "DATA ERROR", False
    if not pnf["anchor"]:
        return "WAIT - NO ANCHOR", False
    if pnf["bias"] == "Bullish" and not pnf["dtb"]:
        return "WAIT - NO DTB", False
    if pnf["bias"] == "Bearish" and not pnf["dbs"]:
        return "WAIT - NO DBS", False
    return "WAIT", False

SECTOR_MAP = {
    "RELIANCE":"Energy","ONGC":"Energy","COALINDIA":"Energy","IOC":"Energy","BPCL":"Energy","GAIL":"Energy",
    "POWERGRID":"Utilities","NTPC":"Utilities",
    "HDFCBANK":"Banking","ICICIBANK":"Banking","SBIN":"Banking","AXISBANK":"Banking","KOTAKBANK":"Banking",
    "INDUSINDBK":"Banking","BANKBARODA":"Banking","PNB":"Banking","IDFCFIRSTB":"Banking","FEDERALBNK":"Banking",
    "BAJFINANCE":"Financials","BAJAJFINSV":"Financials","SHRIRAMFIN":"Financials","CHOLAFIN":"Financials",
    "MUTHOOTFIN":"Financials","SBICARD":"Financials",
    "TCS":"IT","INFY":"IT","HCLTECH":"IT","WIPRO":"IT","TECHM":"IT","LTIM":"IT","MPHASIS":"IT","COFORGE":"IT",
    "MARUTI":"Auto","M&M":"Auto","TATAMOTORS":"Auto","HEROMOTOCO":"Auto","EICHERMOT":"Auto","BAJAJ-AUTO":"Auto","TVSMOTOR":"Auto","ASHOKLEY":"Auto",
    "TATASTEEL":"Metals","JSWSTEEL":"Metals","HINDALCO":"Metals","SAIL":"Metals","JINDALSTEL":"Metals","NATIONALUM":"Metals","VEDL":"Metals",
    "SUNPHARMA":"Pharma","CIPLA":"Pharma","DRREDDY":"Pharma","DIVISLAB":"Pharma","APOLLOHOSP":"Pharma","LUPIN":"Pharma","AUROPHARMA":"Pharma","TORNTPHARM":"Pharma",
    "ITC":"FMCG","HINDUNILVR":"FMCG","NESTLEIND":"FMCG","BRITANNIA":"FMCG","TATACONSUM":"FMCG","DABUR":"FMCG","MARICO":"FMCG","COLPAL":"FMCG",
    "LT":"Capital Goods","BEL":"Defence/Industrial","HAL":"Defence/Industrial","BHEL":"Capital Goods","SIEMENS":"Capital Goods","ABB":"Capital Goods","CUMMINSIND":"Capital Goods",
    "DLF":"Realty","GODREJPROP":"Realty","OBEROIRLTY":"Realty","LODHA":"Realty","PRESTIGE":"Realty","PHOENIXLTD":"Realty",
    "TRENT":"Consumer","TITAN":"Consumer","DMART":"Consumer","KALYANKJIL":"Consumer","JUBLFOOD":"Consumer",
    "BHARTIARTL":"Telecom","INDUSTOWER":"Telecom","IDEA":"Telecom",
    "ADANIENT":"Conglomerate","ADANIPORTS":"Infrastructure","IRCTC":"Travel/Infra","INDIGO":"Aviation","DELHIVERY":"Logistics",
}
def sector_of(s): return SECTOR_MAP.get(s, "Other/Unmapped")

def sector_breadth(rows):
    x = pd.DataFrame(rows)
    if x.empty: return x
    y = x[x["Sector"] != "Other/Unmapped"].copy()
    if y.empty: return pd.DataFrame()
    agg = y.groupby("Sector").agg(
        stocks=("Symbol","count"),
        bullish=("Bias", lambda s: (s=="Bullish").sum()),
        bearish=("Bias", lambda s: (s=="Bearish").sum()),
        dtb=("DTB", lambda s: (s=="✅").sum()),
        dbs=("DBS", lambda s: (s=="✅").sum()),
    ).reset_index()
    agg["Bullish %"] = 100 * agg["bullish"] / agg["stocks"]
    agg["Bearish %"] = 100 * agg["bearish"] / agg["stocks"]
    return agg.sort_values("Bullish %", ascending=False)

def stock_star(sector, breadth, threshold):
    if breadth.empty or sector == "Other/Unmapped":
        return ""
    r = breadth[breadth["Sector"] == sector]
    if r.empty:
        return ""
    r = r.iloc[0]
    if r["Bullish %"] >= threshold or r["Bearish %"] >= threshold:
        return "⭐"
    return ""

# ----------------------------
# Universe helpers
# ----------------------------
def nearest_fno(df, exchange="NSE"):
    inst = "FUTSTK" if exchange == "NSE" else "FUTCOM"
    ex = "NSE" if exchange == "NSE" else "MCX"
    x = df[(df["exchange"] == ex) & (df["instrument"] == inst)].copy()
    if x.empty:
        return x

    x = x.dropna(subset=["security_id"])
    if "expiry_date" in x.columns:
        now = pd.Timestamp.now()
        x = x[(x["expiry_date"].isna()) | (x["expiry_date"] >= now)]
        x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")

    sym = "underlying_symbol"
    if sym not in x.columns:
        return pd.DataFrame()

    # Keep nearest active contract per underlying.
    x[sym] = x[sym].astype(str).str.upper().str.strip()
    rows = []
    for symbol, g in x.groupby(sym, dropna=False):
        if symbol in ("", "NAN", "NONE"):
            continue
        g = g.sort_values("expiry_date", na_position="last")
        rows.append(g.iloc[0])
    return pd.DataFrame(rows).reset_index(drop=True) if rows else pd.DataFrame()

def choose_rows(x, n):
    if x.empty: return x
    return x.head(n).reset_index(drop=True)

# ----------------------------
# Auto refresh
# ----------------------------
if auto:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=refresh_min*60*1000, key="alpha_refresh")
    except Exception:
        pass

# ----------------------------
# Main page
# ----------------------------
st.title("ALPHA ANALYZER")
st.caption("P&F + sector breadth + OI confirmation | close-only signals")

if not st.session_state.alpha_client_id or not st.session_state.alpha_access_token:
    st.warning("Enter Client Code and Access Token in the sidebar to load live data.")
    st.stop()

try:
    instruments = load_instruments()
    st.success(f"Instrument master loaded: {len(instruments):,} rows")
except Exception as e:
    st.error(f"Could not load the instrument master: {e}")
    st.stop()

nse_count = len(nearest_fno(instruments, "NSE"))
mcx_count = len(nearest_fno(instruments, "MCX"))
st.caption(f"Active futures found: NSE {nse_count} | MCX {mcx_count} | Last app refresh: {datetime.now().strftime('%H:%M:%S')}")

mode = st.radio(
    "Dashboard",
    ["NSE P&F", "Sector Breadth", "MCX", "Diagnostics"],
    horizontal=True
)

# ----------------------------
# NSE P&F
# ----------------------------
if mode == "NSE P&F":
    st.subheader("NSE P&F Trading System")
    sys_mode = st.radio("Trading mode", ["Positional", "Intraday"], horizontal=True)
    box = 0.0025 if sys_mode == "Positional" else 0.0015
    st.info(
        f"{sys_mode}: {box*100:.2f}% box | 3-box reversal | Anchor ≥ {anchor_boxes} boxes | "
        f"{'daily closes' if sys_mode=='Positional' else '1-minute closes'} only."
    )

    uni = nearest_fno(instruments, "NSE")
    if uni.empty:
        st.error("No NSE FUTSTK contracts were found in the Dhan instrument master.")
        st.stop()

    sym = "underlying_symbol"
    uni[sym] = uni[sym].astype(str).str.upper().str.strip()
    selected = uni.sort_values(sym).reset_index(drop=True) if scan_all_fno else choose_rows(uni.sort_values(sym), max_scan)

    progress = st.progress(0, text=f"Scanning {len(selected)} NSE F&O stocks...")
    rows = []
    for i, (_, r) in enumerate(selected.iterrows(), 1):
        symbol = str(r[sym])
        try:
            hist = get_daily(r.security_id) if sys_mode == "Positional" else get_intraday(r.security_id, "NSE_FNO", "FUTSTK")
            # Remove the current incomplete 1-min bar.
            if sys_mode == "Intraday" and not hist.empty:
                now = pd.Timestamp.now()
                hist = hist[hist["datetime"] < now.floor("min")].copy()
            pnf = pnf_analysis(hist, box, int(anchor_boxes))
            oi, pchg, doichg = oi_state(hist)
            status, qualified = system_result(pnf, oi, require_oi=require_oi)
            rows.append({
                "Symbol": symbol,
                "Sector": sector_of(symbol),
                "Bias": pnf["bias"],
                "Anchor": "✅" if pnf["anchor"] else "❌",
                "DTB": "✅" if pnf["dtb"] else "❌",
                "DBS": "✅" if pnf["dbs"] else "❌",
                "OI": oi,
                "Price Δ%": pchg,
                "OI Δ": doichg,
                "System": status,
                "Pattern": pnf.get("pattern", "—"),
                "Anchor Boxes": pnf.get("anchor_boxes", 0),
                "SL": pnf["sl"],
                "Reason": pnf["reason"],
            })
        except Exception as e:
            rows.append({
                "Symbol": symbol, "Sector": sector_of(symbol), "Bias":"ERROR",
                "Anchor":"❌","DTB":"❌","DBS":"❌","OI":"ERROR",
                "Price Δ%":np.nan,"OI Δ":np.nan,"System":"DATA ERROR","Pattern":"—","Anchor Boxes":0,"SL":np.nan,
                "Reason":str(e)[:250]
            })
        progress.progress(i/len(selected), text=f"Scanning NSE F&O: {i}/{len(selected)}")
    progress.empty()

    res = pd.DataFrame(rows)
    breadth = sector_breadth(res)
    if not breadth.empty:
        res["⭐"] = res["Sector"].map(lambda s: stock_star(s, breadth, sector_threshold))
    else:
        res["⭐"] = ""

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("F&O stocks scanned", len(res))
    c2.metric("Bullish P&F", int((res["Bias"]=="Bullish").sum()))
    c3.metric("Bearish P&F", int((res["Bias"]=="Bearish").sum()))
    c4.metric("Data errors", int((res["System"]=="DATA ERROR").sum()))

    bullish = res[res["Bias"]=="Bullish"].copy()
    bearish = res[res["Bias"]=="Bearish"].copy()

    # Only bullish and bearish names are shown in the scanner.
    # Neutral / no-P&F / data-error rows remain available through Diagnostics.
    bullish = bullish.sort_values(
        by=["DTB","Anchor Boxes","⭐","Symbol"],
        ascending=[False, False, False, True]
    )
    bearish = bearish.sort_values(
        by=["DBS","Anchor Boxes","⭐","Symbol"],
        ascending=[False, False, False, True]
    )

    st.subheader("NSE F&O P&F Scanner")
    st.caption("ALL available NSE F&O futures are scanned. Only bullish and bearish P&F stocks are displayed below.")

    left, right = st.columns(2)
    with left:
        st.markdown("### 🟢 BULLISH STOCKS")
        if bullish.empty:
            st.info("No bullish P&F stocks currently detected.")
        else:
            st.dataframe(
                bullish[["⭐","Symbol","Sector","Pattern","Anchor Boxes","Anchor","DTB","OI","System","SL","Reason"]],
                use_container_width=True, hide_index=True
            )

    with right:
        st.markdown("### 🔴 BEARISH STOCKS")
        if bearish.empty:
            st.info("No bearish P&F stocks currently detected.")
        else:
            st.dataframe(
                bearish[["⭐","Symbol","Sector","Pattern","Anchor Boxes","Anchor","DBS","OI","System","SL","Reason"]],
                use_container_width=True, hide_index=True
            )

    st.markdown("#### Trade-ready setups")
    ready_left, ready_right = st.columns(2)
    with ready_left:
        buys = bullish[bullish["System"]=="🟢 BUY"]
        st.metric("🟢 BUY setups", len(buys))
        if not buys.empty:
            st.dataframe(
                buys[["⭐","Symbol","Sector","Pattern","Anchor Boxes","OI","System","SL"]],
                use_container_width=True, hide_index=True
            )
    with ready_right:
        sells = bearish[bearish["System"]=="🔴 SELL"]
        st.metric("🔴 SELL setups", len(sells))
        if not sells.empty:
            st.dataframe(
                sells[["⭐","Symbol","Sector","Pattern","Anchor Boxes","OI","System","SL"]],
                use_container_width=True, hide_index=True
            )

# ----------------------------
# Sector breadth
# ----------------------------
elif mode == "Sector Breadth":
    st.subheader("P&F Sector Breadth")
    sys_mode = st.radio("Breadth mode", ["Positional", "Intraday"], horizontal=True)
    box = 0.0025 if sys_mode=="Positional" else 0.0015
    uni = nearest_fno(instruments, "NSE")
    sym = "underlying_symbol" if "underlying_symbol" in uni.columns else "symbol_name"
    selected = choose_rows(uni.sort_values(sym), max_scan)
    rows=[]
    for _,r in selected.iterrows():
        try:
            h = get_daily(r.security_id) if sys_mode=="Positional" else get_intraday(r.security_id, "NSE_FNO", "FUTSTK")
            if sys_mode=="Intraday" and not h.empty:
                h = h[h["datetime"] < pd.Timestamp.now().floor("min")]
            p = pnf_analysis(h, box, int(anchor_boxes))
            symbol=str(r[sym])
            rows.append({"Symbol":symbol,"Sector":sector_of(symbol),"Bias":p["bias"],
                         "DTB":"✅" if p["dtb"] else "❌","DBS":"✅" if p["dbs"] else "❌"})
        except Exception:
            pass
    br=sector_breadth(pd.DataFrame(rows))
    if br.empty:
        st.warning("No sector breadth data returned. Open Diagnostics.")
    else:
        br["⭐ Sector"] = np.where((br["Bullish %"]>=sector_threshold)|(br["Bearish %"]>=sector_threshold),"⭐","")
        st.dataframe(br[["⭐ Sector","Sector","stocks","bullish","Bullish %","bearish","Bearish %","dtb","dbs"]],
                     use_container_width=True, hide_index=True)

# ----------------------------
# MCX
# ----------------------------
elif mode == "MCX":
    st.subheader("MCX P&F Trading System")
    st.caption("Daily 0.25% P&F = direction filter | Intraday 0.15% P&F = entry | OI = secondary confirmation")

    uni = nearest_fno(instruments, "MCX")
    if uni.empty:
        st.error("No MCX FUTCOM contracts were found in the Dhan instrument master.")
        st.stop()

    sym = "underlying_symbol"
    names = sorted(uni[sym].astype(str).unique())
    chosen = st.selectbox("Commodity", names)

    rr = uni[uni[sym].astype(str) == chosen].iloc[0]
    try:
        daily = get_mcx_daily(rr.security_id)
        intra = get_intraday(rr.security_id, "MCX_COMM", "FUTCOM")
        if not intra.empty:
            intra = intra[intra["datetime"] < pd.Timestamp.now().floor("min")]

        dp = pnf_analysis(daily, 0.0025, int(anchor_boxes))
        ip = pnf_analysis(intra, 0.0015, int(anchor_boxes))

        oi, pchg, doichg = oi_state(intra)

        if ip["signal_side"] == "LONG" and dp["bias"] == "Bullish":
            system = "🟢 LONG"
        elif ip["signal_side"] == "SHORT" and dp["bias"] == "Bearish":
            system = "🔴 SHORT"
        elif ip["signal_side"] in ("LONG","SHORT"):
            system = "WAIT - DAILY FILTER"
        else:
            system = "WAIT - NO INTRADAY SETUP"

        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Daily P&F", dp["bias"])
        c2.metric("Intraday P&F", ip["bias"])
        c3.metric("Intraday setup", ip["signal_side"] or "—")
        c4.metric("System", system)
        c5.metric("Initial SL", f"{ip['sl']:.2f}" if pd.notna(ip["sl"]) else "—")

        st.write(f"**Daily reason:** {dp['reason']}")
        st.write(f"**Intraday reason:** {ip['reason']}")
        st.write(f"**Secondary OI:** {oi} | Price Δ {pchg:.2f}% | OI Δ {doichg:.0f}" if pd.notna(pchg) and pd.notna(doichg) else "**Secondary OI:** unavailable")

        if system.startswith("🟢"):
            st.success(f"{system} — daily trend agrees with intraday DTB.")
        elif system.startswith("🔴"):
            st.error(f"{system} — daily trend agrees with intraday DBS.")
        else:
            st.info(system)

        st.markdown("**Exit:** opposite 0.15% intraday P&F reversal signal.")
        st.markdown("**Initial SL:** previous confirmed opposite P&F column extreme at entry.")

        with st.expander("Raw candle/data counts"):
            st.write({"daily_rows": len(daily), "intraday_rows": len(intra)})
            st.dataframe(intra.tail(10), use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"MCX analysis error: {e}")

# ----------------------------
# Diagnostics
# ----------------------------
else:
    st.subheader("Diagnostics")
    st.write("Use this page when any scan is empty. It shows exactly where data stops.")
    st.write({
        "client_code_present": bool(st.session_state.alpha_client_id),
        "access_token_present": bool(st.session_state.alpha_access_token),
        "instrument_rows": len(instruments),
        "NSE FUTSTK": int(((instruments["exchange"]=="NSE") & (instruments["instrument"]=="FUTSTK")).sum()),
        "MCX FUTCOM": int(((instruments["exchange"]=="MCX") & (instruments["instrument"]=="FUTCOM")).sum()),
    })

    if not st.session_state.api_log:
        st.info("No API call logged yet. Open NSE P&F or MCX to run a test.")
    else:
        st.dataframe(pd.DataFrame(st.session_state.api_log).tail(50), use_container_width=True, hide_index=True)

    with st.expander("Instrument master columns"):
        st.write(list(instruments.columns))

    with st.expander("Sample NSE futures"):
        nse = nearest_fno(instruments, "NSE")
        st.dataframe(nse.head(20), use_container_width=True, hide_index=True)

    with st.expander("Sample MCX futures"):
        mcx = nearest_fno(instruments, "MCX")
        st.dataframe(mcx.head(20), use_container_width=True, hide_index=True)


    st.markdown("### Quick API test")
    nse_u = nearest_fno(instruments, "NSE")
    mcx_u = nearest_fno(instruments, "MCX")
    tc1, tc2 = st.columns(2)
    with tc1:
        if not nse_u.empty:
            syms = nse_u["underlying_symbol"].astype(str).tolist()
            test_sym = st.selectbox("Test NSE future", syms, key="test_nse_symbol")
            if st.button("Test NSE daily data"):
                rr = nse_u[nse_u["underlying_symbol"] == test_sym].iloc[0]
                try:
                    test_df = get_daily(rr["security_id"], lookback=30)
                    st.success(f"{test_sym}: {len(test_df)} daily rows returned")
                    st.dataframe(test_df.tail(10), use_container_width=True, hide_index=True)
                except Exception as e:
                    st.error(str(e))
                    st.exception(e)
    with tc2:
        if not mcx_u.empty:
            syms = mcx_u["underlying_symbol"].astype(str).tolist()
            test_sym = st.selectbox("Test MCX future", syms, key="test_mcx_symbol")
            if st.button("Test MCX daily data"):
                rr = mcx_u[mcx_u["underlying_symbol"] == test_sym].iloc[0]
                try:
                    test_df = get_mcx_daily(rr["security_id"])
                    st.success(f"{test_sym}: {len(test_df)} daily rows returned")
                    st.dataframe(test_df.tail(10), use_container_width=True, hide_index=True)
                except Exception as e:
                    st.error(str(e))
                    st.exception(e)

    if st.session_state.last_error:
        st.error(st.session_state.last_error)

st.divider()
st.caption(
    f"Page refresh: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')} | "
    f"API calls in this browser session: {len(st.session_state.api_log)} | "
    f"NSE scan mode: {'ALL F&O' if scan_all_fno else f'{max_scan} stocks'}"
)
