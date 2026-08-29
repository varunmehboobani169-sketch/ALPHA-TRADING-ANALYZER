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
REFRESH_SECONDS = 60

st.set_page_config(page_title="30-Day Monitoring", page_icon="📈", layout="wide")
st.title("📈 30-Day Monitoring")
st.caption("Historical 10:00 ATM / near-ATM IV and OI monitoring for NIFTY and SENSEX")

with st.sidebar:
    st.subheader("Market Access")
    client_id = st.text_input("Client ID", key="history_client_id")
    access_token = st.text_input("Access Token", type="password", key="history_access_token")
    expiry_flag = st.selectbox("Expiry series", ["WEEK", "MONTH"], index=0, key="history_expiry_flag")
    expiry_code = st.selectbox(
        "Expiry code", [0, 1, 2], index=1,
        help="0 = current/near, 1 = next, 2 = far",
        key="history_expiry_code",
    )
    days = st.slider("Lookback", 7, 30, 30, key="history_days")
    auto_refresh = st.checkbox("Auto-refresh every 1 minute", value=True, key="history_auto_refresh")

if not client_id or not access_token:
    st.info("Enter Client ID and Access Token to use the historical monitor.")
    st.stop()


def post(path: str, payload: dict) -> dict:
    try:
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
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API returned invalid JSON.") from exc


def date_windows(start, end):
    current = start
    while current <= end:
        nxt = min(current + timedelta(days=29), end)
        yield current, nxt
        current = nxt + timedelta(days=1)


def extract_raw_rows(body: dict, index_name: str, level: int, side: str) -> tuple[list[dict], int]:
    data = body.get("data") or {}
    leg = data.get("ce" if side == "CE" else "pe") or {}
    timestamps = leg.get("timestamp") or []
    fields = {name: (leg.get(name) or []) for name in ("strike", "spot", "iv", "oi", "close")}
    raw_count = len(timestamps)
    rows = []

    for i, stamp in enumerate(timestamps):
        try:
            ts = datetime.fromtimestamp(int(stamp), IST)
        except (TypeError, ValueError, OSError):
            continue

        if ts.minute not in (58, 59, 0, 1, 2) and ts.hour == 10:
            continue

        if ts.hour != 10 and not (ts.hour == 9 and ts.minute >= 58):
            continue

        rows.append(
            {
                "timestamp": ts,
                "date": ts.date(),
                "index": index_name,
                "level": "ATM" if level == 0 else f"ATM{level:+d}",
                "side": side,
                "strike": fields["strike"][i] if i < len(fields["strike"]) else None,
                "spot": fields["spot"][i] if i < len(fields["spot"]) else None,
                "iv": fields["iv"][i] if i < len(fields["iv"]) else None,
                "oi": fields["oi"][i] if i < len(fields["oi"]) else None,
                "close": fields["close"][i] if i < len(fields["close"]) else None,
            }
        )
    return rows, raw_count


def rolling(index_name: str, level: int, side: str, start, end):
    spec = INDEXES[index_name]
    api_side = "CALL" if side == "CE" else "PUT"
    rows = []
    raw_bars = 0

    for win_start, win_end in date_windows(start, end):
        # toDate is non-inclusive, so add one day to include win_end.
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
                "drvOptionType": api_side,
                "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
                "fromDate": win_start.isoformat(),
                "toDate": (win_end + timedelta(days=1)).isoformat(),
            },
        )
        chunk, count = extract_raw_rows(body, index_name, level, side)
        raw_bars += count
        rows.extend(chunk)

    # From the raw minute series, select the closest available bar to 10:00 for each trading day.
    if not rows:
        return [], raw_bars

    frame = pd.DataFrame(rows)
    frame["delta_seconds"] = frame["timestamp"].map(
        lambda x: abs((x - datetime.combine(x.date(), datetime.min.time(), IST).replace(hour=10)).total_seconds())
    )
    frame = frame[frame["delta_seconds"] <= 120].copy()
    if frame.empty:
        return [], raw_bars
    frame = frame.sort_values(["date", "delta_seconds"]).drop_duplicates("date", keep="first")
    return frame.drop(columns=["delta_seconds"]).to_dict("records"), raw_bars


