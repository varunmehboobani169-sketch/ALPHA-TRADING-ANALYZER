import io
import time
import zipfile
from typing import List, Tuple

import pandas as pd
import requests
import streamlit as st

DHAN_API = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
TARGET_INDEXES = {
    "BANKNIFTY": {"BANKNIFTY", "NIFTY BANK"},
    "FINNIFTY": {"FINNIFTY", "NIFTY FIN SERVICE", "NIFTY FINANCIAL SERVICES"},
    "SENSEX": {"SENSEX", "BSE SENSEX"},
}
SAFE_RPS = 4.0
MIN_INTERVAL = 1.0 / SAFE_RPS
MAX_RETRIES = 7
DAILY_REQUEST_BUDGET = 95000

st.set_page_config(page_title="Dhan Index Options Data Downloader", page_icon="📥", layout="wide")


def col(df, *names):
    m = {str(c).strip().upper(): c for c in df.columns}
    for n in names:
        if n.upper() in m:
            return m[n.upper()]
    return None


def load_master():
    d = pd.read_csv(MASTER_URL, low_memory=False)
    d.columns = [str(c).strip() for c in d.columns]
    return d


def build_universe(d):
    exch, seg, inst = col(d, "EXCH_ID"), col(d, "SEGMENT"), col(d, "INSTRUMENT")
    sid, usid, usym = col(d, "SECURITY_ID"), col(d, "UNDERLYING_SECURITY_ID"), col(d, "UNDERLYING_SYMBOL")
    sym, exp, flag = col(d, "SYMBOL_NAME"), col(d, "EXPIRY_DATE"), col(d, "EXPIRY_FLAG")
    needed = [exch, seg, inst, sid, usid, usym]
    if any(x is None for x in needed):
        raise RuntimeError(f"Unexpected Dhan master columns. Found: {list(d.columns)}")
    x = pd.DataFrame({
        "exchange": d[exch].astype(str).str.upper().str.strip(),
        "segment": d[seg].astype(str).str.upper().str.strip(),
        "instrument": d[inst].astype(str).str.upper().str.strip(),
        "security_id": pd.to_numeric(d[sid], errors="coerce"),
        "underlying_security_id": pd.to_numeric(d[usid], errors="coerce"),
        "underlying_symbol": d[usym].astype(str).str.strip(),
        "symbol": d[sym].astype(str).str.strip() if sym else "",
        "expiry_date": pd.to_datetime(d[exp], errors="coerce") if exp else pd.NaT,
        "expiry_flag": d[flag].astype(str).str.upper().str.strip() if flag else "",
    })
    x["exchange_segment"] = ""
    x.loc[(x.exchange == "NSE") & (x.segment == "D"), "exchange_segment"] = "NSE_FNO"
    x.loc[(x.exchange == "BSE") & (x.segment == "D"), "exchange_segment"] = "BSE_FNO"
    x = x[x.instrument.eq("OPTIDX")]
    x = x[x.exchange_segment.isin(["NSE_FNO", "BSE_FNO"])]
    x = x.dropna(subset=["underlying_security_id"])
    x["family"] = "INDEX"
    return x


def filter_target_indexes(x):
    norm = x["underlying_symbol"].str.upper().str.strip()
    mask = pd.Series(False, index=x.index)
    for names in TARGET_INDEXES.values():
        for name in names:
            mask |= norm.eq(name)
    return x.loc[mask].copy()


def get_underlyings(x):
    return (x.groupby(["exchange", "exchange_segment", "underlying_security_id", "underlying_symbol", "family"], dropna=False)
        .agg(first_expiry=("expiry_date", "min"), last_expiry=("expiry_date", "max"), contracts=("security_id", "count"))
        .reset_index().sort_values(["exchange", "underlying_symbol"]))


def years(text):
    out = []
    for token in str(text).replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(token))
    return sorted(set(out))


def windows(y: int) -> List[Tuple[str, str]]:
    cur, end = pd.Timestamp(y, 1, 1), pd.Timestamp(y + 1, 1, 1)
    out = []
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=30), end)
        out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt
    return out


def unique_year_windows(ys):
    return sum((windows(y) for y in ys), [])


class DhanRateLimiter:
    def __init__(self, min_interval: float = MIN_INTERVAL):
        self.min_interval = min_interval
        self.last_request = 0.0
        self.total_requests = 0

    def wait(self):
        now = time.monotonic()
        sleep_for = self.min_interval - (now - self.last_request)
        if sleep_for > 0:
            time.sleep(sleep_for)
        self.last_request = time.monotonic()
        self.total_requests += 1


RATE_LIMITER = DhanRateLimiter()


