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
        for i, stamp in enumerate(stamps):
            try:
                ts = pd.to_datetime(int(stamp), unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None)
            except Exception:
                continue
            if ts.time().hour != 10 or ts.time().minute != 0:
                continue
            def value(name):
                vals = leg.get(name) or []
                return vals[i] if i < len(vals) else None
            rows.append({
                "date": ts.date(),
                "index": index_name,
                "level": "ATM" if level == 0 else f"ATM{level:+d}",
                "side": side,
                "strike": value("strike"),
                "spot": value("spot"),
                "iv": value("iv"),
                "oi": value("oi"),
                "close": value("close"),
            })
    return rows


def safe_df(rows):
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for col in ("strike", "spot", "iv", "oi", "close"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.drop_duplicates(["date", "index", "level", "side"], keep="last").sort_values(["date", "index", "level", "side"])


if st.button("FETCH LAST 30 DAYS", type="primary", use_container_width=True):
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    all_rows = []
    progress = st.progress(0.0)
    total = len(INDEXES) * len(LEVELS) * 2
    done = 0
    errors = []

    for index_name in INDEXES:
        for level in LEVELS:
            for side in ("CE", "PE"):
                try:
                    all_rows.extend(rolling(index_name, level, side, start, today))
                except Exception as exc:
                    errors.append(f"{index_name} {level:+d} {side}: {exc}")
                done += 1
                progress.progress(done / total)

    data = safe_df(all_rows)
    if data.empty:
        st.error("No historical rows were returned. Check credentials, subscription, expiry series/code, and available history.")
        if errors:
            st.write("\n".join(errors))
        st.stop()

    st.success(f"Loaded {len(data):,} 10:00 observations.")

    st.subheader("ATM IV trend")
    atm = data[data["level"] == "ATM"].copy()
    if not atm.empty:
        pivot = atm.pivot_table(index="date", columns=["index", "side"], values="iv", aggfunc="last")
        st.line_chart(pivot)

    st.subheader("ATM ±2 OI")
    oi = data[data["level"].isin(["ATM-2", "ATM-1", "ATM", "ATM+1", "ATM+2"])].copy()
    st.dataframe(oi, use_container_width=True, hide_index=True)

    st.subheader("Daily IV comparison")
    iv = data.copy()
    iv["IV"] = pd.to_numeric(iv["iv"], errors="coerce")
    iv["IV Change vs Prior Observation"] = iv.sort_values("date").groupby(["index", "level", "side"])["IV"].diff()
    st.dataframe(iv[["date", "index", "level", "side", "strike", "spot", "IV", "IV Change vs Prior Observation", "oi"]], use_container_width=True, hide_index=True)

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