def safe_df(rows):
    columns = ["timestamp", "date", "index", "level", "side", "strike", "spot", "iv", "oi", "close"]
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    for col in ("strike", "spot", "iv", "oi", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return (
        frame.dropna(subset=["date"])
        .drop_duplicates(["date", "index", "level", "side"], keep="last")
        .sort_values(["date", "index", "level", "side"])
        .reset_index(drop=True)
    )


def fetch_monitor_data():
    today = datetime.now(IST).date()
    start = today - timedelta(days=days - 1)
    rows = []
    errors = []
    diagnostics = []
    total = len(INDEXES) * len(LEVELS) * 2
    done = 0
    progress = st.progress(0.0)
    status = st.empty()

    for index_name in INDEXES:
        for level in LEVELS:
            for side in ("CE", "PE"):
                label = "ATM" if level == 0 else f"ATM{level:+d}"
                status.write(f"Fetching {index_name} • {label} • {side}")
                try:
                    chunk, raw_bars = rolling(index_name, level, side, start, today)
                    rows.extend(chunk)
                    diagnostics.append(
                        {
                            "Index": index_name,
                            "Level": label,
                            "Side": side,
                            "Raw minute bars": raw_bars,
                            "10:00 observations": len(chunk),
                        }
                    )
                except Exception as exc:
                    errors.append(f"{index_name} {label} {side}: {exc}")
                done += 1
                progress.progress(done / total)

    progress.empty()
    status.empty()
    return safe_df(rows), errors, pd.DataFrame(diagnostics)


def render_results(data, errors, diagnostics, fetched_at):
    st.success(f"Loaded {len(data):,} 10:00 observations • Last update: {fetched_at.strftime('%H:%M:%S IST')}")

    st.subheader("10:00 ATM IV Trend")
    atm = data[data["level"] == "ATM"]
    if not atm.empty:
        pivot = atm.pivot_table(index="date", columns=["index", "side"], values="iv", aggfunc="last")
        st.line_chart(pivot)

    st.subheader("ATM ±2 OI Monitoring")
    oi = data.copy()
    oi["OI"] = pd.to_numeric(oi["oi"], errors="coerce")
    oi["OI Change vs Prior Day"] = oi.sort_values("date").groupby(["index", "level", "side"])["OI"].diff()
    st.dataframe(
        oi[["date", "index", "level", "strike", "side", "OI", "OI Change vs Prior Day"]],
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

    with st.expander("Data diagnostics"):
        st.caption("Raw minute bars returned by the API and 10:00 observations extracted from them.")
        st.dataframe(diagnostics, use_container_width=True, hide_index=True)

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
DIAG_KEY = "iv_monitor_30d_diagnostics"
TIME_KEY = "iv_monitor_30d_fetched_at"
PARAM_KEY = "iv_monitor_30d_params"
ACTIVE_KEY = "iv_monitor_30d_active"

params = (expiry_flag, int(expiry_code), int(days), client_id)
fetch_clicked = st.button("FETCH / REFRESH LAST 30 DAYS", type="primary", use_container_width=True)

if st.session_state.get(PARAM_KEY) != params:
    st.session_state.pop(DATA_KEY, None)
    st.session_state.pop(ERROR_KEY, None)
    st.session_state.pop(DIAG_KEY, None)
    st.session_state.pop(TIME_KEY, None)
    st.session_state[ACTIVE_KEY] = False

if fetch_clicked:
    with st.spinner("Loading the last 30 days..."):
        data, errors, diagnostics = fetch_monitor_data()
    st.session_state[DATA_KEY] = data
    st.session_state[ERROR_KEY] = errors
    st.session_state[DIAG_KEY] = diagnostics
    st.session_state[TIME_KEY] = datetime.now(IST)
    st.session_state[PARAM_KEY] = params
    st.session_state[ACTIVE_KEY] = not data.empty


@st.fragment(run_every="60s")
def monitoring_panel():
    data = st.session_state.get(DATA_KEY)
    errors = st.session_state.get(ERROR_KEY, [])
    diagnostics = st.session_state.get(DIAG_KEY, pd.DataFrame())
    fetched_at = st.session_state.get(TIME_KEY)
    active = bool(st.session_state.get(ACTIVE_KEY, False))

    if not active or data is None or data.empty:
        st.info("Click FETCH / REFRESH LAST 30 DAYS to load the historical monitor.")
        return

    render_results(data, errors, diagnostics, fetched_at or datetime.now(IST))

    if auto_refresh:
        try:
            fresh_data, fresh_errors, fresh_diag = fetch_monitor_data()
            if not fresh_data.empty:
                st.session_state[DATA_KEY] = fresh_data
                st.session_state[ERROR_KEY] = fresh_errors
                st.session_state[DIAG_KEY] = fresh_diag
                st.session_state[TIME_KEY] = datetime.now(IST)
                render_results(fresh_data, fresh_errors, fresh_diag, st.session_state[TIME_KEY])
        except Exception as exc:
            st.warning(f"Automatic refresh failed; showing the last good dataset. {exc}")
        st.caption("Auto-refresh: ON • every 1 minute")
    else:
        st.caption("Auto-refresh: OFF")


monitoring_panel()
