from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st

API = "https://api.dhan.co/v2"
INDEXES = {
    "NIFTY": {"security_id": 13, "segment": "NSE_FNO"},
    "SENSEX": {"security_id": 51, "segment": "BSE_FNO"},
}
LEVELS = [-2, -1, 0, 1, 2]
REFRESH_SECONDS = 60

st.set_page_config(page_title="30-Day Monitoring", page_icon="📈", layout="wide")

st.title("📈 30-Day Monitoring")
st.caption("Historical 10:00 ATM / near-ATM IV and OI monitoring for NIFTY and SENSEX")

with st.sidebar:
    st.subheader("Market Access")
    client_id = st.text_input("Client ID")
    access_token = st.text_input("Access Token", type="password")
    expiry_flag = st.selectbox("Expiry series", ["WEEK", "MONTH"], index=0)
    expiry_code = st.selectbox("Expiry code", [0, 1, 2], index=0, help="0 = near, 1 = next, 2 = farther expiry")
    days = st.slider("Lookback", 7, 30, 30)
    auto_refresh = st.checkbox("Auto-refresh every 1 minute", value=True)

if not client_id or not access_token:
    st.info("Enter Client ID and Access Token to use the historical monitor.")
    st.stop()


def post(path, payload):
    response = requests.post(
        API + path,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": access_token,
            "client-id": client_id,
        },
        json=payload,
        timeout=90,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    return response.json()


def windows(start, end):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=29), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def rolling(index_name, level, side, start, end):
    spec = INDEXES[index_name]
    rows = []
    for win_start, win_end in windows(start, end):
        body = post(
            "/charts/rollingoption",
            {
                "exchangeSegment": spec["segment"],
                "interval": "1",
                "securityId": str(spec["security_id"]),
                "instrument": "OPTIDX",
                "expiryFlag": expiry_flag,
                "expiryCode": int(expiry_code),
                "strike": "ATM" if level == 0 else f"ATM{level:+d}",
                "drvOptionType": side,
                "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
                "fromDate": win_start.isoformat(),
                "toDate": win_end.isoformat(),
            },
        )
        leg = ((body.get("data") or {}).get("ce" if side == "CE" else "pe") or {})
        stamps = leg.get("timestamp") or []
        values = {name: (leg.get(name) or []) for name in ("strike", "spot", "iv", "oi", "close")}
        for i, stamp in enumerate(stamps):
            try:
                ts = pd.to_datetime(int(stamp), unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None)
            except Exception:
                continue
            if ts.hour != 10 or ts.minute != 0:
                continue
            rows.append(
                {
                    "date": ts.date(),
                    "index": index_name,
                    "level": "ATM" if level == 0 else f"ATM{level:+d}",
                    "side": side,
                    "strike": values["strike"][i] if i < len(values["strike"]) else None,
                    "spot": values["spot"][i] if i < len(values["spot"]) else None,
                    "iv": values["iv"][i] if i < len(values["iv"]) else None,
                    "oi": values["oi"][i] if i < len(values["oi"]) else None,
                    "close": values["close"][i] if i < len(values["close"]) else None,
                }
            )
    return rows


def safe_df(rows):
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for col in ("strike", "spot", "iv", "oi", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return (
        frame.drop_duplicates(["date", "index", "level", "side"], keep="last")
        .sort_values(["date", "index", "level", "side"])
        .reset_index(drop=True)
    )


def fetch_monitor_data():
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    all_rows = []
    errors = []
    total = len(INDEXES) * len(LEVELS) * 2
    done = 0
    progress = st.progress(0.0)

    for index_name in INDEXES:
        for level in LEVELS:
            for side in ("CE", "PE"):
                try:
                    all_rows.extend(rolling(index_name, level, side, start, today))
                except Exception as exc:
                    errors.append(f"{index_name} {level:+d} {side}: {exc}")
                done += 1
                progress.progress(done / total)

    return safe_df(all_rows), errors


def render_results(data, errors, fetched_at):
    st.success(f"Loaded {len(data):,} observations • Last update: {fetched_at.strftime('%H:%M:%S')}")

    st.subheader("ATM IV trend")
    atm = data[data["level"] == "ATM"]
    if not atm.empty:
        pivot = atm.pivot_table(index="date", columns=["index", "side"], values="iv", aggfunc="last")
        st.line_chart(pivot)

    st.subheader("ATM ±2 OI")
    oi = data[data["level"].isin(["ATM-2", "ATM-1", "ATM", "ATM+1", "ATM+2"])].copy()
    st.dataframe(oi, use_container_width=True, hide_index=True)

    st.subheader("Daily IV comparison")
    iv = data.copy().sort_values("date")
    iv["IV"] = pd.to_numeric(iv["iv"], errors="coerce")
    iv["IV Change vs Prior Observation"] = iv.groupby(["index", "level", "side"])["IV"].diff()
    st.dataframe(
        iv[["date", "index", "level", "side", "strike", "spot", "IV", "IV Change vs Prior Observation", "oi"]],
        use_container_width=True,
        hide_index=True,
    )

    if errors:
        st.warning(f"{len(errors)} series were unavailable or returned an API error; the remaining series were retained.")
        with st.expander("Show series errors"):
            st.write("\n".join(errors))

    st.download_button(
        "DOWNLOAD 30-DAY CSV",
        data.to_csv(index=False).encode("utf-8"),
        "iv_monitor_30_day.csv",
        "text/csv",
        use_container_width=True,
    )


DATA_KEY = "iv_monitor_30d_data"
ERROR_KEY = "iv_monitor_30d_errors"
TIME_KEY = "iv_monitor_30d_fetched_at"
PARAM_KEY = "iv_monitor_30d_params"

current_params = (expiry_flag, int(expiry_code), int(days), client_id)

# The fetch button loads and persists the full table in session state.
fetch_clicked = st.button("FETCH / REFRESH LAST 30 DAYS", type="primary", use_container_width=True)
if fetch_clicked or st.session_state.get(PARAM_KEY) != current_params:
    data, errors = fetch_monitor_data()
    st.session_state[DATA_KEY] = data
    st.session_state[ERROR_KEY] = errors
    st.session_state[TIME_KEY] = datetime.now()
    st.session_state[PARAM_KEY] = current_params


@st.fragment(run_every="60s")
def monitoring_view():
    data = st.session_state.get(DATA_KEY)
    errors = st.session_state.get(ERROR_KEY, [])
    fetched_at = st.session_state.get(TIME_KEY)

    if data is None or data.empty:
        st.info("Click FETCH / REFRESH LAST 30 DAYS to load the historical monitor.")
        return

    render_results(data, errors, fetched_at or datetime.now())

    if auto_refresh:
        # Refresh only this panel; the fetched table remains in session state.
        fresh_data, fresh_errors = fetch_monitor_data()
        if not fresh_data.empty:
            st.session_state[DATA_KEY] = fresh_data
            st.session_state[ERROR_KEY] = fresh_errors
            st.session_state[TIME_KEY] = datetime.now()
        st.caption("Auto-refresh: ON • data refreshes every 1 minute")
    else:
        st.caption("Auto-refresh: OFF")


monitoring_view()
