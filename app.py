
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
    anchor_boxes = st.number_input("Anchor minimum", 5, 30, 15, help="New pattern requires strictly MORE than this many boxes; default = >15.")
    max_pullback_boxes = 5
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
    # Use Dhan's detailed master so derivative rows carry UNDERLYING_SECURITY_ID.
    url = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    df = pd.read_csv(url, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]

    aliases = {
        "EXCH_ID": "exchange",
        "SEGMENT": "segment_code",
        "ISIN": "isin",
        "INSTRUMENT": "instrument",
        "UNDERLYING_SECURITY_ID": "underlying_security_id",
        "UNDERLYING_SYMBOL": "underlying_symbol",
        "SYMBOL_NAME": "symbol_name",
        "DISPLAY_NAME": "custom_symbol",
        "SEM_TRADING_SYMBOL": "trading_symbol",
        "EXPIRY_DATE": "expiry_date",
        "SECURITY_ID": "security_id",
        "SEM_SMST_SECURITY_ID": "security_id",
        "SEM_SECURITY_ID": "security_id",
        "INSTRUMENT_TYPE": "instrument_type",
        "SEM_EXCH_INSTRUMENT_TYPE": "instrument_type",
    }

    # Some detailed-master versions still expose compact-style names.
    df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})

    if "security_id" not in df.columns and "SM_SECURITY_ID" in df.columns:
        df["security_id"] = df["SM_SECURITY_ID"]

    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    if "underlying_security_id" in df.columns:
        df["underlying_security_id"] = pd.to_numeric(
            df["underlying_security_id"], errors="coerce"
        )

    for c in [
        "exchange", "segment_code", "instrument", "trading_symbol",
        "custom_symbol", "symbol_name", "underlying_symbol", "instrument_type"
    ]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.upper().str.strip()

    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")

    # Remove Dhan test instruments everywhere.
    text_cols = [
        c for c in [
            "trading_symbol", "custom_symbol", "symbol_name",
            "underlying_symbol"
        ] if c in df.columns
    ]
    if text_cols:
        test_mask = pd.Series(False, index=df.index)
        for c in text_cols:
            test_mask |= df[c].astype(str).str.upper().str.contains("NSETEST", na=False)
        df = df.loc[~test_mask].copy()

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

@st.cache_data(ttl=86400, show_spinner=False)
def get_cash_daily(sec_id, lookback=220):
    end = datetime.now().date()
    start = end - timedelta(days=lookback)
    payload = {
        "securityId": str(int(sec_id)),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "expiryCode": 0,
        "oi": False,
        "fromDate": str(start),
        "toDate": str(end + timedelta(days=1)),
    }
    return candles_to_df(api_post("/charts/historical", payload, "cash_daily"))


