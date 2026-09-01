from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from positional_option_selling import STRATEGY_PRESETS, StrategyConfig, backtest, normalize_option_data
from positional_research_bot import analyze_patterns, discovery_candidates, research_report

st.set_page_config(page_title="Positional Option Selling Research Bot", page_icon="🤖", layout="wide")
st.title("🤖 Positional Option Selling — Research Bot")
st.caption("Upload historical NIFTY option data. The bot discovers recurring patterns, proposes research candidates, and validates them with backtests.")


def read_uploaded_files(uploaded_files):
    frames = []
    for item in uploaded_files:
        if item.name.lower().endswith(".csv"):
            raw = pd.read_csv(item)
            norm = normalize_option_data(raw)
            norm["source_file"] = item.name
            frames.append(norm)
        elif item.name.lower().endswith(".zip"):
            with zipfile.ZipFile(item) as z:
                members = [m for m in z.namelist() if m.lower().endswith(".csv") and not m.endswith("/")]
                if not members:
                    raise ValueError(f"No CSV files found inside {item.name}.")
                for member in members:
                    with z.open(member) as fh:
                        raw = pd.read_csv(fh)
                    norm = normalize_option_data(raw)
                    norm["source_file"] = Path(member).name
                    frames.append(norm)
        else:
            raise ValueError(f"Unsupported file type: {item.name}. Upload CSV or ZIP.")
    if not frames:
        raise ValueError("No CSV files were found in the upload.")
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["datetime", "expiry", "strike", "option_type", "close", "source_file"]
    )


def read_repository_data():
    paths = sorted(Path("data").glob("*.csv"))
    if not paths:
        return pd.DataFrame()
    frames = []
    for path in paths:
        raw = pd.read_csv(path)
        norm = normalize_option_data(raw)
        norm["source_file"] = path.name
        frames.append(norm)
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["datetime", "expiry", "strike", "option_type", "close", "source_file"]
    )


def build_strategy_config(name: str, hold_days: int, entry_time: str, exit_time: str, short_steps: int, wing_steps: int):
    base = STRATEGY_PRESETS[name]
    return StrategyConfig(
        name=base.name,
        entry_time=entry_time,
        exit_time=exit_time,
        hold_trading_days=hold_days,
        short_steps=short_steps,
        wing_steps=wing_steps,
    )


# ---------- Sidebar ----------
with st.sidebar:
    st.header("Dhan Login")
    client_id = st.text_input("Dhan Client ID", value="1113195747")
    access_token = st.text_input("Dhan Access Token", type="password")
    if client_id.strip() and access_token.strip():
        st.success("Dhan credentials entered")
    else:
        st.info("Optional for future live-data research. Historical research works without login.")

    st.divider()
    st.header("Historical Dataset")
    uploaded = st.file_uploader(
        "Upload one ZIP containing NIFTY option CSVs",
        type=["zip", "csv"],
        accept_multiple_files=True,
        help="Recommended: one ZIP with all historical NIFTY option CSV files.",
    )

    st.divider()
    st.header("Research Controls")
    run_research = st.button("RUN RESEARCH BOT", type="primary", use_container_width=True)
    st.caption("The bot first studies patterns, then tests candidate option-selling structures.")

    st.divider()
    st.header("Backtest Controls")
    hold_days = st.number_input("Holding period (trading days)", min_value=1, max_value=30, value=5, step=1)
    entry_time = st.text_input("Entry time", value="09:20")
    exit_time = st.text_input("Exit time", value="15:20")
    short_steps = st.number_input("Short-leg steps from ATM", min_value=0, max_value=10, value=1, step=1)
    wing_steps = st.number_input("Wing / wide steps", min_value=1, max_value=15, value=2, step=1)
    lot_size = st.number_input("NIFTY lot size", min_value=1, value=75, step=1)
    initial_capital = st.number_input("Initial capital", min_value=10000.0, value=1000000.0, step=10000.0)

# ---------- Data load ----------
files = uploaded
if files:
    try:
        data = read_uploaded_files(files)
        st.session_state["positional_data"] = data
        st.session_state["positional_sources"] = [f.name for f in files]
        st.session_state.pop("research_daily", None)
        st.session_state.pop("research_findings", None)
    except Exception as exc:
        st.error(f"Data load failed: {exc}")
        st.stop()
