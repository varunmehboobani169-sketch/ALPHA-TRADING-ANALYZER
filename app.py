import io
import time
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Tuple

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

# Final download scope requested by the user.
TARGETS = {
    "BANKNIFTY": {"aliases": {"BANKNIFTY", "NIFTY BANK"}, "exchange": "NSE_FNO", "expiry_flag": "MONTH"},
    "FINNIFTY": {"aliases": {"FINNIFTY", "NIFTY FIN SERVICE", "NIFTY FINANCIAL SERVICES"}, "exchange": "NSE_FNO", "expiry_flag": "MONTH"},
    "SENSEX": {"aliases": {"SENSEX", "BSE SENSEX"}, "exchange": "BSE_FNO", "expiry_flag": "WEEK"},
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
        sleep_for = self.min_interval - (now - self.last_request)
        if sleep_for > 0:
            time.sleep(sleep_for)
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
            # Fallback: identify by symbol aliases even if Dhan labels the segment slightly differently.
            matches = df[symbols.isin(cfg["aliases"])]
        if matches.empty:
            rows.append({
                "index": target,
                "exchange_segment": cfg["exchange"],
                "underlying_security_id": None,
                "master_symbol": "NOT FOUND",
                "expiry_type": cfg["expiry_flag"],
            })
            continue
        # One stable underlying ID per target/exchange; contract rows can repeat it across expiries.
        pick = matches.sort_values(["expiry_date", "security_id"], na_position="last").iloc[0]
        rows.append({
            "index": target,
            "exchange_segment": cfg["exchange"],
            "underlying_security_id": int(pick["underlying_security_id"]),
            "master_symbol": pick["underlying_symbol"],
            "expiry_type": cfg["expiry_flag"],
        })
    return pd.DataFrame(rows)


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
            status = str(body.get("status", "")).lower()
            if status and status not in {"success", "ok"}:
                # Empty/no-data responses are handled separately; explicit API errors remain visible.
                msg = str(body).lower()
                if any(x in msg for x in ("no data", "not available", "no records")):
                    return body
                raise RuntimeError(str(body)[:900])
            return body

        text = r.text[:900]
        rate_limited = r.status_code in (429, 503) or any(x in text for x in ("805", "904"))
        if rate_limited and attempt < MAX_RETRIES:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        raise RuntimeError(f"Dhan HTTP {r.status_code}: {text}")
    raise RuntimeError("Retry loop exhausted.")


def fetch_job(client: str, token: str, target_row, year: int, offset: str, side: str, expiry_flag: str, expiry_code: int):
    frames = []
    for start, end in windows(year):
        payload = {
            "exchangeSegment": target_row.exchange_segment,
            "interval": "1",
            "securityId": str(int(target_row.underlying_security_id)),
            "instrument": "OPTIDX",
            "expiryFlag": expiry_flag,
            "expiryCode": int(expiry_code),
            "strike": offset,
            "drvOptionType": side,
            "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
            "fromDate": start,
            "toDate": end,
        }
        body = api_post(client, token, payload)
        data = body.get("data") or {}
        leg = data.get("ce" if side == "CALL" else "pe") or {}
        timestamps = leg.get("timestamp") or []
        if not timestamps:
            # Normal condition for a non-existent expiry/strike/date window.
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
        frame["index"] = target_row.index
        frame["underlying_security_id"] = int(target_row.underlying_security_id)
        frame["exchange_segment"] = target_row.exchange_segment
        frame["option_type"] = side
        frame["requested_strike"] = offset
        frame["expiry_type"] = expiry_flag
        frame["expiry_code"] = expiry_code
        frame["year"] = year
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def make_zip(data: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for year, group in data.groupby("year"):
            z.writestr(f"options_{int(year)}.csv", group.sort_values("timestamp").to_csv(index=False))
    return buf.getvalue()


st.title("📥 Dhan Index Options Data Downloader")
st.caption("Final scope: SENSEX WEEKLY • BANKNIFTY MONTHLY • FINNIFTY MONTHLY • ATM−10 to ATM+10 • CE + PE")

with st.sidebar:
    st.header("Dhan Connection")
    client = st.text_input("Dhan Client ID")
    token = st.text_input("Dhan Access Token", type="password")
    st.divider()
    years_text = st.text_input("Years", "2022-2026")
    expiry_codes = st.multiselect("Expiry codes", [0, 1, 2], default=[0, 1, 2], help="Dhan rolling-options expiry codes.")
    st.caption("API pacing: 4 requests/sec max. Safety ceiling: 95,000 requests/run.")

st.info("SENSEX → WEEK only. BANKNIFTY → MONTH only. FINNIFTY → MONTH only. Each uses 21 ATM-relative strikes (−10 … +10) and both CALL + PUT.")

if not client or not token:
    st.warning("Enter your Dhan Client ID and Access Token in the sidebar.")
    st.stop()

if st.button("LOAD FINAL INDEX UNIVERSE", type="primary"):
    try:
        with st.spinner("Loading Dhan instrument master..."):
            raw = load_master()
            targets = find_targets(build_index_universe(raw))
            st.session_state["targets"] = targets
        found = targets[targets["underlying_security_id"].notna()]["index"].tolist()
        st.success(f"Resolved {len(found)}/3 indexes: {', '.join(found) if found else 'none'}")
        st.dataframe(targets, use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(str(exc))

targets = st.session_state.get("targets", pd.DataFrame())
if targets.empty:
    st.stop()

selected = st.multiselect(
    "Indexes to download",
    options=targets["index"].tolist(),
    default=targets[targets["underlying_security_id"].notna()]["index"].tolist(),
)
chosen = targets[targets["index"].isin(selected) & targets["underlying_security_id"].notna()].copy()

if not chosen.empty:
    st.dataframe(chosen, use_container_width=True, hide_index=True)

if st.button("DOWNLOAD FINAL YEAR-WISE DATA", type="primary", use_container_width=True):
    years_list = parse_years(years_text)
    if not years_list:
        st.error("Enter a valid year or year range.")
        st.stop()
    current_year = pd.Timestamp.utcnow().year
    if max(years_list) > current_year or min(years_list) < current_year - MAX_HISTORY_YEARS:
        st.error(f"Select years within Dhan's current rolling historical window: approximately {current_year - MAX_HISTORY_YEARS}–{current_year}.")
        st.stop()
    if not expiry_codes:
        st.error("Select at least one expiry code.")
        st.stop()
    if chosen.empty:
        st.error("No resolved indexes selected.")
        st.stop()

    # Exact final planner: SENSEX=WEEK, BANKNIFTY/FINNIFTY=MONTH.
    jobs = []
    availability = []
    for _, row in chosen.iterrows():
        ef = str(row.expiry_type)
        for year in years_list:
            availability.append({"index": row.index, "year": year, "expiry_type": ef, "status": "PLANNED"})
            for ec in expiry_codes:
                for off in IDX_OFFSETS:
                    for side in ("CALL", "PUT"):
                        jobs.append((row, year, off, side, ef, int(ec)))

    estimated = sum(len(windows(year)) for _, year, *_ in jobs)
    st.subheader("Download plan")
    st.dataframe(pd.DataFrame(availability), use_container_width=True, hide_index=True)
    st.caption(f"Logical jobs: {len(jobs):,} | estimated API requests after 30-day splitting: {estimated:,}")

    if estimated > DAILY_REQUEST_BUDGET:
        st.error(f"Estimated {estimated:,} requests exceeds the 95,000-request safety ceiling. Reduce years or expiry codes.")
        st.stop()

    progress = st.progress(0.0)
    status = st.empty()
    frames, errors = [], []

    for i, (row, year, offset, side, ef, ec) in enumerate(jobs, 1):
        status.write(f"{row.index} | {year} | {ef} | code {ec} | {offset} | {side} | API calls: {RATE.total_requests:,}")
        try:
            frame = fetch_job(client, token, row, year, offset, side, ef, ec)
            if not frame.empty:
                frames.append(frame)
        except Exception as exc:
            errors.append({
                "index": row.index,
                "year": year,
                "expiry_type": ef,
                "expiry_code": ec,
                "strike": offset,
                "side": side,
                "error": str(exc),
            })
            if "Daily safety budget reached" in str(exc):
                break
        progress.progress(i / len(jobs))

    if not frames:
        st.error("No data returned. Verify token/subscription and the selected historical range.")
        if errors:
            st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
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
        st.warning(f"{len(errors):,} logical jobs produced API errors. Empty/no-data historical windows are not counted as errors.")
        st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
