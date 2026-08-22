
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
# Ranking engine
# -----------------------------
SECTOR_MAP = {
    "RELIANCE":"Energy","ONGC":"Energy","COALINDIA":"Energy","IOC":"Energy","BPCL":"Energy","GAIL":"Energy",
    "HDFCBANK":"Banking","ICICIBANK":"Banking","SBIN":"Banking","AXISBANK":"Banking","KOTAKBANK":"Banking",
    "INDUSINDBK":"Banking","BANKBARODA":"Banking","PNB":"Banking","FEDERALBNK":"Banking","IDFCFIRSTB":"Banking",
    "BAJFINANCE":"Financials","BAJAJFINSV":"Financials","SHRIRAMFIN":"Financials","CHOLAFIN":"Financials","MUTHOOTFIN":"Financials",
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
for _sym, _sector in SECTOR_MAP.items():
    SECTOR_BASKETS.setdefault(_sector, []).append(_sym)

def sector_of(symbol):
    return SECTOR_MAP.get(str(symbol).upper(), "Other")

def batch_future_quotes(future_df):
    """
    Fetch current futures LTP + OI.

    Dhan's /marketfeed/ltp returns LTP only. OI is available from
    /marketfeed/quote, so use the Quote endpoint for futures.
    The API supports up to 1000 instruments/request; we use chunks of 500
    to make the scanner more tolerant of a bad/invalid instrument ID.
    """
    if future_df.empty:
        return {}

    ids = [int(x) for x in future_df["security_id"].dropna().tolist()]
    result = {}

    chunk_size = 500
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        body = api_post(
            "/marketfeed/quote",
            {"NSE_FNO": chunk},
            "NSE futures LTP/OI quote",
        )

        data = parse_data(body).get("NSE_FNO", {})
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    result[int(k)] = v

    return result


def previous_oi_from_history(sec_id):
    # One request per selected candidate, intentionally only used for ranking.
    end = datetime.now().date()
    start = end - timedelta(days=5)
    payload = {
        "securityId": str(int(sec_id)),
        "exchangeSegment": "NSE_FNO",
        "instrument": "FUTSTK",
        "expiryCode": 0,
        "oi": True,
        "fromDate": str(start),
        "toDate": str(end + timedelta(days=1)),
    }
    body = api_post("/charts/historical", payload, "previous futures OI")
    data = parse_data(body)
    if not isinstance(data, dict) or "open_interest" not in data:
        return np.nan
    s = pd.to_numeric(pd.Series(data["open_interest"]), errors="coerce").dropna()
    return float(s.iloc[-1]) if not s.empty else np.nan

def sector_relative_strength_map(master, symbols, mode):
    """
    Lightweight sector/NIFTY relative-strength ranking.
    This version uses today's spot day change as the sector basket proxy,
    so it adds ranking information without multiplying historical API calls.
    P&F remains the primary stock pattern.
    """
    result = {s: {"sector": sector_of(s), "sector_bias": "UNAVAILABLE", "sector_star": False} for s in symbols}

    # Calculate per-sector average spot day change from the rows already available.
    # The caller fills Spot Day %; no extra API calls are made here.
    return result

def rank_rows(rows, future_quotes):
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Sector
    df["Sector"] = df["Symbol"].map(sector_of)

    # Futures OI confirmation:
    # current OI vs previous daily OI will be filled only for candidates.
    df["OI State"] = "UNAVAILABLE"
    df["OI Δ"] = np.nan
    return df

def apply_rank_stars(df, previous_oi_map):
    if df.empty:
        return df

    # Sector strength based on P&F breadth among the scanned stocks.
    breadth = (
        df.groupby("Sector")
          .agg(
              valid=("Symbol", "count"),
              bullish=("Bias", lambda s: (s == "Bullish").sum()),
              bearish=("Bias", lambda s: (s == "Bearish").sum())
          )
          .reset_index()
    )
    breadth["bullish_pct"] = 100 * breadth["bullish"] / breadth["valid"].replace(0, np.nan)
    breadth["bearish_pct"] = 100 * breadth["bearish"] / breadth["valid"].replace(0, np.nan)

    bmap = breadth.set_index("Sector").to_dict("index")

    def sector_star(row):
        b = bmap.get(row["Sector"])
        if not b:
            return False
        if row["Bias"] == "Bullish":
            return b["bullish_pct"] >= 50
        if row["Bias"] == "Bearish":
            return b["bearish_pct"] >= 50
        return False

    # P&F star: any directional P&F.
    df["PF Star"] = df["Bias"].isin(["Bullish", "Bearish"])

    # Green star: exact new 3-column pattern is running or confirmed.
    df["Green Star"] = df["Pattern"].isin(["NEW PATTERN", "DTB", "DBS"])

    # OI star: price direction + positive futures OI change.
    oi_ok = []
    for _, row in df.iterrows():
        sid = int(row["Future ID"]) if pd.notna(row["Future ID"]) else None
        prev = previous_oi_map.get(sid, np.nan) if sid is not None else np.nan
        cur = row["Future OI"]
        if pd.isna(cur) or pd.isna(prev):
            oi_ok.append(False)
        else:
            d_oi = float(cur) - float(prev)
            oi_ok.append(d_oi > 0)
    df["OI Star"] = oi_ok

    df["Sector Star"] = df.apply(sector_star, axis=1)
    df["Stars"] = (
        df["PF Star"].astype(int)
        + df["OI Star"].astype(int)
        + df["Sector Star"].astype(int)
    )
    df["Star Display"] = (
        df["PF Star"].map(lambda x: "⭐" if x else "☆")
        + df["OI Star"].map(lambda x: "⭐" if x else "☆")
        + df["Sector Star"].map(lambda x: "⭐" if x else "☆")
        + df["Green Star"].map(lambda x: "🟢★" if x else "☆")
    )
    return df


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

    fut = future_universe(master, "NSE")
    if fut.empty:
        st.error("No NSE FUTSTK universe found.")
        st.stop()

    fut = fut.dropna(subset=["underlying_security_id"]).copy()

    # One batched live cash quote for the complete universe.
    spot_map = batch_ltp(
        "NSE_EQ",
        fut["underlying_security_id"].astype(int).tolist()
    )

    # One batched futures LTP/OI quote for the complete universe.
    future_quote_map = batch_future_quotes(fut)

    # We use previous OI only for ranking candidates. To avoid excessive API usage,
    # fetch it after P&F classification for stars only on currently bullish/bearish rows.
    rows = []
    progress = st.progress(0, text=f"Scanning {len(fut)} stocks...")
    for i, (_, r) in enumerate(fut.iterrows(), 1):
        symbol = str(r["underlying_symbol"])
        sid_cash = int(r["underlying_security_id"])
        sid_future = int(r["security_id"])
        spot = spot_map.get(sid_cash, np.nan)
        q = future_quote_map.get(sid_future, {})
        future_ltp = pd.to_numeric(q.get("last_price"), errors="coerce")
        future_oi = pd.to_numeric(q.get("oi"), errors="coerce")

        try:
            h = historical(sid_cash, "NSE_EQ", "EQUITY", mode)
            p = analyze_new_pattern(h, box)

            rows.append({
                "Symbol": symbol,
                "Sector": sector_of(symbol),
                "Spot LTP": spot,
                "Future LTP": future_ltp,
                "Future OI": future_oi,
                "Future ID": sid_future,
                "Bias": p["bias"],
                "Pattern": p["pattern"],
                "Anchor": p["anchor_boxes"],
                "Pullback": p["pullback_boxes"],
                "System": (
                    "🟢 BUY" if p["dtb"]
                    else "🔴 SELL" if p["dbs"]
                    else "🟡 PROSPECTIVE" if p["prospective"]
                    else "WAIT"
                ),
                "Entry": p["entry_level"],
                "SL": p["sl"],
                "Reason": p["reason"],
            })
        except Exception as e:
            rows.append({
                "Symbol": symbol,
                "Sector": sector_of(symbol),
                "Spot LTP": spot,
                "Future LTP": future_ltp,
                "Future OI": future_oi,
                "Future ID": sid_future,
                "Bias": "ERROR",
                "Pattern": "DATA ERROR",
                "Anchor": 0,
                "Pullback": 0,
                "System": "DATA ERROR",
                "Entry": np.nan,
                "SL": np.nan,
                "Reason": str(e)[:250],
            })
        progress.progress(i / len(fut), text=f"Scanning {i}/{len(fut)}")
    progress.empty()

    res = pd.DataFrame(rows)

    # Previous daily OI only for rows with a valid P&F direction/new pattern.
    candidate_ids = [
        int(x) for x in res.loc[
            res["Bias"].isin(["Bullish", "Bearish"]),
            "Future ID"
        ].dropna().unique()
    ]
    previous_oi = {}
    oi_bar = st.progress(0, text=f"Reading previous OI for {len(candidate_ids)} candidates...")
    for j, sid in enumerate(candidate_ids, 1):
        try:
            previous_oi[sid] = previous_oi_from_history(sid)
        except Exception:
            previous_oi[sid] = np.nan
        if candidate_ids:
            oi_bar.progress(j / len(candidate_ids))
    oi_bar.empty()

    res = apply_rank_stars(res, previous_oi)

    # Show only bullish and bearish stocks in their respective columns.
    bullish = res[res["Bias"] == "Bullish"].copy()
    bearish = res[res["Bias"] == "Bearish"].copy()

    bullish = bullish.sort_values(
        ["Stars", "Green Star", "Anchor", "Pullback", "Symbol"],
        ascending=[False, False, False, True, True]
    )
    bearish = bearish.sort_values(
        ["Stars", "Green Star", "Anchor", "Pullback", "Symbol"],
        ascending=[False, False, False, True, True]
    )

    a,b,c,d = st.columns(4)
    a.metric("F&O stocks", len(res))
    b.metric("Bullish", len(bullish))
    c.metric("Bearish", len(bearish))
    d.metric("3-star", int((res["Stars"] == 3).sum()))

    st.caption("Stars: ⭐ P&F pattern | ⭐ OI buildup | ⭐ Sector P&F breadth | 🟢★ New 3-column pattern")

    left, right = st.columns(2)
    with left:
        st.markdown("### 🟢 BULLISH")
        if bullish.empty:
            st.info("No bullish P&F stocks.")
        else:
            st.dataframe(
                bullish[
                    ["Star Display","Symbol","Sector","Spot LTP","Future LTP","Future OI",
                     "Pattern","Anchor","Pullback","System","Entry","SL"]
                ],
                use_container_width=True, hide_index=True
            )
    with right:
        st.markdown("### 🔴 BEARISH")
        if bearish.empty:
            st.info("No bearish P&F stocks.")
        else:
            st.dataframe(
                bearish[
                    ["Star Display","Symbol","Sector","Spot LTP","Future LTP","Future OI",
                     "Pattern","Anchor","Pullback","System","Entry","SL"]
                ],
                use_container_width=True, hide_index=True
            )

    st.markdown("### ⭐⭐⭐ Top Ranked Setups")
    top3 = res[res["Stars"] == 3].copy()
    if top3.empty:
        st.info("No 3-star setups currently.")
    else:
        st.dataframe(
            top3[
                ["Star Display","Symbol","Sector","Bias","Pattern","Anchor","Pullback",
                 "Spot LTP","Future OI","System","Entry","SL","Reason"]
            ],
            use_container_width=True, hide_index=True
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
