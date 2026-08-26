import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import pandas as pd
import streamlit as st

DEFAULT_CLIENT_ID = "1113195747"
DHAN_API = "https://api.dhan.co/v2"
MAX_OPTION_WORKERS = 3
REQUEST_TIMEOUT = 60
MAX_RETRIES = 2

QUARTERS = {
    "Q1 2024": (date(2024, 1, 1), date(2024, 3, 31)),
    "Q2 2024": (date(2024, 4, 1), date(2024, 6, 30)),
    "Q3 2024": (date(2024, 7, 1), date(2024, 9, 30)),
    "Q4 2024": (date(2024, 10, 1), date(2024, 12, 31)),
    "Q1 2025": (date(2025, 1, 1), date(2025, 3, 31)),
    "Q2 2025": (date(2025, 4, 1), date(2025, 6, 30)),
    "Q3 2025": (date(2025, 7, 1), date(2025, 9, 30)),
    "Q4 2025": (date(2025, 10, 1), date(2025, 12, 31)),
    "Q1 2026": (date(2026, 1, 1), date(2026, 3, 31)),
    "Q2 2026": (date(2026, 4, 1), date(2026, 6, 30)),
    "Q3 2026": (date(2026, 7, 1), date(2026, 8, 26)),
}

st.set_page_config(page_title="FRIDAY", layout="wide")


def parse_datetime(values):
    s = pd.Series(values)
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8:
        med = float(n.dropna().abs().median()) if n.notna().any() else 0
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(s, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).astype("datetime64[ns]")


def dhan_call(path, payload, token, client_id, max_retries=MAX_RETRIES):
    if not token:
        raise ValueError("Enter your Dhan Access Token in the sidebar first.")
    last_error = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            DHAN_API + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "access-token": token,
                "client-id": client_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Dhan HTTP {e.code}: {detail[:1200]}")
            if e.code not in (429, 500, 502, 503, 504) or attempt >= max_retries:
                raise last_error from e
        except urllib.error.URLError as e:
            last_error = RuntimeError(f"Dhan connection error: {e}")
            if attempt >= max_retries:
                raise last_error from e
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 6))
    raise last_error or RuntimeError("Dhan request failed")


