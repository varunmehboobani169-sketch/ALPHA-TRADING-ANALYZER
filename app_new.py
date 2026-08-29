import io
import json
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

DHAN_API = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

st.set_page_config(page_title="Dhan NSE/BSE Options Data Downloader", layout="wide")


def dhan_post(path: str, client_id: str, access_token: str, payload: dict) -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }
    r = requests.post(f"{DHAN_API}{path}", headers=headers, json=payload, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Dhan HTTP {r.status_code}: {r.text[:1000]}")
    data = r.json()
    if isinstance(data, dict) and str(data.get("status", "")).lower() in {"failure", "failed", "error"}:
        raise RuntimeError(data.get("remarks") or data.get("message") or str(data))
    return data


def load_master() -> pd.DataFrame:
    df = pd.read_csv(SCRIP_MASTER_URL, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def col(df: pd.DataFrame, *names: str) -> Optional[str]:
    norm = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for name in names:
        k = name.lower().replace(" ", "_")
        if k in norm:
            return norm[k]
    return None


def infer_segment(row: pd.Series) -> str:
    exch = str(row.get("EXCH_ID", row.get("SEM_EXM_EXCH_ID", ""))).upper()
    seg = str(row.get("SEGMENT", row.get("SEM_SEGMENT", ""))).upper()
    if exch == "NSE" and seg in {"D", "DERIVATIVE", "NSE_FNO"}:
        return "NSE_FNO"
    if exch == "BSE" and seg in {"D", "DERIVATIVE", "BSE_FNO"}:
        return "BSE_FNO"
    return ""


def build_universe(master: pd.DataFrame) -> pd.DataFrame:
    sec = col(master, "SECURITY_ID", "SEM_SECURITY_ID")
    sym = col(master, "SYMBOL_NAME", "SM_SYMBOL_NAME", "TRADING_SYMBOL", "SEM_TRADING_SYMBOL")
    inst = col(master, "INSTRUMENT", "SEM_INSTRUMENT_NAME")
    under = col(master, "UNDERLYING_SECURITY_ID")
    under_sym = col(master, "UNDERLYING_SYMBOL")
    expiry_flag = col(master, "EXPIRY_FLAG", "SEM_EXPIRY_FLAG")
    expiry_date = col(master, "EXPIRY_DATE", "SM_EXPIRY_DATE")
    lot = col(master, "LOT_SIZE", "SEM_LOT_UNITS")
    strike = col(master, "STRIKE_PRICE", "SEM_STRIKE_PRICE")
    opt = col(master, "OPTION_TYPE", "SEM_OPTION_TYPE")
    exchange = col(master, "EXCH_ID", "SEM_EXM_EXCH_ID")
    segment = col(master, "SEGMENT", "SEM_SEGMENT")

    d = pd.DataFrame()
    d["security_id"] = pd.to_numeric(master[sec], errors="coerce") if sec else pd.NA
    d["symbol"] = master[sym].astype(str) if sym else ""
    d["instrument"] = master[inst].astype(str).str.upper() if inst else ""
    d["underlying_security_id"] = pd.to_numeric(master[under], errors="coerce") if under else pd.NA
    d["underlying_symbol"] = master[under_sym].astype(str) if under_sym else d["symbol"]
    d["expiry_flag"] = master[expiry_flag].astype(str).str.upper() if expiry_flag else ""
    d["expiry_date"] = pd.to_datetime(master[expiry_date], errors="coerce") if expiry_date else pd.NaT
    d["lot_size"] = pd.to_numeric(master[lot], errors="coerce") if lot else pd.NA
    d["strike_price"] = pd.to_numeric(master[strike], errors="coerce") if strike else pd.NA
    d["option_type"] = master[opt].astype(str).str.upper() if opt else ""
    d["exchange"] = master[exchange].astype(str).str.upper() if exchange else ""
    d["segment_raw"] = master[segment].astype(str).str.upper() if segment else ""
    d["exchange_segment"] = d.apply(infer_segment, axis=1)
    d = d[(d.instrument.isin(["OPTIDX", "OPTSTK", "OPTIDX", "OPTSTK"]))].copy()
    d = d[d.exchange_segment.isin(["NSE_FNO", "BSE_FNO"])]
    return d.reset_index(drop=True)


def select_underlyings(universe: pd.DataFrame) -> pd.DataFrame:
    # One row per option underlying, retaining exchange/segment and instrument family.
    u = universe.dropna(subset=["underlying_security_id"]).copy()
    u["family"] = u["instrument"].map({"OPTIDX": "INDEX", "OPTSTK": "STOCK"}).fillna("OTHER")
    return (
        u.groupby(["exchange", "exchange_segment", "underlying_security_id", "underlying_symbol", "family"], dropna=False)
        .agg(first_expiry=("expiry_date", "min"), last_expiry=("expiry_date", "max"), contracts=("security_id", "count"))
        .reset_index()
        .sort_values(["exchange", "family", "underlying_symbol"])
    )


def parse_years(text: str) -> List[int]:
    years = []
    for token in str(text).replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            years.extend(range(int(a), int(b) + 1))
        else:
            years.append(int(token))
    return sorted(set(years))


def chunks_by_30_days(year: int) -> Iterable[Tuple[str, str]]:
    start = pd.Timestamp(year=year, month=1, day=1)
    end = pd.Timestamp(year=year + 1, month=1, day=1)
    cur = start
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=30), end)
        yield cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")
        cur = nxt


