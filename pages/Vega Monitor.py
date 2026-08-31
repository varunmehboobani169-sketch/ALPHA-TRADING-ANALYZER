from datetime import datetime
from zoneinfo import ZoneInfo
import time

import numpy as np
import pandas as pd
import requests
import streamlit as st

from auth import require_login, logout_button, FIXED_CLIENT_ID

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
CHAIN_GAP_SECONDS = 3.2
INDEXES = {"NIFTY": {"security_id": 13}, "SENSEX": {"security_id": 51}}

st.set_page_config(page_title="Vega Monitor", page_icon="📈", layout="wide")
client_id, access_token = require_login()

st.title("📈 Vega Monitor")
st.caption("Live Call / Put Vega with CURRENT Vega + Day High + Day Low for every leg")
logout_button()

with st.sidebar:
    st.subheader("Market Access")
    st.caption(f"Fixed Client ID: {FIXED_CLIENT_ID}")
    band = st.selectbox("Combined strike band", [2, 3, 5, 10], index=0,
                        format_func=lambda x: f"ATM−{x} to ATM+{x}", key="vega_band")
    expiry_choice = st.selectbox("Expiry", ["Auto", "Nearest", "Next", "Far"],
                                 index=0, key="vega_expiry_choice")
    aggregation = st.selectbox("Vega aggregation", ["Simple sum", "OI-weighted"], index=0,
                               key="vega_aggregation")
    count_trigger = st.number_input("Prior consecutive changes before signal", min_value=1, max_value=5,
                                    value=2, step=1, key="vega_trigger_count")
    refresh_seconds = st.selectbox("Refresh", [5, 10, 15, 30, 60], index=1,
                                   format_func=lambda x: f"Every {x} seconds", key="vega_refresh")
    auto_refresh = st.checkbox("Auto-refresh", value=True, key="vega_auto_refresh")


def post(path: str, payload: dict) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "access-token": access_token, "client-id": client_id}
    try:
        response = requests.post(API + path, headers=headers, json=payload, timeout=45)
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API returned invalid JSON.") from exc


def get_expiries(security_id: int) -> list:
    today = datetime.now(IST).date()
    key = f"vega_expiry_cache_{security_id}"
    day_key = f"vega_expiry_cache_day_{security_id}"
    if st.session_state.get(day_key) == today and key in st.session_state:
        return st.session_state[key]
    body = post("/optionchain/expirylist", {"UnderlyingScrip": security_id, "UnderlyingSeg": "IDX_I"})
    dates = []
    for value in body.get("data") or []:
        try:
            d = pd.Timestamp(value).date()
            if d >= today:
                dates.append(d)
        except Exception:
            continue
    dates = sorted(set(dates))
    st.session_state[key] = dates
    st.session_state[day_key] = today
    return dates


def choose_expiry(expiries: list, choice: str) -> str:
    if not expiries:
        raise RuntimeError("No active option expiry returned.")
    if choice == "Nearest": idx = 0
    elif choice == "Next": idx = 1 if len(expiries) > 1 else 0
    elif choice == "Far": idx = 2 if len(expiries) > 2 else len(expiries) - 1
    else: idx = 1 if expiries[0] == datetime.now(IST).date() and len(expiries) > 1 else 0
    return expiries[idx].isoformat()


def option_chain(security_id: int, expiry: str) -> dict:
    last = st.session_state.get("vega_last_chain_request", 0.0)
    elapsed = time.monotonic() - last
    if elapsed < CHAIN_GAP_SECONDS:
        time.sleep(CHAIN_GAP_SECONDS - elapsed)
    st.session_state["vega_last_chain_request"] = time.monotonic()
    body = post("/optionchain", {"UnderlyingScrip": security_id, "UnderlyingSeg": "IDX_I", "Expiry": expiry})
    return body.get("data") or {}


