import json
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Dhan NIFTY Market Analyzer", page_icon="📊", layout="wide")

API_BASE = "https://api.dhan.co/v2"
UNDERLYING_SCRIP = 13  # NIFTY example used in Dhan option-chain docs
UNDERLYING_SEG = "IDX_I"


def headers(client_id: str, access_token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


def post_dhan(path: str, client_id: str, access_token: str, payload: dict):
    r = requests.post(f"{API_BASE}{path}", headers=headers(client_id, access_token), json=payload, timeout=15)
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"Dhan API error {r.status_code}: {detail}")
    return r.json()


@st.cache_data(ttl=60, show_spinner=False)
def get_expiries(client_id: str, access_token: str):
    result = post_dhan("/optionchain/expirylist", client_id, access_token, {
        "UnderlyingScrip": UNDERLYING_SCRIP,
        "UnderlyingSeg": UNDERLYING_SEG,
    })
    if result.get("status") != "success":
        raise RuntimeError(result)
    return result.get("data", [])


@st.cache_data(ttl=3, show_spinner=False)
def get_option_chain(client_id: str, access_token: str, expiry: str):
    result = post_dhan("/optionchain", client_id, access_token, {
        "UnderlyingScrip": UNDERLYING_SCRIP,
        "UnderlyingSeg": UNDERLYING_SEG,
        "Expiry": expiry,
    })
    if result.get("status") != "success":
        raise RuntimeError(result)
    return result.get("data", {})


def flatten_chain(data: dict) -> pd.DataFrame:
    rows = []
    for strike_s, pair in (data.get("oc") or {}).items():
        strike = float(strike_s)
        for opt in ("ce", "pe"):
            x = pair.get(opt) or {}
            g = x.get("greeks") or {}
            rows.append({
                "strike": strike,
                "type": opt.upper(),
                "security_id": x.get("security_id"),
                "ltp": x.get("last_price"),
                "oi": x.get("oi", 0),
                "prev_oi": x.get("previous_oi", 0),
                "change_oi": (x.get("oi", 0) or 0) - (x.get("previous_oi", 0) or 0),
                "volume": x.get("volume", 0),
                "iv": x.get("implied_volatility"),
                "delta": g.get("delta"),
                "gamma": g.get("gamma"),
                "theta": g.get("theta"),
                "vega": g.get("vega"),
                "bid": x.get("top_bid_price"),
                "ask": x.get("top_ask_price"),
                "avg_price": x.get("average_price"),
            })
    return pd.DataFrame(rows).sort_values(["strike", "type"]).reset_index(drop=True)


