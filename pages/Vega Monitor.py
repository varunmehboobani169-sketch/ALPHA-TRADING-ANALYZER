from datetime import datetime
from zoneinfo import ZoneInfo
import time

import pandas as pd
import requests
import streamlit as st

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
CHAIN_GAP_SECONDS = 3.2

INDEXES = {
    "NIFTY": {"security_id": 13},
    "SENSEX": {"security_id": 51},
}

st.set_page_config(page_title="Vega Monitor", page_icon="📈", layout="wide")
st.title("📈 Vega Monitor")
st.caption("Live Call Vega / Put Vega monitoring across combined strikes")

with st.sidebar:
    st.subheader("Market Access")
    client_id = st.text_input("Client ID", key="vega_client_id")
    access_token = st.text_input("Access Token", type="password", key="vega_access_token")
    band = st.selectbox(
        "Combined strike band",
        [2, 3, 5, 10],
        index=0,
        format_func=lambda x: f"ATM−{x} to ATM+{x}",
        key="vega_band",
    )
    expiry_choice = st.selectbox(
        "Expiry",
        ["Auto", "Nearest", "Next", "Far"],
        index=0,
        key="vega_expiry_choice",
    )
    aggregation = st.selectbox(
        "Vega aggregation",
        ["Simple sum", "OI-weighted"],
        index=0,
        key="vega_aggregation",
    )
    count_trigger = st.number_input(
        "Prior consecutive changes before signal",
        min_value=1,
        max_value=5,
        value=2,
        step=1,
        key="vega_trigger_count",
    )
    refresh_seconds = st.selectbox(
        "Refresh",
        [5, 10, 15, 30, 60],
        index=1,
        format_func=lambda x: f"Every {x} seconds",
        key="vega_refresh",
    )
    auto_refresh = st.checkbox("Auto-refresh", value=True, key="vega_auto_refresh")

if not client_id or not access_token:
    st.info("Enter Client ID and Access Token to start the Vega Monitor.")
    st.stop()


def post(path: str, payload: dict) -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }
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
    body = post(
        "/optionchain/expirylist",
        {"UnderlyingScrip": security_id, "UnderlyingSeg": "IDX_I"},
    )
    dates = []
    for value in body.get("data") or []:
        try:
            date_value = pd.Timestamp(value).date()
            if date_value >= today:
                dates.append(date_value)
        except Exception:
            continue
    dates = sorted(set(dates))
    st.session_state[key] = dates
    st.session_state[day_key] = today
    return dates


def choose_expiry(expiries: list, choice: str) -> str:
    if not expiries:
        raise RuntimeError("No active option expiry returned.")
    if choice == "Nearest":
        idx = 0
    elif choice == "Next":
        idx = 1 if len(expiries) > 1 else 0
    elif choice == "Far":
        idx = 2 if len(expiries) > 2 else len(expiries) - 1
    else:
        # On the expiry day, use the next expiry as described in the supplied script.
        idx = 1 if expiries[0] == datetime.now(IST).date() and len(expiries) > 1 else 0
    return expiries[idx].isoformat()


def option_chain(security_id: int, expiry: str) -> dict:
    last = st.session_state.get("vega_last_chain_request", 0.0)
    elapsed = time.monotonic() - last
    if elapsed < CHAIN_GAP_SECONDS:
        time.sleep(CHAIN_GAP_SECONDS - elapsed)
    st.session_state["vega_last_chain_request"] = time.monotonic()
    body = post(
        "/optionchain",
        {
            "UnderlyingScrip": security_id,
            "UnderlyingSeg": "IDX_I",
            "Expiry": expiry,
        },
    )
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
            rows.append(
                {
                    "Strike": strike,
                    "Side": side,
                    "Vega": pd.to_numeric(greeks.get("vega"), errors="coerce"),
                    "IV": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                    "OI": pd.to_numeric(leg.get("oi"), errors="coerce"),
                    "Previous OI": pd.to_numeric(leg.get("previous_oi"), errors="coerce"),
                    "LTP": pd.to_numeric(leg.get("last_price"), errors="coerce"),
                }
            )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("No option-chain strikes were returned.")
    return frame, float(spot)


def select_band(df: pd.DataFrame, spot: float, band_size: int) -> tuple[float, pd.DataFrame]:
    strikes = sorted(df["Strike"].dropna().unique().tolist())
    if not strikes:
        raise RuntimeError("No valid strikes returned.")
    atm = min(strikes, key=lambda x: abs(x - spot))
    center = strikes.index(atm)
    selected_strikes = [
        strikes[i]
        for i in range(center - band_size, center + band_size + 1)
        if 0 <= i < len(strikes)
    ]
    selected = df[df["Strike"].isin(selected_strikes)].copy()
    level_map = {strike: i - center for i, strike in enumerate(strikes) if strike in selected_strikes}
    selected["Level"] = selected["Strike"].map(
        lambda strike: "ATM" if level_map[strike] == 0 else f"ATM{level_map[strike]:+d}"
    )
    return atm, selected.sort_values(["Strike", "Side"]).reset_index(drop=True)


