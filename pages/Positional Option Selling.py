import io
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from positional_option_selling import STRATEGY_PRESETS, StrategyConfig, backtest, combine_csvs, normalize_option_data


st.set_page_config(page_title="Positional Option Selling", page_icon="📈", layout="wide")
st.title("📈 Positional Option Selling")
st.caption("Historical NIFTY option-selling research • upload one ZIP • validate • backtest • compare")


def read_zip_dataset(uploaded_file):
    """Extract every CSV from one uploaded ZIP and normalize into one dataset."""
    raw_bytes = uploaded_file.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        members = [
            name for name in zf.namelist()
            if name.lower().endswith(".csv") and not name.startswith("__MACOSX/") and not name.endswith("/")
        ]
        if not members:
            raise ValueError("The ZIP contains no CSV files.")

        frames = []
        errors = []
        for member in members:
            try:
                with zf.open(member) as fh:
                    raw = pd.read_csv(fh)
                norm = normalize_option_data(raw)
                norm["source_file"] = member
                frames.append(norm)
            except Exception as exc:
                errors.append(f"{member}: {exc}")

        if errors:
            # Do not silently discard failed files; make the problem visible.
            st.warning("Some ZIP members could not be read:\n\n" + "\n".join(errors))
        if not frames:
            raise ValueError("No readable CSV files were found inside the ZIP.")

        data = pd.concat(frames, ignore_index=True)
        data = data.drop_duplicates().sort_values("datetime").reset_index(drop=True)
        return data, members


def read_csv_dataset(uploaded_file):
    raw = pd.read_csv(uploaded_file)
    data = normalize_option_data(raw)
    data["source_file"] = uploaded_file.name
    return data.drop_duplicates().sort_values("datetime").reset_index(drop=True), [uploaded_file.name]


def load_local_dataset():
    paths = sorted(Path("data").glob("*.csv"))
    if not paths:
        return None, []
    data = combine_csvs([str(p) for p in paths])
    return data, [p.name for p in paths]


def load_upload(uploaded_file):
    if uploaded_file.name.lower().endswith(".zip"):
        return read_zip_dataset(uploaded_file)
    return read_csv_dataset(uploaded_file)


with st.sidebar:
    st.header("Historical Data")
    uploaded = st.file_uploader(
        "Upload one NIFTY option ZIP",
        type=["zip", "csv"],
        accept_multiple_files=False,
        help="Preferred: one ZIP containing your NIFTY option CSVs for 2022–2026.",
    )
    st.caption("ZIP is the preferred format. Every CSV inside it is extracted, normalized and combined automatically.")

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


# Session persistence: the current uploaded dataset and its inventory remain available
# across Streamlit reruns during the dashboard session.
if uploaded is not None:
    try:
        data, members = load_upload(uploaded)
        st.session_state["positional_dataset"] = data
        st.session_state["positional_dataset_name"] = uploaded.name
        st.session_state["positional_dataset_files"] = members
    except Exception as exc:
        st.error(f"Data load failed: {exc}")
        st.stop()
elif "positional_dataset" in st.session_state:
    data = st.session_state["positional_dataset"]
    members = st.session_state.get("positional_dataset_files", [])
elif Path("data").exists():
    try:
        data, members = load_local_dataset()
    except Exception as exc:
        st.error(f"Repository data load failed: {exc}")
        st.stop()
else:
    data, members = None, []

if data is None or data.empty:
    st.info("Upload one ZIP containing the NIFTY option CSVs. The app will extract, validate and combine them automatically.")
    st.markdown("### Expected dataset")
    st.write("Date/Datetime, Expiry, Strike, CE/PE and Close/LTP. Spot/Underlying is strongly recommended for ATM selection.")
    st.markdown("### Strategy set")
    st.write("ATM Short Straddle • OTM Short Strangle • Wide OTM Strangle • Iron Condor")
    st.stop()

st.success(f"Loaded {len(members)} CSV file(s) from the current dataset.")
with st.expander("Files detected", expanded=False):
    st.write(members)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Rows", f"{len(data):,}")
c2.metric("Trading days", f"{data['date'].nunique():,}")
c3.metric("Strikes", f"{data['strike'].nunique():,}")
c4.metric("Expiries", f"{data['expiry'].dropna().nunique():,}")
c5.metric("Spot rows", f"{data['spot'].notna().sum():,}")

st.subheader("Detected data structure")
st.write("Canonical fields:", list(data.columns))
st.dataframe(data.head(20), use_container_width=True, hide_index=True)

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
        base = STRATEGY_PRESETS[name]
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
        st.warning("No valid trades were produced. Check timestamps, expiry coverage, and spot availability.")

st.divider()
st.subheader("Research guardrails")
st.write("This tab is the historical research layer. It does not yet assume a winning strategy. Costs, slippage, margin utilisation, event filters, expiry-day rules, adjustments and out-of-sample validation should be added before live-trading conclusions.")