def normalize_chain(chain: dict) -> tuple[pd.DataFrame, float]:
    spot = pd.to_numeric(chain.get("last_price"), errors="coerce")
    if pd.isna(spot):
        raise RuntimeError("Underlying last price was not returned.")
    rows = []
    for strike_key, node in (chain.get("oc") or {}).items():
        try: strike = float(strike_key)
        except Exception: continue
        for side in ("CE", "PE"):
            leg = node.get("ce" if side == "CE" else "pe") or {}
            if not leg: continue
            greeks = leg.get("greeks") or {}
            rows.append({
                "Strike": strike,
                "Side": side,
                "Current Vega": pd.to_numeric(greeks.get("vega"), errors="coerce"),
                "IV": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                "OI": pd.to_numeric(leg.get("oi"), errors="coerce"),
                "LTP": pd.to_numeric(leg.get("last_price"), errors="coerce"),
            })
    frame = pd.DataFrame(rows)
    if frame.empty: raise RuntimeError("No option-chain strikes were returned.")
    return frame, float(spot)


def select_band(df: pd.DataFrame, spot: float, band_size: int) -> tuple[float, pd.DataFrame]:
    strikes = sorted(df["Strike"].dropna().unique().tolist())
    if not strikes: raise RuntimeError("No valid strikes returned.")
    atm = min(strikes, key=lambda x: abs(x - spot))
    center = strikes.index(atm)
    selected_strikes = [strikes[i] for i in range(center-band_size, center+band_size+1) if 0 <= i < len(strikes)]
    selected = df[df["Strike"].isin(selected_strikes)].copy()
    level_map = {strike: i-center for i, strike in enumerate(strikes) if strike in selected_strikes}
    selected["Level"] = selected["Strike"].map(lambda strike: "ATM" if level_map[strike] == 0 else f"ATM{level_map[strike]:+d}")
    return atm, selected.sort_values(["Strike", "Side"]).reset_index(drop=True)


def aggregate_vega(df: pd.DataFrame, mode: str) -> tuple[float, float, float]:
    valid = df.dropna(subset=["Current Vega"]).copy()
    if valid.empty: return 0.0, 0.0, 0.0
    if mode == "OI-weighted":
        weights = pd.to_numeric(valid["OI"], errors="coerce").fillna(0.0)
        denom = float(weights.sum())
        valid["Vega value"] = valid["Current Vega"] * weights / denom if denom > 0 else valid["Current Vega"]
        col = "Vega value"
    else:
        col = "Current Vega"
    call_v = float(valid.loc[valid.Side == "CE", col].sum())
    put_v = float(valid.loc[valid.Side == "PE", col].sum())
    return call_v, put_v, put_v-call_v


def signal_state(history: list[dict], prior_changes: int) -> tuple[str, int]:
    if len(history) < 2: return "WAIT", 0
    changes = [b["difference"]-a["difference"] for a,b in zip(history[:-1], history[1:])]
    latest = changes[-1]
    if latest == 0: return "WATCH", 0
    direction = 1 if latest > 0 else -1
    count = 0
    for ch in reversed(changes):
        if (direction > 0 and ch > 0) or (direction < 0 and ch < 0): count += 1
        else: break
    return (("BEARISH" if direction > 0 else "BULLISH") if count >= prior_changes+1 else "WATCH"), count


def day_history_key(name: str, date: str, expiry: str, band_size: int, aggregation_name: str) -> str:
    return f"vega_history::{name}::{date}::{expiry}::{band_size}::{aggregation_name}"


def update_day_extremes(key: str, table: pd.DataFrame) -> pd.DataFrame:
    state_key = f"{key}::leg_extremes"
    state = st.session_state.setdefault(state_key, {})
    for row in table.itertuples(index=False):
        value = getattr(row, "Current_Vega", np.nan)
        if pd.isna(value): continue
        level = getattr(row, "Level"); strike = float(getattr(row, "Strike")); side = getattr(row, "Side")
        leg_key = (level, strike, side); value = float(value)
        if leg_key not in state:
            state[leg_key] = {"high": value, "low": value}
        else:
            state[leg_key]["high"] = max(state[leg_key]["high"], value)
            state[leg_key]["low"] = min(state[leg_key]["low"], value)
    out = table.copy()
    out["Day High Vega"] = out.apply(lambda r: state.get((r["Level"],float(r["Strike"]),r["Side"]),{}).get("high"), axis=1)
    out["Day Low Vega"] = out.apply(lambda r: state.get((r["Level"],float(r["Strike"]),r["Side"]),{}).get("low"), axis=1)
    out["Vega Range"] = out["Day High Vega"] - out["Day Low Vega"]
    return out


