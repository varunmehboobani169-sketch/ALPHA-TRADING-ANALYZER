from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import time

import numpy as np
import pandas as pd
import requests
import streamlit as st

from auth import require_login, logout_button, FIXED_CLIENT_ID
from vega_strategy_930 import evaluate_signal

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
SECURITY_ID = 13
CHAIN_GAP_SECONDS = 3.2
FREEZE_TIME = dtime(9, 30)

st.set_page_config(page_title="Vega 9:30 Strategy", page_icon="⚡", layout="wide")
client_id, access_token = require_login()
logout_button()

st.title("⚡ Vega 9:30 Fixed-ATM Strategy")
st.caption("Freeze ATM at 09:30 → track CE−PE Vega difference → 3 consecutive rising/falling minutes → first signal of the day")

with st.sidebar:
    st.subheader("Strategy Rules")
    st.info(f"Fixed Client ID: {FIXED_CLIENT_ID}")
    refresh = st.selectbox("Live refresh", [5, 10, 15, 30, 60], index=1, format_func=lambda x: f"Every {x}s")
    auto_refresh = st.checkbox("Auto-refresh", value=True)


def post(path: str, payload: dict) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json", "access-token": access_token, "client-id": client_id}
    try:
        r = requests.post(API + path, headers=headers, json=payload, timeout=45)
        if r.status_code >= 400:
            raise RuntimeError(f"Dhan API {r.status_code}: {r.text[:500]}")
        return r.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error: {exc}") from exc


def expiries():
    body = post("/optionchain/expirylist", {"UnderlyingScrip": SECURITY_ID, "UnderlyingSeg": "IDX_I"})
    today = datetime.now(IST).date()
    vals = []
    for x in body.get("data") or []:
        try:
            d = pd.Timestamp(x).date()
            if d >= today:
                vals.append(d)
        except Exception:
            pass
    return sorted(set(vals))


def chain(expiry):
    last = st.session_state.get("v930_last_request", 0.0)
    wait = CHAIN_GAP_SECONDS - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    st.session_state["v930_last_request"] = time.monotonic()
    body = post("/optionchain", {"UnderlyingScrip": SECURITY_ID, "UnderlyingSeg": "IDX_I", "Expiry": expiry.isoformat()})
    return body.get("data") or {}


def flatten(data):
    spot = pd.to_numeric(data.get("last_price"), errors="coerce")
    if pd.isna(spot):
        raise RuntimeError("NIFTY spot was not returned.")
    rows = []
    for strike_key, node in (data.get("oc") or {}).items():
        try:
            strike = float(strike_key)
        except Exception:
            continue
        for side in ("CE", "PE"):
            leg = node.get("ce" if side == "CE" else "pe") or {}
            if not leg:
                continue
            g = leg.get("greeks") or {}
            rows.append({
                "Strike": strike,
                "Side": side,
                "LTP": pd.to_numeric(leg.get("last_price"), errors="coerce"),
                "IV": pd.to_numeric(leg.get("implied_volatility"), errors="coerce"),
                "OI": pd.to_numeric(leg.get("oi"), errors="coerce"),
                "Vega": pd.to_numeric(g.get("vega"), errors="coerce"),
                "Security ID": leg.get("security_id"),
            })
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No option-chain legs were returned.")
    return df, float(spot)


def frozen_atm_key(day: str) -> str:
    return f"v930_frozen::{day}"


def observations_key(day: str) -> str:
    return f"v930_obs::{day}"


