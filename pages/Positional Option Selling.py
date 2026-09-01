import io

import pandas as pd
import streamlit as st

from positional_option_selling import STRATEGY_PRESETS, StrategyConfig, backtest, combine_csvs, normalize_option_data


st.set_page_config(page_title="Positional Option Selling", page_icon="📈", layout="wide")
st.title("📈 Positional Option Selling")
st.caption("Historical NIFTY option-selling research • multi-year CSV backtest • strategy comparison")

with st.sidebar:
    st.header("Data")
    uploaded = st.file_uploader("Upload NIFTY option CSVs", type=["csv"], accept_multiple_files=True)
    data_note = st.text_area(
        "Expected fields",
        value="Date/Datetime, Expiry, Strike, CE/PE, Close; Spot/Underlying is strongly recommended.",
        disabled=True,
    )

    st.header("Backtest")
    strategy_name = st.selectbox("Strategy", list(STRATEGY_PRESETS))
    hold_days = st.number_input("Holding period (trading days)", min_value=1, max_value=30, value=5, step=1)
    entry_time = st.text_input("Entry time", value="09:20")
    exit_time = st.text_input("Exit time", value="15:20")
    short_steps = st.number_input("Short-leg strike steps from ATM", min_value=0, max_value=10, value=1, step=1)
    wing_steps = st.number_input("Long-wing / wide strike steps", min_value=1, max_value=15, value=2, step=1)
    lot_size = st.number_input("NIFTY lot size", min_value=1, value=75, step=1)
    initial_capital = st.number_input("Initial capital", min_value=10000.0, value=1000000.0, step=10000.0)
    run = st.button("RUN BACKTEST", type="primary", use_container_width=True)


def show_mapping_preview(uploaded_files):
    sample = pd.read_csv(uploaded_files[0], nrows=5)
    st.subheader("Detected data structure")
    st.write("Columns found:", list(sample.columns))
    st.dataframe(sample, use_container_width=True, hide_index=True)


if not uploaded:
    st.info("Upload the NIFTY option CSV files to begin. You can upload 2022, 2024, December 2024, 2025 and 2026 together.")
    st.markdown("### Built-in strategy set")
    st.write("ATM Short Straddle • OTM Short Strangle • Wide OTM Strangle • Iron Condor")
    st.markdown("### Important")
    st.write("The engine does not invent missing expiry or spot data. It will show a clear error when the dataset lacks fields required for a defensible positional backtest.")
    st.stop()

try:
    show_mapping_preview(uploaded)
    data = combine_csvs(uploaded)
except Exception as exc:
    st.error(f"Data load failed: {exc}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Rows", f"{len(data):,}")
c2.metric("Trading days", f"{data['date'].nunique():,}")
c3.metric("Strikes", f"{data['strike'].nunique():,}")
c4.metric("Expiries", f"{data['expiry'].dropna().nunique():,}")

st.subheader("Data coverage")
st.write({
    "First timestamp": str(data["datetime"].min()),
    "Last timestamp": str(data["datetime"].max()),
    "CE rows": int((data["option_type"] == "CE").sum()),
    "PE rows": int((data["option_type"] == "PE").sum()),
    "Rows with expiry": int(data["expiry"].notna().sum()),
    "Rows with spot": int(data["spot"].notna().sum()),
})

if run:
    base = STRATEGY_PRESETS[strategy_name]
    config = StrategyConfig(
        name=base.name,
        entry_time=entry_time,
        exit_time=exit_time,
        hold_trading_days=int(hold_days),
        short_steps=int(short_steps),
        wing_steps=int(wing_steps),
    )
    try:
        trades, stats = backtest(data, config, initial_capital=float(initial_capital), lot_size=int(lot_size))
    except Exception as exc:
        st.error(f"Backtest failed: {exc}")
        st.stop()

    st.subheader("Results")
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("Trades", stats["trades"])
    r2.metric("Net P&L", f"₹{stats['net_pnl']:,.0f}")
    r3.metric("Win rate", f"{stats['win_rate']:.1f}%")
    r4.metric("Avg P&L", f"₹{stats.get('avg_pnl', 0):,.0f}")
    r5.metric("Max drawdown", f"₹{stats['max_drawdown']:,.0f}")

    if trades.empty:
        st.warning("No valid trades were produced. This usually means the dataset lacks usable spot/expiry values, the entry/exit timestamps do not exist, or the requested holding period cannot be formed.")
    else:
        st.line_chart(trades.set_index("Exit Date")["Equity"])
        st.dataframe(trades, use_container_width=True, hide_index=True)
        st.download_button(
            "Download trade log CSV",
            data=trades.to_csv(index=False).encode("utf-8"),
            file_name=f"{strategy_name.lower().replace(' ', '_')}_trades.csv",
            mime="text/csv",
        )

st.divider()
st.subheader("Next research layer")
st.write("Once the historical schema is confirmed, this page can be extended to compare all strategies across the same date set, include costs/slippage, expiry-day handling, stop/target exits, and parameter sweeps without changing the raw data.")