@st.fragment(run_every=refresh_seconds if auto_refresh else None)
def monitor():
    now = datetime.now(IST)
    trading_date = now.date().isoformat()
    for name, spec in INDEXES.items():
        st.subheader(name)
        try:
            expiries = get_expiries(spec["security_id"])
            expiry = choose_expiry(expiries, expiry_choice)
            raw, spot = normalize_chain(option_chain(spec["security_id"], expiry))
            atm, table = select_band(raw, spot, int(band))

            key = day_history_key(name, trading_date, expiry, int(band), aggregation)
            call_v, put_v, difference = aggregate_vega(table, aggregation)
            history = st.session_state.setdefault(key, [])
            previous = history[-1] if history else None
            history.append({"time": now, "call": call_v, "put": put_v, "difference": difference, "spot": spot, "atm": atm})
            if len(history) > 500: del history[:-500]

            signal, count = signal_state(history, int(count_trigger))
            display = update_day_extremes(key, table)

            atm_rows = display[display["Level"] == "ATM"]
            st.markdown("### ATM VEGA — CURRENT RUNNING / DAY HIGH / DAY LOW")
            if not atm_rows.empty:
                cols = st.columns(6)
                ce = atm_rows[atm_rows.Side == "CE"].iloc[0] if not atm_rows[atm_rows.Side == "CE"].empty else None
                pe = atm_rows[atm_rows.Side == "PE"].iloc[0] if not atm_rows[atm_rows.Side == "PE"].empty else None
                if ce is not None:
                    cols[0].metric("CE CURRENT VEGA", f"{ce['Current Vega']:.4f}")
                    cols[1].metric("CE DAY HIGH VEGA", f"{ce['Day High Vega']:.4f}")
                    cols[2].metric("CE DAY LOW VEGA", f"{ce['Day Low Vega']:.4f}")
                if pe is not None:
                    cols[3].metric("PE CURRENT VEGA", f"{pe['Current Vega']:.4f}")
                    cols[4].metric("PE DAY HIGH VEGA", f"{pe['Day High Vega']:.4f}")
                    cols[5].metric("PE DAY LOW VEGA", f"{pe['Day Low Vega']:.4f}")
            else:
                st.warning("ATM CE/PE Vega is not available in the current option-chain response.")

            st.caption(f"RUNNING = latest live option-chain Vega. High/Low = extrema observed during THIS trading day. Spot: {spot:,.2f} • ATM: {atm:,.0f} • Expiry: {expiry} • Updated: {now.strftime('%H:%M:%S IST')}")
            st.markdown("### ALL LEGS — CURRENT VEGA / DAY HIGH / DAY LOW")
            st.dataframe(display[["Level","Strike","Side","Current Vega","Day High Vega","Day Low Vega","Vega Range","IV","OI","LTP"]], use_container_width=True, hide_index=True)

            hist_df = pd.DataFrame(history)
            if not hist_df.empty:
                plot_df = hist_df[["time","call","put","difference"]].rename(columns={"time":"Time","call":"Call Vega","put":"Put Vega","difference":"Vega Difference"})
                st.line_chart(plot_df, x="Time")

            if signal == "BEARISH": st.warning("Vega Difference has increased for the required consecutive observations — bearish pressure.")
            elif signal == "BULLISH": st.success("Vega Difference has decreased for the required consecutive observations — bullish pressure.")
            else: st.info(f"Monitoring. Consecutive-change count: {count}.")

            st.download_button(f"DOWNLOAD {name} VEGA HISTORY", hist_df.to_csv(index=False).encode("utf-8"), f"vega_{name.lower()}_{trading_date}.csv", "text/csv", use_container_width=True)
        except Exception as exc:
            st.error(f"{name}: {exc}")


monitor()