def analyze(df: pd.DataFrame, spot: float):
    strikes = sorted(df["strike"].unique())
    atm = min(strikes, key=lambda s: abs(s - spot))
    window = max(5, min(10, len(strikes) // 2))
    near = df[df["strike"].between(atm - (window * (strikes[1] - strikes[0]) if len(strikes) > 1 else 0),
                                   atm + (window * (strikes[1] - strikes[0]) if len(strikes) > 1 else 0))]

    ce = near[near.type == "CE"].copy()
    pe = near[near.type == "PE"].copy()

    total_call_oi = float(ce.oi.sum())
    total_put_oi = float(pe.oi.sum())
    pcr = total_put_oi / total_call_oi if total_call_oi else None

    max_ce = float(df[df.type == "CE"].groupby("strike").oi.sum().idxmax()) if not df.empty else None
    max_pe = float(df[df.type == "PE"].groupby("strike").oi.sum().idxmax()) if not df.empty else None

    # Simple, transparent first-pass scoring. This is not a trading recommendation.
    bull = 0
    bear = 0
    if pcr is not None:
        if pcr > 1.05:
            bull += 2
        elif pcr < 0.90:
            bear += 2
    ce_chg = ce.change_oi.sum()
    pe_chg = pe.change_oi.sum()
    if ce_chg > 0:
        bear += 1
    if pe_chg > 0:
        bull += 1
    if ce_chg < 0:
        bull += 1
    if pe_chg < 0:
        bear += 1

    if bull - bear >= 2:
        bias = "BULLISH"
    elif bear - bull >= 2:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    strength = min(100, 50 + abs(bull - bear) * 12)
    atm_row = near[(near.strike == atm)]
    atm_iv = float(atm_row.iv.dropna().mean()) if not atm_row.iv.dropna().empty else None

    return {
        "atm": atm,
        "pcr": pcr,
        "max_ce": max_ce,
        "max_pe": max_pe,
        "call_oi": total_call_oi,
        "put_oi": total_put_oi,
        "call_chg": float(ce_chg),
        "put_chg": float(pe_chg),
        "bias": bias,
        "strength": strength,
        "atm_iv": atm_iv,
    }


def fmt_num(x):
    if x is None or pd.isna(x):
        return "—"
    return f"{x:,.0f}"


def fmt_float(x, decimals=2):
    if x is None or pd.isna(x):
        return "—"
    return f"{x:,.{decimals}f}"


st.title("📊 Dhan NIFTY Market Analyzer")
st.caption("First prototype: DhanHQ V2 Option Chain + transparent OI/IV interpretation")

with st.sidebar:
    st.header("Connection")
    client_id = st.text_input("Dhan Client ID", value=st.secrets.get("DHAN_CLIENT_ID", ""), type="password")
    access_token = st.text_input("Dhan Access Token", value=st.secrets.get("DHAN_ACCESS_TOKEN", ""), type="password")
    st.divider()
    st.info("Option-chain requests are rate-limited by Dhan. This app caches chain data for 3 seconds.")

if not client_id or not access_token:
    st.warning("Enter your Dhan Client ID and Access Token in the sidebar, or put them in .streamlit/secrets.toml.")
    st.code('DHAN_CLIENT_ID = "your_client_id"\nDHAN_ACCESS_TOKEN = "your_access_token"', language="toml")
    st.stop()

try:
    expiries = get_expiries(client_id, access_token)
except Exception as e:
    st.error(f"Could not load Dhan expiries: {e}")
    st.stop()

if not expiries:
    st.error("Dhan returned no active expiries.")
    st.stop()

col1, col2, col3 = st.columns([1.3, 1, 1])
with col1:
    expiry = st.selectbox("Expiry", expiries, index=0)
with col2:
    st.metric("Data refresh", "3 sec cache")
with col3:
    if st.button("🔄 Refresh now", use_container_width=True):
        get_option_chain.clear()
        st.rerun()

try:
    data = get_option_chain(client_id, access_token, expiry)
    df = flatten_chain(data)
except Exception as e:
    st.error(f"Could not load option chain: {e}")
    st.stop()

spot = float(data.get("last_price", 0) or 0)
analysis = analyze(df, spot)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("NIFTY Spot", fmt_float(spot, 2))
c2.metric("ATM Strike", fmt_float(analysis["atm"], 0))
c3.metric("PCR (near ATM)", fmt_float(analysis["pcr"], 2))
c4.metric("ATM IV", fmt_float(analysis["atm_iv"], 2))
c5.metric("Bias", analysis["bias"])

st.subheader("Market Interpretation")
bi, m1, m2, m3 = st.columns([1.2, 1, 1, 1])
with bi:
    st.metric("Directional Score", f"{analysis['strength']:.0f}%")
with m1:
    st.metric("Highest Call OI", fmt_float(analysis["max_ce"], 0))
with m2:
    st.metric("Highest Put OI", fmt_float(analysis["max_pe"], 0))
with m3:
    st.metric("OI Pressure", "Call > Put" if analysis["call_chg"] > analysis["put_chg"] else "Put > Call")

if analysis["bias"] == "BULLISH":
    st.success("Bullish structure: near-ATM put OI is comparatively stronger and/or call-side OI pressure is easing.")
elif analysis["bias"] == "BEARISH":
    st.error("Bearish structure: near-ATM call OI is comparatively stronger and/or put-side OI is weakening.")
else:
    st.info("Mixed structure: current OI signals do not show a strong directional edge.")

st.subheader("Option Chain")
show_cols = ["strike", "CE", "PE"]
chain = df.pivot(index="strike", columns="type", values=["ltp", "oi", "change_oi", "volume", "iv", "delta", "bid", "ask"])
chain.columns = [f"{a}_{b}" for a, b in chain.columns]
chain = chain.reset_index()

for t in ["CE", "PE"]:
    for field in ["ltp", "oi", "change_oi", "volume", "iv", "delta", "bid", "ask"]:
        col = f"{field}_{t}"
        if col not in chain:
            chain[col] = None

display = chain[[
    "strike",
    "ltp_CE", "oi_CE", "change_oi_CE", "volume_CE", "iv_CE", "delta_CE",
    "ltp_PE", "oi_PE", "change_oi_PE", "volume_PE", "iv_PE", "delta_PE",
]].copy()
display.columns = [
    "Strike", "CE LTP", "CE OI", "CE ΔOI", "CE Volume", "CE IV", "CE Delta",
    "PE LTP", "PE OI", "PE ΔOI", "PE Volume", "PE IV", "PE Delta"
]

step = (sorted(df.strike.unique())[1] - sorted(df.strike.unique())[0]) if df.strike.nunique() > 1 else 50
window = 12 * step
focus = display[(display["Strike"] >= analysis["atm"] - window) & (display["Strike"] <= analysis["atm"] + window)]
st.dataframe(focus, use_container_width=True, hide_index=True)

st.subheader("OI Structure")
oi_plot = df.pivot(index="strike", columns="type", values="oi").fillna(0)
st.line_chart(oi_plot)

st.subheader("Current IV by Strike")
iv_plot = df.pivot(index="strike", columns="type", values="iv").dropna(how="all")
st.line_chart(iv_plot)

with st.expander("Raw Dhan response"):
    st.json(data)

st.caption(f"Last loaded: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Dhan API option-chain data")
