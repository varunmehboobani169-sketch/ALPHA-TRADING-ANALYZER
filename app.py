import time
from datetime import datetime, date

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Dhan F&O Market Intelligence", page_icon="📊", layout="wide")

API_BASE = "https://api.dhan.co/v2"
INSTRUMENT_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"


def headers(client_id: str, access_token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


def dhan_post(path: str, client_id: str, access_token: str, payload: dict):
    r = requests.post(
        f"{API_BASE}{path}",
        headers=headers(client_id, access_token),
        json=payload,
        timeout=20,
    )
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"Dhan API error {r.status_code}: {detail}")
    return r.json()


@st.cache_data(ttl=3600, show_spinner=False)
def load_instruments() -> pd.DataFrame:
    df = pd.read_csv(INSTRUMENT_URL, low_memory=False)
    # Keep the columns needed for discovery and scanning.
    wanted = [
        "EXCH_ID", "SEGMENT", "INSTRUMENT", "UNDERLYING_SECURITY_ID",
        "UNDERLYING_SYMBOL", "SYMBOL_NAME", "DISPLAY_NAME", "INSTRUMENT_TYPE", "SECURITY_ID",
        "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE", "LOT_SIZE",
    ]
    present = [c for c in wanted if c in df.columns]
    df = df[present].copy()
    if "SM_EXPIRY_DATE" in df.columns:
        df["SM_EXPIRY_DATE"] = pd.to_datetime(df["SM_EXPIRY_DATE"], errors="coerce").dt.date
    for c in ["UNDERLYING_SECURITY_ID", "STRIKE_PRICE", "LOT_SIZE"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=60, show_spinner=False)
def get_expiries(client_id: str, access_token: str, underlying_scrip: int, underlying_seg: str):
    result = dhan_post(
        "/optionchain/expirylist",
        client_id,
        access_token,
        {"UnderlyingScrip": int(underlying_scrip), "UnderlyingSeg": underlying_seg},
    )
    if result.get("status") != "success":
        raise RuntimeError(result)
    return result.get("data", [])


@st.cache_data(ttl=3, show_spinner=False)
def get_option_chain(client_id: str, access_token: str, underlying_scrip: int, underlying_seg: str, expiry: str):
    result = dhan_post(
        "/optionchain",
        client_id,
        access_token,
        {
            "UnderlyingScrip": int(underlying_scrip),
            "UnderlyingSeg": underlying_seg,
            "Expiry": expiry,
        },
    )
    if result.get("status") != "success":
        raise RuntimeError(result)
    return result.get("data", {})


@st.cache_data(ttl=3, show_spinner=False)
def get_market_quotes(client_id: str, access_token: str, exchange_segment: str, security_ids: tuple):
    # Dhan supports up to 1000 instruments per Market Quote request.
    result = dhan_post(
        "/marketfeed/quote",
        client_id,
        access_token,
        {exchange_segment: [int(x) for x in security_ids]},
    )
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
                "oi": x.get("oi", 0) or 0,
                "prev_oi": x.get("previous_oi", 0) or 0,
                "change_oi": (x.get("oi", 0) or 0) - (x.get("previous_oi", 0) or 0),
                "volume": x.get("volume", 0) or 0,
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


