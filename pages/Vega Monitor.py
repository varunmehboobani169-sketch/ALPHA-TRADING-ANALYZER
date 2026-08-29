from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
CHAIN_GAP_SECONDS = 3.2
LEVELS_ALL = list(range(-10, 11))

INDEXES = {
    "NIFTY": {"security_id": 13, "under_segment": "IDX_I"},
    "SENSEX": {"security_id": 51, "under_segment": "IDX_I"},
}

st.set_page_config(page_title="Vega Monitor", page_icon="📈", layout="wide")
st.title("📈 Vega Monitor")
st.caption("Script-inspired Call Vega / Put Vega monitoring across combined strikes")

with st.sidebar:
    st.subheader("Market Access")
    client_id = st.text_input("Client ID", type="default", key="vega_client_id")
    access_token = st.text_input("Access Token", type="password", key="vega_access_token")
    index_name = st.selectbox("Index", list(INDEXES), key="vega_index")
    band = st.selectbox("Combined strike band", [2, 3, 5, 10], index=0, format_func=lambda x: f"ATM-{x} to ATM+{x}", key="vega_band")
    expiry_choice = st.selectbox("Expiry", ["Auto", "Nearest", "Next", "Far"], index=0, key="vega_expiry_choice")
    count_trigger = st.number_input("Consecutive changes before signal", min_value=1, max_value=5, value=2, step=1, key="vega_trigger_count")
    refresh_seconds = st.selectbox("Refresh", [5, 10, 15, 30, 60], index=1, format_func=lambda x: f"Every {x} seconds", key="vega_refresh")
    auto_refresh = st.checkbox("Auto-refresh", value=True, key="vega_auto_refresh")

if not client_id or not access_token:
    st.info("Enter Client ID and Access Token to start the Vega Monitor.")
    st.stop()


def headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


def post(path: str, payload: dict) -> dict:
    try:
        response = requests.post(API + path, headers=headers(), json=payload, timeout=45)
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API returned invalid JSON.") from exc


def get_expiries(security_id: int) -> list[str]:
    body = post("/optionchain/expirylist", {"UnderlyingScrip": security_id, "UnderlyingSeg": "IDX_I"})
    dates = []
    today = datetime.now(IST).date()
    for value in body.get("data") or []:
        try:
            d = pd.Timestamp(value).date()
            if d >= today:
                dates.append(d)
        except Exception:
            continue
    return sorted(set(dates))


def choose_expiry(expiries: list[str], choice: str) -> str:
    if not expiries:
        raise RuntimeError("No active option expiries returned.")
    if choice == "Nearest":
        idx = 0
    elif choice == "Next":
        idx = 1 if len(expiries) > 1 else 0
    elif choice == "Far":
        idx = 2 if len(expiries) > 2 else len(expiries) - 1
    else:
        idx = 1 if len(expiries) > 1 and expiries[0] == datetime.now(IST).date() else 0
    return expiries[idx].isoformat()


def option_chain(security_id: int, expiry: str) -> dict:
    last = st.session_state.get("vega_last_chain_request", 0.0)
    import time
    elapsed = time.monotonic() - last
    if elapsed < CHAIN_GAP_SECONDS:
        time.sleep(CHAIN_GAP_SECONDS - elapsed)
    st.session_state["vega_last_chain_request"] = time.monotonic()
    return post("/optionchain", {"UnderlyingScrip": security_id, "UnderlyingSeg": "IDX_I", "Expiry": expiry}).get("data") or {}


def normalize_chain(chain: dict) -> pd.DataFrame:
    rows = []
    for strike_key, node in (chain.get("oc") or {}).items():
        try:
            strike = float(strike_key)
        except Exception:
            continue
        for side in ("CE", "PE"):
            leg = node.get("ce" if side == "CE" else "pe") or {}
            greeks = leg.get("greeks") or {}
            if not leg:
                continue
            rows.append(
                {
                    "strike": strike,
                    "side": side,
                    "vega": pd.to_numeric(greeks.get("vega"), errors="coerce"),
                    "iv": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                    "oi": pd.to_numeric(leg.get("oi"), errors="coerce"),
                    "ltp": pd.to_numeric(leg.get("last_price"), errors="coerce"),
                    "security_id": leg.get("security_id"),
                    "previous_oi": pd.to_numeric(leg.get("previous_oi"), errors="coerce"),
                    "previous_close": pd.to_numeric(leg.get("previous_close_price"), errors="coerce"),
                }
            )
    return pd.DataFrame(rows)