def post(client, token, payload):
    delay = 1.0
    for attempt in range(MAX_RETRIES + 1):
        if RATE_LIMITER.total_requests >= DAILY_REQUEST_BUDGET:
            raise RuntimeError(f"Daily downloader safety budget reached at {DAILY_REQUEST_BUDGET:,} API requests.")
        RATE_LIMITER.wait()
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
            delay = min(delay * 2, 30)
            continue
        if r.status_code < 400:
            j = r.json()
            status = str(j.get("status", "")).lower()
            if status and status not in ("success", "ok"):
                raise RuntimeError(str(j)[:700])
            return j
        body_text = r.text[:700]
        rate_limited = r.status_code in (429, 503) or "805" in body_text or "904" in body_text
        if rate_limited and attempt < MAX_RETRIES:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        raise RuntimeError(f"Dhan HTTP {r.status_code}: {body_text}")
    raise RuntimeError("Request retry loop exhausted.")


def fetch_one(client, token, row, year, offset, side, expiry_flag, expiry_code):
    frames = []
    for a, b in windows(year):
        payload = {
            "exchangeSegment": row.exchange_segment,
            "interval": "1",
            "securityId": str(int(row.underlying_security_id)),
            "instrument": "OPTIDX",
            "expiryFlag": expiry_flag,
            "expiryCode": int(expiry_code),
            "strike": offset,
            "drvOptionType": side,
            "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
            "fromDate": a,
            "toDate": b,
        }
        j = post(client, token, payload)
        leg = (j.get("data") or {}).get("ce" if side == "CALL" else "pe") or {}
        ts = leg.get("timestamp") or []
        if not ts:
            continue
        n = len(ts)

        def arr(k):
            z = list(leg.get(k) or [])
            return (z + [None] * n)[:n]

        f = pd.DataFrame({
            "timestamp": pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None),
            "open": arr("open"), "high": arr("high"), "low": arr("low"), "close": arr("close"),
            "iv": arr("iv"), "volume": arr("volume"), "strike": arr("strike"),
            "oi": arr("oi"), "spot": arr("spot"),
        })
        f["underlying_symbol"] = row.underlying_symbol
        f["underlying_security_id"] = int(row.underlying_security_id)
        f["exchange_segment"] = row.exchange_segment
        f["family"] = "INDEX"
        f["option_type"] = side
        f["requested_strike"] = offset
        f["expiry_flag"] = expiry_flag
        f["expiry_code"] = expiry_code
        f["year"] = year
        frames.append(f)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def detect_available_expiry_flags(x, selected, years):
    """Return only WEEK/MONTH flags that actually occur in the master for the selected indexes/years."""
    if x.empty or selected.empty:
        return []
    work = x[x["underlying_security_id"].isin(selected["underlying_security_id"])].copy()
    work = work[work["expiry_date"].dt.year.isin(years)]
    if work.empty:
        return []
    flags = set()
    raw = work["expiry_flag"].astype(str).str.upper().str.strip()
    for v in raw.dropna().unique():
        if v in {"W", "WEEK", "WEEKLY"}:
            flags.add("WEEK")
        elif v in {"M", "MONTH", "MONTHLY"}:
            flags.add("MONTH")
    return [f for f in ("WEEK", "MONTH") if f in flags]


def expiry_flag_exists_for_index(x, row, year, flag):
    work = x[(x.underlying_security_id == row.underlying_security_id) & (x.expiry_date.dt.year == year)].copy()
    raw = work.expiry_flag.astype(str).str.upper().str.strip()
    if flag == "WEEK":
        return raw.isin(["W", "WEEK", "WEEKLY"]).any()
    return raw.isin(["M", "MONTH", "MONTHLY"]).any()


def zip_by_year(df):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for y, g in df.groupby("year"):
            z.writestr(f"options_{int(y)}.csv", g.sort_values("timestamp").to_csv(index=False))
    return b.getvalue()


st.title("📥 Dhan Index Options Data Downloader")
st.caption("BANKNIFTY + FINNIFTY + SENSEX • ATM−10 … ATM+10 • CE + PE • year-wise")

with st.sidebar:
    st.header("Dhan Connection")
    client = st.text_input("Dhan Client ID")
    token = st.text_input("Dhan Access Token", type="password")
    st.divider()
    years_text = st.text_input("Years", "2022-2026")
    expiry_codes = st.multiselect("Expiry codes", [0, 1, 2], default=[0, 1, 2])
    st.caption("Index strikes: 21 levels from ATM−10 through ATM+10.")
    st.caption("Weekly data is skipped automatically when the instrument master shows no weekly contracts for that index/year.")

st.info("Current scope: BANKNIFTY, FINNIFTY and SENSEX only. Every available weekly/monthly series is downloaded across ATM−10 to ATM+10 for CALL and PUT.")
st.warning("Protection: max 4 requests/sec, exponential backoff on rate-limit errors, and a 95,000-request safety ceiling per run.")