def aggregate_vega(df: pd.DataFrame, mode: str) -> tuple[float, float, float]:
    valid = df.dropna(subset=["Vega"]).copy()
    if valid.empty:
        return 0.0, 0.0, 0.0
    if mode == "OI-weighted":
        weights = pd.to_numeric(valid["OI"], errors="coerce").fillna(0.0)
        denominator = float(weights.sum())
        if denominator > 0:
            valid["Vega contribution"] = valid["Vega"] * weights / denominator
        else:
            valid["Vega contribution"] = valid["Vega"]
        value_col = "Vega contribution"
    else:
        value_col = "Vega"
    call_vega = float(valid.loc[valid["Side"] == "CE", value_col].sum())
    put_vega = float(valid.loc[valid["Side"] == "PE", value_col].sum())
    difference = put_vega - call_vega
    return call_vega, put_vega, difference


def signal_state(history: list[dict], prior_changes: int) -> tuple[str, int]:
    if len(history) < 2:
        return "WAIT", 0
    changes = [
        current["difference"] - previous["difference"]
        for previous, current in zip(history[:-1], history[1:])
    ]
    latest = changes[-1]
    if latest == 0:
        return "WATCH", 0
    direction = 1 if latest > 0 else -1
    count = 0
    for change in reversed(changes):
        if direction > 0 and change > 0:
            count += 1
        elif direction < 0 and change < 0:
            count += 1
        else:
            break
    if count >= prior_changes + 1:
        return ("BEARISH" if direction > 0 else "BULLISH"), count
    return "WATCH", count


def build_history_key(index_name: str, expiry: str, band_size: int, aggregation_name: str) -> str:
    return f"vega_history::{index_name}::{expiry}::{band_size}::{aggregation_name}"


@st.fragment(run_every=refresh_seconds if auto_refresh else None)
def monitor():
    now = datetime.now(IST)
    for name, spec in INDEXES.items():
        st.subheader(name)
        try:
            expiries = get_expiries(spec["security_id"])
            expiry = choose_expiry(expiries, expiry_choice)
            chain = option_chain(spec["security_id"], expiry)
            raw, spot = normalize_chain(chain)
            atm, table = select_band(raw, spot, int(band))
            call_vega, put_vega, difference = aggregate_vega(table, aggregation)

            history_key = build_history_key(name, expiry, int(band), aggregation)
            history = st.session_state.setdefault(history_key, [])
            previous = history[-1] if history else None
            sample = {
                "time": now,
                "call": call_vega,
                "put": put_vega,
                "difference": difference,
                "spot": spot,
                "atm": atm,
            }
            sample["diff_change"] = difference - previous["difference"] if previous else None
            history.append(sample)
            if len(history) > 500:
                del history[:-500]

            signal, count = signal_state(history, int(count_trigger))

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Spot", f"{spot:,.2f}")
            c2.metric("ATM", f"{atm:,.0f}")
            c3.metric("Call Vega", f"{call_vega:.2f}")
            c4.metric("Put Vega", f"{put_vega:.2f}")
            c5.metric("Vega Difference", f"{difference:+.2f}", signal)

            st.caption(
                f"Expiry: {expiry} • Band: ATM−{band} to ATM+{band} • "
                f"Continuous count: {count} • Updated: {now.strftime('%H:%M:%S IST')}"
            )

            display = table[["Level", "Strike", "Side", "Vega", "IV", "OI", "LTP"]].copy()
            previous_table_key = history_key + "::table"
            previous_table = st.session_state.get(previous_table_key)
            if previous_table is not None and not previous_table.empty:
                prev_lookup = previous_table.set_index(["Level", "Strike", "Side"])["Vega"].to_dict()
                display["Vega Change"] = display.apply(
                    lambda row: row["Vega"] - prev_lookup.get((row["Level"], row["Strike"], row["Side"]))
                    if pd.notna(row["Vega"]) and prev_lookup.get((row["Level"], row["Strike"], row["Side"])) is not None
                    else None,
                    axis=1,
                )
            else:
                display["Vega Change"] = None
            st.session_state[previous_table_key] = display.copy()

            st.dataframe(display, use_container_width=True, hide_index=True)

            hist_df = pd.DataFrame(history)
            if not hist_df.empty:
                plot_df = hist_df[["time", "call", "put", "difference"]].rename(
                    columns={
                        "time": "Time",
                        "call": "Call Vega",
                        "put": "Put Vega",
                        "difference": "Vega Difference",
                    }
                )
                try:
                    st.line_chart(plot_df, x="Time")
                except Exception:
                    st.dataframe(plot_df, use_container_width=True, hide_index=True)

            st.markdown("**Current reading**")
            if signal == "BEARISH":
                st.warning("Vega Difference has increased for the required consecutive observations — bearish pressure.")
            elif signal == "BULLISH":
                st.success("Vega Difference has decreased for the required consecutive observations — bullish pressure.")
            else:
                st.info(f"Monitoring. Current consecutive-change count: {count}.")

            with st.expander("Vega calculation / script mapping"):
                st.write(
                    "The live per-strike Vega comes directly from the option-chain Greeks. "
                    "The dashboard then combines the selected strikes. Default aggregation is a simple sum. "
                    "Vega Difference is defined as Put Vega − Call Vega so a rising Difference maps to the bearish reading described in the supplied script. "
                    "The supplied script does not disclose its proprietary weighting formula, so this implementation is transparent rather than claiming to reproduce an undisclosed formula."
                )

            st.download_button(
                f"DOWNLOAD {name} VEGA HISTORY",
                hist_df.to_csv(index=False).encode("utf-8"),
                f"vega_{name.lower()}_history.csv",
                "text/csv",
                use_container_width=True,
            )
        except Exception as exc:
            st.error(f"{name}: {exc}")


monitor()