def dhan_profile(token, client_id):
    if not token:
        return False, "No token entered"
    req = urllib.request.Request(
        DHAN_API + "/profile",
        method="GET",
        headers={"Accept": "application/json", "access-token": token, "client-id": client_id},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8"))
        validity = body.get("data", {}).get("tokenValidity") or body.get("tokenValidity")
        return True, f"Token verified{f' | Valid until: {validity}' if validity else ''}"
    except Exception as e:
        return False, str(e)


def series_from_response(body, source_key=None):
    if not isinstance(body, dict):
        return pd.DataFrame()
    data = body.get("data", body)
    if source_key and isinstance(data, dict) and isinstance(data.get(source_key), dict):
        data = data[source_key]
    if not isinstance(data, dict) or not data.get("timestamp"):
        return pd.DataFrame()
    n = len(data["timestamp"])
    keys = ["timestamp", "open", "high", "low", "close", "iv", "volume", "oi", "strike", "spot"]
    out = pd.DataFrame({k: (data.get(k) if isinstance(data.get(k), list) else [None] * n) for k in keys})
    out["timestamp"] = parse_datetime(out["timestamp"])
    for c in keys:
        if c != "timestamp":
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def rolling_option_part(body, offset, option_type):
    key = "ce" if option_type == "CALL" else "pe"
    df = series_from_response(body, key)
    if df.empty:
        return df
    df["strike_offset"] = offset
    df["option_type"] = "CE" if option_type == "CALL" else "PE"
    return df


def date_chunks(start_date, end_date, max_days):
    out = []
    cur = start_date
    while cur <= end_date:
        ce = min(cur + timedelta(days=max_days - 1), end_date)
        out.append((cur, ce))
        cur = ce + timedelta(days=1)
    return out


def make_option_job(cur, ce, offset, opt):
    strike = "ATM" if offset == 0 else (f"ATM+{offset}" if offset > 0 else f"ATM{offset}")
    payload = {
        "exchangeSegment": "NSE_FNO",
        "interval": "1",
        "securityId": 13,
        "instrument": "OPTIDX",
        "expiryFlag": "WEEK",
        "expiryCode": 1,
        "strike": strike,
        "drvOptionType": opt,
        "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
        "fromDate": cur.strftime("%Y-%m-%d"),
        "toDate": (ce + timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    return cur, ce, offset, opt, payload


def run_option_job(job, token, client_id, q_label):
    cur, ce, offset, opt, payload = job
    body = dhan_call("/charts/rollingoption", payload, token, client_id)
    part = rolling_option_part(body, offset, opt)
    if not part.empty:
        part["quarter"] = q_label
    return part


def option_quarter_download(q_label, start_date, end_date, token, client_id, progress_cb=None, status_cb=None):
    offsets = list(range(-10, 11))
    # 10-day windows reduce Dhan gateway payload size.
    jobs = [make_option_job(cur, ce, offset, opt)
            for cur, ce in date_chunks(start_date, end_date, 10)
            for offset in offsets
            for opt in ("CALL", "PUT")]
    total = len(jobs)
    if total == 0:
        raise ValueError(f"No request windows were created for {q_label}.")

    # Preflight: one tiny request must succeed before the full run begins.
    if status_cb:
        status_cb(f"Preflight 0% — testing Dhan weekly options request ({total:,} total requests)")
    first = run_option_job(jobs[0], token, client_id, q_label)
    if first.empty:
        raise RuntimeError("Dhan accepted the request but returned no candles during preflight. Full download was not started.")

    chunks = [first]
    errors = []
    done = 1
    if progress_cb:
        progress_cb(done / total)
    if status_cb:
        status_cb(f"Preflight 0% complete. Starting {MAX_OPTION_WORKERS} workers.")

    # Submit only small batches so hundreds of DataFrames/futures are not held in memory at once.
    remaining = jobs[1:]
    batch_size = MAX_OPTION_WORKERS * 4
    for batch_start in range(0, len(remaining), batch_size):
        batch = remaining[batch_start:batch_start + batch_size]
        with ThreadPoolExecutor(max_workers=MAX_OPTION_WORKERS) as executor:
            future_map = {executor.submit(run_option_job, job, token, client_id, q_label): job for job in batch}
            for future in as_completed(future_map):
                job = future_map[future]
                try:
                    part = future.result()
                    if not part.empty:
                        chunks.append(part)
                except Exception as exc:
                    cur, ce, offset, opt, _ = job
                    errors.append({
                        "from": cur.isoformat(),
                        "to": ce.isoformat(),
                        "strike_offset": offset,
                        "option_type": "CE" if opt == "CALL" else "PE",
                        "error": str(exc),
                    })
                done += 1
                p = done / total
                if progress_cb:
                    progress_cb(p)
                if status_cb:
                    status_cb(f"{p*100:.1f}% — {done:,}/{total:,} requests completed | {len(errors)} failed")

        # Stop early only if the failure rate is clearly unusable.
        completed_nonpreflight = done - 1
        if completed_nonpreflight >= 20 and len(errors) / max(1, completed_nonpreflight) > 0.25:
            raise RuntimeError(
                f"Dhan is failing too many requests ({len(errors)}/{completed_nonpreflight}). "
                "Download stopped early instead of producing an incomplete research file."
            )

    if not chunks:
        raise RuntimeError(f"No usable option candles were returned for {q_label}.")

    out = pd.concat(chunks, ignore_index=True)
    out = out.drop_duplicates(subset=["timestamp", "option_type", "strike_offset", "strike"])
    out = out.sort_values(["timestamp", "option_type", "strike_offset"]).reset_index(drop=True)
    out.attrs["failed_requests"] = errors
    return out


def download_spot_or_vix(symbol, start_date, end_date, timeframe, token, client_id, progress_cb=None):
    sid = "13" if symbol == "NIFTY" else "21"
    interval = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60, "Daily": None}[timeframe]
    windows = date_chunks(start_date, end_date, 90)
    chunks = []
    for i, (cur, ce) in enumerate(windows, start=1):
        if interval is None:
            path = "/charts/historical"
            payload = {"securityId": sid, "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0, "oi": False, "fromDate": cur.strftime("%Y-%m-%d"), "toDate": (ce + timedelta(days=1)).strftime("%Y-%m-%d")}
        else:
            path = "/charts/intraday"
            payload = {"securityId": sid, "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": interval, "oi": False, "fromDate": cur.strftime("%Y-%m-%d 00:00:00"), "toDate": (ce + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")}
        part = series_from_response(dhan_call(path, payload, token, client_id))
        if not part.empty:
            chunks.append(part)
        if progress_cb:
            progress_cb(i / len(windows))
    if not chunks:
        raise ValueError(f"Dhan returned no {symbol} candles for the selected range.")
    return pd.concat(chunks, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def render_data_vault(token, client_id):
    st.title("FRIDAY — DATA VAULT")
    st.caption("2025 + 2026 historical research collector. 2024 data already owned separately.")

    dataset = st.selectbox("Dataset", ["NIFTY Spot", "India VIX", "NIFTY Weekly Options ATM±10"])
    if dataset == "NIFTY Weekly Options ATM±10":
        st.info("1-minute weekly expired-option data. 21 strike levels × CE/PE. OI + IV + volume + spot. Dhan requests are split into 10-day windows. Progress is shown request-by-request.")
    else:
        timeframe = st.selectbox("Timeframe", ["1-minute", "5-minute", "15-minute", "25-minute", "60-minute", "Daily"], index=0)
        st.info("Historical OHLC download. OI is disabled for Spot/VIX.")

    period = st.selectbox("Research Period", ["Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025", "FULL 2025", "Q1 2026", "Q2 2026", "Q3 2026", "2026 YTD", "FULL 2026 AVAILABLE", "Custom"])
    custom_start = st.date_input("Custom start", value=date(2025, 1, 1), min_value=date(2025, 1, 1), max_value=date(2026, 8, 26))
    custom_end = st.date_input("Custom end", value=date(2025, 3, 31), min_value=date(2025, 1, 1), max_value=date(2026, 8, 26))
    ranges = {
        **QUARTERS,
        "FULL 2025": (date(2025, 1, 1), date(2025, 12, 31)),
        "2026 YTD": (date(2026, 1, 1), date(2026, 8, 26)),
        "FULL 2026 AVAILABLE": (date(2026, 1, 1), date(2026, 8, 26)),
        "Custom": (custom_start, custom_end),
    }
    st.caption(f"Selected period: {ranges[period][0]} → {ranges[period][1]}")

    if st.button("DOWNLOAD QUARTER DATA", use_container_width=True):
        if not token:
            st.error("Enter your Dhan Access Token first.")
            return

        gauge = st.progress(0, text="FRIDAY Data Vault: 0%")
        status = st.empty()
        error_box = st.empty()
        started = time.time()

        try:
            if period == "FULL 2025":
                ranges_to_get = list(QUARTERS.items())[4:8]
            elif period in ("2026 YTD", "FULL 2026 AVAILABLE"):
                ranges_to_get = list(QUARTERS.items())[8:11]
            elif period == "Custom":
                ranges_to_get = [("Custom", (custom_start, custom_end))]
            else:
                ranges_to_get = [(period, ranges[period])]

            parts = []
            all_failed = []
            for qi, (label, (qs, qe)) in enumerate(ranges_to_get):
                base = qi / len(ranges_to_get)
                span = 1 / len(ranges_to_get)

                def update_progress(p, base=base, span=span, label=label):
                    pct = min(99.9, (base + p * span) * 100)
                    gauge.progress(pct / 100, text=f"FRIDAY Data Vault: {pct:.1f}% — {label}")

                def update_status(msg, label=label):
                    elapsed = int(time.time() - started)
                    status.info(f"{label} | {msg} | elapsed {elapsed}s")

                status.info(f"{label}: starting")
                if dataset == "NIFTY Weekly Options ATM±10":
                    part = option_quarter_download(label, qs, qe, token, client_id, update_progress, update_status)
                    all_failed.extend(part.attrs.get("failed_requests", []))
                else:
                    part = download_spot_or_vix("NIFTY" if dataset == "NIFTY Spot" else "INDIA VIX", qs, qe, timeframe, token, client_id, update_progress)
                    part["period"] = label
                parts.append(part)
                gauge.progress((qi + 1) / len(ranges_to_get), text=f"FRIDAY Data Vault: {(qi + 1) / len(ranges_to_get) * 100:.1f}% — {label} complete")

            if len(parts) == 1:
                out_df = parts[0]
                safe = period.replace(" ", "_").replace("±", "PLUS_MINUS")
                st.success(f"Download complete — {len(out_df):,} rows")
                st.dataframe(out_df.head(500), use_container_width=True, hide_index=True)
                st.download_button("DOWNLOAD CSV", out_df.to_csv(index=False).encode(), f"FRIDAY_{safe}_{dataset.replace(' ', '_').replace('±', 'PLUS_MINUS')}.csv", "text/csv", use_container_width=True)

                if all_failed:
                    st.warning(f"Completed with {len(all_failed)} failed API requests. Review the error log below before using the dataset for research.")
                    st.download_button("DOWNLOAD FAILED REQUEST LOG (.CSV)", pd.DataFrame(all_failed).to_csv(index=False).encode(), f"FRIDAY_{safe}_FAILED_REQUESTS.csv", "text/csv")
            else:
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    for label, part in zip([x[0] for x in ranges_to_get], parts):
                        safe = label.replace(" ", "_")
                        z.writestr(f"{safe}_{dataset.replace(' ', '_').replace('±', 'PLUS_MINUS')}.csv", part.to_csv(index=False))
                    z.writestr("README.txt", f"FRIDAY research data package. Period={period}. Options are 1-minute weekly ATM-10..ATM+10 with OI/IV/volume/spot. Dhan requests are split into 10-day windows with retries. Failed requests={len(all_failed)}.")
                    if all_failed:
                        z.writestr("FAILED_REQUESTS.csv", pd.DataFrame(all_failed).to_csv(index=False))
                st.success(f"Quarter-wise package ready — {len(parts)} periods")
                st.download_button("DOWNLOAD QUARTER-WISE PACKAGE (.ZIP)", out.getvalue(), f"FRIDAY_{period.replace(' ', '_')}_{dataset.replace(' ', '_').replace('±', 'PLUS_MINUS')}.zip", "application/zip", use_container_width=True)

            gauge.progress(1.0, text="FRIDAY Data Vault: 100% ✅")
        except Exception as exc:
            status.error(f"Download stopped after {time.time() - started:.0f}s: {exc}")
            st.error("FRIDAY DID NOT SILENTLY FAIL. The exact failing request is shown below.")
            st.exception(exc)
            st.warning("No final research CSV was produced because the run was incomplete. Retry the same quarter after checking the exact error above.")


def render_analyzer():
    st.title("FRIDAY — OPTION PATTERN RESEARCH")
    st.caption("Upload any quarter; report naming follows the selected period.")
    period = st.selectbox("Research Period", ["Q1 2024", "Q2 2024", "Q3 2024", "Q4 2024", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025", "Q1 2026", "Q2 2026", "Q3 2026", "Full 2024", "Full 2025", "2026 YTD", "Custom"])
    custom_start = st.date_input("Custom start", value=date(2025, 1, 1))
    custom_end = st.date_input("Custom end", value=date(2025, 3, 31))
    st.caption(f"Selected period: {custom_start} → {custom_end}")
    if st.button("ANALYZE PATTERNS", use_container_width=True):
        st.info("Upload and analysis pipeline is preserved for the existing research workflow. AI research remains deferred until the offline dataset is complete.")


with st.sidebar:
    st.subheader("FRIDAY")
    client_id = st.text_input("Dhan Client ID", value=DEFAULT_CLIENT_ID).strip()
    token = st.text_input("Dhan Access Token", value="", type="password").strip()
    if token:
        ok, msg = dhan_profile(token, client_id)
        (st.success if ok else st.error)(msg)
    module = st.radio("MODULE", ["Data Vault", "Pattern Research"], index=0)

if module == "Data Vault":
    render_data_vault(token, client_id)
else:
    render_analyzer()
