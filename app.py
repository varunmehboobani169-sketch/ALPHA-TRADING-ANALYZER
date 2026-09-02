from __future__ import annotations

import time
import pandas as pd
import streamlit as st

from nifty_collector import capture_snapshot, expiries, WINDOW, STRIKE_STEP

st.set_page_config(page_title="NIFTY ATM ±20 Collector", page_icon="📊", layout="wide")

if "data" not in st.session_state:
    st.session_state.data = pd.DataFrame()
if "connected" not in st.session_state:
    st.session_state.connected = False
if "expiry_list" not in st.session_state:
    st.session_state.expiry_list = []
if "running" not in st.session_state:
    st.session_state.running = False

with st.sidebar:
    st.header("🔐 Dhan Login")
    client_id = st.text_input("Dhan Client ID", value="1113195747")
    access_token = st.text_input("Dhan Access Token", type="password")
    if st.button("Connect to Dhan", use_container_width=True, type="primary"):
        if not access_token.strip():
            st.error("Enter your Dhan Access Token.")
        else:
            try:
                st.session_state.expiry_list = expiries(client_id, access_token)
                st.session_state.connected = True
                st.success("Dhan connected")
            except Exception as exc:
                st.session_state.connected = False
                st.error(f"Connection failed: {exc}")

    if st.session_state.connected:
        st.success("API status: Connected")
    st.divider()
    st.subheader("Collection")
    interval = st.number_input("Interval (seconds)", min_value=3, max_value=60, value=5, step=1)
    expiry_mode = st.radio("Expiry", ["Nearest active", "Choose expiry"])
    if st.session_state.connected and st.session_state.expiry_list:
        expiry = st.session_state.expiry_list[0] if expiry_mode == "Nearest active" else st.selectbox("Expiry", st.session_state.expiry_list)
    else:
        expiry = None
    st.divider()
    st.write("**Universe**")
    st.write("NIFTY ATM−20 … ATM … ATM+20")
    st.write("41 strikes × CE/PE = 82 rows/snapshot")
    st.write(f"Strike step: {STRIKE_STEP} points")
    if st.button("Clear collected data", use_container_width=True):
        st.session_state.data = pd.DataFrame()
        st.session_state.running = False
        st.rerun()
    st.caption("The access token is held only in the current Streamlit session; do not commit it to GitHub.")

st.title("📊 NIFTY Options Data Collection Dashboard")
st.caption("Clean collection layer for future backtesting and strategy research.")

if not st.session_state.connected:
    st.info("Connect to Dhan from the left sidebar to begin.")
    st.stop()
if not expiry:
    st.error("No active NIFTY option expiry found.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
if c1.button("▶ Start", type="primary", use_container_width=True):
    st.session_state.running = True
if c2.button("⏹ Stop", use_container_width=True):
    st.session_state.running = False
c3.metric("Selected expiry", expiry)
snapshots = int(st.session_state.data["captured_at"].nunique()) if not st.session_state.data.empty else 0
c4.metric("Snapshots", f"{snapshots:,}")

if st.session_state.running:
    try:
        snap = capture_snapshot(client_id, access_token, expiry)
        st.session_state.data = pd.concat([st.session_state.data, snap], ignore_index=True)
    except Exception as exc:
        st.error(f"Collection error: {exc}")

if not st.session_state.data.empty:
    df = st.session_state.data.copy()
    latest_ts = df["captured_at"].max()
    latest = df[df["captured_at"] == latest_ts].copy()
    spot = float(latest["spot"].iloc[0])
    atm = int(latest["atm"].iloc[0])

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("NIFTY Spot", f"{spot:,.2f}")
    m2.metric("ATM", f"{atm:,}")
    m3.metric("Rows", f"{len(latest):,}/82")
    m4.metric("Snapshots", f"{snapshots:,}")
    m5.metric("Last capture", str(latest_ts)[11:19])

    st.subheader("Current ATM ±20 Chain")
    ce = latest[latest.option_type == "CE"].set_index("strike")
    pe = latest[latest.option_type == "PE"].set_index("strike")
    table = pd.DataFrame({"Strike": sorted(set(ce.index).union(pe.index))})
    table["Offset"] = ((table["Strike"] - atm) / STRIKE_STEP).astype(int)
    table["Level"] = table["Offset"].map(lambda x: "ATM" if x == 0 else f"ATM{x:+d}")
    for side, frame in (("CE", ce), ("PE", pe)):
        for field, label in (("last_price", "LTP"), ("oi", "OI"), ("oi_change", "OI Chg"), ("volume", "Volume"), ("iv", "IV"), ("vega", "Vega"), ("bid", "Bid"), ("ask", "Ask")):
            table[f"{side} {label}"] = table["Strike"].map(frame[field])
    st.dataframe(table, use_container_width=True, hide_index=True)

    st.subheader("ATM Summary")
    if atm in ce.index and atm in pe.index:
        a = st.columns(6)
        a[0].metric("ATM CE", f"{float(ce.loc[atm, 'last_price']):,.2f}")
        a[1].metric("ATM PE", f"{float(pe.loc[atm, 'last_price']):,.2f}")
        a[2].metric("Straddle", f"{float(ce.loc[atm, 'last_price']) + float(pe.loc[atm, 'last_price']):,.2f}")
        a[3].metric("CE OI", f"{int(ce.loc[atm, 'oi']):,}")
        a[4].metric("PE OI", f"{int(pe.loc[atm, 'oi']):,}")
        avg_iv = (float(ce.loc[atm, 'iv']) + float(pe.loc[atm, 'iv'])) / 2
        a[5].metric("ATM Avg IV", f"{avg_iv:.2f}")

    st.subheader("Collection Quality")
    q = st.columns(4)
    q[0].metric("Rows/snapshot", f"{len(latest)}/82")
    q[1].metric("Missing LTP", f"{int(latest['last_price'].isna().sum())}")
    q[2].metric("Missing OI", f"{int(latest['oi'].isna().sum())}")
    q[3].metric("Strikes captured", f"{latest['strike'].nunique()}/41")

    st.subheader("Collected Data")
    st.dataframe(df.sort_values("captured_at", ascending=False).head(82 * 10), use_container_width=True, hide_index=True)
    st.download_button("⬇ Download CSV", df.to_csv(index=False).encode("utf-8"), f"nifty_atm_pm20_{expiry}.csv", "text/csv", use_container_width=True)
else:
    st.info("No snapshots yet. Press Start to collect the first ATM ±20 snapshot.")

if st.session_state.running:
    st.caption(f"Collector running — polling every {interval} seconds.")
    time.sleep(int(interval))
    st.rerun()