if not client or not token:
    st.warning("Enter Dhan Client ID and Access Token in the sidebar.")
    st.stop()

if st.button("LOAD BANKNIFTY + FINNIFTY + SENSEX", type="primary"):
    try:
        with st.spinner("Loading Dhan instrument master..."):
            raw = load_master()
            u = filter_target_indexes(build_universe(raw))
            st.session_state["contracts"] = u
            st.session_state["underlyings"] = get_underlyings(u)
        found = sorted(st.session_state["underlyings"].underlying_symbol.unique())
        st.success(f"Loaded {len(found):,} requested indexes: {', '.join(found)}")
        st.caption(f"Master rows: {len(raw):,} | matching index-option contracts: {len(u):,} | segments: {sorted(u.exchange_segment.unique())}")
    except Exception as e:
        st.error(str(e))

u = st.session_state.get("underlyings", pd.DataFrame())
if u.empty:
    st.stop()

st.subheader("Requested index universe")
st.dataframe(u, use_container_width=True, height=240)

available = sorted(u.underlying_symbol.unique())
default_selected = available
selected_symbols = st.multiselect("Indexes", available, default=default_selected)
selected = u[u.underlying_symbol.isin(selected_symbols)].copy()
st.metric("Selected", len(selected))

if st.button("DOWNLOAD YEAR-WISE DATA", type="primary", use_container_width=True):
    ys = years(years_text)
    if not ys or not expiry_codes or selected.empty:
        st.error("Select valid years, expiry codes and at least one index.")
        st.stop()

    idx_offsets = [f"ATM{n:+d}" if n else "ATM" for n in range(-10, 11)]
    jobs = []
    availability_rows = []

    for _, r in selected.iterrows():
        for y in ys:
            for ef in ("WEEK", "MONTH"):
                available_flag = expiry_flag_exists_for_index(st.session_state["contracts"], r, y, ef)
                availability_rows.append({
                    "index": r.underlying_symbol,
                    "year": y,
                    "expiry_type": ef,
                    "status": "AVAILABLE" if available_flag else "SKIPPED — NO CONTRACTS",
                })
                if not available_flag:
                    continue
                for ec in expiry_codes:
                    for off in idx_offsets:
                        for side in ["CALL", "PUT"]:
                            jobs.append((r, y, off, side, ef, ec))

    estimated_requests = sum(len(windows(y)) for _, y, _, _, _, _ in jobs)
    st.subheader("Expiry availability")
    st.dataframe(pd.DataFrame(availability_rows), use_container_width=True, hide_index=True)
    st.caption(f"Planned logical jobs: {len(jobs):,} | estimated API requests after 30-day splitting: {estimated_requests:,}")

    if estimated_requests > DAILY_REQUEST_BUDGET:
        st.error(
            f"This selection is estimated at {estimated_requests:,} API requests, above the 95,000-request safety ceiling. "
            "Reduce the year range or expiry codes for this run."
        )
        st.stop()

    if not jobs:
        st.warning("No weekly/monthly contract availability was found for the selected indexes and years.")
        st.stop()

    prog = st.progress(0.0)
    status = st.empty()
    frames, errors = [], []
    total_jobs = len(jobs)

    for i, (r, y, off, side, ef, ec) in enumerate(jobs, 1):
        status.write(
            f"{r.underlying_symbol} | {r.exchange_segment} | {y} | {ef} {ec} | "
            f"{off} | {side} | API calls: {RATE_LIMITER.total_requests}"
        )
        try:
            z = fetch_one(client, token, r, y, off, side, ef, ec)
            if not z.empty:
                frames.append(z)
        except Exception as e:
            errors.append({
                "underlying": r.underlying_symbol,
                "year": y,
                "expiry_flag": ef,
                "expiry_code": ec,
                "strike": off,
                "side": side,
                "error": str(e),
            })
            if "Daily downloader safety budget reached" in str(e):
                break
        prog.progress(i / total_jobs)

    if not frames:
        st.error("No data returned. Check Dhan API/data subscription, token validity, dates and instrument availability.")
        if errors:
            st.dataframe(pd.DataFrame(errors), use_container_width=True)
    else:
        data = pd.concat(frames, ignore_index=True).drop_duplicates()
        st.success(
            f"Downloaded {len(data):,} rows across {data.year.nunique()} year(s). "
            f"API requests sent: {RATE_LIMITER.total_requests:,}."
        )
        st.download_button(
            "DOWNLOAD YEAR-WISE ZIP",
            zip_by_year(data),
            "banknifty_finnifty_sensex_yearwise.zip",
            "application/zip",
            use_container_width=True,
        )
        if errors:
            st.warning(f"{len(errors):,} request groups had API errors after controlled retries.")
            st.dataframe(pd.DataFrame(errors), use_container_width=True)