@st.fragment(run_every=refresh if auto_refresh else None)
def live():
    now = datetime.now(IST)
    day = now.date().isoformat()

    try:
        exps = expiries()
        if not exps:
            st.error("No active NIFTY expiry available.")
            return
        expiry = exps[0]
        data = chain(expiry)
        df, spot = flatten(data)

        freeze_key = frozen_atm_key(day)
        frozen = st.session_state.get(freeze_key)
        if frozen is None and now.time() >= FREEZE_TIME:
            strikes = sorted(df["Strike"].dropna().unique().tolist())
            atm = min(strikes, key=lambda x: abs(x - spot))
            ce = df[(df.Strike == atm) & (df.Side == "CE")].head(1)
            pe = df[(df.Strike == atm) & (df.Side == "PE")].head(1)
            if ce.empty or pe.empty:
                raise RuntimeError("Frozen ATM CE/PE legs are not available.")
            frozen = {
                "date": day,
                "atm": float(atm),
                "expiry": expiry.isoformat(),
                "ce_security_id": ce.iloc[0]["Security ID"],
                "pe_security_id": pe.iloc[0]["Security ID"],
                "freeze_spot": float(spot),
                "frozen_at": now.isoformat(),
            }
            st.session_state[freeze_key] = frozen

        if frozen is None:
            st.warning("Waiting for the 09:30 ATM freeze. The live spot can move, but ATM will be frozen only once at/after 09:30.")
            st.stop()

        atm = frozen["atm"]
        ce = df[(df.Strike == atm) & (df.Side == "CE")].head(1)
        pe = df[(df.Strike == atm) & (df.Side == "PE")].head(1)
        if ce.empty or pe.empty:
            raise RuntimeError("Frozen ATM CE/PE not present in current option-chain response.")

        ce_row, pe_row = ce.iloc[0], pe.iloc[0]
        obs = st.session_state.setdefault(observations_key(day), [])
        obs.append({
            "time": now.isoformat(),
            "ce_vega": float(ce_row["Vega"]),
            "pe_vega": float(pe_row["Vega"]),
        })
        if len(obs) > 3000:
            del obs[:-3000]

        # Keep a single decision per day.
        traded_key = f"v930_traded::{day}"
        already_traded = bool(st.session_state.get(traded_key, False))
        signal = evaluate_signal(obs, already_traded=already_traded)
        if signal.signal in ("BUY CE", "BUY PE") and not already_traded:
            st.session_state[traded_key] = True

        diff = float(ce_row["Vega"] - pe_row["Vega"])
        c = st.columns(6)
        c[0].metric("FROZEN ATM", f"{atm:,.0f}")
        c[1].metric("FREEZE SPOT", f"{frozen['freeze_spot']:,.2f}")
        c[2].metric("CE VEGA", f"{float(ce_row['Vega']):.4f}")
        c[3].metric("PE VEGA", f"{float(pe_row['Vega']):.4f}")
        c[4].metric("CE−PE VEGA", f"{diff:+.4f}")
        c[5].metric("SIGNAL", signal.signal)

        if signal.signal == "BUY CE":
            st.success("🟢 BUY CE — CE−PE Vega difference has risen for 3 consecutive completed minutes.")
        elif signal.signal == "BUY PE":
            st.error("🔴 BUY PE — CE−PE Vega difference has fallen for 3 consecutive completed minutes.")
        elif signal.signal == "LOCKED":
            st.info("Daily signal already triggered. No second entry is permitted by this strategy.")
        else:
            st.warning(signal.reason)

        st.markdown("### Frozen ATM leg details")
        leg_view = pd.DataFrame([
            ["CE", atm, ce_row["LTP"], ce_row["IV"], ce_row["Vega"], frozen["ce_security_id"]],
            ["PE", atm, pe_row["LTP"], pe_row["IV"], pe_row["Vega"], frozen["pe_security_id"]],
        ], columns=["Side", "Strike", "LTP", "IV", "Vega", "Security ID"])
        st.dataframe(leg_view, use_container_width=True, hide_index=True)

        minute = pd.DataFrame(obs)
        if not minute.empty:
            minute["time"] = pd.to_datetime(minute["time"])
            minute["minute"] = minute["time"].dt.floor("min")
            minute = minute.sort_values("time").drop_duplicates("minute", keep="last")
            minute["Difference"] = minute["ce_vega"] - minute["pe_vega"]
            show = minute[["minute", "ce_vega", "pe_vega", "Difference"]].tail(20).rename(columns={"minute":"Minute", "ce_vega":"CE Vega", "pe_vega":"PE Vega"})
            st.markdown("### Last completed minutes")
            st.dataframe(show, use_container_width=True, hide_index=True)
            chart = show.set_index("Minute")[["CE Vega", "PE Vega", "Difference"]]
            st.line_chart(chart)

        st.caption(f"Fixed Client ID {FIXED_CLIENT_ID} • ATM frozen {frozen['frozen_at']} • Expiry {frozen['expiry']} • Updated {now.strftime('%H:%M:%S IST')}")
        st.download_button("DOWNLOAD TODAY'S VEGA STRATEGY LOG", pd.DataFrame(obs).to_csv(index=False).encode("utf-8"), f"vega_930_{day}.csv", "text/csv", use_container_width=True)

    except Exception as exc:
        st.error(f"NIFTY: {exc}")


live()
