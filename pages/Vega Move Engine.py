from datetime import datetime
from zoneinfo import ZoneInfo
import time

import numpy as np
import pandas as pd
import requests
import streamlit as st

from auth import require_login, logout_button, FIXED_CLIENT_ID
from vega_direction_engine import calculate_vega_signal

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
CHAIN_GAP_SECONDS = 3.2
INDEXES = {"NIFTY": {"security_id": 13}, "SENSEX": {"security_id": 51}}

st.set_page_config(page_title="Vega Move Engine", page_icon="🔥", layout="wide")
client_id, access_token = require_login()

st.title("🔥 Vega Move Engine")
st.caption("Vega-first alert: detect when a meaningful market move is building, then estimate the directional side.")
logout_button()

with st.sidebar:
    st.subheader("Vega Engine")
    st.caption(f"Client ID: {FIXED_CLIENT_ID}")
    band = st.selectbox("Strike band", [2, 3, 5, 10], index=1,
                        format_func=lambda x: f"ATM−{x} to ATM+{x}", key="vde_band")
    refresh_seconds = st.selectbox("Refresh", [5, 10, 15, 30, 60], index=1,
                                   format_func=lambda x: f"Every {x} seconds", key="vde_refresh")
    auto_refresh = st.checkbox("Auto-refresh", value=True, key="vde_auto")


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
    key = f"vde_expiries_{security_id}"
    day_key = f"vde_expiry_day_{security_id}"
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
            pass
    dates = sorted(set(dates))
    st.session_state[key] = dates
    st.session_state[day_key] = today
    return dates


def choose_expiry(expiries: list) -> str:
    if not expiries:
        raise RuntimeError("No active option expiry returned.")
    # On expiry day, move to the next available expiry to avoid a nearly-zero-TTE chain.
    today = datetime.now(IST).date()
    idx = 1 if expiries[0] == today and len(expiries) > 1 else 0
    return expiries[idx].isoformat()


def option_chain(security_id: int, expiry: str) -> dict:
    last = st.session_state.get("vde_last_chain_request", 0.0)
    elapsed = time.monotonic() - last
    if elapsed < CHAIN_GAP_SECONDS:
        time.sleep(CHAIN_GAP_SECONDS - elapsed)
    st.session_state["vde_last_chain_request"] = time.monotonic()
    body = post("/optionchain", {"UnderlyingScrip": security_id, "UnderlyingSeg": "IDX_I", "Expiry": expiry})
    return body.get("data") or {}


def normalize_chain(chain: dict) -> tuple[pd.DataFrame, float]:
    spot = pd.to_numeric(chain.get("last_price"), errors="coerce")
    if pd.isna(spot):
        raise RuntimeError("Underlying last price was not returned.")
    rows = []
    for strike_key, node in (chain.get("oc") or {}).items():
        try:
            strike = float(strike_key)
        except Exception:
            continue
        for side in ("CE", "PE"):
            leg = node.get("ce" if side == "CE" else "pe") or {}
            if not leg:
                continue
            greeks = leg.get("greeks") or {}
            rows.append({
                "Level": "",
                "Strike": strike,
                "Side": side,
                "Current Vega": pd.to_numeric(greeks.get("vega"), errors="coerce"),
                "IV": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                "OI": pd.to_numeric(leg.get("oi"), errors="coerce"),
                "LTP": pd.to_numeric(leg.get("last_price"), errors="coerce"),
            })
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("No option-chain strikes were returned.")
    return frame, float(spot)


