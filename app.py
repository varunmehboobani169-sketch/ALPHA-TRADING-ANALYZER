import math
import time
from datetime import datetime, timedelta, time as clock_time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

# Internal API endpoint. Provider branding is intentionally absent from the UI.
API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")

INDEXES = {
    "NIFTY": {"security_id": "13", "index_segment": "IDX_I", "option_segment": "NSE_FNO"},
    "SENSEX": {"security_id": "51", "index_segment": "IDX_I", "option_segment": "BSE_FNO"},
}
MONITORED_LEVELS = [-2, -1, 0, 1, 2]
CHAIN_GAP_SECONDS = 3.1
MAX_HISTORY_YEARS = 5

st.set_page_config(page_title="IV Monitor", page_icon="📊", layout="wide")


def auth_headers(client_id: str, token: str) -> dict:
    return {"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client_id}


def post_api(client_id: str, token: str, path: str, payload: dict, timeout: int = 45) -> dict:
    try:
        r = requests.post(API + path, headers=auth_headers(client_id, token), json=payload, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"API error {r.status_code}: {r.text[:500]}")
        return r.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error: {exc}") from exc


def get_expiries(client_id: str, token: str, security_id: str) -> list[str]:
    body = post_api(client_id, token, "/optionchain/expirylist", {"UnderlyingScrip": int(security_id), "UnderlyingSeg": "IDX_I"})
    return [str(x) for x in (body.get("data") or [])]


def choose_expiry(expiries: list[str], day) -> str | None:
    valid = []
    for value in expiries:
        try:
            d = pd.Timestamp(value).date()
            if d >= day:
                valid.append(d)
        except Exception:
            continue
    return min(valid).isoformat() if valid else None


def get_option_chain(client_id: str, token: str, index_name: str, expiry: str) -> dict:
    now = time.monotonic()
    last = st.session_state.get("last_chain_call", 0.0)
    delay = CHAIN_GAP_SECONDS - (now - last)
    if delay > 0:
        time.sleep(delay)
    st.session_state["last_chain_call"] = time.monotonic()
    body = post_api(
        client_id,
        token,
        "/optionchain",
        {"UnderlyingScrip": int(INDEXES[index_name]["security_id"]), "UnderlyingSeg": "IDX_I", "Expiry": expiry},
    )
    return body.get("data") or {}


def daily_history(client_id: str, token: str, index_name: str, start: str, end: str) -> dict:
    return post_api(
        client_id,
        token,
        "/charts/historical",
        {"securityId": INDEXES[index_name]["security_id"], "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0, "oi": False, "fromDate": start, "toDate": end},
    )


def intraday(client_id: str, token: str, index_name: str, security_id: str, segment: str, instrument: str, day) -> dict:
    return post_api(
        client_id,
        token,
        "/charts/intraday",
        {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument, "interval": "1", "oi": True, "fromDate": f"{day.isoformat()} 10:00:00", "toDate": f"{day.isoformat()} 10:02:00"},
    )


def bar_at_10(body: dict, day) -> dict | None:
    timestamps = body.get("timestamp") or []
    closes = body.get("close") or []
    oi = body.get("open_interest") or []
    if not timestamps or not closes:
        return None
    target = datetime.combine(day, clock_time(10, 0), IST)
    for i, stamp in enumerate(timestamps):
        dt = datetime.fromtimestamp(int(stamp), IST)
        if dt.date() == day and dt >= target:
            return {"datetime": dt, "price": float(closes[i]), "oi": float(oi[i]) if i < len(oi) and oi[i] is not None else None}
    return None


def index_at_10(client_id: str, token: str, name: str, day):
    cfg = INDEXES[name]
    body = intraday(client_id, token, name, cfg["security_id"], "IDX_I", "INDEX", day)
    return bar_at_10(body, day)


def option_at_10(client_id: str, token: str, name: str, security_id: str, day):
    cfg = INDEXES[name]
    body = intraday(client_id, token, name, security_id, cfg["option_segment"], "OPTIDX", day)
    return bar_at_10(body, day)


def previous_close(client_id: str, token: str, name: str, today):
    body = daily_history(client_id, token, name, (today - timedelta(days=10)).isoformat(), (today + timedelta(days=1)).isoformat())
    rows = []
    for stamp, close in zip(body.get("timestamp") or [], body.get("close") or []):
        dt = datetime.fromtimestamp(int(stamp), IST)
        if dt.date() < today and close is not None:
            rows.append((dt, float(close)))
    if not rows:
        raise RuntimeError("No previous trading-day close was returned.")
    return rows[-1]


def bs_price(spot, strike, years, sigma, is_call, rate):
    if years <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / (sigma * root)
    d2 = d1 - sigma * root
    nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    nd2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    dk = strike * math.exp(-rate * years)
    return spot * nd1 - dk * nd2 if is_call else dk * (1 - nd2) - spot * (1 - nd1)


def solve_iv(price, spot, strike, expiry, asof, is_call, rate):
    if any(v is None for v in (price, spot, strike, expiry, asof)) or price <= 0 or spot <= 0 or strike <= 0:
        return None
    expiry_dt = datetime.combine(pd.Timestamp(expiry).date(), clock_time(15, 30), IST)
    years = max((expiry_dt - asof).total_seconds() / (365 * 24 * 3600), 1e-8)
    intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    if price < intrinsic - 1e-8:
        return None
    lo, hi = 0.0001, 5.0
    if price > bs_price(spot, strike, years, hi, is_call, rate) + 1e-7:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, years, mid, is_call, rate) > price:
            hi = mid
        else:
            lo = mid
    return ((lo + hi) / 2) * 100


def chain_frame(data: dict) -> pd.DataFrame:
    rows = []
    for strike_text, node in (data.get("oc") or {}).items():
        try:
            strike = float(strike_text)
        except Exception:
            continue
        for side, key in (("CE", "ce"), ("PE", "pe")):
            leg = node.get(key) or {}
            if leg:
                rows.append({
                    "strike": strike,
                    "side": side,
                    "security_id": str(leg.get("security_id") or ""),
                    "ltp": leg.get("last_price"),
                    "iv": leg.get("implied_volatility"),
                    "oi": leg.get("oi"),
                    "previous_oi": leg.get("previous_oi"),
                    "previous_close": leg.get("previous_close_price"),
                })
    return pd.DataFrame(rows)


def nearest_strike(frame, spot):
    strikes = sorted(pd.to_numeric(frame["strike"], errors="coerce").dropna().unique())
    if not strikes:
        raise RuntimeError("No option strikes were returned.")
    return min(strikes, key=lambda x: abs(x - spot))


def monitored_strikes(frame, atm):
    strikes = sorted(pd.to_numeric(frame["strike"], errors="coerce").dropna().unique())
    atm = min(strikes, key=lambda x: abs(x - atm))
    idx = strikes.index(atm)
    return {level: strikes[idx + level] for level in MONITORED_LEVELS if 0 <= idx + level < len(strikes)}


def make_snapshot(client_id, token, name, day, rate):
    cfg = INDEXES[name]
    expiry = choose_expiry(get_expiries(client_id, token, cfg["security_id"]), day)
    if not expiry:
        raise RuntimeError(f"No active expiry found for {name}.")
    chain = get_option_chain(client_id, token, name, expiry)
    frame = chain_frame(chain)
    if frame.empty:
        raise RuntimeError(f"No option-chain data returned for {name}.")
    spot10 = index_at_10(client_id, token, name, day)
    if not spot10:
        raise RuntimeError(f"10:00 index bar is not available for {name}.")
    prev_spot, prev_dt = previous_close(client_id, token, name, day)
    atm = nearest_strike(frame, spot10["price"])
    prev_atm = nearest_strike(frame, prev_spot)

    iv_rows = []
    for side in ("CE", "PE"):
        today_row = frame[(frame.strike == atm) & (frame.side == side)]
        prev_row = frame[(frame.strike == prev_atm) & (frame.side == side)]
        if today_row.empty:
            continue
        t = today_row.iloc[0]
        p = prev_row.iloc[0] if not prev_row.empty else None
        today_iv = None
        if t.security_id:
            try:
                bar = option_at_10(client_id, token, name, t.security_id, day)
                if bar:
                    today_iv = solve_iv(bar["price"], spot10["price"], atm, expiry, bar["datetime"], side == "CE", rate)
            except Exception:
                pass
        if today_iv is None and t.iv is not None:
            today_iv = float(t.iv)
        yday_iv = None
        if p is not None and p.previous_close is not None:
            yday_iv = solve_iv(float(p.previous_close), prev_spot, prev_atm, expiry, prev_dt, side == "CE", rate)
        iv_rows.append({
            "side": side,
            "today_iv": today_iv,
            "yday_iv": yday_iv,
            "change": today_iv - yday_iv if today_iv is not None and yday_iv is not None else None,
            "today_strike": atm,
            "yday_strike": prev_atm,
        })

    oi_rows = []
    for level, strike in monitored_strikes(frame, atm).items():
        for side in ("CE", "PE"):
            row = frame[(frame.strike == strike) & (frame.side == side)]
            if row.empty:
                continue
            r = row.iloc[0]
            lock_oi = None
            if r.security_id:
                try:
                    bar = option_at_10(client_id, token, name, r.security_id, day)
                    if bar:
                        lock_oi = bar["oi"]
                except Exception:
                    pass
            if lock_oi is None and r.oi is not None:
                lock_oi = float(r.oi)
            oi_rows.append({
                "level": "ATM" if level == 0 else f"ATM{level:+d}",
                "offset": level,
                "strike": strike,
                "side": side,
                "security_id": r.security_id,
                "lock_oi": lock_oi,
            })
    return {"index": name, "expiry": expiry, "spot10": spot10["price"], "previous_spot": prev_spot, "lock_time": spot10["datetime"], "atm": atm, "iv": pd.DataFrame(iv_rows), "oi": pd.DataFrame(oi_rows)}


def refresh_oi(client_id, token, snapshot):
    frame = chain_frame(get_option_chain(client_id, token, snapshot["index"], snapshot["expiry"]))
    rows = []
    for _, base in snapshot["oi"].iterrows():
        row = frame[(frame.strike == base.strike) & (frame.side == base.side)]
        if row.empty:
            continue
        r = row.iloc[0]
        current = float(r.oi) if r.oi is not None else None
        prev = float(r.previous_oi) if r.previous_oi is not None else None
        lock = float(base.lock_oi) if base.lock_oi is not None else None
        rows.append({
            "Level": base.level,
            "Strike": base.strike,
            "Side": base.side,
            "OI Now": current,
            "Δ OI since 10:00": current - lock if current is not None and lock is not None else None,
            "Δ OI vs yesterday": current - prev if current is not None and prev is not None else None,
            "Yesterday OI": prev,
            "LTP": r.ltp,
            "IV now": r.iv,
        })
    return pd.DataFrame(rows)


def fnum(x, decimals=2):
    return "—" if x is None or pd.isna(x) else f"{x:,.{decimals}f}"


st.title("📊 IV Monitor")
st.caption("NIFTY + SENSEX • daily 10:00 ATM lock • IV change • ATM±1/±2 OI movement")

with st.sidebar:
    st.subheader("Market Access")
    client_id = st.text_input("Client ID")
    access_token = st.text_input("Access Token", type="password")
    rate_pct = st.number_input("IV calculation rate (%)", min_value=0.0, max_value=20.0, value=6.5, step=0.25)
    st.caption("Access credentials stay in this session.")

if not client_id or not access_token:
    st.info("Enter your Client ID and Access Token to start the monitor.")
    st.stop()

now = datetime.now(IST)
today = now.date()
if now.weekday() >= 5:
    st.warning("The monitor is waiting for the next market day.")
    st.stop()
if now.time() < clock_time(10, 0):
    remaining = datetime.combine(today, clock_time(10, 0), IST) - now
    st.info(f"10:00 ATM lock is pending. Time remaining: {str(remaining).split('.')[0]}.")
    st.stop()

rate = rate_pct / 100.0
st.session_state.setdefault("snapshots", {})


@st.fragment(run_every="60s")
def monitor():
    for name in INDEXES:
        key = f"{name}:{today.isoformat()}"
        try:
            snapshot = st.session_state["snapshots"].get(key)
            if snapshot is None:
                snapshot = make_snapshot(client_id, access_token, name, today, rate)
                st.session_state["snapshots"][key] = snapshot

            st.subheader(name)
            a, b, c, d = st.columns(4)
            a.metric("10:00 Spot", fnum(snapshot["spot10"]))
            b.metric("Locked ATM", fnum(snapshot["atm"]))
            c.metric("Yesterday Close", fnum(snapshot["previous_spot"]))
            d.metric("Nearest Expiry", snapshot["expiry"])
            st.caption(f"ATM locked from the {snapshot['lock_time'].strftime('%H:%M:%S IST')} bar.")

            iv = snapshot["iv"]
            if iv.empty:
                st.warning("IV comparison is unavailable for this index.")
            else:
                x, y = st.columns(2)
                for holder, side in ((x, "CE"), (y, "PE")):
                    row = iv[iv.side == side]
                    if row.empty:
                        continue
                    r = row.iloc[0]
                    arrow = "↑" if r.change is not None and r.change > 0 else ("↓" if r.change is not None and r.change < 0 else "→")
                    with holder:
                        st.metric(f"{side} IV @ 10:00", f"{fnum(r.today_iv)}%")
                        st.metric(f"{side} IV vs yesterday {arrow}", f"{fnum(r.change)} vol pts")
                        st.caption(f"Yesterday ATM strike: {fnum(r.yday_strike)}")

            try:
                live = refresh_oi(client_id, access_token, snapshot)
                if not live.empty:
                    st.markdown("**Open-interest movement around locked ATM**")
                    view = live.copy()
                    view["Strike"] = view["Strike"].map(fnum)
                    for col in ("OI Now", "Yesterday OI", "Δ OI since 10:00", "Δ OI vs yesterday"):
                        view[col] = view[col].map(lambda x: fnum(x, 0))
                    view["LTP"] = view["LTP"].map(fnum)
                    view["IV now"] = view["IV now"].map(lambda x: f"{fnum(x)}%")
                    st.dataframe(view, use_container_width=True, hide_index=True)
                else:
                    st.info("No OI rows returned on the latest refresh.")
            except Exception as exc:
                st.warning(f"OI refresh temporarily unavailable: {exc}")
            st.divider()


try:
    monitor()
except Exception as exc:
    st.error(f"Monitor error: {exc}")
