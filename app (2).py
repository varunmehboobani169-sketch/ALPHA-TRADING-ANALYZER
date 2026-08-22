
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
    max_scan = st.slider("F&O/MCX scan size", 5, 80, 20, step=5)
    anchor_boxes = st.number_input("Minimum anchor boxes", 5, 30, 15)
    sector_threshold = st.slider("Super sector breadth %", 50, 90, 70)

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

    aliases = {
        "SEM_EXM_EXCH_ID": "exchange",
        "EXCH_ID": "exchange",
        "SEM_SEGMENT": "segment_code",
        "SEGMENT": "segment_code",
        "SEM_SECURITY_ID": "security_id",
        "SECURITY_ID": "security_id",
        "SEM_TRADING_SYMBOL": "trading_symbol",
        "TRADING_SYMBOL": "trading_symbol",
        "SEM_CUSTOM_SYMBOL": "display_name",
        "DISPLAY_NAME": "display_name",
        "SEM_INSTRUMENT_NAME": "instrument",
        "INSTRUMENT": "instrument",
        "SM_SYMBOL_NAME": "symbol_name",
        "SYMBOL_NAME": "symbol_name",
        "UNDERLYING_SYMBOL": "underlying_symbol",
        "UNDERLYING_SECURITY_ID": "underlying_security_id",
        "SEM_EXPIRY_DATE": "expiry_date",
        "EXPIRY_DATE": "expiry_date",
    }
    rename = {}
    for c in df.columns:
        rename[c] = aliases.get(c, c)
    df = df.rename(columns=rename)

    if "security_id" in df.columns:
        df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    for c in ["exchange", "instrument", "symbol_name", "underlying_symbol"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.upper().str.strip()

    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")
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
    # Dhan documents epoch timestamps for historical response.
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
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
# P&F engine
# This is a close-only percentage-box implementation.
# ----------------------------
def pnf_columns(closes, box_pct=0.0025, reversal=3):
    prices = pd.Series(closes).dropna().astype(float).tolist()
    if len(prices) < 3:
        return []

    cols = []
    direction = None
    anchor_price = prices[0]
    boxes = 0
    current_high = anchor_price
    current_low = anchor_price

    for p in prices[1:]:
        if direction is None:
            if p >= anchor_price * (1 + box_pct):
                direction = "X"
                boxes = 1
                current_high = anchor_price * (1 + box_pct)
                while p >= current_high * (1 + box_pct):
                    current_high *= (1 + box_pct)
                    boxes += 1
                current_low = anchor_price
            elif p <= anchor_price * (1 - box_pct):
                direction = "O"
                boxes = 1
                current_low = anchor_price * (1 - box_pct)
                while p <= current_low * (1 - box_pct):
                    current_low *= (1 - box_pct)
                    boxes += 1
                current_high = anchor_price
            continue

        if direction == "X":
            while p >= current_high * (1 + box_pct):
                current_high *= (1 + box_pct)
                boxes += 1
            reversal_level = current_high * ((1 - box_pct) ** reversal)
            if p <= reversal_level:
                cols.append({"type": "X", "boxes": boxes, "high": current_high, "low": current_low})
                direction = "O"
                current_low = current_high * (1 - box_pct)
                current_high = current_low * (1 + box_pct)
                boxes = reversal
        else:
            while p <= current_low * (1 - box_pct):
                current_low *= (1 - box_pct)
                boxes += 1
            reversal_level = current_low * ((1 + box_pct) ** reversal)
            if p >= reversal_level:
                cols.append({"type": "O", "boxes": boxes, "high": current_high, "low": current_low})
                direction = "X"
                current_high = current_low * (1 + box_pct)
                current_low = current_high * (1 - box_pct)
                boxes = reversal

    if direction:
        cols.append({
            "type": direction,
            "boxes": boxes,
            "high": current_high,
            "low": current_low,
        })
    return cols

def pnf_analysis(df, box_pct, anchor_min=15):
    if df.empty or len(df) < 5:
        return {
            "bias": "NO DATA",
            "anchor": False, "dtb": False, "dbs": False,
            "signal_side": None, "reason": "Insufficient price history",
            "sl": np.nan, "columns": 0
        }

    cols = pnf_columns(df["close"], box_pct, 3)
    out = {
        "bias": "Neutral", "anchor": False, "dtb": False, "dbs": False,
        "signal_side": None, "reason": "No fresh setup",
        "sl": np.nan, "columns": len(cols)
    }
    if not cols:
        out["bias"] = "NO P&F"
        out["reason"] = "Could not build P&F columns"
        return out

    cur = cols[-1]
    out["bias"] = "Bullish" if cur["type"] == "X" else "Bearish"

    if cur["type"] == "X":
        prev_x = None
        for c in reversed(cols[:-1]):
            if c["type"] == "X":
                prev_x = c
                break
        if prev_x is None:
            out["reason"] = "No previous X-column"
            return out

        anchor = None
        for c in cols[:-1]:
            if c["type"] == "X" and c["boxes"] >= anchor_min:
                anchor = c
        out["anchor"] = anchor is not None

        if anchor is None:
            out["reason"] = f"No X-column anchor >= {anchor_min} boxes"
        elif cur["high"] > prev_x["high"]:
            out["dtb"] = True
            out["signal_side"] = "LONG"
            out["reason"] = f"Anchor ({anchor['boxes']} boxes) + DTB"
            # Structural SL = most recent O-column low.
            last_o = next((c for c in reversed(cols[:-1]) if c["type"] == "O"), None)
            if last_o:
                out["sl"] = last_o["low"]
        else:
            out["reason"] = "Anchor present; current X-column has no fresh DTB"
    else:
        prev_o = None
        for c in reversed(cols[:-1]):
            if c["type"] == "O":
                prev_o = c
                break
        if prev_o is None:
            out["reason"] = "No previous O-column"
            return out

        anchor = None
        for c in cols[:-1]:
            if c["type"] == "O" and c["boxes"] >= anchor_min:
                anchor = c
        out["anchor"] = anchor is not None

        if anchor is None:
            out["reason"] = f"No O-column anchor >= {anchor_min} boxes"
        elif cur["low"] < prev_o["low"]:
            out["dbs"] = True
            out["signal_side"] = "SHORT"
            out["reason"] = f"Anchor ({anchor['boxes']} boxes) + DBS"
            last_x = next((c for c in reversed(cols[:-1]) if c["type"] == "X"), None)
            if last_x:
                out["sl"] = last_x["high"]
        else:
            out["reason"] = "Anchor present; current O-column has no fresh DBS"

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

def system_result(pnf, oi):
    # Both long and short are supported for the scanner.
    if pnf["signal_side"] == "LONG":
        if oi == "LONG BUILDUP":
            return "🟢 BUY", True
        return "WAIT - OI", False
    if pnf["signal_side"] == "SHORT":
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
    x = df[(df["exchange"]==ex) & (df["instrument"]==inst)].copy()
    if x.empty: return x
    sym = "underlying_symbol" if "underlying_symbol" in x.columns else "symbol_name"
    if sym not in x.columns:
        return pd.DataFrame()
    x[sym] = x[sym].astype(str).str.upper().str.strip()
    x = x.dropna(subset=["security_id"])
    if "expiry_date" in x.columns:
        x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")
        x = x.sort_values("expiry_date")
    out=[]
    for s,g in x.groupby(sym):
        g=g.dropna(subset=["expiry_date"]) if "expiry_date" in g.columns else g
        if not g.empty:
            out.append(g.iloc[0])
    return pd.DataFrame(out)

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
        f"{sys_mode}: {box*100:.2f}% box | 3-box reversal | "
        f"{'daily closes' if sys_mode=='Positional' else '1-minute closes'} only."
    )

    uni = nearest_fno(instruments, "NSE")
    if uni.empty:
        st.error("No NSE FUTSTK contracts were found in the Dhan instrument master.")
        st.stop()

    sym = "underlying_symbol" if "underlying_symbol" in uni.columns else "symbol_name"
    uni[sym] = uni[sym].astype(str).str.upper().str.strip()
    selected = choose_rows(uni.sort_values(sym), max_scan)

    progress = st.progress(0)
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
            status, qualified = system_result(pnf, oi)
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
                "SL": pnf["sl"],
                "Reason": pnf["reason"],
            })
        except Exception as e:
            rows.append({
                "Symbol": symbol, "Sector": sector_of(symbol), "Bias":"ERROR",
                "Anchor":"❌","DTB":"❌","DBS":"❌","OI":"ERROR",
                "Price Δ%":np.nan,"OI Δ":np.nan,"System":"DATA ERROR","SL":np.nan,
                "Reason":str(e)[:250]
            })
        progress.progress(i/len(selected))
    progress.empty()

    res = pd.DataFrame(rows)
    breadth = sector_breadth(res)
    if not breadth.empty:
        res["⭐"] = res["Sector"].map(lambda s: stock_star(s, breadth, sector_threshold))
    else:
        res["⭐"] = ""

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Stocks scanned", len(res))
    c2.metric("BUY signals", int((res["System"]=="🟢 BUY").sum()))
    c3.metric("SELL signals", int((res["System"]=="🔴 SELL").sum()))
    c4.metric("Data errors", int((res["System"]=="DATA ERROR").sum()))

    st.subheader("System Results")
    st.dataframe(
        res[["⭐","Symbol","Sector","Bias","Anchor","DTB","DBS","OI","Price Δ%","OI Δ","System","SL","Reason"]],
        use_container_width=True, hide_index=True
    )

    buys = res[res["System"]=="🟢 BUY"]
    sells = res[res["System"]=="🔴 SELL"]
    a,b = st.columns(2)
    with a:
        st.markdown("### 🟢 BUY")
        st.dataframe(buys[["⭐","Symbol","Sector","System","SL","Reason"]], use_container_width=True, hide_index=True)
    with b:
        st.markdown("### 🔴 SELL")
        st.dataframe(sells[["⭐","Symbol","Sector","System","SL","Reason"]], use_container_width=True, hide_index=True)

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

    sym = "underlying_symbol" if "underlying_symbol" in uni.columns else "symbol_name"
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

    if st.session_state.last_error:
        st.error(st.session_state.last_error)

st.divider()
st.caption(f"Page refresh: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')} | API calls in this browser session: {len(st.session_state.api_log)}")