@st.cache_data(ttl=180, show_spinner=False)
def get_cash_intraday(sec_id, days=5):
    now = datetime.now()
    start = now - timedelta(days=days)
    payload = {
        "securityId": str(int(sec_id)),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "interval": "1",
        "oi": False,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return candles_to_df(api_post("/charts/intraday", payload, "cash_intraday"))


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
    EXACT NEW 3-COLUMN P&F PATTERN.

    Bullish:
      Column 1 = X Anchor with >15 boxes (default anchor_min=15 means >15).
      Column 2 = O Pullback with 1 to 5 boxes only.
      Column 3 = X breakout column.
      BUY only when Column 3 breaks the high of Column 1 (DTB).

    Bearish mirror:
      Column 1 = O Anchor with >15 boxes.
      Column 2 = X Pullback with 1 to 5 boxes.
      Column 3 = O breakdown column.
      SELL only when Column 3 breaks the low of Column 1 (DBS).

    No trade/entry is generated in Column 2.
    The setup is invalid if the pullback is >5 boxes or if extra columns
    appear between Anchor, Pullback and Breakout.
    """
    out = {
        "bias": "NO DATA",
        "anchor": False,
        "dtb": False,
        "dbs": False,
        "signal_side": None,
        "reason": "Insufficient price history",
        "sl": np.nan,
        "columns": 0,
        "anchor_boxes": 0,
        "pullback_boxes": 0,
        "pattern": "—",
        "pf_pattern": False,
        "signal_price": np.nan,
        "entry_level": np.nan,
    }

    if df is None or df.empty or "close" not in df.columns:
        return out

    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(closes) < 10:
        return out

    cols = pnf_build_columns(closes, box_pct, reversal)
    out["columns"] = len(cols)
    if len(cols) < 3:
        out["bias"] = "NO P&F"
        out["reason"] = f"Only {len(cols)} P&F columns; need at least 3"
        return out

    cur = cols[-1]
    out["bias"] = "Bullish" if cur["type"] == "X" else "Bearish"
    out["signal_price"] = float(closes.iloc[-1])

    # Search ONLY the immediate last 3 columns:
    # [Anchor, Pullback, Breakout]
    c1, c2, c3 = cols[-3], cols[-2], cols[-1]

    # ---------------- Bullish new pattern ----------------
    if c1["type"] == "X" and c2["type"] == "O" and c3["type"] == "X":
        out["anchor"] = c1["boxes"] > anchor_min
        out["anchor_boxes"] = c1["boxes"]
        out["pullback_boxes"] = c2["boxes"]
        out["entry_level"] = c1["high"]

        if out["anchor"] and (1 <= c2["boxes"] <= 5):
            out["pf_pattern"] = True

        if not out["anchor"]:
            out["reason"] = f"3-column sequence found, but Anchor is {c1['boxes']} X's (must be > {anchor_min})"
            return out

        if not (1 <= c2["boxes"] <= 5):
            out["reason"] = f"Invalid pullback: {c2['boxes']} O's (must be 1–5)"
            return out

        # Third X column must break the Anchor high.
        if c3["high"] > c1["high"]:
            out["dtb"] = True
            out["signal_side"] = "LONG"
            out["pattern"] = "NEW 3-COLUMN DTB"
            out["reason"] = (
                f"VALID: {c1['boxes']}X Anchor → {c2['boxes']}O Pullback → "
                f"3rd X breaks Anchor high (DTB)"
            )
            # Structural SL below the pullback O column.
            out["sl"] = c2["low"]
        else:
            out["reason"] = (
                f"Valid 3-column structure ({c1['boxes']}X → {c2['boxes']}O → X), "
                f"but 3rd X has not broken Anchor high"
            )
        return out

    # ---------------- Bearish mirror pattern ----------------
    if c1["type"] == "O" and c2["type"] == "X" and c3["type"] == "O":
        out["anchor"] = c1["boxes"] > anchor_min
        out["anchor_boxes"] = c1["boxes"]
        out["pullback_boxes"] = c2["boxes"]
        out["entry_level"] = c1["low"]

        if out["anchor"] and (1 <= c2["boxes"] <= 5):
            out["pf_pattern"] = True

        if not out["anchor"]:
            out["reason"] = f"3-column sequence found, but Anchor is {c1['boxes']} O's (must be > {anchor_min})"
            return out

        if not (1 <= c2["boxes"] <= 5):
            out["reason"] = f"Invalid pullback: {c2['boxes']} X's (must be 1–5)"
            return out

        if c3["low"] < c1["low"]:
            out["dbs"] = True
            out["signal_side"] = "SHORT"
            out["pattern"] = "NEW 3-COLUMN DBS"
            out["reason"] = (
                f"VALID: {c1['boxes']}O Anchor → {c2['boxes']}X Pullback → "
                f"3rd O breaks Anchor low (DBS)"
            )
            out["sl"] = c2["high"]
        else:
            out["reason"] = (
                f"Valid 3-column structure ({c1['boxes']}O → {c2['boxes']}X → O), "
                f"but 3rd O has not broken Anchor low"
            )
        return out

    # If latest 3 columns are not exactly the new pattern:
    out["reason"] = (
        f"Latest P&F columns are {c1['type']}{c2['type']}{c3['type']}; "
        "new setup requires X-O-X bullish or O-X-O bearish"
    )
    return out


# ----------------------------
# Sector relative-strength / ratio engine
# ----------------------------
SECTOR_PROXY = {
    # Practical sector proxy baskets made from liquid NSE F&O stocks.
    # The ratio is the equally-weighted sector basket price divided by NIFTY proxy.
    "Banking": ["HDFCBANK","ICICIBANK","SBIN","AXISBANK","KOTAKBANK","INDUSINDBK","BANKBARODA","PNB","FEDERALBNK","IDFCFIRSTB"],
    "IT": ["TCS","INFY","HCLTECH","WIPRO","TECHM","LTIM","MPHASIS","COFORGE"],
    "Auto": ["MARUTI","M&M","TATAMOTORS","HEROMOTOCO","EICHERMOT","BAJAJ-AUTO","TVSMOTOR","ASHOKLEY"],
    "Pharma": ["SUNPHARMA","CIPLA","DRREDDY","DIVISLAB","APOLLOHOSP","LUPIN","AUROPHARMA","TORNTPHARM"],
    "Metals": ["TATASTEEL","JSWSTEEL","HINDALCO","SAIL","JINDALSTEL","NATIONALUM","VEDL"],
    "FMCG": ["ITC","HINDUNILVR","NESTLEIND","BRITANNIA","TATACONSUM","DABUR","MARICO","COLPAL"],
    "Energy": ["RELIANCE","ONGC","COALINDIA","IOC","BPCL","GAIL"],
    "Financials": ["BAJFINANCE","BAJAJFINSV","SHRIRAMFIN","CHOLAFIN","MUTHOOTFIN","SBICARD"],
    "Realty": ["DLF","GODREJPROP","OBEROIRLTY","LODHA","PRESTIGE","PHOENIXLTD"],
    "Telecom": ["BHARTIARTL","INDUSTOWER","IDEA"],
    "Capital Goods": ["LT","BEL","BHEL","SIEMENS","ABB","CUMMINSIND"],
    "Consumer": ["TRENT","TITAN","DMART","KALYANKJIL","JUBLFOOD"],
    "Infrastructure": ["ADANIPORTS","IRCTC","DELHIVERY"],
}

@st.cache_data(ttl=900, show_spinner=False)
def get_nifty_cash_daily(days=260):
    # NIFTY 50 index is typically available in the instrument master as INDEX.
    candidates = instruments[
        (instruments["exchange"] == "NSE") &
        (instruments["instrument"].astype(str).str.upper().isin(["INDEX","IDX_I"]))
    ].copy()
    if candidates.empty:
        return pd.DataFrame()
    symcols = [c for c in ["trading_symbol","custom_symbol","symbol_name","underlying_symbol"] if c in candidates.columns]
    if not symcols:
        return pd.DataFrame()
    candidates["_name"] = candidates[symcols[0]].astype(str).str.upper()
    hit = candidates[candidates["_name"].str.contains("NIFTY 50|NIFTY", regex=True, na=False)]
    if hit.empty:
        hit = candidates
    sid = int(hit.iloc[0]["security_id"])
    end = datetime.now().date()
    start = end - timedelta(days=days)
    payload = {
        "securityId": str(sid),
        "exchangeSegment": "IDX_I",
        "instrument": "INDEX",
        "expiryCode": 0,
        "oi": False,
        "fromDate": str(start),
        "toDate": str(end + timedelta(days=1)),
    }
    try:
        return candles_to_df(api_post("/charts/historical", payload, "nifty_index_daily"))
    except Exception:
        # Some account/instrument masters expose NIFTY index under NSE_EQ.
        payload["exchangeSegment"] = "NSE_EQ"
        payload["instrument"] = "INDEX"
        try:
            return candles_to_df(api_post("/charts/historical", payload, "nifty_index_daily_fallback"))
        except Exception:
            return pd.DataFrame()

@st.cache_data(ttl=900, show_spinner=False)
def get_nifty_cash_intraday(days=5):
    candidates = instruments[
        (instruments["instrument"].astype(str).str.upper().isin(["INDEX","IDX_I"])) &
        (instruments["exchange"].isin(["NSE","IDX_I"]))
    ].copy()
    if candidates.empty:
        return pd.DataFrame()
    symcols = [c for c in ["trading_symbol","custom_symbol","symbol_name","underlying_symbol"] if c in candidates.columns]
    candidates["_name"] = candidates[symcols[0]].astype(str).str.upper()
    hit = candidates[candidates["_name"].str.contains("NIFTY 50|NIFTY", regex=True, na=False)]
    if hit.empty:
        hit = candidates
    sid = int(hit.iloc[0]["security_id"])
    now = datetime.now()
    start = now - timedelta(days=days)
    payload = {
        "securityId": str(sid),
        "exchangeSegment": "IDX_I",
        "instrument": "INDEX",
        "interval": "1",
        "oi": False,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        return candles_to_df(api_post("/charts/intraday", payload, "nifty_index_intraday"))
    except Exception:
        return pd.DataFrame()

def normalized_price_series(df, date_col="datetime"):
    if df is None or df.empty:
        return pd.Series(dtype=float)
    x = df[["datetime","close"]].copy()
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
    x["close"] = pd.to_numeric(x["close"], errors="coerce")
    x = x.dropna().sort_values("datetime")
    return x.drop_duplicates("datetime").set_index("datetime")["close"]

def build_sector_ratio_from_cash(sector, mode):
    """
    Build an equally-weighted sector basket divided by NIFTY.
    We deliberately use cash/spot prices on both sides.
    The output is a synthetic ratio series used only for relative-strength P&F.
    """
    names = [x.upper() for x in SECTOR_PROXY.get(sector, [])]
    if not names:
        return pd.DataFrame()

    series = []
    if mode == "Positional":
        nifty = get_nifty_cash_daily()
    else:
        nifty = get_nifty_cash_intraday()
    n = normalized_price_series(nifty)
    if n.empty:
        return pd.DataFrame()

    for symbol in names:
        match = instruments[
            (instruments["exchange"] == "NSE") &
            (instruments["instrument"] == "EQUITY") &
            (instruments["trading_symbol"].astype(str).str.upper() == symbol)
        ] if "trading_symbol" in instruments.columns else pd.DataFrame()

        if match.empty:
            continue
        sid = int(match.iloc[0]["security_id"])
        try:
            if mode == "Positional":
                h = get_cash_daily(sid)
            else:
                h = get_cash_intraday(sid, days=5)
                if not h.empty:
                    h = h[h["datetime"] < pd.Timestamp.now().floor("min")]
            s = normalized_price_series(h)
            if not s.empty:
                series.append(s.rename(symbol))
        except Exception:
            continue

    if not series:
        return pd.DataFrame()

    basket = pd.concat(series, axis=1).ffill()
    basket = basket.mean(axis=1, skipna=True)
    n = n.reindex(basket.index).ffill()
    ratio = basket / n
    ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
    return pd.DataFrame({"close": ratio.values, "datetime": ratio.index})

def sector_ratio_pattern(sector, mode, anchor_min=15):
    ratio_df = build_sector_ratio_from_cash(sector, mode)
    if ratio_df.empty:
        return {
            "bias": "UNAVAILABLE",
            "pattern": "—",
            "star": False,
            "reason": "No sector/NIFTY ratio data"
        }
    box = 0.0025 if mode == "Positional" else 0.0015
    p = pnf_analysis(ratio_df, box, anchor_min)
    bullish = p.get("signal_side") == "LONG" or (
        p.get("bias") == "Bullish" and not p.get("signal_side")
    )
    bearish = p.get("signal_side") == "SHORT" or (
        p.get("bias") == "Bearish" and not p.get("signal_side")
    )
    return {
        "bias": "Bullish" if bullish else "Bearish" if bearish else p.get("bias", "Neutral"),
        "pattern": p.get("pattern", "—"),
        "star": bool(p.get("dtb") if bullish else p.get("dbs") if bearish else False),
        "reason": p.get("reason", "")
    }


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
    """
    Return one nearest usable futures contract per underlying.
    Robust to detailed instrument-master variants where expiry_date is absent
    or named differently.
    """
    inst = "FUTSTK" if exchange == "NSE" else "FUTCOM"
    ex = "NSE" if exchange == "NSE" else "MCX"

    x = df[
        (df["exchange"].astype(str).str.upper() == ex) &
        (df["instrument"].astype(str).str.upper() == inst)
    ].copy()

    if x.empty:
        return x

    x = x.dropna(subset=["security_id"]).copy()

    # Ensure expiry_date exists before sorting/filtering.
    if "expiry_date" not in x.columns:
        # Try alternate detailed-master names, if present.
        alt = next(
            (c for c in ["EXPIRY_DATE", "SEM_EXPIRY_DATE", "SM_EXPIRY_DATE"] if c in x.columns),
            None
        )
        if alt:
            x["expiry_date"] = pd.to_datetime(x[alt], errors="coerce")
        else:
            x["expiry_date"] = pd.NaT
    else:
        x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")

    # Only reject contracts that are definitely expired.
    now = pd.Timestamp.now()
    if x["expiry_date"].notna().any():
        x = x[(x["expiry_date"].isna()) | (x["expiry_date"] >= now)]

    sym = "underlying_symbol"
    if sym not in x.columns:
        # Detailed master fallback from trading symbol, e.g.
        # RELIANCE-Aug2026-FUT -> RELIANCE.
        if "trading_symbol" in x.columns:
            x[sym] = (
                x["trading_symbol"].astype(str)
                .str.split("-", n=1).str[0]
                .str.upper().str.strip()
            )
        else:
            return pd.DataFrame()

    x[sym] = x[sym].astype(str).str.upper().str.strip()

    rows = []
    for symbol, g in x.groupby(sym, dropna=False):
        if symbol in ("", "NAN", "NONE"):
            continue
        # Prefer dated contracts; if expiry isn't available, keep the first row.
        g = g.sort_values("expiry_date", na_position="last")
        rows.append(g.iloc[0])

    return pd.DataFrame(rows).reset_index(drop=True) if rows else pd.DataFrame()


def choose_rows(x, n):
    if x.empty: return x
    return x.head(n).reset_index(drop=True)

def nse_fno_stock_universe(instruments):
    """
    Map each active NSE stock future directly to its underlying CASH security ID
    where Dhan's detailed instrument master provides UNDERLYING_SECURITY_ID.
    This avoids symbol-text mismatches that can cause historical cash requests
    to fail for every stock.
    """
    fut = nearest_fno(instruments, "NSE")
    if fut.empty:
        return pd.DataFrame()

    fut = fut.copy()
    if "underlying_symbol" not in fut.columns:
        return pd.DataFrame()

    fut["underlying_symbol"] = fut["underlying_symbol"].astype(str).str.upper().str.strip()
    fut = fut.dropna(subset=["security_id"]).drop_duplicates("underlying_symbol")

    rows = []
    for _, f in fut.iterrows():
        symbol = str(f["underlying_symbol"]).upper().strip()
        cash_id = f.get("underlying_security_id", np.nan)

        # Prefer the exact derivative->underlying ID supplied by Dhan.
        if pd.isna(cash_id):
            # Fallback: exact NSE cash equity trading-symbol match.
            eq = instruments[
                (instruments["exchange"] == "NSE") &
                (instruments["instrument"] == "EQUITY")
            ].copy()
            if eq.empty:
                continue

            if "trading_symbol" in eq.columns:
                eq["equity_symbol"] = eq["trading_symbol"].astype(str).str.upper().str.strip()
            else:
                eq["equity_symbol"] = eq["symbol_name"].astype(str).str.upper().str.strip()

            exact = eq[eq["equity_symbol"] == symbol]
            if exact.empty:
                exact = eq[
                    eq["equity_symbol"].str.replace("-EQ", "", regex=False) == symbol
                ]
            if exact.empty:
                continue
            cash_id = exact.iloc[0]["security_id"]

        if pd.isna(cash_id):
            continue

        rows.append({
            "Symbol": symbol,
            "cash_security_id": int(float(cash_id)),
            "future_security_id": int(f["security_id"]),
            "future_expiry": f.get("expiry_date", pd.NaT),
        })

    return pd.DataFrame(rows).drop_duplicates("Symbol").reset_index(drop=True) if rows else pd.DataFrame()

def market_quote_bulk(payload, label="quote"):
    """One batched Market Quote call. Dhan permits up to 1000 instruments/request."""
    if not payload:
        return {}
    return api_post("/marketfeed/quote", payload, label)


def parse_quote_response(body, segment):
    out = {}
    data = body.get("data", {}) if isinstance(body, dict) else {}
    seg = data.get(segment, {}) if isinstance(data, dict) else {}
    if isinstance(seg, dict):
        for sid, item in seg.items():
            if isinstance(item, dict):
                out[int(sid)] = item
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def previous_daily_oi_map(future_ids):
    """Fetch previous daily OI once per future per day, not every 3-minute refresh."""
    result = {}
    yday = (datetime.now().date() - timedelta(days=5))
    today = datetime.now().date()
    ids = list(future_ids)
    for sid in ids:
        try:
            payload = {
                "securityId": str(int(sid)),
                "exchangeSegment": "NSE_FNO",
                "instrument": "FUTSTK",
                "expiryCode": 0,
                "oi": True,
                "fromDate": str(yday),
                "toDate": str(today + timedelta(days=1)),
            }
            body = api_post("/charts/historical", payload, "previous_oi")
            df = candles_to_df(body)
            if not df.empty and "open_interest" in df.columns:
                s = pd.to_numeric(df["open_interest"], errors="coerce").dropna()
                result[int(sid)] = float(s.iloc[-1]) if not s.empty else np.nan
            else:
                result[int(sid)] = np.nan
        except Exception:
            result[int(sid)] = np.nan
    return result


def futures_confirmation(symbols_df, require_oi=True):
    """Get current futures OI in bulk and compare against cached previous OI."""
    if symbols_df.empty:
        return {}
    ids = symbols_df["future_security_id"].astype(int).tolist()
    # One Market Quote request for all futures (normally < 1000).
    q = market_quote_bulk({"NSE_FNO": ids}, "NSE futures quote")
    current = parse_quote_response(q, "NSE_FNO")
    prev = previous_daily_oi_map(tuple(ids))
    result = {}
    for _, r in symbols_df.iterrows():
        sid = int(r["future_security_id"])
        item = current.get(sid, {})
        oi = pd.to_numeric(item.get("oi"), errors="coerce")
        last_price = pd.to_numeric(item.get("last_price"), errors="coerce")
        day_close = pd.to_numeric((item.get("ohlc") or {}).get("close"), errors="coerce")
        prev_oi = prev.get(sid, np.nan)

        if pd.isna(oi) or pd.isna(prev_oi):
            state = "UNAVAILABLE"
        elif oi > prev_oi:
            # Direction comes from the cash P&F signal; positive OI change
            # is the confirmation for fresh positioning.
            state = "OI BUILDUP"
        else:
            state = "OI NOT BUILDING"
        result[r["Symbol"]] = {
            "oi": float(oi) if not pd.isna(oi) else np.nan,
            "prev_oi": float(prev_oi) if not pd.isna(prev_oi) else np.nan,
            "oi_change": float(oi - prev_oi) if not (pd.isna(oi) or pd.isna(prev_oi)) else np.nan,
            "state": state,
            "future_ltp": float(last_price) if not pd.isna(last_price) else np.nan,
            "future_close": float(day_close) if not pd.isna(day_close) else np.nan,
        }
    return result

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
    [
        "Market Overview",
        "NSE Intraday P&F",
        "NSE Positional P&F",
        "Bullish Stocks",
        "Bearish Stocks",
        "Sector Breadth",
        "MCX Intraday",
        "MCX Positional",
        "Diagnostics",
    ],
    horizontal=True,
    key="dashboard_mode",
)

# Module-specific refresh:
# Intraday P&F = 1 minute
# Positional / Sector = 15 minutes
# Diagnostics = manual
# Market Overview / Bullish / Bearish = 3 minutes
refresh_minutes = {
    "Market Overview": 3,
    "Trade Ranking": 3,
    "NSE Intraday P&F": 1,
    "NSE Positional P&F": 15,
    "Bullish Stocks": 3,
    "Bearish Stocks": 3,
    "Sector Breadth": 15,
    "MCX Intraday": 1,
    "MCX Positional": 15,
    "Diagnostics": 0,
}[mode]

if auto and refresh_minutes:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(
            interval=refresh_minutes * 60 * 1000,
            key=f"alpha_refresh_{mode.replace(' ', '_')}"
        )
    except Exception:
        pass

st.caption(
    f"Auto refresh: {'ON' if auto else 'OFF'} | "
    f"{'Every ' + str(refresh_minutes) + ' min' if refresh_minutes else 'Manual'}"
)



def evaluate_trade_stars(pnf, oi_state_text, sector_info, direction):
    """
    Star model requested by user:
      ⭐ = current directional P&F pattern
      🟢⭐ = additional green star when the NEW 3-column setup is present
      ⭐ = OI confirmation
      ⭐ = sector/relative-strength confirmation

    The first visible column is a compact overall star string.
    P&F DTB/DBS remains the actual entry trigger.
    """
    # Base P&F pattern: any running bullish/bearish P&F structure.
    pf_star = pnf.get("pf_pattern", False) and (
        (direction == "LONG") or (direction == "SHORT") or pnf.get("bias") in ("Bullish","Bearish")
    )

    # New 3-column pattern: valid Anchor >15 -> 1-5 pullback -> 3rd column.
    new_pattern_star = pnf.get("pf_pattern", False) and (
        pnf.get("pattern") in (
            "PROSPECTIVE LONG", "PROSPECTIVE SHORT",
            "NEW 3-COLUMN DTB", "NEW 3-COLUMN DBS"
        )
    )

    oi_star = oi_state_text == "OI BUILDUP"

    sector_star = False
    if direction == "LONG":
        sector_star = sector_info.get("bias") == "Bullish"
    elif direction == "SHORT":
        sector_star = sector_info.get("bias") == "Bearish"
    else:
        # For a running pattern before signal, align to P&F bias.
        if pnf.get("bias") == "Bullish":
            sector_star = sector_info.get("bias") == "Bullish"
        elif pnf.get("bias") == "Bearish":
            sector_star = sector_info.get("bias") == "Bearish"

    # Core visible rating:
    # 1 = P&F pattern
    # +1 = OI
    # +1 = sector
    # Additional green star is displayed separately in "Stars".
    base_stars = int(pf_star) + int(oi_star) + int(sector_star)
    return base_stars, pf_star, new_pattern_star, oi_star, sector_star

# ----------------------------
# Shared NSE P&F scanner
# ----------------------------
@st.cache_data(ttl=55, show_spinner=False)
def run_nse_pnf_scan(sys_mode, anchor_min, require_oi, universe_signature):
    universe = nse_fno_stock_universe(instruments)
    if universe.empty:
        return pd.DataFrame()

    box = 0.0025 if sys_mode == "Positional" else 0.0015
    selected = universe.sort_values("Symbol").reset_index(drop=True)

    # One batched futures quote call per cache interval.
    oi_map = futures_confirmation(selected, require_oi=require_oi)

    rows = []
    for _, r in selected.iterrows():
        symbol = str(r["Symbol"])
        try:
            if sys_mode == "Positional":
                hist = get_cash_daily(r["cash_security_id"])
            else:
                hist = get_cash_intraday(r["cash_security_id"], days=5)
                if not hist.empty:
                    hist = hist[hist["datetime"] < pd.Timestamp.now().floor("min")].copy()

            pnf = pnf_analysis(hist, box, int(anchor_min))
            oi_info = oi_map.get(symbol, {})
            oi_state_text = oi_info.get("state", "UNAVAILABLE")

            if require_oi and pnf["signal_side"]:
                oi_ok = oi_state_text == "OI BUILDUP"
            else:
                oi_ok = True

            # P&F DTB/DBS is the actual entry trigger.
            # OI and Sector are ranking stars only.
            if pnf["signal_side"] == "LONG" and pnf.get("dtb"):
                status = "🟢 BUY"
            elif pnf["signal_side"] == "SHORT" and pnf.get("dbs"):
                status = "🔴 SELL"
            elif not pnf["anchor"]:
                status = "WAIT - NO ANCHOR"
            elif pnf["bias"] == "Bullish":
                status = "WAIT - NO DTB"
            elif pnf["bias"] == "Bearish":
                status = "WAIT - NO DBS"
            else:
                status = "WAIT"

            rows.append({
                "Symbol": symbol,
                "Sector": sector_of(symbol),
                "Bias": pnf["bias"],
                "Pattern": pnf.get("pattern", "—"),
                "Anchor Boxes": pnf.get("anchor_boxes", 0),
                "Pullback Boxes": pnf.get("pullback_boxes", 0),
                "Entry Level": pnf.get("entry_level", np.nan),
                "Anchor": "✅" if pnf["anchor"] else "❌",
                "DTB": "✅" if pnf["dtb"] else "❌",
                "DBS": "✅" if pnf["dbs"] else "❌",
                "Futures OI": oi_state_text,
                "OI Δ": oi_info.get("oi_change", np.nan),
                "System": status,
                "Stars": (
                    ("⭐" if pnf_star else "☆") +
                    ("⭐" if oi_star else "☆") +
                    ("⭐" if sector_star else "☆") +
                    ("🟢★" if new_pattern_star else "☆")
                ),
                "Star Count": stars,
                "Four-Star Score": stars + (1 if new_pattern_star else 0),
                "P&F ⭐": "⭐" if pnf_star else "—",
                "OI ⭐": "⭐" if oi_star else "—",
                "Sector ⭐": "⭐" if sector_star else "—",
                "Sector Ratio": sector_info.get("bias", "—"),
                "Sector Pattern": sector_info.get("pattern", "—"),
                "SL": pnf["sl"],
                "Reason": pnf["reason"],
            })
        except Exception as e:
            rows.append({
                "Symbol": symbol,
                "Sector": sector_of(symbol),
                "Bias": "ERROR",
                "Pattern": "—",
                "Anchor Boxes": 0,
                "Pullback Boxes": 0,
                "Entry Level": np.nan,
                "Anchor": "❌",
                "DTB": "❌",
                "DBS": "❌",
                "Futures OI": "ERROR",
                "OI Δ": np.nan,
                "System": "DATA ERROR",
                "Stars": "—",
                "Star Count": 0,
                "Four-Star Score": 0,
                "New Pattern": "—",
                "P&F ⭐": "—",
                "OI ⭐": "—",
                "Sector ⭐": "—",
                "Sector Ratio": "ERROR",
                "Sector Pattern": "—",
                "SL": np.nan,
                "Reason": str(e)[:300],
            })
    res = pd.DataFrame(rows)
    if res.empty:
        return res
    breadth = sector_breadth(res)
    if not breadth.empty:
        res["⭐"] = res["Sector"].map(lambda s: stock_star(s, breadth, sector_threshold))
    else:
        res["⭐"] = ""
    return res

# ----------------------------
# Market Overview
# ----------------------------
if mode == "Market Overview":
    st.subheader("Market Overview")
    universe = nse_fno_stock_universe(instruments)
    st.metric("NSE F&O stocks mapped to cash", len(universe))
    st.metric("MCX futures discovered", len(nearest_fno(instruments, "MCX")))
    st.info(
        "Intraday P&F refreshes every 1 minute; positional and sector views every 15 minutes. "
        "The main engine uses CASH/spot price for NSE P&F and futures OI only for confirmation."
    )

    recent_logs = pd.DataFrame(st.session_state.api_log).tail(20)
    if not recent_logs.empty:
        st.markdown("### Recent API activity")
        st.dataframe(recent_logs, use_container_width=True, hide_index=True)


# ----------------------------
# Trade Ranking
# ----------------------------
elif mode == "Trade Ranking":
    st.subheader("Trade Ranking — 3-Star Model")
    st.caption(
        "⭐ = running P&F directional pattern. 🟢★ = NEW 3-column setup "
        "(>15 Anchor → 1–5 pullback → 3rd column). OI and Sector add normal confirmation stars. "
        "DTB/DBS remains the entry trigger."
    )
    sys_mode = st.radio("P&F timeframe", ["Intraday", "Positional"], horizontal=True)
    res = run_nse_pnf_scan(
        sys_mode,
        int(anchor_boxes),
        require_oi,
        tuple(nse_fno_stock_universe(instruments)["Symbol"].tolist())
    )

    if res.empty:
        st.info("No ranking data returned.")
    else:
        ranked = res[res["Bias"].isin(["Bullish","Bearish"])].copy()
        ranked = ranked.sort_values(
            by=["Star Count","Anchor Boxes","Pullback Boxes","Symbol"],
            ascending=[False, False, True, True]
        )
        st.dataframe(
            ranked[[
                "Stars","Symbol","Sector","Bias","Pattern","Anchor Boxes","Pullback Boxes",
                "Sector Ratio","Futures OI","System","Entry Level","SL","Reason"
            ]],
            use_container_width=True, hide_index=True
        )
        st.markdown("### ⭐⭐⭐ Strongest")
        strongest = ranked[ranked["Star Count"] == 3]
        if strongest.empty:
            st.info("No 3-star setups currently.")
        else:
            st.dataframe(
                strongest[[
                    "Stars","Symbol","Sector","Bias","Pattern","Anchor Boxes","Pullback Boxes",
                    "Sector Ratio","System","Entry Level","SL"
                ]],
                use_container_width=True, hide_index=True
            )


# ----------------------------
# NSE Intraday / Positional
# ----------------------------
elif mode in ("NSE Intraday P&F", "NSE Positional P&F"):
    sys_mode = "Intraday" if mode == "NSE Intraday P&F" else "Positional"
    st.subheader(f"NSE {sys_mode} P&F")
    st.info(
        f"{sys_mode}: CASH/SPOT price | "
        f"{'0.15% box' if sys_mode == 'Intraday' else '0.25% box'} | "
        "3-box reversal | "
        f"{'1-minute completed closes' if sys_mode == 'Intraday' else 'daily closes'} | "
        "Futures OI confirmation"
    )

    res = run_nse_pnf_scan(
        sys_mode,
        int(anchor_boxes),
        require_oi,
        tuple(nse_fno_stock_universe(instruments)["Symbol"].tolist())
    )

    if res.empty:
        st.warning("No NSE F&O data returned.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Stocks scanned", len(res))
        c2.metric("Bullish", int((res["Bias"] == "Bullish").sum()))
        c3.metric("Bearish", int((res["Bias"] == "Bearish").sum()))
        c4.metric("P&F entries", int(res["System"].isin(["🟢 BUY","🔴 SELL"]).sum()))
        st.caption(
            f"Cash P&F uses the mapped underlying SECURITY ID from Dhan. Exact 3-column DTB/DBS found: {int(res['System'].isin(['🟢 BUY','🔴 SELL']).sum())}. "
            "The first column shows the total 0–3 star rating."
        )

        cols = ["⭐","Symbol","Sector","Pattern","Anchor Boxes","Anchor","DTB","DBS",
                "Futures OI","OI Δ","System","SL","Reason"]
        cols = [
            "Stars","Symbol","Sector","Pattern","Prospective","Anchor Boxes","Pullback Boxes",
            "Sector Ratio","Anchor","DTB","DBS","Futures OI","OI Δ","System","Entry Level","SL","Reason"
        ]
        available = [c for c in cols if c in res.columns]
        st.dataframe(res[available], use_container_width=True, hide_index=True)

# ----------------------------
# Bullish / Bearish dedicated pages
# ----------------------------
elif mode in ("Bullish Stocks", "Bearish Stocks"):
    sys_mode = st.radio("P&F timeframe", ["Intraday", "Positional"], horizontal=True)
    res = run_nse_pnf_scan(
        sys_mode,
        int(anchor_boxes),
        require_oi,
        tuple(nse_fno_stock_universe(instruments)["Symbol"].tolist())
    )

    bullish = res[res["Bias"] == "Bullish"].copy() if not res.empty else pd.DataFrame()
    bearish = res[res["Bias"] == "Bearish"].copy() if not res.empty else pd.DataFrame()
    target = bullish if mode == "Bullish Stocks" else bearish
    pattern_col = "DTB" if mode == "Bullish Stocks" else "DBS"

    if target.empty:
        st.info(f"No {mode.lower()} currently detected.")
    else:
        target = target.sort_values(
            by=[pattern_col, "Anchor Boxes", "⭐", "Symbol"],
            ascending=[False, False, False, True]
        )
        st.subheader(f"{'🟢' if mode == 'Bullish Stocks' else '🔴'} {mode}")
        st.dataframe(
            target[["⭐","Symbol","Sector","Pattern","Anchor Boxes","Anchor",
                    pattern_col,"Futures OI","System","SL","Reason"]],
            use_container_width=True, hide_index=True
        )

        ready = target[target["System"].isin(["🟢 BUY","🔴 SELL"])]
        st.metric("Trade-ready setups", len(ready))

# ----------------------------
# Sector breadth
# ----------------------------
elif mode == "Sector Breadth":
    st.subheader("P&F Sector Breadth")
    sys_mode = st.radio("Breadth mode", ["Positional", "Intraday"], horizontal=True)
    box = 0.0025 if sys_mode=="Positional" else 0.0015

    universe = nse_fno_stock_universe(instruments)
    if universe.empty:
        st.warning("No NSE F&O cash mappings found.")
        st.stop()

    progress = st.progress(0, text=f"Calculating sector breadth for {len(universe)} stocks...")
    rows=[]
    for i, (_, r) in enumerate(universe.iterrows(), 1):
        try:
            h = get_cash_daily(r["cash_security_id"]) if sys_mode=="Positional" else get_cash_intraday(r["cash_security_id"], days=5)
            if sys_mode=="Intraday" and not h.empty:
                h = h[h["datetime"] < pd.Timestamp.now().floor("min")]
            p = pnf_analysis(h, box, int(anchor_boxes))
            symbol=str(r["Symbol"])
            rows.append({
                "Symbol":symbol,
                "Sector":sector_of(symbol),
                "Bias":p["bias"],
                "DTB":"✅" if p["dtb"] else "❌",
                "DBS":"✅" if p["dbs"] else "❌",
            })
        except Exception:
            pass
        progress.progress(i/len(universe), text=f"Sector breadth: {i}/{len(universe)}")
    progress.empty()

    br=sector_breadth(pd.DataFrame(rows))
    if br.empty:
        st.warning("No sector breadth data returned.")
    else:
        br["⭐ Sector"] = np.where(
            (br["Bullish %"]>=sector_threshold)|(br["Bearish %"]>=sector_threshold),
            "⭐",""
        )
        st.dataframe(
            br[["⭐ Sector","Sector","stocks","bullish","Bullish %","bearish","Bearish %","dtb","dbs"]],
            use_container_width=True, hide_index=True
        )


# ----------------------------
# MCX
# ----------------------------
elif mode in ("MCX Intraday", "MCX Positional"):
    sys_mode = "Intraday" if mode == "MCX Intraday" else "Positional"
    st.subheader(f"MCX {sys_mode} P&F Trading System")
    st.caption(
        "MCX: daily 0.25% P&F provides the higher-timeframe direction; "
        "intraday 0.15% P&F provides the entry. OI is secondary confirmation."
    )

    uni = nearest_fno(instruments, "MCX")
    if uni.empty:
        st.error("No MCX FUTCOM contracts were found.")
        st.stop()

    sym = "underlying_symbol"
    names = sorted(uni[sym].astype(str).unique())
    chosen = st.selectbox("Commodity", names, key=f"mcx_{sys_mode}_commodity")
    rr = uni[uni[sym].astype(str) == chosen].iloc[0]

    try:
        daily = get_mcx_daily(rr.security_id)
        intra = get_intraday(rr.security_id, "MCX_COMM", "FUTCOM")
        if not intra.empty:
            intra = intra[intra["datetime"] < pd.Timestamp.now().floor("min")]

        dp = pnf_analysis(daily, 0.0025, int(anchor_boxes))
        ip = pnf_analysis(intra, 0.0015, int(anchor_boxes))
        oi, pchg, doichg = oi_state(intra)

        if sys_mode == "Positional":
            system = (
                "🟢 LONG" if dp["signal_side"] == "LONG"
                else "🔴 SHORT" if dp["signal_side"] == "SHORT"
                else "WAIT"
            )
            signal = dp
            st.info("Positional MCX: 0.25% / 3-box / daily close. Exit on opposite daily P&F reversal.")
        else:
            if dp["bias"] == "Bullish" and ip["signal_side"] == "LONG":
                system = "🟢 LONG"
            elif dp["bias"] == "Bearish" and ip["signal_side"] == "SHORT":
                system = "🔴 SHORT"
            elif ip["signal_side"] in ("LONG","SHORT"):
                system = "WAIT - DAILY FILTER"
            else:
                system = "WAIT - NO INTRADAY SETUP"
            signal = ip
            st.info("Intraday MCX: 0.15% / 3-box / 1-minute close, filtered by the 0.25% daily trend.")

        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Daily P&F", dp["bias"])
        c2.metric("Intraday P&F", ip["bias"])
        c3.metric("Setup", signal["pattern"])
        c4.metric("System", system)
        c5.metric("Initial SL", f"{signal['sl']:.2f}" if pd.notna(signal["sl"]) else "—")

        st.write(f"**Daily:** {dp['reason']}")
        st.write(f"**Intraday:** {ip['reason']}")
        if pd.notna(pchg) and pd.notna(doichg):
            st.write(f"**OI:** {oi} | Price Δ {pchg:.2f}% | OI Δ {doichg:.0f}")
        else:
            st.write("**OI:** unavailable")
        st.markdown("**Exit:** opposite P&F reversal signal.")
    except Exception as e:
        st.error(f"MCX analysis error: {e}")
# ----------------------------
# Diagnostics
# ----------------------------
else:
    st.subheader("Diagnostics")
    st.write("Operational diagnostics: credentials, instrument counts, API activity and errors.")
    st.write({
        "client_code_present": bool(st.session_state.alpha_client_id),
        "access_token_present": bool(st.session_state.alpha_access_token),
        "instrument_rows": len(instruments),
        "NSE FUTSTK contracts": int(((instruments["exchange"]=="NSE") & (instruments["instrument"]=="FUTSTK")).sum()),
        "NSE unique F&O stocks mapped to cash": len(nse_fno_stock_universe(instruments)),
        "NSE F&O rows with underlying_security_id": int(
            instruments[
                (instruments["exchange"] == "NSE") &
                (instruments["instrument"] == "FUTSTK")
            ]["underlying_security_id"].notna().sum()
        ) if "underlying_security_id" in instruments.columns else 0,
        "MCX FUTCOM contracts": int(((instruments["exchange"]=="MCX") & (instruments["instrument"]=="FUTCOM")).sum()),
    })

    if not st.session_state.api_log:
        st.info("No API call logged yet. Open NSE P&F or MCX to run a test.")
    else:
        st.dataframe(pd.DataFrame(st.session_state.api_log).tail(50), use_container_width=True, hide_index=True)

    with st.expander("Instrument master columns"):
        st.write(list(instruments.columns))




    if st.session_state.last_error:
        st.error(st.session_state.last_error)

st.divider()
st.caption(f"Page refresh: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')} | API calls this session: {len(st.session_state.api_log)}")