def select_band(df: pd.DataFrame, spot: float, band_size: int) -> tuple[float, pd.DataFrame]:
    strikes = sorted(df["Strike"].dropna().unique().tolist())
    atm = min(strikes, key=lambda x: abs(x - spot))
    center = strikes.index(atm)
    selected_strikes = [strikes[i] for i in range(center - band_size, center + band_size + 1)
                        if 0 <= i < len(strikes)]
    selected = df[df["Strike"].isin(selected_strikes)].copy()
    level_map = {strike: i - center for i, strike in enumerate(strikes) if strike in selected_strikes}
    selected["Level"] = selected["Strike"].map(
        lambda strike: "ATM" if level_map[strike] == 0 else f"ATM{level_map[strike]:+d}"
    )
    return atm, selected.sort_values(["Strike", "Side"]).reset_index(drop=True)


def aggregate_snapshot(table: pd.DataFrame) -> dict:
    valid = table.dropna(subset=["Current Vega"]).copy()
    call_vega = float(valid.loc[valid.Side == "CE", "Current Vega"].sum())
    put_vega = float(valid.loc[valid.Side == "PE", "Current Vega"].sum())
    call_iv = float(pd.to_numeric(valid.loc[valid.Side == "CE", "IV"], errors="coerce").mean()) if not valid.loc[valid.Side == "CE"].empty else 0.0
    put_iv = float(pd.to_numeric(valid.loc[valid.Side == "PE", "IV"], errors="coerce").mean()) if not valid.loc[valid.Side == "PE"].empty else 0.0
    return {"vega": call_vega, "iv": call_iv}, {"vega": put_vega, "iv": put_iv}


def update_leg_extremes(key: str, table: pd.DataFrame) -> pd.DataFrame:
    state_key = f"{key}::leg_extremes"
    state = st.session_state.setdefault(state_key, {})
    for row in table.itertuples(index=False):
        value = getattr(row, "Current_Vega", np.nan)
        if pd.isna(value):
            continue
        leg_key = (getattr(row, "Level"), float(getattr(row, "Strike")), getattr(row, "Side"))
        value = float(value)
        state.setdefault(leg_key, {"high": value, "low": value})
        state[leg_key]["high"] = max(state[leg_key]["high"], value)
        state[leg_key]["low"] = min(state[leg_key]["low"], value)

    out = table.copy()
    out["Day High Vega"] = out.apply(
        lambda r: state[(r["Level"], float(r["Strike"]), r["Side"])] ["high"], axis=1
    )
    out["Day Low Vega"] = out.apply(
        lambda r: state[(r["Level"], float(r["Strike"]), r["Side"])] ["low"], axis=1
    )
    out["Vega Range %"] = out.apply(
        lambda r: ((float(r["Current Vega"]) - r["Day Low Vega"]) /
                   max(r["Day High Vega"] - r["Day Low Vega"], 1e-9) * 100.0), axis=1
    )
    return out


def render_signal(signal):
    cols = st.columns(6)
    cols[0].metric("MOVEMENT", f"{signal.movement_score:.0f}/100")
    cols[1].metric("STATE", signal.movement_state)
    cols[2].metric("DIRECTION", signal.direction)
    cols[3].metric("DIRECTION SCORE", f"{signal.direction_score:+.0f}")
    cols[4].metric("CONFIDENCE", signal.confidence)
    cols[5].metric("EXPANSION", "YES" if signal.expansion else "NO")

    if signal.movement_state in ("EXTREME EXPANSION", "HIGH PROBABILITY MOVE"):
        st.error(f"🔥 {signal.movement_state}: Vega conditions are strongly consistent with an upcoming larger-than-normal move.")
    elif signal.movement_state == "MOVEMENT BUILDING":
        st.warning("⚠️ Vega movement is building. Watch for price confirmation before acting.")
    else:
        st.info("Vega is not yet showing a strong movement regime.")

    detail = pd.DataFrame([
        ["Call Vega pressure", f"{signal.call_pressure:+.2f}%"],
        ["Put Vega pressure", f"{signal.put_pressure:+.2f}%"],
        ["Call Vega acceleration", f"{signal.call_acceleration:+.2f}"],
        ["Put Vega acceleration", f"{signal.put_acceleration:+.2f}"],
        ["IV asymmetry (CE−PE)", f"{signal.iv_asymmetry:+.2f}%"],
        ["Call Vega range position", f"{signal.call_range_position:.1f}%"],
        ["Put Vega range position", f"{signal.put_range_position:.1f}%"],
    ], columns=["Metric", "Value"])
    st.dataframe(detail, use_container_width=True, hide_index=True)


