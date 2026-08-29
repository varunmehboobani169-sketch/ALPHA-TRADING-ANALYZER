import io
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple

import pandas as pd
import requests
import streamlit as st

DHAN_API = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
SAFE_RPS = 4.0
MIN_INTERVAL = 1.0 / SAFE_RPS
MAX_RETRIES = 7
DAILY_REQUEST_BUDGET = 95_000
MAX_HISTORY_YEARS = 5

TARGETS = {
    "BANKNIFTY": {"aliases": {"BANKNIFTY", "NIFTY BANK"}, "exchange": "NSE_FNO", "expiry_type": "MONTH"},
    "FINNIFTY": {"aliases": {"FINNIFTY", "NIFTY FIN SERVICE", "NIFTY FINANCIAL SERVICES"}, "exchange": "NSE_FNO", "expiry_type": "MONTH"},
    "SENSEX": {"aliases": {"SENSEX", "BSE SENSEX"}, "exchange": "BSE_FNO", "expiry_type": "WEEK"},
}
IDX_OFFSETS = [f"ATM{n:+d}" if n else "ATM" for n in range(-10, 11)]

st.set_page_config(page_title="Dhan Index Options Downloader", page_icon="📥", layout="wide")


@dataclass
class RateLimiter:
    min_interval: float = MIN_INTERVAL
    last_request: float = 0.0
    total_requests: int = 0

    def wait(self) -> None:
        now = time.monotonic()
        delay = self.min_interval - (now - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        self.total_requests += 1


RATE = RateLimiter()


def col(df: pd.DataFrame, *names: str):
    mapping = {str(c).strip().upper(): c for c in df.columns}
    for name in names:
        if name.upper() in mapping:
            return mapping[name.upper()]
    return None


def load_master() -> pd.DataFrame:
    df = pd.read_csv(MASTER_URL, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def build_index_universe(df: pd.DataFrame) -> pd.DataFrame:
    exch = col(df, "EXCH_ID")
    seg = col(df, "SEGMENT")
    inst = col(df, "INSTRUMENT")
    sid = col(df, "SECURITY_ID")
    usid = col(df, "UNDERLYING_SECURITY_ID")
    usym = col(df, "UNDERLYING_SYMBOL")
    symbol = col(df, "SYMBOL_NAME")
    expiry = col(df, "EXPIRY_DATE")
    required = [exch, seg, inst, sid, usid, usym]
    if any(x is None for x in required):
        raise RuntimeError(f"Unexpected Dhan instrument-master columns: {list(df.columns)}")
    out = pd.DataFrame({
        "exchange": df[exch].astype(str).str.upper().str.strip(),
        "segment": df[seg].astype(str).str.upper().str.strip(),
        "instrument": df[inst].astype(str).str.upper().str.strip(),
        "security_id": pd.to_numeric(df[sid], errors="coerce"),
        "underlying_security_id": pd.to_numeric(df[usid], errors="coerce"),
        "underlying_symbol": df[usym].astype(str).str.strip(),
        "symbol": df[symbol].astype(str).str.strip() if symbol else "",
        "expiry_date": pd.to_datetime(df[expiry], errors="coerce") if expiry else pd.NaT,
    })
    out["exchange_segment"] = ""
    out.loc[(out.exchange == "NSE") & (out.segment == "D"), "exchange_segment"] = "NSE_FNO"
    out.loc[(out.exchange == "BSE") & (out.segment == "D"), "exchange_segment"] = "BSE_FNO"
    out = out[(out.instrument == "OPTIDX") & out.exchange_segment.isin(["NSE_FNO", "BSE_FNO"])]
    out = out.dropna(subset=["underlying_security_id"])
    return out


def find_targets(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    symbols = df["underlying_symbol"].astype(str).str.upper().str.strip()
    for target, cfg in TARGETS.items():
        matches = df[symbols.isin(cfg["aliases"]) & df["exchange_segment"].eq(cfg["exchange"])]
        if matches.empty:
            matches = df[symbols.isin(cfg["aliases"])]
        if matches.empty:
            rows.append({
                "index": target,
                "exchange_segment": cfg["exchange"],
                "underlying_security_id": "",
                "master_symbol": "NOT FOUND",
                "expiry_type": cfg["expiry_type"],
            })
            continue
        pick = matches.sort_values(["expiry_date", "security_id"], na_position="last").iloc[0]
        rows.append({
            "index": target,
            "exchange_segment": cfg["exchange"],
            "underlying_security_id": str(int(pick["underlying_security_id"])),
            "master_symbol": str(pick["underlying_symbol"]),
            "expiry_type": cfg["expiry_type"],
        })
    result = pd.DataFrame(rows)
    for c in result.columns:
        result[c] = result[c].astype(str)
    return result


def parse_years(text: str) -> List[int]:
    result: List[int] = []
    for token in str(text).replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            result.extend(range(int(a), int(b) + 1))
        else:
            result.append(int(token))
    return sorted(set(result))


def windows(year: int) -> List[Tuple[str, str]]:
    cur = pd.Timestamp(year, 1, 1)
    end = pd.Timestamp(year + 1, 1, 1)
    out = []
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=30), end)
        out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt
    return out


def api_post(client: str, token: str, payload: dict):
    delay = 1.0
    for attempt in range(MAX_RETRIES + 1):
        if RATE.total_requests >= DAILY_REQUEST_BUDGET:
            raise RuntimeError(f"Daily safety budget reached at {DAILY_REQUEST_BUDGET:,} API requests.")
        RATE.wait()
        try:
            r = requests.post(
                DHAN_API + "/charts/rollingoption",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "access-token": token,
                    "client-id": client,
                },
                json=payload,
                timeout=90,
            )
        except requests.RequestException as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"Network error after retries: {exc}") from exc
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if r.status_code < 400:
            try:
                body = r.json()
            except ValueError as exc:
                raise RuntimeError("Dhan returned a non-JSON response.") from exc
            return body
        text = r.text[:900]
        limited = r.status_code in (429, 503) or any(code in text for code in ("805", "904"))
        if limited and attempt < MAX_RETRIES:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        raise RuntimeError(f"Dhan HTTP {r.status_code}: {text}")
    raise RuntimeError("Retry loop exhausted.")