def analyze_chain(df: pd.DataFrame, spot: float):
    if df.empty:
        return {}
    strikes = sorted(df["strike"].dropna().unique())
    atm = min(strikes, key=lambda s: abs(s - spot))
    step = strikes[1] - strikes[0] if len(strikes) > 1 else 50
    window = 10 * step
    near = df[df.strike.between(atm - window, atm + window)].copy()
    ce = near[near.type == "CE"].copy()
    pe = near[near.type == "PE"].copy()
    call_oi = float(ce.oi.sum())
    put_oi = float(pe.oi.sum())
    pcr = put_oi / call_oi if call_oi else None
    max_ce = float(df[df.type == "CE"].groupby("strike").oi.sum().idxmax())
    max_pe = float(df[df.type == "PE"].groupby("strike").oi.sum().idxmax())
    call_chg = float(ce.change_oi.sum())
    put_chg = float(pe.change_oi.sum())
    atm_rows = near[near.strike == atm]
    atm_iv = float(atm_rows.iv.dropna().mean()) if not atm_rows.iv.dropna().empty else None

    bull = 0
    bear = 0
    if pcr is not None:
        if pcr > 1.05:
            bull += 2
        elif pcr < 0.90:
            bear += 2
    if call_chg < 0:
        bull += 1
    elif call_chg > 0:
        bear += 1
    if put_chg > 0:
        bull += 1
    elif put_chg < 0:
        bear += 1

    diff = bull - bear
    if diff >= 2:
        bias = "BULLISH"
    elif diff <= -2:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return {
        "atm": atm,
        "pcr": pcr,
        "max_ce": max_ce,
        "max_pe": max_pe,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "call_chg": call_chg,
        "put_chg": put_chg,
        "atm_iv": atm_iv,
        "bias": bias,
        "strength": min(100, 50 + abs(diff) * 12),
    }


def build_option_table(df: pd.DataFrame, atm: float, step: float, window_steps: int = 12):
    chain = df.pivot(index="strike", columns="type", values=["ltp", "oi", "change_oi", "volume", "iv", "delta"])
    chain.columns = [f"{a}_{b}" for a, b in chain.columns]
    chain = chain.reset_index()
    needed = []
    for t in ["CE", "PE"]:
        for field in ["ltp", "oi", "change_oi", "volume", "iv", "delta"]:
            c = f"{field}_{t}"
            needed.append(c)
            if c not in chain:
                chain[c] = None
    display = chain[["strike"] + needed].copy()
    display.columns = [
        "Strike", "CE LTP", "CE OI", "CE ΔOI", "CE Volume", "CE IV", "CE Delta",
        "PE LTP", "PE OI", "PE ΔOI", "PE Volume", "PE IV", "PE Delta",
    ]
    return display[(display.Strike >= atm - window_steps * step) & (display.Strike <= atm + window_steps * step)]


def fmt_num(x):
    if x is None or pd.isna(x):
        return "—"
    return f"{x:,.0f}"


def fmt_float(x, decimals=2):
    if x is None or pd.isna(x):
        return "—"
    return f"{x:,.{decimals}f}"


def normalise_instrument_columns(df: pd.DataFrame) -> pd.DataFrame:
    # Some Dhan file versions expose detailed/compact names; map both when possible.
    aliases = {
        "SEM_EXM_EXCH_ID": "EXCH_ID",
        "SEM_SEGMENT": "SEGMENT",
        "SEM_INSTRUMENT_NAME": "INSTRUMENT",
        "SEM_EXPIRY_DATE": "SM_EXPIRY_DATE",
        "SEM_STRIKE_PRICE": "STRIKE_PRICE",
        "SEM_OPTION_TYPE": "OPTION_TYPE",
        "SEM_SMST_SECURITY_ID": "SECURITY_ID",
    }
    for old, new in aliases.items():
        if old in df.columns and new not in df.columns:
            df[new] = df[old]
    return df


def build_fno_universe(inst: pd.DataFrame):
    df = normalise_instrument_columns(inst.copy())
    today = date.today()
    # Stock and index futures, current/next expiry only.
    fut = df[(df.EXCH_ID == "NSE") & (df.INSTRUMENT.isin(["FUTSTK", "FUTIDX"]))].copy()
    fut = fut[fut.SM_EXPIRY_DATE.notna() & (fut.SM_EXPIRY_DATE >= today)].copy()
    fut = fut.sort_values(["UNDERLYING_SYMBOL", "SM_EXPIRY_DATE"])
    fut = fut.drop_duplicates(subset=["UNDERLYING_SYMBOL", "INSTRUMENT"], keep="first")
    return fut