@st.fragment(run_every=refresh_seconds if auto_refresh else None)
def monitor():
    now = datetime.now(IST)
    trading_date = now.date().isoformat()

    for name, spec in INDEXES.items():
        st.divider()
        st.subheader(name)
        try:
            expiry = choose_expiry(get_expiries(spec["security_id"]))
            raw, spot = normalize_chain(option_chain(spec["security_id"], expiry))
            atm, table = select_band(raw, spot, int(band))
            table = update_leg_extremes(f"vde::{name}::{trading_date}::{expiry}::{band}", table)

            history_key = f"vde_history::{name}::{trading_date}::{expiry}::{band}"
            history = st.session_state.setdefault(history_key, [])
            call_payload, put_payload = aggregate_snapshot(table)
            history.append({"time": now, "call": call_payload, "put": put_payload, "spot": spot, "atm": atm})
            if len(history) > 500:
                del history[:-500]

            signal = calculate_vega_signal(history)
            render_signal(signal)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("SPOT", f"{spot:,.2f}")
            c2.metric("ATM", f"{atm:,.0f}")
            c3.metric("EXPIRY", expiry)
            c4.metric("OBSERVATIONS", len(history))

            atm_rows = table[table["Level"] == "ATM"]
            st.markdown("### ATM VEGA — CURRENT / DAY HIGH / DAY LOW")
            if not atm_rows.empty:
                cards = st.columns(6)
                ce = atm_rows[atm_rows.Side == "CE"].iloc[0] if not atm_rows[atm_rows.Side == "CE"].empty else None
                pe = atm_rows[atm_rows.Side == "PE"].iloc[0] if not atm_rows[atm_rows.Side == "PE"].empty else None
                if ce is not None:
                    cards[0].metric("CE CURRENT", f"{ce['Current Vega']:.4f}")
                    cards[1].metric("CE HIGH", f"{ce['Day High Vega']:.4f}")
                    cards[2].metric("CE LOW", f"{ce['Day Low Vega']:.4f}")
                if pe is not None:
                    cards[3].metric("PE CURRENT", f"{pe['Current Vega']:.4f}")
                    cards[4].metric("PE HIGH", f"{pe['Day High Vega']:.4f}")
                    cards[5].metric("PE LOW", f"{pe['Day Low Vega']:.4f}")

            st.markdown("### ALL LEGS — CURRENT VEGA / DAY HIGH / DAY LOW")
            st.dataframe(
                table[["Level", "Strike", "Side", "Current Vega", "Day High Vega", "Day Low Vega", "Vega Range %", "IV", "OI", "LTP"]],
                use_container_width=True, hide_index=True
            )

            hist_df = pd.DataFrame(history)
            if not hist_df.empty:
                chart = pd.DataFrame({
                    "Time": hist_df["time"],
                    "Call Vega": hist_df["call"].apply(lambda x: x["vega"]),
                    "Put Vega": hist_df["put"].apply(lambda x: x["vega"]),
                }).set_index("Time")
                st.line_chart(chart)

            st.caption(f"Updated {now.strftime('%H:%M:%S IST')} • Fixed Client ID {FIXED_CLIENT_ID} • Auto refresh: {auto_refresh}")
            st.download_button(
                f"DOWNLOAD {name} VEGA SIGNAL HISTORY",
                hist_df.to_csv(index=False).encode("utf-8"),
                f"vde_{name.lower()}_{trading_date}.csv", "text/csv",
                use_container_width=True
            )
        except Exception as exc:
            st.error(f"{name}: {exc}")


monitor()
