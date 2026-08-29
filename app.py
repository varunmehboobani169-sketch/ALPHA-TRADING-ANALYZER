import math
import time
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

# API endpoint is intentionally kept in code; provider branding is not shown in the UI.
API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")

# Stable index IDs used by the market-data API.
INDEXES = {
    "NIFTY": {"security_id": "13", "segment": "IDX_I"},
    "SENSEX": {"security_id": "51", "segment": "IDX_I"},
}

ATM_LEVELS = [-2, -1, 0, 1, 2]
OPTION_CHAIN_GAP = 3.1  # documented limit is one unique option-chain request every 3 seconds
HISTORY_LIMIT_YEARS = 5
RISK_FREE_RATE = 0.065

st.set_page_config(page_title="IV Monitor", page_icon="📊", layout="wide")


def headers(client_id: str, token: str) -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }


def api_post(client_id: str, token: str, path: str, payload: dict, timeout: int = 45):
    try:
        r = requests.post(API + path, headers=headers(client_id, token), json=payload, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"API error {r.status_code}: {r.text[:500]}")
        return r.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error: {exc}") from exc


def option_chain(client_id: str, token: str, security_id: str, expiry: str):
    now = time.monotonic()
    last = st.session_state.get("last_chain_request", 0.0)
    wait = OPTION_CHAIN_GAP - (now - last)
    if wait > 0:
        time.sleep(wait)
    st.session_state["last_chain_request"] = time.monotonic()
    body = api_post(
        client_id,
        token,
        "/optionchain",
        {"UnderlyingScrip": int(security_id), "UnderlyingSeg": "IDX_I", "Expiry": expiry},
    )
    return body.get("data") or {}


def expiry_list(client_id: str, token: str, security_id: str):
    body = api_post(
        client_id,
        token,
        "/optionchain/expirylist",
        {"UnderlyingScrip": int(security_id), "UnderlyingSeg": "IDX_I"},
    )
    return list(body.get("data") or [])


def choose_expiry(expiries: list[str], day):
    dates = []
    for x in expiries:
        try:
            d = pd.Timestamp(x).date()
            if d >= day:
                dates.append(d)
        except Exception:
            pass
    return min(dates).isoformat() if dates else None


def historical_daily(client_id: str, token: str, security_id: str, from_date: str, to_date: str):
    return api_post(
        client_id,
        token,
        "/charts/historical",
        {
            "securityId": str(security_id),
            "exchangeSegment": "IDX_I",
            "instrument": "INDEX",
            "expiryCode": 0,
            "oi": False,
            "fromDate": from_date,
            "toDate": to_date,
        },
    )


def intraday(client_id: str, token: str, security_id: str, from_dt: str, to_dt: str, oi: bool):
    return api_post(
        client_id,
        token,
        "/charts/intraday",
        {
            "securityId": str(security_id),
            "exchangeSegment": "IDX_I" if security_id in {"13", "51"} else "NSE_FNO",
            "instrument": "INDEX" if security_id in {"13", "51"} else "OPTIDX",
            "interval": "1",
            "oi": oi,
            "fromDate": from_dt,
            "toDate": to_dt,
        },
    )