def rolling_option_request(
    client_id: str,
    token: str,
    exchange_segment: str,
    security_id: int,
    instrument: str,
    expiry_flag: str,
    expiry_code: int,
    strike: str,
    side: str,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    payload = {
        "exchangeSegment": exchange_segment,
        "interval": "1",
        "securityId": str(int(security_id)),
        "instrument": instrument,
        "expiryFlag": expiry_flag,
        "expiryCode": int(expiry_code),
        "strike": strike,
        "drvOptionType": side,
        "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
        "fromDate": from_date,
        "toDate": to_date,
    }
    obj = dhan_post("/charts/rollingoption", client_id, token, payload)
    data = obj.get("data", {}) if isinstance(obj, dict) else {}
    leg = data.get("ce" if side == "CALL" else "pe") or {}
    ts = leg.get("timestamp", []) or []
    if not ts:
        return pd.DataFrame()
    n = len(ts)

    def arr(name: str):
        a = leg.get(name, []) or []
        if len(a) < n:
            a = list(a) + [None] * (n - len(a))
        return a[:n]

    out = pd.DataFrame({
        "timestamp": pd.to_datetime(ts, unit="s", errors="coerce", utc=True).tz_convert("Asia/Kolkata").tz_localize(None),
        "open": arr("open"), "high": arr("high"), "low": arr("low"), "close": arr("close"),
        "iv": arr("iv"), "volume": arr("volume"), "actual_strike": arr("strike"),
        "oi": arr("oi"), "spot": arr("spot"),
    })
    out["option_type"] = side
    out["requested_strike"] = strike
    out["underlying_security_id"] = int(security_id)
    out["exchange_segment"] = exchange_segment
    out["expiry_flag"] = expiry_flag
    out["expiry_code"] = int(expiry_code)
    out["year"] = pd.to_datetime(out.timestamp).dt.year
    return out


def download_underlying(
    client_id: str,
    token: str,
    row: pd.Series,
    years: List[int],
    index_offsets: List[int],
    stock_offsets: List[int],
    expiry_codes: List[int],
    delay_seconds: float,
    progress_cb,
) -> pd.DataFrame:
    family = row["family"]
    offsets = index_offsets if family == "INDEX" else stock_offsets
    instrument = "OPTIDX" if family == "INDEX" else "OPTSTK"
    expiry_flags = ["WEEK", "MONTH"] if family == "INDEX" else ["MONTH"]
    frames: List[pd.DataFrame] = []
    tasks = [(y, ef, ec, off, side) for y in years for ef in expiry_flags for ec in expiry_codes for off in offsets for side in ["CALL", "PUT"]]
    total = max(1, len(tasks))
    for i, (year, ef, ec, off, side) in enumerate(tasks, start=1):
        if delay_seconds:
            time.sleep(delay_seconds)
        for start, end in chunks_by_30_days(year):
            try:
                f = rolling_option_request(
                    client_id, token, row["exchange_segment"], int(row["underlying_security_id"]),
                    instrument, ef, ec, off, side, start, end,
                )
                if not f.empty:
                    f["underlying_symbol"] = row["underlying_symbol"]
                    f["family"] = family
                    frames.append(f)
            except Exception as exc:
                st.session_state.setdefault("download_errors", []).append({
                    "underlying": row["underlying_symbol"], "year": year, "expiry_flag": ef,
                    "expiry_code": ec, "strike": off, "side": side, "error": str(exc),
                })
        progress_cb(i / total, f"{row['underlying_symbol']} — {year} {ef} code {ec} {off} {side}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def make_zip(files: Dict[str, bytes]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    bio.seek(0)
    return bio.getvalue()


st.title("Dhan NSE/BSE Options Data Downloader")
st.caption("Year-wise minute expired-options downloader. Built around DhanHQ v2 rolling expired-options API.")

with st.sidebar:
    st.header("Dhan Connection")
    client_id = st.text_input("Dhan Client ID")
    token = st.text_input("Dhan Access Token", type="password")
    st.info("Keep credentials local/session-only. Do not commit them to GitHub.")

    years_text = st.text_input("Years", value="2022-2026")
    selected_years = parse_years(years_text)

    st.subheader("Strike range")
    st.checkbox("Index: ATM-10 to ATM+10", value=True, disabled=True)
    stock_range = st.selectbox("F&O stocks", ["ATM-3 to ATM+3 (Dhan API limit)", "ATM only"], index=0)
    index_offsets = [f"ATM{n:+d}" if n else "ATM" for n in range(-10, 11)]
    stock_offsets = [f"ATM{n:+d}" if n else "ATM" for n in (range(-3, 4) if stock_range.startswith("ATM-3") else [0])]
    expiry_codes = st.multiselect("Expiry codes", [1, 2, 3, 4], default=[1, 2, 3, 4])
    delay = st.number_input("Delay between requests (sec)", min_value=0.0, max_value=10.0, value=0.25, step=0.25)

if not client_id or not token:
    st.warning("Enter your Dhan Client ID and Access Token in the sidebar.")
    st.stop()

if st.button("Load NSE/BSE F&O Universe", type="primary"):
    try:
        with st.spinner("Downloading Dhan instrument master..."):
            master = load_master()
            st.session_state.universe = build_universe(master)
            st.session_state.underlyings = select_underlyings(st.session_state.universe)
        st.success(f"Loaded {len(st.session_state.underlyings):,} option underlyings.")
    except Exception as e:
        st.error(str(e))

underlyings = st.session_state.get("underlyings", pd.DataFrame())
if underlyings.empty:
    st.stop()

left, right = st.columns([1.6, 1])
with left:
    st.subheader("Universe")
    exchanges = st.multiselect("Exchange", sorted(underlyings.exchange.unique()), default=sorted(underlyings.exchange.unique()))
    families = st.multiselect("Type", ["INDEX", "STOCK"], default=["INDEX", "STOCK"])
    filtered = underlyings[underlyings.exchange.isin(exchanges) & underlyings.family.isin(families)].copy()
    st.dataframe(filtered, use_container_width=True, height=420)
with right:
    st.subheader("Selection")
    default_syms = filtered.underlying_symbol.head(min(10, len(filtered))).tolist()
    symbols = st.multiselect("Underlying symbols", sorted(filtered.underlying_symbol.unique()), default=default_syms)
    selected = filtered[filtered.underlying_symbol.isin(symbols)].copy()
    st.metric("Selected underlyings", len(selected))
    st.caption(f"Years: {selected_years or 'none'}")

if st.button("DOWNLOAD YEAR-WISE DATA", type="primary", use_container_width=True):
    if not selected_years:
        st.error("Specify at least one year.")
        st.stop()
    if not expiry_codes:
        st.error("Select at least one expiry code.")
        st.stop()

    st.session_state.download_errors = []
    all_frames: List[pd.DataFrame] = []
    overall = st.progress(0.0)
    status = st.empty()

    # Approximate work count for the top-level progress bar.
    for idx, (_, row) in enumerate(selected.iterrows(), start=1):
        status.info(f"Downloading {row['underlying_symbol']} ({idx}/{len(selected)})...")
        local = st.progress(0.0)
        df = download_underlying(
            client_id, token, row, selected_years, index_offsets,
            [f"ATM{n:+d}" if n else "ATM" for n in (range(-3, 4) if stock_range.startswith("ATM-3") else [0])],
            expiry_codes, float(delay), local.progress,
        )
        if not df.empty:
            all_frames.append(df)
        local.empty()
        overall.progress(idx / len(selected))

    if not all_frames:
        st.error("No data returned. Check Dhan Data API access/subscription, token validity, dates, and expiry-code selection.")
        if st.session_state.download_errors:
            st.dataframe(pd.DataFrame(st.session_state.download_errors), use_container_width=True)
        st.stop()

    result = pd.concat(all_frames, ignore_index=True)
    result = result.sort_values(["year", "underlying_symbol", "timestamp", "option_type", "requested_strike"]).reset_index(drop=True)

    files: Dict[str, bytes] = {}
    manifest_rows = []
    for year, year_df in result.groupby("year"):
        csv = year_df.to_csv(index=False).encode("utf-8")
        name = f"options_{int(year)}.csv"
        files[name] = csv
        manifest_rows.append({
            "year": int(year), "rows": len(year_df), "underlyings": year_df.underlying_symbol.nunique(),
            "first_timestamp": str(year_df.timestamp.min()), "last_timestamp": str(year_df.timestamp.max()),
        })

    manifest = pd.DataFrame(manifest_rows).sort_values("year")
    files["manifest.csv"] = manifest.to_csv(index=False).encode("utf-8")
    if st.session_state.download_errors:
        files["errors.csv"] = pd.DataFrame(st.session_state.download_errors).to_csv(index=False).encode("utf-8")

    zip_bytes = make_zip(files)
    st.session_state.download_zip = zip_bytes
    st.session_state.download_manifest = manifest
    st.session_state.download_result = result

    status.success(f"Finished: {len(result):,} rows across {result.year.nunique()} year(s).")

if "download_manifest" in st.session_state:
    st.subheader("Download summary")
    st.dataframe(st.session_state.download_manifest, use_container_width=True)
    st.download_button(
        "Download ZIP — one CSV per year",
        data=st.session_state.download_zip,
        file_name="dhan_options_yearwise.zip",
        mime="application/zip",
        use_container_width=True,
    )
    if st.session_state.download_errors:
        st.warning(f"{len(st.session_state.download_errors)} request groups failed. See errors.csv inside the ZIP.")
        st.dataframe(pd.DataFrame(st.session_state.download_errors), use_container_width=True, height=240)

st.divider()
st.markdown("### Data model")
st.write("Each row is a 1-minute observation with timestamp, OHLC, IV, volume, OI, actual strike, spot, requested ATM-relative strike, option type, underlying, expiry flag/code and exchange segment.")
st.caption("Dhan's rolling expired-options API supports Index Options up to ATM±10 and other contracts up to ATM±3, with a maximum 30-day range per request and up to five years of history.")
