from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")

INDEXES = {
    "NIFTY": {"security_id": 13, "segment": "NSE_FNO"},
    "SENSEX": {"security_id": 51, "segment": "BSE_FNO"},
}
LEVELS = [-2, -1, 0, 1, 2]
AUTO_REFRESH_SECONDS = 60

st.set_page_config(page_title="30-Day Monitoring", page_icon="📈", layout="wide")

st.title("📈 30-Day Monitoring")
st.caption("Historical 10:00 ATM / near-ATM IV and OI monitoring for NIFTY and SENSEX")

with st.sidebar:
    st.subheader("Market Access")
    client_id = st.text_input("Client ID", key="history_client_id")
    access_token = st.text_input("Access Token", type="password", key="history_access_token")
    expiry_flag = st.selectbox("Expiry series", ["WEEK", "MONTH"], index=0, key="history_expiry_flag")
    expiry_code = st.selectbox(
        "Expiry code", [0, 1, 2], index=0,
        help="0 = near, 1 = next, 2 = farther expiry",
        key="history_expiry_code",
    )
    days = st.slider("Lookback", 7, 30, 30, key="history_days")
    auto_refresh = st.checkbox("Auto-refresh every 1 minute", value=True, key="history_auto_refresh")

if not client_id or not access_token:
    st.info("Enter Client ID and Access Token to use the historical monitor.")
    st.stop()


def post(path: str, payload: dict) -> dict:
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
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API returned invalid JSON.") from exc


def windows(start, end):
    current = start
    while current <= end:
        nxt = min(current + timedelta(days=29), end)
        yield current, nxt
        current = nxt + timedelta(days=1)


def rolling(index_name: str, level: int, side: str, start, end):
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
        timestamps = leg.get("timestamp") or []
        values = {name: (leg.get(name) or []) for name in ("strike", "spot", "iv", "oi", "close")}

        for i, stamp in enumerate(timestamps):
            try:
                ts = pd.to_datetime(int(stamp), unit="s", utc=True).tz_convert(IST).tz_localize(None)
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


def safe_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=["date", "index", "level", "side", "strike", "spot", "iv", "oi", "close"]
        )
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in ("strike", "spot", "iv", "oi", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return (
        frame.drop_duplicates(["date", "index", "level", "side"], keep="last")
        .sort_values(["date", "index", "level", "side"])
        .reset_index(drop=True)
    )


def fetch_monitor_data():
    now = datetime.now(IST)
    today = now.date()
    start = today - timedelta(days=days - 1)
    all_rows = []
    errors = []

    total = len(INDEXES) * len(LEVELS) * 2
    done = 0
    progress = st.progress(0.0)
    status = st.empty()

    for index_name in INDEXES:
        for level in LEVELS:
            for side in ("CE", "PE"):
                status.write(f"Fetching {index_name} • {('ATM' if level == 0 else f'ATM{level:+d}')} • {side}")
                try:
                    all_rows.extend(rolling(index_name, level, side, start, today))
                except Exception as exc:
                    errors.append(f"{index_name} {level:+d} {side}: {exc}")
                done += 1
                progress.progress(done / total)

    progress.empty()
    status.empty()
    return safe_df(all_rows), errors


def render_results(data: pd.DataFrame, errors: list[str], fetched_at: datetime):
    st.success(
        f"Loaded {len(data):,} 10:00 observations • "
        f"Last update: {fetched_at.strftime('%H:%M:%S IST')}"
    )

    st.subheader("10:00 ATM IV Trend")
    atm = data[data["level"] == "ATM"].copy()
    if not atm.empty:
        pivot = atm.pivot_table(
            index="date",
            columns=["index", "side"],
            values="iv",
            aggfunc="last",
        )
        st.line_chart(pivot)

    st.subheader("ATM ±2 OI Monitoring")
    oi = data[data["level"].isin(["ATM-2", "ATM-1", "ATM", "ATM+1", "ATM+2"])].copy()
    oi["OI"] = pd.to_numeric(oi["oi"], errors="coerce")
    oi["OI Change vs Previous Observation"] = (
        oi.sort_values("date")
        .groupby(["index", "level", "side"])["OI"]
        .diff()
    )
    st.dataframe(
        oi[["date", "index", "level", "strike", "side", "OI", "OI Change vs Previous Observation"]],
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Daily IV Comparison")
    iv = data.copy().sort_values("date")
    iv["IV"] = pd.to_numeric(iv["iv"], errors="coerce")
    iv["IV Change vs Prior Day"] = iv.groupby(["index", "level", "side"])["IV"].diff()
    st.dataframe(
        iv[["date", "index", "level", "strike", "side", "spot", "IV", "IV Change vs Prior Day", "oi"]],
        use_container_width=True,
        hide_index=True,
    )

    if errors:
        st.warning(f"{len(errors)} series could not be loaded; available series are still displayed.")
        with st.expander("Show unavailable series"):
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
ACTIVE_KEY = "iv_monitor_30d_active"

params = (expiry_flag, int(expiry_code), int(days), client_id)

fetch_clicked = st.button("FETCH / REFRESH LAST 30 DAYS", type="primary", use_container_width=True)
if fetch_clicked:
    with st.spinner("Loading the last 30 days..."):
        data, errors = fetch_monitor_data()
    if data.empty:
        st.session_state[ACTIVE_KEY] = False
        st.session_state[DATA_KEY] = data
        st.session_state[ERROR_KEY] = errors
        st.session_state[TIME_KEY] = datetime.now(IST)
        st.session_state[PARAM_KEY] = params
        st.error("No 10:00 historical rows were returned. Check credentials and the selected expiry series/code.")
    else:
        st.session_state[ACTIVE_KEY] = True
        st.session_state[DATA_KEY] = data
        st.session_state[ERROR_KEY] = errors
        st.session_state[TIME_KEY] = datetime.now(IST)
        st.session_state[PARAM_KEY] = params

stored_params = st.session_state.get(PARAM_KEY)
active = bool(st.session_state.get(ACTIVE_KEY, False))

if stored_params is not None and stored_params != params:
    active = False
    st.session_state[ACTIVE_KEY] = False
    st.session_state.pop(DATA_KEY, None)
    st.session_state.pop(ERROR_KEY, None)
    st.session_state.pop(TIME_KEY, None)


@st.fragment(run_every="60s")
def monitoring_panel():
    data = st.session_state.get(DATA_KEY)
    errors = st.session_state.get(ERROR_KEY, [])
    fetched_at = st.session_state.get(TIME_KEY)
    active_now = bool(st.session_state.get(ACTIVE_KEY, False))

    if not active_now or data is None or data.empty:
        st.info("Click FETCH / REFRESH LAST 30 DAYS to load the historical monitor.")
        return

    # First render the persisted table immediately; then refresh the dataset for the next minute.
    render_results(data, errors, fetched_at or datetime.now(IST))

    if auto_refresh:
        try:
            fresh_data, fresh_errors = fetch_monitor_data()
            if not fresh_data.empty:
                st.session_state[DATA_KEY] = fresh_data
                st.session_state[ERROR_KEY] = fresh_errors
                st.session_state[TIME_KEY] = datetime.now(IST)
        except Exception as exc:
            st.warning(f"Automatic refresh failed; showing the last good dataset. {exc}")
        st.caption("Auto-refresh: ON • refreshes every 1 minute")
    else:
        st.caption("Auto-refresh: OFF")


monitoring_panel()
