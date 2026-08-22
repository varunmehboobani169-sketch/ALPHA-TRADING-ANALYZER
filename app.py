
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

    x["expiry_date"] = (
        pd.to_datetime(x["expiry_date"], errors="coerce")
        if "expiry_date" in x.columns
        else pd.NaT
    )
    now = pd.Timestamp.now()
    if x["expiry_date"].notna().any():
        x = x[x["expiry_date"].isna() | (x["expiry_date"] >= now)]

    if "underlying_symbol" not in x.columns:
        x["underlying_symbol"] = (
            x["trading_symbol"].astype(str).str.split("-", n=1).str[0].str.upper()
        )

    x = x.dropna(subset=["security_id"])
    rows = []
    for sym, g in x.groupby("underlying_symbol"):
        g = g.sort_values("expiry_date", na_position="last")
        rows.append(g.iloc[0])

    return pd.DataFrame(rows).reset_index(drop=True)

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

if auto:
    try:
        from streamlit_autorefresh import st_autorefresh
        mins = 1 if page in ("NSE Intraday P&F", "MCX Intraday") else 15
        if page == "Market Overview":
            mins = 3
        st_autorefresh(interval=mins * 60 * 1000, key=f"refresh_{page}")
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
    box = 0.0015 if mode == "Intraday" else 0.0025
    st.title(page)
    st.caption(
        "Core = cash/spot P&F. OI is calculated from the SAME active futures contract: "
        "Current OI − Previous OI, combined with futures price direction. "
        "A failure in OI will NOT create DATA ERROR in the P&F scan."
    )

    fut = future_universe(master, "NSE")
    if fut.empty:
        st.error("No NSE FUTSTK universe found.")
        st.stop()

    fut = fut.dropna(subset=["underlying_security_id"]).copy()
    spot_map = batch_ltp("NSE_EQ", fut["underlying_security_id"].astype(int).tolist())

    # PASS 1: core P&F only. This should always remain independent.
    rows = []
    progress = st.progress(0, text=f"Scanning P&F for {len(fut)} stocks...")
    for i, (_, r) in enumerate(fut.iterrows(), 1):
        symbol = str(r["underlying_symbol"])
        sid_cash = int(r["underlying_security_id"])
        try:
            h = historical(sid_cash, "NSE_EQ", "EQUITY", mode)
            p = analyze_new_pattern(h, box)
            rows.append({
                "Symbol": symbol,
                "Sector": sector_of(symbol),
                "Spot LTP": spot_map.get(sid_cash, np.nan),
                "Future ID": int(r["security_id"]),
                "Bias": p["bias"],
                "Pattern": p["pattern"],
                "Anchor": p["anchor_boxes"],
                "Pullback": p["pullback_boxes"],
                "System": (
                    "🟢 BUY" if p["dtb"] else
                    "🔴 SELL" if p["dbs"] else
                    "🟡 PROSPECTIVE" if p["prospective"] else
                    "WAIT"
                ),
                "Entry": p["entry_level"],
                "SL": p["sl"],
                "Reason": p["reason"],
            })
        except Exception as e:
            rows.append({
                "Symbol": symbol,
                "Sector": sector_of(symbol),
                "Spot LTP": spot_map.get(sid_cash, np.nan),
                "Future ID": int(r["security_id"]),
                "Bias": "ERROR",
                "Pattern": "DATA ERROR",
                "Anchor": 0,
                "Pullback": 0,
                "System": "DATA ERROR",
                "Entry": np.nan,
                "SL": np.nan,
                "Reason": str(e)[:250],
            })
        progress.progress(i / len(fut), text=f"P&F {i}/{len(fut)}")
    progress.empty()

    res = pd.DataFrame(rows)

    # PASS 2: OI from the exact SAME active futures contract.
    oi_enabled = st.checkbox("Enable OI confirmation", True, key=f"oi_{mode}")
    res["OI State"] = "OFF"
    res["Current OI"] = np.nan
    res["Previous OI"] = np.nan
    res["OI Δ"] = np.nan
    res["OI Δ %"] = np.nan
    res["OI Price Δ %"] = np.nan
    res["OI ⭐"] = False

    if oi_enabled and not res.empty:
        directional = res[res["Bias"].isin(["Bullish", "Bearish"])].copy()
        oi_bar = st.progress(0, text=f"Calculating same-contract futures OI for {len(directional)} directional stocks...")
        for j, (_, row) in enumerate(directional.iterrows(), 1):
            direction = "LONG" if row["Bias"] == "Bullish" else "SHORT"
            conf = classify_futures_oi(int(row["Future ID"]), direction)
            mask = res["Symbol"] == row["Symbol"]
            res.loc[mask, "OI State"] = conf["state"]
            res.loc[mask, "Current OI"] = conf["current_oi"]
            res.loc[mask, "Previous OI"] = conf["previous_oi"]
            res.loc[mask, "OI Δ"] = conf["oi_change"]
            res.loc[mask, "OI Δ %"] = conf["oi_change_pct"]
            res.loc[mask, "OI Price Δ %"] = conf["price_change_pct"]
            res.loc[mask, "OI ⭐"] = conf["star"]
            oi_bar.progress(j / len(directional))
        oi_bar.empty()

    # PASS 3: sector breadth from the completed P&F scan.
    res["Sector Breadth %"] = np.nan
    res["Sector ⭐"] = False
    for symbol in res["Symbol"].tolist():
        row = res[res["Symbol"] == symbol].iloc[0]
        direction = "LONG" if row["Bias"] == "Bullish" else "SHORT" if row["Bias"] == "Bearish" else None
        s = sector_breadth_star(res, symbol, direction)
        res.loc[res["Symbol"] == symbol, "Sector Breadth %"] = s["breadth"]
        res.loc[res["Symbol"] == symbol, "Sector ⭐"] = s["star"]

    # 3 normal stars + green additional star for the exact new 3-column setup.
    res["P&F ⭐"] = res["Bias"].isin(["Bullish", "Bearish"])
    res["Green ⭐"] = res["Pattern"].isin(["NEW PATTERN", "DTB", "DBS"])
    res["Star Count"] = (
        res["P&F ⭐"].astype(int)
        + res["OI ⭐"].astype(int)
        + res["Sector ⭐"].astype(int)
    )
    res["Stars"] = (
        res["P&F ⭐"].map(lambda x: "⭐" if x else "☆")
        + res["OI ⭐"].map(lambda x: "⭐" if x else "☆")
        + res["Sector ⭐"].map(lambda x: "⭐" if x else "☆")
        + res["Green ⭐"].map(lambda x: "🟢★" if x else "☆")
    )

    # Keep P&F entry independent from confirmations.
    res["System"] = np.where(
        res["Pattern"].eq("DTB"), "🟢 BUY",
        np.where(
            res["Pattern"].eq("DBS"), "🔴 SELL",
            np.where(res["Pattern"].eq("NEW PATTERN"), "🟡 PROSPECTIVE", res["System"])
        )
    )

    bullish = res[res["Bias"] == "Bullish"].sort_values(
        ["Star Count", "Green ⭐", "Anchor", "Pullback"],
        ascending=[False, False, False, True]
    )
    bearish = res[res["Bias"] == "Bearish"].sort_values(
        ["Star Count", "Green ⭐", "Anchor", "Pullback"],
        ascending=[False, False, False, True]
    )

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Stocks", len(res))
    c2.metric("Bullish", len(bullish))
    c3.metric("Bearish", len(bearish))
    c4.metric("⭐3", int((res["Star Count"] == 3).sum()))

    left,right = st.columns(2)
    with left:
        st.markdown("### 🟢 BULLISH")
        st.dataframe(
            bullish[
                ["Stars","Symbol","Sector","Spot LTP","Pattern","Anchor","Pullback",
                 "OI State","Current OI","Previous OI","OI Δ","OI Δ %","OI Price Δ %",
                 "Sector Breadth %","System","Entry","SL"]
            ],
            use_container_width=True, hide_index=True
        )
    with right:
        st.markdown("### 🔴 BEARISH")
        st.dataframe(
            bearish[
                ["Stars","Symbol","Sector","Spot LTP","Pattern","Anchor","Pullback",
                 "OI State","Current OI","Previous OI","OI Δ","OI Δ %","OI Price Δ %",
                 "Sector Breadth %","System","Entry","SL"]
            ],
            use_container_width=True, hide_index=True
        )

    st.caption(
        "Stars: ⭐ P&F | ⭐ OI confirmation | ⭐ Sector breadth | 🟢★ New 3-column pattern. "
        "P&F DTB/DBS remains the actual entry trigger."
    )

elif page in ("MCX Intraday", "MCX Positional"):
    mode = "Intraday" if page == "MCX Intraday" else "Positional"
    box = 0.0015 if mode == "Intraday" else 0.0025
    st.title(page)

    fut = future_universe(master, "MCX")
    if fut.empty:
        st.error("No MCX FUTCOM universe found.")
        st.stop()

    names = sorted(fut["underlying_symbol"].astype(str).unique())
    symbol = st.selectbox("Commodity", names)
    r = fut[fut["underlying_symbol"] == symbol].iloc[0]

    try:
        h = historical(
            r["security_id"],
            "MCX_COMM",
            "FUTCOM",
            mode,
        )
        p = analyze_new_pattern(h, box)
        st.write(p)
    except Exception as e:
        st.error(f"MCX data error: {e}")

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
