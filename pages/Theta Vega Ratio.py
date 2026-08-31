import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

from theta_vega_ratio import ThetaVegaConfig, VegaLegState, VegaState

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="Theta Vega Ratio", page_icon="Θ", layout="wide")
st.title("Θ Theta Vega Ratio")
st.caption("Intraday NIFTY short-premium monitor: Theta/Vega carry + IV condition + Vega high/low tracking")

with st.sidebar:
    st.subheader("Market Access")
    client_id = st.text_input("Client ID", key="tvr_client_id")
    access_token = st.text_input("Access Token", type="password", key="tvr_access_token")
    refresh = st.selectbox("Refresh", [5, 10, 15, 30, 60], index=1, format_func=lambda x: f"Every {x}s")
    auto_refresh = st.checkbox("Auto-refresh", value=True)
    band = st.selectbox("ATM monitoring band", [0, 1, 2, 3], index=0,
                        format_func=lambda x: "ATM only" if x == 0 else f"ATM ±{x} strikes")

if not client_id or not access_token:
    st.info("Enter Client ID and Access Token to run the live Theta Vega Ratio monitor.")
    st.stop()

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "access-token": access_token,
    "client-id": client_id,
}


def post(path, payload):
    try:
        r = requests.post(API + path, headers=HEADERS, json=payload, timeout=45)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Dhan API error: {exc}") from exc


def expiry_list():
    body = post("/optionchain/expirylist", {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"})
    vals = []
    for x in body.get("data") or []:
        try:
            d = pd.Timestamp(x).date()
            if d >= datetime.now(IST).date():
                vals.append(d)
        except Exception:
            pass
    return sorted(set(vals))


def chain(expiry):
    body = post("/optionchain", {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": expiry.isoformat()})
    return body.get("data") or {}


def flatten(data):
    spot = pd.to_numeric(data.get("last_price"), errors="coerce")
    if pd.isna(spot):
        raise RuntimeError("NIFTY spot not returned by option chain.")
    rows = []
    for strike_key, node in (data.get("oc") or {}).items():
        try:
            strike = float(strike_key)
        except Exception:
            continue
        for side in ["CE", "PE"]:
            leg = node.get("ce" if side == "CE" else "pe") or {}
            if not leg:
                continue
            greeks = leg.get("greeks") or {}
            rows.append({
                "Strike": strike,
                "Side": side,
                "LTP": pd.to_numeric(leg.get("last_price"), errors="coerce"),
                "IV": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                "OI": pd.to_numeric(leg.get("oi"), errors="coerce"),
                "Delta": pd.to_numeric(greeks.get("delta"), errors="coerce"),
                "Theta": pd.to_numeric(greeks.get("theta"), errors="coerce"),
                "Vega": pd.to_numeric(greeks.get("vega"), errors="coerce"),
                "Gamma": pd.to_numeric(greeks.get("gamma"), errors="coerce"),
            })
    return pd.DataFrame(rows), float(spot)


def pick_atm(df, spot):
    strikes = sorted(df.Strike.dropna().unique())
    if not strikes:
        raise RuntimeError("No strikes returned.")
    return min(strikes, key=lambda k: abs(k - spot))


def build_metrics(df, atm, band_size):
    strikes = sorted(df.Strike.unique())
    center = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))
    chosen = strikes[max(0, center-band_size): center+band_size+1]
    sub = df[df.Strike.isin(chosen)].copy()
    ce = sub[sub.Side == "CE"]
    pe = sub[sub.Side == "PE"]
    call = ce["Vega"].sum(min_count=1)
    put = pe["Vega"].sum(min_count=1)
    atmrow_c = sub[(sub.Strike == atm) & (sub.Side == "CE")].head(1)
    atmrow_p = sub[(sub.Strike == atm) & (sub.Side == "PE")].head(1)
    if atmrow_c.empty or atmrow_p.empty:
        raise RuntimeError("ATM CE/PE not available.")
    c = atmrow_c.iloc[0]; p = atmrow_p.iloc[0]
    theta = -(float(c.Theta) + float(p.Theta))
    vega = abs(float(c.Vega) + float(p.Vega))
    ratio = theta / vega if vega else 0.0
    iv = (float(c.IV) + float(p.IV)) / 2.0
    premium = float(c.LTP) + float(p.LTP)
    return sub, float(call), float(put), theta, vega, ratio, iv, premium


@st.fragment(run_every=refresh if auto_refresh else None)
def live():
    try:
        exps = expiry_list()
        if not exps:
            st.error("No weekly expiry available.")
            return
        expiry = exps[0]
        data = chain(expiry)
        raw, spot = flatten(data)
        atm = pick_atm(raw, spot)
        table, call_v, put_v, theta, vega, ratio, iv, premium = build_metrics(raw, atm, band)

        # Persist day-high/day-low independently for each leg.
        state_key = f"tvr_vega_state_{expiry.isoformat()}"
        state = st.session_state.get(state_key)
        if state is None:
            state = VegaState(VegaLegState(), VegaLegState())
            st.session_state[state_key] = state
        state.call.update(float(table[(table.Strike == atm) & (table.Side == "CE")].Vega.iloc[0]))
        state.put.update(float(table[(table.Strike == atm) & (table.Side == "PE")].Vega.iloc[0]))

        c1,c2,c3,c4,c5,c6 = st.columns(6)
        c1.metric("Spot", f"{spot:,.2f}")
        c2.metric("ATM", f"{atm:,.0f}")
        c3.metric("Theta", f"{theta:.4f}")
        c4.metric("Vega", f"{vega:.4f}")
        c5.metric("Theta/Vega", f"{ratio:.2f}")
        c6.metric("ATM IV", f"{iv:.2f}")

        st.subheader("Vega Range — ATM legs")
        v1,v2,v3,v4,v5,v6 = st.columns(6)
        v1.metric("CE Vega Now", f"{state.call.current:.4f}")
        v2.metric("CE Vega High", f"{state.call.day_high:.4f}")
        v3.metric("CE Vega Low", f"{state.call.day_low:.4f}")
        v4.metric("PE Vega Now", f"{state.put.current:.4f}")
        v5.metric("PE Vega High", f"{state.put.day_high:.4f}")
        v6.metric("PE Vega Low", f"{state.put.day_low:.4f}")

        st.caption(f"Expiry: {expiry} • Combined ATM premium: {premium:.2f} • Updated {datetime.now(IST).strftime('%H:%M:%S IST')}")

        view = table[["Strike","Side","LTP","IV","OI","Delta","Theta","Vega","Gamma"]].copy()
        st.dataframe(view, use_container_width=True, hide_index=True)

        cfg = ThetaVegaConfig()
        checks = pd.DataFrame([
            ["After 09:36", datetime.now(IST).time() >= cfg.entry_time],
            ["Theta/Vega >= 1.5", ratio >= cfg.theta_vega_min],
            ["15m IV change <= +2%", "Use live historical IV series"],
            ["15:05 hard exit", datetime.now(IST).time() >= cfg.hard_exit_time],
        ], columns=["Rule","Status"])
        st.subheader("Entry Monitor")
        st.dataframe(checks, use_container_width=True, hide_index=True)

    except Exception as exc:
        st.error(str(exc))


live()
