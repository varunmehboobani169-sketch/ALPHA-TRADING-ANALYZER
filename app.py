import math
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")

INDEXES = {
    "NIFTY": {"security_id": 13, "segment": "IDX_I", "option_segment": "NSE_FNO"},
    "SENSEX": {"security_id": 51, "segment": "IDX_I", "option_segment": "BSE_FNO"},
}
LEVELS = [-2, -1, 0, 1, 2]
CHAIN_GAP_SECONDS = 3.2
REFRESH_SECONDS = 60
RISK_FREE_RATE = 0.065

st.set_page_config(page_title="IV Monitor", page_icon="📊", layout="wide")


def auth_headers(client_id: str, access_token: str) -> dict:
    return {"Accept": "application/json", "Content-Type": "application/json", "access-token": access_token, "client-id": client_id}


def post_api(client_id: str, access_token: str, path: str, payload: dict, timeout: int = 45) -> dict:
    try:
        response = requests.post(API + path, headers=auth_headers(client_id, access_token), json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API returned invalid JSON.") from exc


def throttle_chain():
    last = st.session_state.get("last_chain_request", 0.0)
    elapsed = time.monotonic() - last
    if elapsed < CHAIN_GAP_SECONDS:
        time.sleep(CHAIN_GAP_SECONDS - elapsed)
    st.session_state["last_chain_request"] = time.monotonic()


def expiry_list(client_id: str, access_token: str, security_id: int) -> list[str]:
    body = post_api(client_id, access_token, "/optionchain/expirylist", {"UnderlyingScrip": security_id, "UnderlyingSeg": "IDX_I"})
    return [str(x) for x in (body.get("data") or [])]


def nearest_expiry(expiries: list[str], today) -> str:
    dates = []
    for item in expiries:
        try:
            d = pd.Timestamp(item).date()
        except Exception:
            continue
        if d >= today:
            dates.append(d)
    if not dates:
        raise RuntimeError("No active option expiry was returned.")
    return min(dates).isoformat()


def option_chain(client_id: str, access_token: str, security_id: int, expiry: str) -> dict:
    throttle_chain()
    body = post_api(client_id, access_token, "/optionchain", {"UnderlyingScrip": security_id, "UnderlyingSeg": "IDX_I", "Expiry": expiry})
    return body.get("data") or {}


def intraday(client_id: str, access_token: str, security_id: int, segment: str, instrument: str, start: str, end: str, oi: bool = False) -> dict:
    return post_api(client_id, access_token, "/charts/intraday", {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument, "interval": "1", "oi": oi, "fromDate": start, "toDate": end})


def daily_history(client_id: str, access_token: str, security_id: int, start: str, end: str) -> dict:
    return post_api(client_id, access_token, "/charts/historical", {"securityId": str(security_id), "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0, "oi": False, "fromDate": start, "toDate": end})


def extract_ohlc_bar(body: dict, target_date, target_time: dtime):
    timestamps = body.get("timestamp") or []
    closes = body.get("close") or []
    if not timestamps or not closes:
        return None
    target = datetime.combine(target_date, target_time, IST)
    for ts, close in zip(timestamps, closes):
        dt = datetime.fromtimestamp(int(ts), IST)
        if dt.date() == target_date and dt >= target and close is not None:
            return {"datetime": dt, "price": float(close)}
    return None


def get_spot_10am(client_id: str, access_token: str, index_name: str, day):
    spec = INDEXES[index_name]
    body = intraday(client_id, access_token, spec["security_id"], spec["segment"], "INDEX", f"{day.isoformat()} 10:00:00", f"{day.isoformat()} 10:02:00")
    return extract_ohlc_bar(body, day, dtime(10, 0))


def get_previous_close(client_id: str, access_token: str, index_name: str, day) -> tuple[float, datetime]:
    spec = INDEXES[index_name]
    body = daily_history(client_id, access_token, spec["security_id"], (day - timedelta(days=10)).isoformat(), (day + timedelta(days=1)).isoformat())
    rows = []
    for ts, close in zip(body.get("timestamp") or [], body.get("close") or []):
        if close is None:
            continue
        dt = datetime.fromtimestamp(int(ts), IST)
        if dt.date() < day:
            rows.append((dt, float(close)))
    if not rows:
        raise RuntimeError(f"No previous close found for {index_name}.")
    return rows[-1][1], rows[-1][0]


def normalize_chain(chain: dict) -> pd.DataFrame:
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
            rows.append({"strike": strike, "side": side, "security_id": leg.get("security_id"), "ltp": leg.get("last_price"), "iv": leg.get("implied_volatility"), "oi": leg.get("oi"), "previous_oi": leg.get("previous_oi"), "previous_close_price": leg.get("previous_close_price")})
    return pd.DataFrame(rows)


def nearest_strike(strikes, spot: float) -> float:
    if not strikes:
        raise RuntimeError("No strikes returned in option chain.")
    return min(strikes, key=lambda x: abs(x - spot))


def offset_strikes(df: pd.DataFrame, atm: float) -> dict[int, float]:
    strikes = sorted(pd.to_numeric(df["strike"], errors="coerce").dropna().unique().tolist())
    if atm not in strikes:
        atm = nearest_strike(strikes, atm)
    idx = strikes.index(atm)
    return {level: strikes[idx + level] for level in LEVELS if 0 <= idx + level < len(strikes)}


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def option_theoretical_price(spot, strike, t, sigma, call=True, rate=RISK_FREE_RATE):
    if t <= 0:
        return max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if call:
        return spot * norm_cdf(d1) - strike * math.exp(-rate * t) * norm_cdf(d2)
    return strike * math.exp(-rate * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def calculate_iv(price, spot, strike, expiry, asof, call=True):
    try:
        price, spot, strike = float(price), float(spot), float(strike)
    except (TypeError, ValueError):
        return None
    if price <= 0 or spot <= 0 or strike <= 0:
        return None
    expiry_dt = datetime.combine(pd.Timestamp(expiry).date(), dtime(15, 30), IST)
    t = (expiry_dt - asof).total_seconds() / (365.0 * 24.0 * 3600.0)
    if t <= 0:
        return None
    intrinsic = max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    if price < intrinsic:
        return None
    lo, hi = 1e-6, 5.0
    if option_theoretical_price(spot, strike, t, hi, call) < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2.0
        model = option_theoretical_price(spot, strike, t, mid, call)
        if model > price:
            hi = mid
        else:
            lo = mid
    return ((lo + hi) / 2.0) * 100.0


def option_price_at_10am(client_id: str, access_token: str, option_security_id: int, option_segment: str, day):
    body = intraday(client_id, access_token, int(option_security_id), option_segment, "OPTIDX", f"{day.isoformat()} 10:00:00", f"{day.isoformat()} 10:02:00", oi=True)
    return extract_ohlc_bar(body, day, dtime(10, 0))


def create_snapshot(client_id: str, access_token: str, index_name: str, day):
    spec = INDEXES[index_name]
    expiry = nearest_expiry(expiry_list(client_id, access_token, spec["security_id"]), day)
    chain = option_chain(client_id, access_token, spec["security_id"], expiry)
    df = normalize_chain(chain)
    if df.empty:
        raise RuntimeError(f"No option-chain data returned for {index_name}.")
    spot10 = get_spot_10am(client_id, access_token, index_name, day)
    if spot10 is None:
        raise RuntimeError(f"No 10:00 spot bar found for {index_name}.")
    previous_close, previous_close_dt = get_previous_close(client_id, access_token, index_name, day)
    strikes = sorted(pd.to_numeric(df["strike"], errors="coerce").dropna().unique().tolist())
    atm = nearest_strike(strikes, spot10["price"])
    previous_atm = nearest_strike(strikes, previous_close)

    iv_rows = []
    for side in ("CE", "PE"):
        current = df[(df["strike"] == atm) & (df["side"] == side)]
        previous = df[(df["strike"] == previous_atm) & (df["side"] == side)]
        if current.empty:
            continue
        cur = current.iloc[0]
        prev = previous.iloc[0] if not previous.empty else None
        current_iv = None
        if pd.notna(cur["security_id"]):
            try:
                option_bar = option_price_at_10am(client_id, access_token, int(cur["security_id"]), spec["option_segment"], day)
            except Exception:
                option_bar = None
            if option_bar:
                current_iv = calculate_iv(option_bar["price"], spot10["price"], atm, expiry, option_bar["datetime"], call=(side == "CE"))
        if current_iv is None and pd.notna(cur["iv"]):
            current_iv = float(cur["iv"])
        previous_iv = None
        if prev is not None and pd.notna(prev["previous_close_price"]):
            previous_iv = calculate_iv(float(prev["previous_close_price"]), previous_close, previous_atm, expiry, previous_close_dt, call=(side == "CE"))
        iv_rows.append({"Side": side, "10:00 IV": current_iv, "Yesterday ATM-close IV": previous_iv, "IV Change": current_iv - previous_iv if current_iv is not None and previous_iv is not None else None, "10:00 ATM Strike": atm, "Yesterday ATM Strike": previous_atm})

    oi_rows = []
    for level, strike in offset_strikes(df, atm).items():
        for side in ("CE", "PE"):
            row = df[(df["strike"] == strike) & (df["side"] == side)]
            if row.empty:
                continue
            r = row.iloc[0]
            lock_oi = None
            if r.security_id:
                try:
                    bar = option_price_at_10am(client_id, access_token, int(r["security_id"]), spec["option_segment"], day)
                    if bar and bar.get("oi") is not None:
                        lock_oi = float(bar["oi"])
                except Exception:
                    lock_oi = None
            if lock_oi is None and r.oi is not None:
                lock_oi = float(r.oi)
            oi_rows.append({"Level": "ATM" if level == 0 else f"ATM{level:+d}", "Strike": strike, "Side": side, "OI at 10:00": lock_oi, "Yesterday OI": float(r["previous_oi"]) if pd.notna(r["previous_oi"]) else None, "Option Security ID": r["security_id"]})
    return {"index": index_name, "expiry": expiry, "spot10": spot10["price"], "lock_time": spot10["datetime"], "atm": atm, "iv": pd.DataFrame(iv_rows), "oi": pd.DataFrame(oi_rows)}


def refresh_current_oi(client_id: str, access_token: str, snapshot: dict) -> pd.DataFrame:
    spec = INDEXES[snapshot["index"]]
    df = normalize_chain(option_chain(client_id, access_token, spec["security_id"], snapshot["expiry"]))
    if df.empty:
        return pd.DataFrame()
    rows = []
    for _, base in snapshot["oi"].iterrows():
        current = df[(df["strike"] == float(base["Strike"])) & (df["side"] == base["Side"])]
        if current.empty:
            continue
        r = current.iloc[0]
        now_oi = float(r["oi"]) if pd.notna(r["oi"]) else None
        lock_oi = float(base["OI at 10:00"]) if pd.notna(base["OI at 10:00"]) else None
        prev_oi = float(r["previous_oi"]) if pd.notna(r["previous_oi"]) else None
        rows.append({"Level": base["Level"], "Strike": float(base["Strike"]), "Side": base["Side"], "OI Now": now_oi, "Change Since 10:00": now_oi - lock_oi if now_oi is not None and lock_oi is not None else None, "Change vs Yesterday": now_oi - prev_oi if now_oi is not None and prev_oi is not None else None, "Yesterday OI": prev_oi, "Option LTP": float(r["ltp"]) if pd.notna(r["ltp"]) else None, "IV Now": float(r["iv"]) if pd.notna(r["iv"]) else None})
    return pd.DataFrame(rows)


def run_monitor(client_id: str, access_token: str, day):
    snapshots, errors = {}, {}
    for index_name in ("NIFTY", "SENSEX"):
        try:
            snapshots[index_name] = create_snapshot(client_id, access_token, index_name, day)
        except Exception as exc:
            errors[index_name] = str(exc)
    return snapshots, errors


st.title("📊 IV Monitor")
st.caption("NIFTY + SENSEX • daily 10:00 ATM lock • IV change • OI monitoring at ATM−2, ATM−1, ATM, ATM+1, ATM+2")

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
if now.time() < dtime(10, 0):
    remaining = datetime.combine(today, dtime(10, 0), IST) - now
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
                snapshot = create_snapshot(client_id, access_token, name, today)
                st.session_state["snapshots"][key] = snapshot

            st.subheader(name)
            c1, c2, c3 = st.columns(3)
            c1.metric("10:00 Spot", f"{snapshot['spot10']:,.2f}")
            c2.metric("ATM", f"{snapshot['atm']:,.2f}")
            c3.metric("Expiry", snapshot["expiry"])

            iv_df = snapshot["iv"].copy()
            for col_name in ("10:00 IV", "Yesterday ATM-close IV", "IV Change"):
                if col_name in iv_df.columns:
                    iv_df[col_name] = pd.to_numeric(iv_df[col_name], errors="coerce").round(2)
            st.write("**ATM IV comparison**")
            st.dataframe(iv_df, use_container_width=True, hide_index=True)

            oi_df = refresh_current_oi(client_id, access_token, snapshot)
            st.write("**OI movement: ATM−2 to ATM+2**")
            st.dataframe(oi_df, use_container_width=True, hide_index=True)
            st.caption(f"10:00 lock: {snapshot['lock_time'].strftime('%Y-%m-%d %H:%M:%S')} IST")
        except Exception as exc:
            st.error(f"{name}: {exc}")


monitor()