def previous_trading_close(client_id: str, token: str, security_id: str, today) -> tuple[float, datetime]:
    start = (today - timedelta(days=10)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    body = historical_daily(client_id, token, security_id, start, end)
    ts = body.get("timestamp") or []
    closes = body.get("close") or []
    if not ts or not closes:
        raise RuntimeError("No daily index history returned.")
    rows = []
    for t, c in zip(ts, closes):
        d = datetime.fromtimestamp(int(t), IST)
        if d.date() < today and c is not None:
            rows.append((d, float(c)))
    if not rows:
        raise RuntimeError("No previous trading-day close found.")
    return rows[-1][1], rows[-1][0]


def bar_at_or_after(body: dict, target_date, target_time: dt_time):
    ts = body.get("timestamp") or []
    close = body.get("close") or []
    oi = body.get("open_interest") or []
    if not ts or not close:
        return None
    target = datetime.combine(target_date, target_time, IST)
    for i, t in enumerate(ts):
        d = datetime.fromtimestamp(int(t), IST)
        if d >= target and d.date() == target_date:
            return {"datetime": d, "price": float(close[i]), "oi": float(oi[i]) if i < len(oi) and oi[i] is not None else None}
    return None


def spot_10am(client_id: str, token: str, security_id: str, day):
    body = intraday(
        client_id,
        token,
        security_id,
        f"{day.isoformat()} 10:00:00",
        f"{day.isoformat()} 10:02:00",
        False,
    )
    return bar_at_or_after(body, day, dt_time(10, 0))


def option_10am(client_id: str, token: str, security_id: str, day):
    body = intraday(
        client_id,
        token,
        security_id,
        f"{day.isoformat()} 10:00:00",
        f"{day.isoformat()} 10:02:00",
        True,
    )
    return bar_at_or_after(body, day, dt_time(10, 0))


def bs_price(spot, strike, t, sigma, is_call, r=RISK_FREE_RATE):
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    nd2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    disc_k = strike * math.exp(-r * t)
    if is_call:
        return spot * nd1 - disc_k * nd2
    return disc_k * (1.0 - nd2) - spot * (1.0 - nd1)


def implied_vol(price, spot, strike, expiry, asof, is_call):
    if any(x is None for x in (price, spot, strike, expiry, asof)) or price <= 0 or spot <= 0 or strike <= 0:
        return None
    if not is_call and price >= strike:
        return None
    if is_call and price < max(spot - strike, 0):
        return None
    if not is_call and price < max(strike - spot, 0):
        return None
    expiry_dt = datetime.combine(pd.Timestamp(expiry).date(), dt_time(15, 30), IST)
    t = max((expiry_dt - asof).total_seconds() / (365.0 * 24 * 3600), 1e-7)
    lo, hi = 0.0001, 5.0
    p_hi = bs_price(spot, strike, t, hi, is_call)
    if price > p_hi + 1e-8:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        p = bs_price(spot, strike, t, mid, is_call)
        if p > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2 * 100.0


def normalize_chain(chain: dict) -> pd.DataFrame:
    rows = []
    spot = chain.get("last_price")
    for strike_key, node in (chain.get("oc") or {}).items():
        try:
            strike = float(strike_key)
        except Exception:
            continue
        for side, key in (("CE", "ce"), ("PE", "pe")):
            leg = node.get(key) or {}
            if not leg:
                continue
            rows.append(
                {
                    "strike": strike,
                    "side": side,
                    "security_id": str(leg.get("security_id") or ""),
                    "ltp": leg.get("last_price"),
                    "iv": leg.get("implied_volatility"),
                    "oi": leg.get("oi"),
                    "previous_oi": leg.get("previous_oi"),
                    "previous_close_price": leg.get("previous_close_price"),
                    "volume": leg.get("volume"),
                    "spot": spot,
                }
            )
    return pd.DataFrame(rows)


def atm_strike(df: pd.DataFrame, spot: float) -> float:
    strikes = sorted(pd.to_numeric(df["strike"], errors="coerce").dropna().unique())
    return min(strikes, key=lambda x: abs(x - spot))


def nearest_offset_map(df: pd.DataFrame, atm: float) -> dict[int, float]:
    strikes = sorted(pd.to_numeric(df["strike"], errors="coerce").dropna().unique())
    if atm not in strikes:
        atm = min(strikes, key=lambda x: abs(x - atm))
    i = strikes.index(atm)
    return {level: strikes[i + level] for level in ATM_LEVELS if 0 <= i + level < len(strikes)}


def today_expiry_chain(client_id, token, name, day):
    sec = INDEXES[name]["security_id"]
    expiries = st.session_state.setdefault("expiries", {})
    key = f"{name}:{day.isoformat()}"
    exp = expiries.get(key)
    if not exp:
        exp = choose_expiry(expiry_list(client_id, token, sec), day)
        if not exp:
            raise RuntimeError(f"No active expiry found for {name}.")
        expiries[key] = exp
    chain = option_chain(client_id, token, sec, exp)
    return exp, chain


def build_snapshot(client_id, token, name: str, day, expiry: str, chain: dict, spot10: dict, prev_spot: float, prev_close_dt: datetime):
    cdf = normalize_chain(chain)
    if cdf.empty:
        raise RuntimeError(f"No option-chain rows returned for {name}.")
    today_spot = float(spot10["price"])
    atm = atm_strike(cdf, today_spot)
    strike_map = nearest_offset_map(cdf, atm)
    prev_atm = atm_strike(cdf, float(prev_spot))
    rows = []
    for side in ("CE", "PE"):
        today_atm = cdf[(cdf.strike == atm) & (cdf.side == side)]
        prev_atm_row = cdf[(cdf.strike == prev_atm) & (cdf.side == side)]
        if today_atm.empty:
            continue
        tr = today_atm.iloc[0]
        pr = prev_atm_row.iloc[0] if not prev_atm_row.empty else None
        t_iv = None
        # Reconstruct the 10:00 option IV from its 10:00 minute close for a time-accurate comparison.
        if tr.security_id:
            try:
                obar = option_10am(client_id, token, tr.security_id, day)
                if obar:
                    t_iv = implied_vol(
                        obar["price"],
                        today_spot,
                        atm,
                        expiry,
                        obar["datetime"],
                        side == "CE",
                    )
            except Exception:
                t_iv = None
        if t_iv is None:
            t_iv = float(tr.iv) if tr.iv is not None else None
        prev_iv = None
        if pr is not None and pr.previous_close_price is not None:
            prev_iv = implied_vol(
                float(pr.previous_close_price),
                float(prev_spot),
                float(prev_atm),
                expiry,
                prev_close_dt,
                side == "CE",
            )
        rows.append(
            {
                "side": side,
                "today_iv": t_iv,
                "yday_iv": prev_iv,
                "iv_change": (t_iv - prev_iv) if t_iv is not None and prev_iv is not None else None,
                "today_strike": atm,
                "yday_strike": prev_atm,
                "today_price": tr.ltp,
                "yday_price": pr.previous_close_price if pr is not None else None,
            }
        )

    oi_rows = []
    for level, strike in strike_map.items():
        for side in ("CE", "PE"):
            row = cdf[(cdf.strike == strike) & (cdf.side == side)]
            if row.empty:
                continue
            r = row.iloc[0]
            oi_rows.append(
                {
                    "level": "ATM" if level == 0 else f"ATM{level:+d}",
                    "offset": level,
                    "strike": strike,
                    "side": side,
                    "security_id": r.security_id,
                    "current_oi": float(r.oi) if r.oi is not None else None,
                    "previous_oi": float(r.previous_oi) if r.previous_oi is not None else None,
                    "oi_change_prev_close": (float(r.oi) - float(r.previous_oi)) if r.oi is not None and r.previous_oi is not None else None,
                    "option_ltp": r.ltp,
                    "provider_iv": r.iv,
                }
            )

    return {
        "index": name,
        "expiry": expiry,
        "spot10": today_spot,
        "atm": atm,
        "yday_spot": prev_spot,
        "lock_time": spot10["datetime"],
        "iv": pd.DataFrame(rows),
        "oi": pd.DataFrame(oi_rows),
    }


def refresh_oi(client_id, token, snapshot):
    name = snapshot["index"]
    expiry = snapshot["expiry"]
    chain = option_chain(client_id, token, INDEXES[name]["security_id"], expiry)
    cdf = normalize_chain(chain)
    out = []
    base = snapshot["oi"]
    for _, b in base.iterrows():
        row = cdf[(cdf.strike == b.strike) & (cdf.side == b.side)]
        if row.empty:
            continue
        r = row.iloc[0]
        current = float(r.oi) if r.oi is not None else None
        change_lock = current - float(b.current_oi) if current is not None and b.current_oi is not None else None
        change_prev = current - float(r.previous_oi) if current is not None and r.previous_oi is not None else None
        out.append(
            {
                "level": b.level,
                "strike": b.strike,
                "side": b.side,
                "OI": current,
                "Δ OI since 10:00": change_lock,
                "Δ OI vs yesterday": change_prev,
                "Yesterday OI": r.previous_oi,
                "LTP": r.ltp,
                "IV now": r.iv,
            }
        )
    return pd.DataFrame(out)


def fmt_num(x, decimals=2):
    if x is None or pd.isna(x):
        return "—"
    return f"{x:,.{decimals}f}"


st.title("📊 IV Monitor")
st.caption("NIFTY + SENSEX • 10:00 ATM lock • ATM±1/±2 OI movement")

with st.sidebar:
    st.subheader("Market Access")
    client_id = st.text_input("Client ID")
    access_token = st.text_input("Access Token", type="password")
    st.divider()
    st.caption("Credentials are used only for this session and are not displayed back to you.")
    st.number_input("IV calculation rate (%)", min_value=0.0, max_value=15.0, value=6.5, step=0.25)

if not client_id or not access_token:
    st.info("Enter your Client ID and Access Token to start the monitor.")
    st.stop()

now = datetime.now(IST)
today = now.date()

if now.weekday() >= 5:
    st.warning("The monitor is waiting for the next market day.")
    st.stop()

if now.time() < dt_time(10, 0):
    remaining = datetime.combine(today, dt_time(10, 0), IST) - now
    st.info(f"10:00 ATM lock is pending. Time remaining: {str(remaining).split('.')[0]}.")
    st.stop()

# One daily lock per index. Once captured, ATM never moves for the rest of the session.
st.session_state.setdefault("daily_snapshots", {})

@st.fragment(run_every="60s")
def monitor():
    for name in INDEXES:
        key = f"{name}:{today.isoformat()}"
        try:
            snapshot = st.session_state["daily_snapshots"].get(key)
            if snapshot is None:
                expiry, chain = today_expiry_chain(client_id, access_token, name, today)
                spot10 = spot_10am(client_id, access_token, INDEXES[name]["security_id"], today)
                if not spot10:
                    st.warning(f"{name}: 10:00 price bar is not available yet.")
                    continue
                prev_spot, prev_dt = previous_trading_close(client_id, access_token, INDEXES[name]["security_id"], today)
                snapshot = build_snapshot(client_id, access_token, name, today, expiry, chain, spot10, prev_spot, prev_dt)
                st.session_state["daily_snapshots"][key] = snapshot

            st.subheader(name)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("10:00 Spot", fmt_num(snapshot["spot10"], 2))
            c2.metric("Locked ATM", fmt_num(snapshot["atm"], 2))
            c3.metric("Yesterday Close", fmt_num(snapshot["yday_spot"], 2))
            c4.metric("Expiry", snapshot["expiry"])
            st.caption(f"ATM locked from the {snapshot['lock_time'].strftime('%H:%M:%S IST')} minute. Yesterday close: {snapshot['yday_spot']:,.2f}.")

            iv = snapshot["iv"].copy()
            for _, r in iv.iterrows():
                direction = "↑" if r.iv_change is not None and r.iv_change > 0 else ("↓" if r.iv_change is not None and r.iv_change < 0 else "→")
                d1, d2, d3 = st.columns(3)
                d1.metric(f"{r.side} IV @ 10:00", f"{fmt_num(r.today_iv, 2)}%")
                d2.metric(f"{r.side} IV yday close", f"{fmt_num(r.yday_iv, 2)}%")
                d3.metric(f"{r.side} IV change {direction}", f"{fmt_num(r.iv_change, 2)} vol pts")
            st.caption(f"Yesterday ATM strike was {fmt_num(iv.yday_strike.iloc[0], 2) if not iv.empty else '—'}; today's locked ATM strike is {fmt_num(snapshot['atm'], 2)}.")

            try:
                oi_live = refresh_oi(client_id, access_token, snapshot)
                if not oi_live.empty:
                    st.markdown("**Open-interest movement around locked ATM**")
                    display = oi_live[["level", "strike", "side", "OI", "Δ OI since 10:00", "Δ OI vs yesterday", "Yesterday OI", "LTP", "IV now"]].copy()
                    display["OI"] = display["OI"].map(lambda x: fmt_num(x, 0))
                    display["Yesterday OI"] = display["Yesterday OI"].map(lambda x: fmt_num(x, 0))
                    display["Δ OI since 10:00"] = display["Δ OI since 10:00"].map(lambda x: fmt_num(x, 0))
                    display["Δ OI vs yesterday"] = display["Δ OI vs yesterday"].map(lambda x: fmt_num(x, 0))
                    display["strike"] = display["strike"].map(lambda x: fmt_num(x, 2))
                    display["LTP"] = display["LTP"].map(lambda x: fmt_num(x, 2))
                    display["IV now"] = display["IV now"].map(lambda x: f"{fmt_num(x, 2)}%" if x is not None and not pd.isna(x) else "—")
                    st.dataframe(display, use_container_width=True, hide_index=True)
                else:
                    st.info("OI refresh returned no rows. The lock remains unchanged.")
            except Exception as exc:
                st.warning(f"OI refresh temporarily unavailable: {exc}")

            st.divider()

try:
    monitor()
except Exception as exc:
    st.error(f"Monitor error: {exc}")
