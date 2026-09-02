from __future__ import annotations

import json
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st

from auth import FIXED_CLIENT_ID, is_authenticated, credentials, login_form, logout_button

st.set_page_config(page_title="NIFTY 1-Min Historical Collector", page_icon="📊", layout="wide")

with st.sidebar:
    st.header("🔐 Dhan Login")
    if is_authenticated():
        st.success(f"Connected • {FIXED_CLIENT_ID}")
        logout_button()
    else:
        st.caption("Use your Dhan Access Token. It stays in the Streamlit session and is never written to GitHub.")
        login_form()

st.title("📊 NIFTY Weekly Options — Historical Data Collector")
st.caption("Six-month blocks • 1-minute candles • weekly expiry • dynamic ATM ±20 • OI + Volume + IV + Greeks")

if not is_authenticated():
    st.info("Log in from the left sidebar to access the historical collector.")
    st.stop()

client_id, token = credentials()
out_dir = Path("data/historical")
out_dir.mkdir(parents=True, exist_ok=True)

with st.sidebar:
    st.divider()
    st.subheader("Historical Range")
    period = st.selectbox("Six-month block", [
        "2024 H1  • Jan–Jun", "2024 H2  • Jul–Dec",
        "2025 H1  • Jan–Jun", "2025 H2  • Jul–Dec",
        "2026 H1  • Jan–Jun", "2026 H2  • Jul–Dec",
    ], index=0)
    rf = st.number_input("Risk-free rate (%)", min_value=0.0, max_value=20.0, value=6.5, step=0.25)
    start_button = st.button("🚀 DOWNLOAD SIX MONTHS", type="primary", use_container_width=True)
    st.caption("The backend automatically chunks the selected six-month period into API-safe windows and resumes completed expiries.")

ranges = {
    "2024 H1  • Jan–Jun": ("2024-01-01", "2024-07-01"),
    "2024 H2  • Jul–Dec": ("2024-07-01", "2025-01-01"),
    "2025 H1  • Jan–Jun": ("2025-01-01", "2025-07-01"),
    "2025 H2  • Jul–Dec": ("2025-07-01", "2026-01-01"),
    "2026 H1  • Jan–Jun": ("2026-01-01", "2026-07-01"),
    "2026 H2  • Jul–Dec": ("2026-07-01", "2027-01-01"),
}
start, end = ranges[period]
job_dir = out_dir / period.split("  ")[0].replace(" ", "_")
job_dir.mkdir(parents=True, exist_ok=True)

summary_path = job_dir / "job_summary.json"
if summary_path.exists():
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("status") == "complete":
            st.success(f"Completed: {period} • {summary.get('expiry_done', 0)}/{summary.get('expiry_total', 0)} weekly expiries • {summary.get('rows', 0):,} rows")
    except Exception:
        pass

st.subheader("Collection specification")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Period", period.split("  ")[0])
c2.metric("Frequency", "1 minute")
c3.metric("Universe", "ATM −20 … +20")
c4.metric("Sides", "CE + PE")
c5.metric("Per minute", "Up to 82 contracts")

st.info("The backend breaks the six-month range into chunks of at most 90 days, discovers NIFTY weekly contracts from Dhan's instrument master, retrieves 1-minute OHLC/OI/Volume, maps each minute to the actual ATM strike using NIFTY spot, and keeps only ATM−20 through ATM+20. Each weekly expiry is checkpointed separately.")

progress_bar = st.progress(0.0)
status_box = st.empty()

if start_button:
    try:
        # Lazy import: keeps the Streamlit UI bootable even if a data-processing module has an environment issue.
        from historical_nifty import JobConfig, run_job

        def update_progress(p):
            total = max(int(p.get("expiry_total", 1)), 1)
            done = int(p.get("expiry_done", 0))
            progress_bar.progress(min(done / total, 1.0))
            status_box.write(
                f"**{p.get('status', 'running').upper()}** — expiry {p.get('expiry', 'preparing')} — "
                f"{done}/{total} expiries — {int(p.get('rows', 0)):,} rows — "
                f"failed contracts: {int(p.get('failed_contracts', 0)):,}"
            )

        result = run_job(
            client_id,
            token,
            JobConfig(start=start, end=end, risk_free_rate=float(rf) / 100.0),
            job_dir,
            progress_cb=update_progress,
        )
        progress_bar.progress(1.0)
        status_box.success(f"Historical collection completed for {period}. {result.get('rows', 0):,} rows written.")
    except Exception as exc:
        status_box.error(f"Collector failed: {exc}")
        with st.expander("Technical error details"):
            st.code(traceback.format_exc())

files = sorted(job_dir.glob("nifty_weekly_*.parquet"))
if files:
    st.subheader("Saved weekly datasets")
    rows = []
    for path in files:
        try:
            d = pd.read_parquet(path, columns=["timestamp", "expiry", "strike_offset", "option_type"])
            rows.append({"File": path.name, "Expiry": str(d["expiry"].iloc[0]) if len(d) else "", "Rows": len(d), "Strikes observed": d["strike_offset"].nunique() if len(d) else 0, "Size MB": round(path.stat().st_size / 1e6, 1)})
        except Exception as exc:
            rows.append({"File": path.name, "Expiry": f"read error: {exc}", "Rows": 0, "Strikes observed": 0, "Size MB": round(path.stat().st_size / 1e6, 1)})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    chosen = st.selectbox("Preview dataset", [p.name for p in files])
    preview_path = job_dir / chosen
    try:
        preview = pd.read_parquet(preview_path)
        st.dataframe(preview.head(300), use_container_width=True, hide_index=True)
        st.download_button("⬇ Download selected weekly Parquet", preview_path.read_bytes(), file_name=chosen, mime="application/octet-stream", use_container_width=True)
    except Exception as exc:
        st.error(f"Preview failed: {exc}")

st.divider()
st.subheader("Dataset schema")
st.code("timestamp, date, time, expiry, security_id, spot, atm, strike_offset, moneyness, strike, option_type, open_price, high_price, low_price, close_price, volume, open_interest, iv, delta, gamma, theta, vega, time_to_expiry_years")
st.caption("IV and Greeks are reconstructed from historical option close + NIFTY spot using Black-Scholes-style calculations and the selected risk-free rate. Dhan's historical intraday endpoint supplies 1-minute OHLC/OI/Volume; current Dhan Option Chain supplies live Greeks but is not a historical Greek series.")