def quote_frame(universe: pd.DataFrame, quote_data: dict) -> pd.DataFrame:
    rows = []
    for seg, values in quote_data.items():
        for sid, q in (values or {}).items():
            rows.append({
                "security_id": int(sid),
                "segment": seg,
                "ltp": q.get("last_price"),
                "net_change": q.get("net_change"),
                "open": (q.get("ohlc") or {}).get("open"),
                "close": (q.get("ohlc") or {}).get("close"),
                "high": (q.get("ohlc") or {}).get("high"),
                "low": (q.get("ohlc") or {}).get("low"),
                "volume": q.get("volume"),
                "oi": q.get("oi"),
                "oi_day_high": q.get("oi_day_high"),
                "oi_day_low": q.get("oi_day_low"),
                "buy_qty": q.get("buy_quantity"),
                "sell_qty": q.get("sell_quantity"),
            })
    qf = pd.DataFrame(rows)
    if qf.empty:
        return qf
    out = universe.merge(qf, left_on="SECURITY_ID", right_on="security_id", how="inner")
    out["day_pct"] = (out["net_change"] / out["close"].replace(0, pd.NA)) * 100
    out["buy_sell_imbalance"] = (
        (out["buy_qty"].fillna(0) - out["sell_qty"].fillna(0)) /
        (out["buy_qty"].fillna(0) + out["sell_qty"].fillna(0)).replace(0, pd.NA)
    ) * 100
    out["oi_position"] = ((out["oi"] - out["oi_day_low"]) /
                          (out["oi_day_high"] - out["oi_day_low"]).replace(0, pd.NA)) * 100

    # Transparent ranking score using live futures structure.
    # It is a ranking aid, not a guaranteed directional forecast.
    out["price_score"] = out["day_pct"].fillna(0).clip(-5, 5) * 8
    out["flow_score"] = out["buy_sell_imbalance"].fillna(0).clip(-100, 100) * 0.15
    # High current OI relative to today's range can add confidence to the move,
    # but we do not call it long/short buildup because quote API does not expose previous OI.
    out["oi_score"] = (out["oi_position"].fillna(50) - 50) * 0.12
    out["bullish_score"] = out["price_score"] + out["flow_score"] + out["oi_score"]
    out["bearish_score"] = -out["bullish_score"]
    out["bullish_score"] = out["bullish_score"].clip(-100, 100)
    out["bearish_score"] = out["bearish_score"].clip(-100, 100)
    out["bias"] = out["bullish_score"].apply(lambda x: "BULLISH" if x > 10 else ("BEARISH" if x < -10 else "NEUTRAL"))
    return out.sort_values("bullish_score", ascending=False).reset_index(drop=True)


# ---------------- UI ----------------
st.title("📊 Dhan F&O Market Intelligence")
st.caption("Live DhanHQ V2 market data • index/stock option chains • derivatives scanner")

with st.sidebar:
    st.header("Connection")
    client_id = st.text_input("Dhan Client ID", value=st.secrets.get("DHAN_CLIENT_ID", ""), type="password")
    access_token = st.text_input("Dhan Access Token", value=st.secrets.get("DHAN_ACCESS_TOKEN", ""), type="password")
    st.divider()
    st.caption("Keep credentials in Streamlit Secrets. Never commit the token to GitHub.")

if not client_id or not access_token:
    st.warning("Add your Dhan Client ID and Access Token in Streamlit Secrets or the sidebar.")
    st.code('DHAN_CLIENT_ID = "your_client_id"\nDHAN_ACCESS_TOKEN = "your_access_token"', language="toml")
    st.stop()

try:
    instruments = load_instruments()
except Exception as e:
    st.error(f"Could not load Dhan instrument master: {e}")
    st.stop()

# Build index/stock option universe from instrument master.
all_under = instruments[instruments["INSTRUMENT"].isin(["INDEX", "EQUITY"])].copy()
deriv_opts = instruments[instruments["INSTRUMENT"].isin(["OPTIDX", "OPTSTK"])].copy()
futures = build_fno_universe(instruments)

# Map index names to their underlying IDs for option chain.
index_candidates = instruments[(instruments["EXCH_ID"] == "NSE") & (instruments["INSTRUMENT"] == "INDEX")].copy()
index_candidates["name_key"] = index_candidates["SYMBOL_NAME"].astype(str).str.upper()