def fetch_job(client: str, token: str, row, year: int, offset: str, side: str, expiry_type: str, expiry_code: int):
    frames = []
    for start, end in windows(year):
        payload = {
            "exchangeSegment": str(row.exchange_segment),
            "interval": "1",
            "securityId": str(row.underlying_security_id),
            "instrument": "OPTIDX",
            "expiryFlag": expiry_type,
            "expiryCode": int(expiry_code),
            "strike": offset,
            "drvOptionType": side,
            "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
            "fromDate": start,
            "toDate": end,
        }
        body = api_post(client, token, payload)
        leg = ((body.get("data") or {}).get("ce" if side == "CALL" else "pe") or {})
        timestamps = leg.get("timestamp") or []
        if not timestamps:
            continue
        n = len(timestamps)

        def arr(name):
            values = list(leg.get(name) or [])
            return (values + [None] * n)[:n]

        frame = pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None),
            "open": arr("open"),
            "high": arr("high"),
            "low": arr("low"),
            "close": arr("close"),
            "iv": arr("iv"),
            "volume": arr("volume"),
            "strike": arr("strike"),
            "oi": arr("oi"),
            "spot": arr("spot"),
        })
        frame["index"] = str(row.index)
        frame["underlying_security_id"] = str(row.underlying_security_id)
        frame["exchange_segment"] = str(row.exchange_segment)
        frame["option_type"] = str(side)
        frame["requested_strike"] = str(offset)
        frame["expiry_type"] = str(expiry_type)
        frame["expiry_code"] = int(expiry_code)
        frame["year"] = int(year)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def make_zip(data: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for year, group in data.groupby("year"):
            z.writestr(f"options_{int(year)}.csv", group.sort_values("timestamp").to_csv(index=False))
    return buf.getvalue()


st.title("📥 Dhan Index Options Data Downloader")
st.caption("SENSEX WEEKLY • BANKNIFTY MONTHLY • FINNIFTY MONTHLY • ATM−10 to ATM+10 • CE + PE")

with st.sidebar:
    st.header("Dhan Connection")
    client = st.text_input("Dhan Client ID")
    token = st.text_input("Dhan Access Token", type="password")
    st.divider()
    years_text = st.text_input("Years", "2022-2026")
    expiry_codes = st.multiselect("Expiry codes", [0, 1, 2], default=[0, 1, 2], help="Dhan rolling-options expiry codes.")
    st.caption("API cap: 4 requests/sec • safety budget: 95,000 requests/run")

st.info("SENSEX → WEEK only. BANKNIFTY → MONTH only. FINNIFTY → MONTH only. Every series uses 21 ATM-relative strikes (ATM−10 … ATM+10) and both CALL + PUT.")

if not client or not token:
    st.warning("Enter Dhan Client ID and Access Token in the sidebar.")
    st.stop()

if st.button("LOAD FINAL INDEX UNIVERSE", type="primary"):
    try:
        with st.spinner("Loading Dhan instrument master..."):
            raw = load_master()
            targets = find_targets(build_index_universe(raw))
            st.session_state["targets"] = targets
        found = targets.loc[targets["underlying_security_id"].ne(""), "index"].tolist()
        st.success(f"Resolved {len(found)}/3 indexes: {', '.join(found) if found else 'none'}")
        st.dataframe(targets, use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(f"Unable to load instrument master: {exc}")

targets = st.session_state.get("targets", pd.DataFrame())
if targets.empty:
    st.stop()

valid_indexes = targets.loc[targets["underlying_security_id"].ne(""), "index"].tolist()
selected = st.multiselect("Indexes to download", options=targets["index"].tolist(), default=valid_indexes)
chosen = targets[targets["index"].isin(selected) & targets["underlying_security_id"].ne("")].copy()
if not chosen.empty:
    st.dataframe(chosen, use_container_width=True, hide_index=True)

if st.button("DOWNLOAD FINAL YEAR-WISE DATA", type="primary", use_container_width=True):
    years_list = parse_years(years_text)
    current_year = datetime.now().year
    min_year = current_year - MAX_HISTORY_YEARS
    if not years_list:
        st.error("Enter a valid year or year range.")
        st.stop()
    if min(years_list) < min_year or max(years_list) > current_year:
        st.error(f"Select years within Dhan's current rolling historical window: approximately {min_year}-{current_year}.")
        st.stop()
    if not expiry_codes:
        st.error("Select at least one expiry code.")
        st.stop()
    if chosen.empty:
        st.error("No resolved indexes selected.")
        st.stop()

    jobs = []
    availability = []
    for _, row in chosen.iterrows():
        expiry_type = str(row.expiry_type)
        for year in years_list:
            availability.append({
                "index": str(row.index),
                "year": int(year),
                "expiry_type": expiry_type,
                "status": "PLANNED",
            })
            for code in expiry_codes:
                for offset in IDX_OFFSETS:
                    for side in ("CALL", "PUT"):
                        jobs.append((row, year, offset, side, expiry_type, int(code)))

    # Explicit dtypes prevent pandas/Arrow from inferring mixed object columns.
    plan_df = pd.DataFrame(availability, columns=["index", "year", "expiry_type", "status"])
    plan_df["index"] = plan_df["index"].fillna("").astype("string")
    plan_df["year"] = pd.to_numeric(plan_df["year"], errors="coerce").astype("Int64")
    plan_df["expiry_type"] = plan_df["expiry_type"].fillna("").astype("string")
    plan_df["status"] = plan_df["status"].fillna("").astype("string")

    estimated = sum(len(windows(year)) for _, year, *_ in jobs)
    st.subheader("Download plan")
    st.dataframe(plan_df, use_container_width=True, hide_index=True)
    st.caption(f"Logical jobs: {len(jobs):,} • estimated API requests after 30-day splitting: {estimated:,}")

    if estimated > DAILY_REQUEST_BUDGET:
        st.error(f"Estimated {estimated:,} requests exceeds the 95,000-request safety ceiling. Reduce years or expiry codes.")
        st.stop()

    progress = st.progress(0.0)
    status_box = st.empty()
    frames, errors = [], []
    total_jobs = len(jobs)

    for i, (row, year, offset, side, expiry_type, expiry_code) in enumerate(jobs, 1):
        status_box.write(
            f"{row.index} | {year} | {expiry_type} | code {expiry_code} | {offset} | {side} | API calls: {RATE.total_requests:,}"
        )
        try:
            frame = fetch_job(client, token, row, year, offset, side, expiry_type, expiry_code)
            if not frame.empty:
                frames.append(frame)
        except Exception as exc:
            errors.append({
                "index": str(row.index),
                "year": int(year),
                "expiry_type": str(expiry_type),
                "expiry_code": int(expiry_code),
                "strike": str(offset),
                "side": str(side),
                "error": str(exc),
            })
            if "Daily safety budget reached" in str(exc):
                break
        progress.progress(i / total_jobs)

    if not frames:
        st.error("No data returned. Verify the Dhan token, subscription, selected years and historical availability.")
        if errors:
            error_df = pd.DataFrame(errors)
            for c in error_df.columns:
                error_df[c] = error_df[c].astype(str)
            st.dataframe(error_df, use_container_width=True, hide_index=True)
        st.stop()

    data = pd.concat(frames, ignore_index=True).drop_duplicates()
    data = data.sort_values(["year", "index", "timestamp", "option_type", "requested_strike"])
    st.success(f"Downloaded {len(data):,} rows. API requests sent: {RATE.total_requests:,}.")
    st.download_button(
        "DOWNLOAD YEAR-WISE ZIP",
        make_zip(data),
        "sensex_weekly_banknifty_finnifty_monthly.zip",
        "application/zip",
        use_container_width=True,
    )
    if errors:
        st.warning(f"{len(errors):,} logical jobs produced API errors. Empty/no-data windows are not counted as errors.")
        error_df = pd.DataFrame(errors)
        for c in error_df.columns:
            error_df[c] = error_df[c].astype(str)
        st.dataframe(error_df, use_container_width=True, hide_index=True)