def pick_strikes(df: pd.DataFrame, band_size: int):
    strikes = sorted(df["strike"].dropna().unique().tolist())
    spot = None
    if not df.empty:
        # Estimate ATM from the strike with the highest combined OI when no direct spot field is available.
        oi = df.groupby("strike")["oi"].sum(min_count=1)
        if not oi.dropna().empty:
            atm = float(oi.idxmax())
        else:
            atm = strikes[len(strikes) // 2]
    else:
        raise RuntimeError("No option-chain strikes returned.")
    idx = strikes.index(atm)
    selected = [
        (level, strikes[idx + level])
        for level in range(-band_size, band_size + 1)
        if 0 <= idx + level < len(strikes)
    ]
    return atm, selected


def build_vega_table(df: pd.DataFrame, selected):
    levels = dict(selected)
    out = []
    for level, strike in selected:
        for side in ("CE", "PE"):
            row = df[(df["strike"] == strike) & (df["side"] == side)]
            if row.empty:
                continue
            r = row.iloc[0]
            out.append(
                {
                    "Level": "ATM" if level == 0 else f"ATM{level:+d}",
                    "Strike": strike,
                    "Side": side,
                    "Vega": float(r["vega"]) if pd.notna(r["vega"]) else None,
                    "IV": float(r["iv"]) if pd.notna(r["iv"]) else None,
                    "OI": float(r["oi"]) if pd.notna(r["oi"]) else None,
                    "LTP": float(r["ltp"]) if pd.notna(r["ltp"]) else None,
                }
            )
    return pd.DataFrame(out)


def aggregate(df: pd.DataFrame):
    valid = df.dropna(subset=["Vega"]).copy()
    call_vega = float(valid.loc[valid["Side"] == "CE", "Vega"].sum())
    put_vega = float(valid.loc[valid["Side"] == "PE", "Vega"].sum())
    # Transparent, script-inspired difference: Put Vega - Call Vega.
    difference = put_vega - call_vega
    return call_vega, put_vega, difference


def signal_from_history(history: list[dict], trigger: int):
    if len(history) < 2:
        return "WAIT", 0
    changes = [x["difference"] - y["difference"] for y, x in zip(history[:-1], history[1:])]
    direction = 1 if changes[-1] > 0 else -1 if changes[-1] < 0 else 0
    count = 0
    for change in reversed(changes):
        if direction == 0:
            break
        if (change > 0 and direction > 0) or (change < 0 and direction < 0):
            count += 1
        else:
            break
    if count >= trigger + 1:
        return ("BEARISH" if direction > 0 else "BULLISH"), count
    return "WATCH", count


def fetch_one(index_name: str, expiry: str, band_size: int):
    chain = option_chain(INDEXES[index_name]["security_id"], expiry)
    df = normalize_chain(chain)
    if df.empty:
        raise RuntimeError(f"No option-chain data returned for {index_name}.")
    atm, selected = pick_strikes(df, band_size)
    table = build_vega_table(df, selected)
    call_vega, put_vega, difference = aggregate(table)
    return {"chain": df, "atm": atm, "selected": selected, "table": table, "call_vega": call_vega, "put_vega": put_vega, "difference": difference}


@st.fragment(run_every=refresh_seconds if auto_refresh else None)
def monitor_panel():
    try:
        expiries = get_expiries(INDEXES[index_name]["security_id"])
        if expiry_choice == "Auto" and expiries and expiries[0] == datetime.now(IST).date() and len(expiries) > 1:
            expiry = expiries[1].isoformat()
        else:
            expiry = choose_expiry(expiries, expiry_choice)
        current = fetch_one(index_name, expiry, band)

        hist_key = f"vega_history_{index_name}_{expiry}_{band}"
        history = st.session_state.setdefault(hist_key, [])
        now = datetime.now(IST)
        history.append({"time": now, "call": current["call_vega"], "put": current["put_vega"], "difference": current["difference"]})
        if len(history) > 500:
            del history[:-500]

        signal, count = signal_from_history(history, int(count_trigger))
        st.session_state["vega_latest_signal"] = signal

        st.caption(f"Expiry: {expiry} • ATM: {current['atm']:,.0f} • Combined band: ±{band} • Last update: {now.strftime('%H:%M:%S IST')}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Call Vega", f"{current['call_vega']:.2f}")
        m2.metric("Put Vega", f"{current['put_vega']:.2f}")
        m3.metric("Vega Difference", f"{current['difference']:+.2f}")
        m4.metric("Signal", signal, f"Count {count}")

        st.subheader("Vega by Strike")
        table = current["table"].copy()
        table["Vega Change vs Last"] = table.apply(
            lambda r: None,
            axis=1,
        )
        st.dataframe(table, use_container_width=True, hide_index=True)

        st.subheader("Vega Difference History")
        hist_df = pd.DataFrame(history)
        if not hist_df.empty:
            hist_df = hist_df.copy()
            hist_df["time"] = pd.to_datetime(hist_df["time"], errors="coerce")
            hist_plot = hist_df[["time", "difference", "call", "put"]].rename(
                columns={"time": "Time", "difference": "Vega Difference", "call": "Call Vega", "put": "Put Vega"}
            ).sort_values("Time")
            try:
                st.line_chart(hist_plot, x="Time")
            except Exception:
                st.dataframe(hist_plot, use_container_width=True, hide_index=True)

        st.subheader("Interpretation")
        st.info(
            "Script-inspired reading: a rising positive Vega Difference is treated as bearish; "
            "a falling Difference is treated as bullish. The continuous count requires "
            f"{int(count_trigger)} consecutive changes before the next change produces the signal."
        )

        st.download_button(
            "DOWNLOAD VEGA HISTORY",
            hist_df.to_csv(index=False).encode("utf-8"),
            f"vega_monitor_{index_name.lower()}.csv",
            "text/csv",
            use_container_width=True,
        )
    except Exception as exc:
        st.error(f"{index_name}: {exc}")


monitor_panel()