# Favor exact NIFTY/BANKNIFTY rows.
index_map = {}
for name in ["NIFTY", "BANKNIFTY"]:
    rows = index_candidates[index_candidates["name_key"] == name]
    if not rows.empty:
        index_map[name] = int(rows.iloc[0]["SECURITY_ID"]) if "SECURITY_ID" in rows.columns else None
index_map.setdefault("NIFTY", 13)

# Main navigation.
tab_index, tab_scanner, tab_stock, tab_info = st.tabs([
    "📈 Index Options",
    "🔥 F&O Stock Scanner",
    "🧩 Stock Option Analyzer",
    "ℹ️ System"
])

with tab_index:
    st.subheader("Index Option Chain")
    idx_col, exp_col = st.columns([1.2, 1])
    with idx_col:
        index_name = st.selectbox("Underlying", ["NIFTY", "BANKNIFTY"])
    underlying_id = index_map.get(index_name)
    if not underlying_id:
        st.error(f"Could not find {index_name} security ID in Dhan instrument master.")
        st.stop()
    try:
        expiries = get_expiries(client_id, access_token, underlying_id, "IDX_I")
    except Exception as e:
        st.error(f"Could not load {index_name} expiries: {e}")
        st.stop()
    with exp_col:
        expiry = st.selectbox("Expiry", expiries, index=0)
    if st.button("🔄 Refresh index data", key="refresh_index", use_container_width=True):
        get_option_chain.clear()
        get_expiries.clear()
        st.rerun()
    try:
        data = get_option_chain(client_id, access_token, underlying_id, "IDX_I", expiry)
        df = flatten_chain(data)
    except Exception as e:
        st.error(f"Could not load option chain: {e}")
        st.stop()
    spot = float(data.get("last_price", 0) or 0)
    a = analyze_chain(df, spot)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(f"{index_name} Spot", fmt_float(spot))
    c2.metric("ATM", fmt_float(a["atm"], 0))
    c3.metric("PCR", fmt_float(a["pcr"]))
    c4.metric("ATM IV", fmt_float(a["atm_iv"]))
    c5.metric("Bias", a["bias"])
    st.write(f"**Highest Call OI:** {fmt_float(a['max_ce'],0)}   |   **Highest Put OI:** {fmt_float(a['max_pe'],0)}")
    step = sorted(df.strike.unique())[1] - sorted(df.strike.unique())[0] if df.strike.nunique() > 1 else 50
    st.dataframe(build_option_table(df, a["atm"], step), use_container_width=True, hide_index=True)
    st.subheader("OI by Strike")
    st.line_chart(df.pivot(index="strike", columns="type", values="oi").fillna(0))
    st.subheader("IV by Strike")
    st.line_chart(df.pivot(index="strike", columns="type", values="iv").ffill().fillna(0))