else:
    data = st.session_state.get("positional_data", pd.DataFrame())
    if data.empty:
        data = read_repository_data()
        if not data.empty:
            st.session_state["positional_data"] = data

if data.empty:
    st.info("Upload your NIFTY option ZIP/CSV to begin. The recommended format is one ZIP containing all historical datasets.")
    st.markdown("### What the bot will do")
    st.write("1) Validate the dataset  2) Detect recurring premium/skew patterns  3) Build research buckets  4) Test positional selling candidates  5) Report the strongest evidence")
    st.markdown("### Required fields")
    st.write("Date/Datetime, Expiry, Strike, CE/PE and Close/LTP. Spot/Underlying is strongly recommended.")
    st.stop()

# ---------- Data summary ----------
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Rows", f"{len(data):,}")
c2.metric("Trading days", f"{data['date'].nunique():,}")
c3.metric("Strikes", f"{data['strike'].nunique():,}")
c4.metric("Expiries", f"{data['expiry'].dropna().nunique():,}")
c5.metric("Source files", f"{data['source_file'].nunique():,}")

st.subheader("Data Coverage")
st.write({
    "First timestamp": str(data["datetime"].min()),
    "Last timestamp": str(data["datetime"].max()),
    "CE rows": int((data["option_type"] == "CE").sum()),
    "PE rows": int((data["option_type"] == "PE").sum()),
    "Rows with expiry": int(data["expiry"].notna().sum()),
    "Rows with spot": int(data["spot"].notna().sum()),
})

if run_research:
    with st.spinner("Research bot is scanning the historical option data..."):
        try:
            daily, findings = analyze_patterns(data)
            buckets = discovery_candidates(daily)
            report = research_report(daily, findings)
            st.session_state["research_daily"] = daily
            st.session_state["research_findings"] = findings
            st.session_state["research_buckets"] = buckets
            st.session_state["research_report"] = report
        except Exception as exc:
            st.error(f"Research failed: {exc}")

if "research_report" in st.session_state:
    st.subheader("Research Bot Report")
    st.text(st.session_state["research_report"])

    findings = st.session_state.get("research_findings", [])
    if findings:
        st.subheader("Pattern Findings")
        st.dataframe(pd.DataFrame([{"Pattern": f.title, "Evidence": f.evidence, "Implication": f.implication, "Strength": round(f.strength, 2)} for f in findings]), use_container_width=True, hide_index=True)

    buckets = st.session_state.get("research_buckets", pd.DataFrame())
    if not buckets.empty:
        st.subheader("Research Buckets")
        st.dataframe(buckets, use_container_width=True, hide_index=True)

    daily = st.session_state.get("research_daily", pd.DataFrame())
    if not daily.empty:
        st.subheader("Premium Pattern Series")
        st.line_chart(daily.set_index("date")["atm_straddle"])

    st.divider()
    st.subheader("Candidate Strategy Validation")
    names = list(STRATEGY_PRESETS)
    comparison = []
    trade_logs = {}
    for name in names:
        config = build_strategy_config(name, int(hold_days), entry_time, exit_time, int(short_steps), int(wing_steps))
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
            comparison.append({"Strategy": name, "Trades": 0, "Net P&L": 0.0, "Return %": 0.0, "Win Rate %": 0.0, "Avg P&L": 0.0, "Max Drawdown": 0.0, "Error": str(exc)})

    summary = pd.DataFrame(comparison)
    st.dataframe(summary, use_container_width=True, hide_index=True)
    valid = summary[summary["Trades"] > 0]
    if not valid.empty:
        best = valid.sort_values(["Net P&L", "Max Drawdown"], ascending=[False, False]).iloc[0]
        st.success(f"Top observed candidate by net P&L: {best['Strategy']} — ₹{best['Net P&L']:,.0f}")
        selected = st.selectbox("View trade log", list(trade_logs))
        trades = trade_logs[selected]
        if not trades.empty:
            st.line_chart(trades.set_index("Exit Date")["Equity"])
            st.dataframe(trades, use_container_width=True, hide_index=True)
            st.download_button("Download trade log", trades.to_csv(index=False).encode("utf-8"), f"{selected.lower().replace(' ', '_')}_trades.csv", "text/csv")

st.divider()
st.caption("Research conclusions are evidence-based hypotheses until confirmed with out-of-sample testing, realistic costs/slippage, and robustness checks.")
