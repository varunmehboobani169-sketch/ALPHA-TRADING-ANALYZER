from pathlib import Path

import pandas as pd
import streamlit as st

from positional_option_selling import STRATEGY_PRESETS, StrategyConfig, backtest, combine_csvs


st.set_page_config(page_title="Positional Option Selling", page_icon="📈", layout="wide")
st.title("📈 Positional Option Selling")
st.caption("Historical NIFTY option-selling research • multi-year CSV backtest • strategy comparison")

with st.sidebar:
    st.header("Data")
    uploaded = st.file_uploader("Upload NIFTY option CSVs", type=["csv"], accept_multiple_files=True)
    local_files = sorted(Path("data").glob("*.csv"))
    if local_files:
        st.caption(f"Repository data found: {len(local_files)} CSV file(s)")
    else:
        st.caption("No CSVs are currently stored in /data")

    st.header("Backtest")
    run_mode = st.radio("Mode", ["Compare strategies", "Single strategy"], index=0)
    strategy_name = st.selectbox("Strategy", list(STRATEGY_PRESETS))
    hold_days = st.number_input("Holding period (trading days)", min_value=1, max_value=30, value=5, step=1)
    entry_time = st.text_input("Entry time", value="09:20")
    exit_time = st.text_input("Exit time", value="15:20")
    short_steps = st.number_input("Short-leg strike steps from ATM", min_value=0, max_value=10, value=1, step=1)
    wing_steps = st.number_input("Long-wing / wide strike steps", min_value=1, max_value=15, value=2, step=1)
    lot_size = st.number_input("NIFTY lot size", min_value=1, value=75, step=1)
    initial_capital = st.number_input("Initial capital", min_value=10000.0, value=1000000.0, step=10000.0)
    run = st.button("RUN BACKTEST", type="primary", use_container_width=True)


def load_uploaded(files):
    return combine_csvs(files)


def load_local(files):
    return combine_csvs(files)


def strategy_config(name: str) -> StrategyConfig:
    return StrategyConfig(
        name=name,
        entry_time=entry_time,
        exit_time=exit_time,
        hold_trading_days=int(hold_days),
        short_steps=int(short_steps),
        wing_steps=int(wing_steps),
    )


files = uploaded if uploaded else local_files

if not files:
    st.info("Upload the NIFTY option CSV files or place them in the repository /data folder to begin.")
    st.markdown("### Built-in strategy set")
    st.write("ATM Short Straddle • OTM Short Strangle • Wide OTM Strangle • Iron Condor")
    st.markdown("### Required historical fields")
    st.write("Date/Datetime, Expiry, Strike, CE/PE and Close/LTP. Spot/Underlying is strongly recommended for ATM selection.")
    st.stop()

try:
    data = load_uploaded(files) if uploaded else load_local(files)
except Exception as exc:
    st.error(f"Data load failed: {exc}")
    st.stop()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Rows", f"{len(data):,}")
c2.metric("Trading days", f"{data['date'].nunique():,}")
c3.metric("Strikes", f"{data['strike'].nunique():,}")
c4.metric("Expiries", f"{data['expiry'].dropna().nunique():,}")
c5.metric("Files", f"{data['source_file'].nunique():,}")

st.subheader("Data coverage")
st.write({
    "First timestamp": str(data["datetime"].min()),
    "Last timestamp": str(data["datetime"].max()),
    "CE rows": int((data["option_type"] == "CE").sum()),
    "PE rows": int((data["option_type"] == "PE").sum()),
    "Rows with expiry": int(data["expiry"].notna().sum()),
    "Rows with spot": int(data["spot"].notna().sum()),
})

st.subheader("Source files")
st.dataframe(
    data.groupby("source_file", dropna=False).agg(
        Rows=("close", "size"),
        First=("datetime", "min"),
        Last=("datetime", "max"),
        Trading_Days=("date", "nunique"),
        Expiries=("expiry", "nunique"),
    ).reset_index(),
    use_container_width=True,
    hide_index=True,
)

if run:
    names = list(STRATEGY_PRESETS) if run_mode == "Compare strategies" else [strategy_name]
    comparison = []
    trade_logs = {}

    for name in names:
        config = strategy_config(name)
        try:
            trades, stats = backtest(data, config, initial_capital=float(initial_capital), lot_size=int(lot_size))
            comparison.append({
                "Strategy": name,
                "Trades": stats["trades"],
                "Net P&L": stats["net_pnl"],
                "Return %": stats["return_pct"],
                "Win Rate %": stats["win_rate"],
                "Avg P&L": stats.get("avg_pnl", 0),
                "Max Drawdown": stats["max_drawdown"],
            })
            trade_logs[name] = trades
        except Exception as exc:
            comparison.append({
                "Strategy": name,
                "Trades": 0,
                "Net P&L": 0.0,
                "Return %": 0.0,
                "Win Rate %": 0.0,
                "Avg P&L": 0.0,
                "Max Drawdown": 0.0,
                "Error": str(exc),
            })

    summary = pd.DataFrame(comparison)
    st.subheader("Strategy comparison")
    st.dataframe(summary, use_container_width=True, hide_index=True)

    valid = summary[summary["Trades"] > 0]
    if not valid.empty:
        best = valid.sort_values("Net P&L", ascending=False).iloc[0]
        st.success(f"Best net P&L in this run: {best['Strategy']} — ₹{best['Net P&L']:,.0f}")

        selected_log = st.selectbox("Trade log", list(trade_logs))
        trades = trade_logs[selected_log]
        if not trades.empty:
            st.line_chart(trades.set_index("Exit Date")["Equity"])
            st.dataframe(trades, use_container_width=True, hide_index=True)
            st.download_button(
                "Download selected trade log CSV",
                data=trades.to_csv(index=False).encode("utf-8"),
                file_name=f"{selected_log.lower().replace(' ', '_')}_trades.csv",
                mime="text/csv",
            )
    else:
        st.warning("No valid trades were produced. Check the data timestamps, expiry coverage, and whether the dataset contains spot prices.")

st.divider()
st.subheader("Research guardrails")
st.write("This is a research backtest engine. It does not yet model brokerage, exchange charges, taxes, bid/ask spread, slippage, margin utilisation, early assignment/exercise, or special expiry-day rules. Those should be added before judging live-trading viability.")