with tab_scanner:
    st.subheader("🔥 Top 15 Bullish / Bearish F&O Stocks")
    st.caption("Ranks current NSE stock-futures structure using price change, buy/sell quantity imbalance and OI position within today's OI range.")
    col_a, col_b = st.columns([1, 1])
    with col_a:
        universe_limit = st.slider("Stocks to scan", 25, 250, 100, 25)
    with col_b:
        if st.button("🔄 Run F&O scanner", key="run_scanner", use_container_width=True):
            get_market_quotes.clear()
            st.rerun()

    stock_futs = futures[futures["INSTRUMENT"] == "FUTSTK"].copy()
    stock_futs = stock_futs.head(universe_limit)
    ids = tuple(stock_futs["SECURITY_ID"].dropna().astype(int).unique())
    if not ids:
        st.warning("No current NSE stock-futures contracts found in the Dhan instrument master.")
    else:
        with st.spinner("Reading live F&O quotes…"):
            try:
                qdata = get_market_quotes(client_id, access_token, "NSE_FNO", ids)
                sf = quote_frame(stock_futs, qdata)
            except Exception as e:
                st.error(f"Could not load F&O quotes: {e}")
                sf = pd.DataFrame()
        if not sf.empty:
            bull = sf.sort_values("bullish_score", ascending=False).head(15).copy()
            bear = sf.sort_values("bullish_score", ascending=True).head(15).copy()
            show_cols = ["SYMBOL_NAME", "SM_EXPIRY_DATE", "ltp", "day_pct", "volume", "oi", "buy_sell_imbalance", "oi_position", "bullish_score"]
            st.markdown("### 🟢 Top 15 Bullish")
            b = bull[show_cols].copy()
            b.columns = ["Stock", "Expiry", "LTP", "Day %", "Volume", "OI", "Buy/Sell Imb. %", "OI Position %", "Bull Score"]
            st.dataframe(b.style.format({"LTP":"{:,.2f}", "Day %":"{:,.2f}", "Volume":"{:,.0f}", "OI":"{:,.0f}", "Buy/Sell Imb. %":"{:,.1f}", "OI Position %":"{:,.1f}", "Bull Score":"{:,.1f}"}), use_container_width=True, hide_index=True)
            st.markdown("### 🔴 Top 15 Bearish")
            b2 = bear[show_cols].copy()
            b2.columns = ["Stock", "Expiry", "LTP", "Day %", "Volume", "OI", "Buy/Sell Imb. %", "OI Position %", "Bull Score"]
            st.dataframe(b2.style.format({"LTP":"{:,.2f}", "Day %":"{:,.2f}", "Volume":"{:,.0f}", "OI":"{:,.0f}", "Buy/Sell Imb. %":"{:,.1f}", "OI Position %":"{:,.1f}", "Bull Score":"{:,.1f}"}), use_container_width=True, hide_index=True)
            st.info("Scanner score is a market-state ranking aid. Dhan's Quote API does not expose previous-day OI in this response, so this fast scanner does not label moves as long/short buildup. Use the Stock Option Analyzer below for full option-chain confirmation.")

with tab_stock:
    st.subheader("🧩 Stock Option Analyzer")
    opt_stocks = deriv_opts[(deriv_opts["EXCH_ID"] == "NSE") & (deriv_opts["UNDERLYING_SECURITY_ID"].notna())].copy()
    names = sorted(opt_stocks["UNDERLYING_SYMBOL"].dropna().astype(str).unique().tolist())
    stock_name = st.selectbox("Stock underlying", names, index=0 if names else None)
    if stock_name:
        match = opt_stocks[opt_stocks["UNDERLYING_SYMBOL"].astype(str) == stock_name]
        underlying_id = int(match.iloc[0]["UNDERLYING_SECURITY_ID"])
        try:
            expiries = get_expiries(client_id, access_token, underlying_id, "NSE_EQ")
            expiry = st.selectbox("Expiry", expiries, key="stock_expiry")
            data = get_option_chain(client_id, access_token, underlying_id, "NSE_EQ", expiry)
            df = flatten_chain(data)
            spot = float(data.get("last_price", 0) or 0)
            a = analyze_chain(df, spot)
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Spot", fmt_float(spot))
            c2.metric("ATM", fmt_float(a["atm"], 0))
            c3.metric("PCR", fmt_float(a["pcr"]))
            c4.metric("ATM IV", fmt_float(a["atm_iv"]))
            c5.metric("Bias", a["bias"])
            st.write(f"**Resistance proxy:** {fmt_float(a['max_ce'],0)}   |   **Support proxy:** {fmt_float(a['max_pe'],0)}")
            step = sorted(df.strike.unique())[1] - sorted(df.strike.unique())[0] if df.strike.nunique() > 1 else 1
            st.dataframe(build_option_table(df, a["atm"], step), use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Could not analyse {stock_name}: {e}")

with tab_info:
    st.subheader("What this version does")
    st.markdown(
        """
        **Index Options:** NIFTY and BANKNIFTY option chains with OI, ΔOI, IV, Greeks, PCR and strike structure.\n\n
        **F&O Stock Scanner:** scans current NSE stock futures in one/batched Market Quote request and ranks the strongest bullish and bearish structures.\n\n
        **Stock Option Analyzer:** select any NSE F&O stock and inspect its full option chain and the same transparent option-structure analysis.\n\n
        The scanner is intentionally separated from the option-chain analyzer because Dhan's Option Chain endpoint is rate-limited much more tightly than Market Quote. This lets the broad scanner stay fast while detailed option-chain work remains on-demand.
        """
    )
