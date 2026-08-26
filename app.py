import io
import json
import re
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
PAIR_TOLERANCE = pd.Timedelta(minutes=2)
SPOT_TOLERANCE = pd.Timedelta(minutes=20)

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
TARGET_QUARTERS = [k for k in QUARTERS if k.startswith("Q") and ("2025" in k or "2026" in k)]

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


def find_col(df, names):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in lookup:
            return lookup[key]
    for key, original in lookup.items():
        if any(name.lower().replace(" ", "_") in key for name in names):
            return original
    return None


def num_col(df, names):
    c = find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def read_csvs(files):
    frames = []
    for f in files or []:
        d = pd.read_csv(f, low_memory=False)
        if not d.empty:
            frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_time(df):
    c = find_col(df, ["timestamp", "datetime", "date_time", "exchange_timestamp", "timestamp_ist", "time", "date"])
    if c is None:
        raise ValueError(f"Timestamp column not found. Columns: {list(df.columns)}")
    out = df.copy()
    out["timestamp"] = parse_datetime(out[c])
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def normalize_options(df):
    x = normalize_time(df)
    strike = find_col(x, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = find_col(x, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = find_col(x, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    if strike is None or side is None:
        raise ValueError(f"Options require strike and CE/PE columns. Found: {list(df.columns)}")
    out = pd.DataFrame({
        "timestamp": x["timestamp"],
        "strike": pd.to_numeric(x[strike], errors="coerce"),
        "side": x[side].astype(str).str.upper().str.strip(),
    })
    out["side"] = out["side"].replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
    if expiry is not None:
        out["expiry"] = pd.to_datetime(x[expiry], errors="coerce").dt.normalize()
    else:
        out["expiry"] = pd.NaT
    for names, target in [
        (["close", "ltp", "last_price", "price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["volume", "vol", "traded_volume"], "volume"),
    ]:
        v = num_col(x, names)
        out[target] = v if v is not None else np.nan
    if "option_type" in x.columns:
        alt = x["option_type"].astype(str).str.upper().str.strip().replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"})
        out.loc[~out["side"].isin(["CE", "PE"]), "side"] = alt.loc[~out["side"].isin(["CE", "PE"])]
    return out.dropna(subset=["timestamp", "strike"])[out["side"].isin(["CE", "PE"])].sort_values("timestamp").reset_index(drop=True)


def normalize_spot(df):
    x = normalize_time(df)
    p = num_col(x, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if p is None:
        raise ValueError("NIFTY Spot has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "nifty_spot": p}).dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    x = normalize_time(df)
    p = num_col(x, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if p is None:
        raise ValueError("India VIX has no recognizable close/price column.")
    return pd.DataFrame({"timestamp": x.timestamp, "vix_close": p}).dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def synchronize(options, spot, vix):
    o = options.sort_values("timestamp").copy()
    s = spot.sort_values("timestamp").copy()
    m = pd.merge_asof(o, s, on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE)
    m = m.dropna(subset=["nifty_spot"])
    if not vix.empty and not m.empty:
        v = vix.sort_values("timestamp").copy()
        m = pd.merge_asof(m.sort_values("timestamp"), v, on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE)
    return m.sort_values("timestamp").reset_index(drop=True)


def pair_ce_pe(m):
    if m.empty:
        return pd.DataFrame()
    x = m.copy()
    x["timestamp"] = pd.to_datetime(x.timestamp).astype("datetime64[ns]")
    x["expiry_dt"] = pd.to_datetime(x.expiry, errors="coerce").dt.normalize()
    x["expiry_key"] = x["expiry_dt"].dt.strftime("%Y-%m-%d").fillna("NO_EXPIRY")
    x["strike"] = pd.to_numeric(x.strike, errors="coerce")
    x = x.dropna(subset=["timestamp", "strike"])
    ce = x[x.side == "CE"].copy()
    pe = x[x.side == "PE"].copy()
    if ce.empty or pe.empty:
        return pd.DataFrame()
    for c in ["nifty_spot", "vix_close", "close", "iv", "oi", "volume"]:
        if c in pe.columns:
            pe[c + "_pe"] = pe[c]
    pe = pe.rename(columns={"timestamp": "pe_timestamp"})
    ce = ce.sort_values(["timestamp", "expiry_key", "strike"], kind="mergesort").reset_index(drop=True)
    pe = pe.sort_values(["pe_timestamp", "expiry_key", "strike"], kind="mergesort").reset_index(drop=True)
    paired = pd.merge_asof(
        ce,
        pe,
        left_on="timestamp",
        right_on="pe_timestamp",
        by=["expiry_key", "strike"],
        direction="nearest",
        tolerance=PAIR_TOLERANCE,
        suffixes=("", "_dup"),
    ).dropna(subset=["pe_timestamp", "close", "close_pe"])
    if paired.empty:
        return pd.DataFrame()
    paired["pair_timestamp"] = paired.timestamp
    paired["nifty_spot_pair"] = paired.nifty_spot
    paired["vix_close_pair"] = paired.get("vix_close", np.nan)
    paired["pair_gap_seconds"] = (paired.pe_timestamp - paired.timestamp).abs().dt.total_seconds()
    return paired.sort_values("pair_timestamp").reset_index(drop=True)


def build_features(m):
    p = pair_ce_pe(m)
    if p.empty:
        return pd.DataFrame(), pd.DataFrame()
    p["spot_dist"] = (p.strike - p.nifty_spot_pair).abs()
    atm = p.sort_values(["pair_timestamp", "spot_dist"], kind="mergesort").drop_duplicates("pair_timestamp")
    def arr(c):
        return pd.to_numeric(atm[c], errors="coerce").to_numpy() if c in atm.columns else np.full(len(atm), np.nan)
    f = pd.DataFrame({
        "timestamp": atm.pair_timestamp.to_numpy(),
        "nifty_spot": arr("nifty_spot_pair"),
        "atm_strike": arr("strike"),
        "ce_close": arr("close"),
        "pe_close": arr("close_pe"),
        "ce_iv": arr("iv"),
        "pe_iv": arr("iv_pe"),
        "ce_oi": arr("oi"),
        "pe_oi": arr("oi_pe"),
        "ce_volume": arr("volume"),
        "pe_volume": arr("volume_pe"),
        "vix_close": arr("vix_close_pair"),
        "pair_gap_seconds": arr("pair_gap_seconds"),
    }).sort_values("timestamp").reset_index(drop=True)
    f["pcr_oi"] = f.pe_oi / f.ce_oi.replace(0, np.nan)
    f["straddle"] = f.ce_close + f.pe_close
    f["atm_iv"] = pd.concat([f.ce_iv, f.pe_iv], axis=1).mean(axis=1)
    for n in [1, 4, 16]:
        f[f"spot_ret_{n}"] = f.nifty_spot.pct_change(n)
    f["spot_vol_8"] = f.spot_ret_1.rolling(8).std()
    f["spot_ma_8"] = f.nifty_spot.rolling(8).mean()
    f["spot_ma_32"] = f.nifty_spot.rolling(32).mean()
    f["spot_trend"] = f.spot_ma_8 - f.spot_ma_32
    f["straddle_change"] = f.straddle.diff()
    f["straddle_ret"] = f.straddle.pct_change()
    f["iv_change"] = f.atm_iv.diff()
    f["vix_change"] = f.vix_close.diff()
    f["vix_ret"] = f.vix_close.pct_change()
    f["pcr_change"] = f.pcr_oi.diff()
    for n in [4, 16]:
        f[f"forward_spot_{n}"] = f.nifty_spot.shift(-n) / f.nifty_spot - 1
        f[f"forward_straddle_{n}"] = f.straddle.shift(-n) / f.straddle - 1
    return f, p


def discover_patterns(f):
    # Deliberately matches the original FRIDAY report scale: returns and rates are decimal values,
    # exactly as the earlier Q4 2024 audit reports displayed them.
    rules = [
        ("IV rising + spot flat", (f.iv_change > 0) & (f.spot_ret_4.abs() < 0.001)),
        ("IV falling + spot flat", (f.iv_change < 0) & (f.spot_ret_4.abs() < 0.001)),
        ("PCR rising", f.pcr_change > 0),
        ("PCR falling", f.pcr_change < 0),
        ("VIX rising", f.vix_change > 0),
        ("VIX falling", f.vix_change < 0),
        ("Straddle expanding", f.straddle_change > 0),
        ("Straddle contracting", f.straddle_change < 0),
        ("Spot uptrend", f.spot_trend > 0),
        ("Spot downtrend", f.spot_trend < 0),
    ]
    rows = []
    for name, mask in rules:
        d = f.loc[mask].dropna(subset=["forward_spot_4", "forward_straddle_4"])
        if len(d) >= 10:
            rows.append({
                "pattern": name,
                "observations": len(d),
                "avg_next_4_spot_pct": d.forward_spot_4.mean(),
                "avg_next_16_spot_pct": d.forward_spot_16.mean(),
                "avg_next_4_straddle_pct": d.forward_straddle_4.mean(),
                "avg_next_16_straddle_pct": d.forward_straddle_16.mean(),
                "next_4_spot_up_rate": (d.forward_spot_4 > 0).mean(),
                "next_4_straddle_up_rate": (d.forward_straddle_4 > 0).mean(),
            })
    return pd.DataFrame(rows).sort_values("observations", ascending=False) if rows else pd.DataFrame()


def diagnostics(o, s, v, m, p, f):
    return pd.DataFrame({
        "Check": [
            "Option rows", "Spot rows", "VIX rows", "Option+Spot synced", "CE rows", "PE rows",
            "CE/PE pairs", "ATM observations", "Option start", "Option end", "Spot start", "Spot end"
        ],
        "Value": [
            len(o), len(s), len(v), len(m),
            int((o.side == "CE").sum()) if not o.empty else 0,
            int((o.side == "PE").sum()) if not o.empty else 0,
            len(p), len(f),
            str(o.timestamp.min()) if not o.empty else "N/A",
            str(o.timestamp.max()) if not o.empty else "N/A",
            str(s.timestamp.min()) if not s.empty else "N/A",
            str(s.timestamp.max()) if not s.empty else "N/A",
        ],
    })


def fmt_md(v):
    if pd.isna(v):
        return ""
    if isinstance(v, (float, np.floating)):
        return f"{float(v):.6g}"
    return str(v).replace("|", "\\|")


def markdown_table(df):
    if df is None or df.empty:
        return "_No rows._"
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for row in df.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(fmt_md(v) for v in row) + " |")
    return "\n".join(lines)


def build_report(period_label, o, s, v, m, p, f, patterns):
    safe = period_label.upper().replace(" ", "_").replace("/", "-")
    lines = [
        f"# FRIDAY — OPTION PATTERN RESEARCH — {period_label}",
        "",
        "## Scope",
        f"Analysis period: **{period_label}**",
        "Sources: Options + NIFTY Spot + India VIX (if uploaded). Futures excluded.",
        "",
        "## Input Diagnostics",
        markdown_table(diagnostics(o, s, v, m, p, f)),
        "",
        "## Pattern Summary",
        markdown_table(patterns),
        "",
        "## Research Notes",
        f"ATM observations analysed: **{len(f):,}**",
        "Current FRIDAY is a statistical pattern analyzer, not the later autonomous AI research engine.",
        "",
    ]
    return "\n".join(lines), safe


def safe_period_from_filename(name):
    """Best-effort quarter detection from a filename."""
    stem = name.upper().replace("-", "_").replace(" ", "_")
    m = re.search(r"Q([1-4])[_]?20(25|26)", stem)
    if m:
        return f"Q{m.group(1)} 20{m.group(2)}"
    return None


def slice_to_quarter(df, quarter):
    qstart, qend = QUARTERS[quarter]
    start = pd.Timestamp(qstart)
    end = pd.Timestamp(qend) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return df[(df.timestamp >= start) & (df.timestamp <= end)].copy()


def quarter_from_dates(df):
    if df.empty or "timestamp" not in df.columns:
        return []
    dmin = df.timestamp.min().date()
    dmax = df.timestamp.max().date()
    hits = []
    for q in TARGET_QUARTERS:
        qs, qe = QUARTERS[q]
        if dmax >= qs and dmin <= qe:
            hits.append(q)
    return hits


def load_inputs_by_quarter(files, normalizer):
    """Read one or more CSVs and split them into target quarters by actual timestamps."""
    bucket = {q: [] for q in TARGET_QUARTERS}
    for uploaded in files or []:
        raw = pd.read_csv(uploaded, low_memory=False)
        if raw.empty:
            continue
        data = normalizer(raw)
        if data.empty:
            continue
        named_q = safe_period_from_filename(uploaded.name)
        if named_q in bucket:
            bucket[named_q].append(data)
            continue
        for q in quarter_from_dates(data):
            part = slice_to_quarter(data, q)
            if not part.empty:
                bucket[q].append(part)
    return {q: (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()) for q, parts in bucket.items()}


def process_quarter(q, option_df, spot_df, vix_df):
    if option_df.empty:
        raise ValueError(f"{q}: no option data found.")
    if spot_df.empty:
        raise ValueError(f"{q}: no NIFTY Spot data found.")
    m = synchronize(option_df, spot_df, vix_df)
    if m.empty:
        raise ValueError(f"{q}: no Option/Spot timestamps overlap within {SPOT_TOLERANCE}.")
    p = pair_ce_pe(m)
    if p.empty:
        raise ValueError(f"{q}: no CE/PE pairs found within ±{int(PAIR_TOLERANCE.total_seconds())} seconds for the same expiry/strike.")
    f, p2 = build_features(m)
    if f.empty:
        raise ValueError(f"{q}: pairing worked, but no ATM observations could be built.")
    patterns = discover_patterns(f)
    return m, p2, f, patterns


def build_quarter_package(results):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as z:
        for q, result in results.items():
            m, p, f, patterns, o, s, v = result
            report_text, safe = build_report(q, o, s, v, m, p, f, patterns)
            z.writestr(f"{safe}_REPORT.md", report_text)
            z.writestr(f"{safe}_features.csv", f.to_csv(index=False))
            z.writestr(f"{safe}_patterns.csv", patterns.to_csv(index=False))
            z.writestr(f"{safe}_diagnostics.csv", diagnostics(o, s, v, m, p, f).to_csv(index=False))
            z.writestr(f"{safe}_pairs.csv", p.to_csv(index=False))
        z.writestr("README.txt", "FRIDAY quarter-wise audit reports generated with the original report structure and original metric scale. Futures excluded. The autonomous AI research engine is not enabled.")
    return package.getvalue()


# ----------------------------- Data Vault -----------------------------

def dhan_call(path, payload, token, client_id, max_retries=3):
    if not token:
        raise ValueError("Enter your Dhan Access Token in the sidebar first.")
    last = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            DHAN_API + path,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client_id},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            last = RuntimeError(f"Dhan HTTP {e.code}: {detail[:1200]}")
            if e.code not in (429, 500, 502, 503, 504) or attempt >= max_retries:
                raise last from e
        except urllib.error.URLError as e:
            last = RuntimeError(f"Dhan connection error: {e}")
            if attempt >= max_retries:
                raise last from e
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 8))
    raise last or RuntimeError("Dhan request failed")


def dhan_profile(token, client_id):
    if not token:
        return False, "No token entered"
    req = urllib.request.Request(DHAN_API + "/profile", method="GET", headers={"Accept": "application/json", "access-token": token, "client-id": client_id})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
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
    cols = {k: data.get(k, [None] * n) for k in ["timestamp", "open", "high", "low", "close", "iv", "volume", "oi", "strike", "spot"]}
    out = pd.DataFrame({k: (v if isinstance(v, list) else [None] * n) for k, v in cols.items()})
    out["timestamp"] = parse_datetime(out["timestamp"])
    for c in cols:
        if c != "timestamp":
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def rolling_option_part(body, offset, opt):
    key = "ce" if opt == "CALL" else "pe"
    df = series_from_response(body, key)
    if df.empty:
        return df
    df["strike_offset"] = offset
    df["option_type"] = "CE" if opt == "CALL" else "PE"
    return df


def date_chunks(start_date, end_date, max_days=10):
    chunks = []
    cur = start_date
    while cur <= end_date:
        ce = min(cur + timedelta(days=max_days - 1), end_date)
        chunks.append((cur, ce))
        cur = ce + timedelta(days=1)
    return chunks


def option_quarter_download(q_label, start_date, end_date, token, client_id, progress_cb=None, status_cb=None):
    jobs = []
    for cur, ce in date_chunks(start_date, end_date, 10):
        for offset in range(-10, 11):
            for opt in ("CALL", "PUT"):
                strike = "ATM" if offset == 0 else (f"ATM+{offset}" if offset > 0 else f"ATM{offset}")
                payload = {
                    "exchangeSegment": "NSE_FNO", "interval": "1", "securityId": 13, "instrument": "OPTIDX",
                    "expiryFlag": "WEEK", "expiryCode": 1, "strike": strike, "drvOptionType": opt,
                    "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
                    "fromDate": cur.strftime("%Y-%m-%d"), "toDate": (ce + timedelta(days=1)).strftime("%Y-%m-%d"),
                }
                jobs.append((cur, ce, offset, opt, payload))
    total = len(jobs)
    if status_cb:
        status_cb(f"Preflight: testing Dhan weekly options request for {q_label}...")
    first = jobs[0]
    _, _, offset, opt, payload = first
    first_body = dhan_call("/charts/rollingoption", payload, token, client_id, max_retries=3)
    chunks = []
    first_part = rolling_option_part(first_body, offset, opt)
    if not first_part.empty:
        first_part["quarter"] = q_label
        chunks.append(first_part)
    done = 1
    if progress_cb:
        progress_cb(done / total)
    if status_cb:
        status_cb(f"Preflight passed. Downloading {total:,} requests with {MAX_OPTION_WORKERS} workers...")
    errors = []
    with ThreadPoolExecutor(max_workers=MAX_OPTION_WORKERS) as executor:
        fmap = {executor.submit(dhan_call, "/charts/rollingoption", job[4], token, client_id, 3): job for job in jobs[1:]}
        for fut in as_completed(fmap):
            job = fmap[fut]
            try:
                body = fut.result()
                part = rolling_option_part(body, job[2], job[3])
                if not part.empty:
                    part["quarter"] = q_label
                    chunks.append(part)
            except Exception as exc:
                errors.append({"from_date": str(job[0]), "to_date": str(job[1]), "strike_offset": job[2], "option_type": "CE" if job[3] == "CALL" else "PE", "error": str(exc)})
            done += 1
            if progress_cb:
                progress_cb(done / total)
            if status_cb and (done % 10 == 0 or done == total):
                status_cb(f"{done:,}/{total:,} requests complete | failed {len(errors)}")
            if done > 20 and len(errors) / max(1, done - 1) > 0.25:
                raise RuntimeError(f"Dhan is failing too many requests ({len(errors)}/{done-1}). Stopped instead of producing incomplete research data.")
    if not chunks:
        raise RuntimeError(f"No usable weekly ATM±10 option candles were returned for {q_label}.")
    out = pd.concat(chunks, ignore_index=True)
    out = out.drop_duplicates(subset=["timestamp", "option_type", "strike_offset", "strike"]).sort_values(["timestamp", "option_type", "strike_offset"]).reset_index(drop=True)
    out.attrs["failed_requests"] = errors
    return out


def download_spot_or_vix(symbol, start_date, end_date, timeframe, token, client_id, progress_cb=None):
    sid = "13" if symbol == "NIFTY" else "21"
    interval = {"1-minute": 1, "5-minute": 5, "15-minute": 15, "25-minute": 25, "60-minute": 60, "Daily": None}[timeframe]
    windows = date_chunks(start_date, end_date, 90)
    chunks = []
    for i, (cur, ce) in enumerate(windows, 1):
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
        st.info("1-minute weekly expired-option data. 21 strike levels × CE/PE. OI + IV + volume + spot. Requests are split into 10-day windows and show live progress.")
    else:
        timeframe = st.selectbox("Timeframe", ["1-minute", "5-minute", "15-minute", "25-minute", "60-minute", "Daily"], index=0)
        st.info("Historical OHLC download. OI is disabled for Spot/VIX.")
    period = st.selectbox("Research Period", ["Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025", "FULL 2025", "Q1 2026", "Q2 2026", "Q3 2026", "2026 YTD", "FULL 2026 AVAILABLE", "Custom"])
    custom_start = st.date_input("Custom start", value=date(2025, 1, 1), min_value=date(2025, 1, 1), max_value=date(2026, 8, 26))
    custom_end = st.date_input("Custom end", value=date(2025, 3, 31), min_value=date(2025, 1, 1), max_value=date(2026, 8, 26))
    ranges = {**QUARTERS, "FULL 2025": (date(2025, 1, 1), date(2025, 12, 31)), "2026 YTD": (date(2026, 1, 1), date(2026, 8, 26)), "FULL 2026 AVAILABLE": (date(2026, 1, 1), date(2026, 8, 26)), "Custom": (custom_start, custom_end)}
    st.caption(f"Selected period: {ranges[period][0]} → {ranges[period][1]}")
    if st.button("DOWNLOAD QUARTER DATA", use_container_width=True):
        if not token:
            st.error("Enter your Dhan Access Token first.")
            return
        gauge = st.progress(0, text="FRIDAY Data Vault: 0%")
        status = st.empty()
        started = time.time()
        try:
            if period == "FULL 2025": ranges_to_get = list(QUARTERS.items())[4:8]
            elif period in ("2026 YTD", "FULL 2026 AVAILABLE"): ranges_to_get = list(QUARTERS.items())[8:11]
            elif period == "Custom": ranges_to_get = [("Custom", (custom_start, custom_end))]
            else: ranges_to_get = [(period, ranges[period])]
            parts = []
            for qi, (label, (qs, qe)) in enumerate(ranges_to_get):
                base = qi / len(ranges_to_get)
                span = 1 / len(ranges_to_get)
                def update_progress(p, base=base, span=span, label=label):
                    pct = min(99.9, (base + p * span) * 100)
                    gauge.progress(pct / 100, text=f"FRIDAY Data Vault: {pct:.1f}% — {label}")
                def update_status(msg, label=label):
                    status.info(f"{label}: {msg} | elapsed {int(time.time() - started)}s")
                status.info(f"{label}: starting")
                if dataset == "NIFTY Weekly Options ATM±10":
                    part = option_quarter_download(label, qs, qe, token, client_id, update_progress, update_status)
                else:
                    part = download_spot_or_vix("NIFTY" if dataset == "NIFTY Spot" else "INDIA VIX", qs, qe, timeframe, token, client_id, update_progress)
                    part["period"] = label
                parts.append(part)
                gauge.progress((qi + 1) / len(ranges_to_get), text=f"FRIDAY Data Vault: {(qi + 1) / len(ranges_to_get) * 100:.1f}% — {label} complete")
            if len(parts) == 1:
                out_df = parts[0]
                safe = period.replace(" ", "_").replace("±", "PLUS_MINUS")
                csv_bytes = out_df.to_csv(index=False).encode("utf-8")
                st.success(f"Download complete — {len(out_df):,} rows")
                st.dataframe(out_df.head(500), use_container_width=True, hide_index=True)
                st.download_button("DOWNLOAD CSV", csv_bytes, f"FRIDAY_{safe}_{dataset.replace(' ', '_').replace('±', 'PLUS_MINUS')}.csv", "text/csv", use_container_width=True)
            else:
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    for label, part in zip([x[0] for x in ranges_to_get], parts):
                        safe = label.replace(" ", "_")
                        z.writestr(f"{safe}_{dataset.replace(' ', '_').replace('±', 'PLUS_MINUS')}.csv", part.to_csv(index=False))
                    z.writestr("README.txt", f"FRIDAY research data package. Period={period}. Options are 1-minute weekly ATM-10..ATM+10 with OI/IV/volume/spot.")
                st.success(f"Quarter-wise package ready — {len(parts)} periods")
                st.download_button("DOWNLOAD COMPLETE QUARTER PACKAGE (.ZIP)", out.getvalue(), f"FRIDAY_{period.replace(' ', '_')}_COMPLETE_RESEARCH_PACKAGE.zip", "application/zip", use_container_width=True)
            gauge.progress(1.0, text="FRIDAY Data Vault: 100% ✅")
        except Exception as exc:
            status.error(f"Download stopped after {time.time() - started:.0f}s: {exc}")
            st.exception(exc)


def render_pattern_research():
    st.title("FRIDAY — OPTION PATTERN RESEARCH")
    st.caption("Quarter-wise statistical audit builder. Report structure and metric scale match the earlier Q4 2024 FRIDAY audit reports.")

    st.subheader("1. Select report periods")
    selected = st.multiselect("Quarter(s) to generate", TARGET_QUARTERS, default=["Q1 2025"], help="You can generate one quarter, several quarters, or the whole 2025-2026 set in one run.")

    st.subheader("2. Upload research data")
    st.caption("Upload the quarter CSVs for Options, NIFTY Spot and India VIX. FRIDAY detects quarters from filenames when possible; otherwise it uses the actual timestamps.")
    opt_files = st.file_uploader("Option Data CSV(s)", type=["csv"], accept_multiple_files=True, key="opt_report")
    spot_files = st.file_uploader("NIFTY Spot CSV(s)", type=["csv"], accept_multiple_files=True, key="spot_report")
    vix_files = st.file_uploader("India VIX CSV(s) — optional", type=["csv"], accept_multiple_files=True, key="vix_report")

    if st.button("GENERATE QUARTER-WISE REPORTS", use_container_width=True):
        if not selected:
            st.error("Select at least one quarter.")
            return
        if not opt_files or not spot_files:
            st.error("Upload Option Data and NIFTY Spot data first. India VIX remains optional.")
            return

        bar = st.progress(0, text="FRIDAY Reports: 0%")
        status = st.empty()
        start = time.time()
        try:
            opt_by_q = load_inputs_by_quarter(opt_files, normalize_options)
            spot_by_q = load_inputs_by_quarter(spot_files, normalize_spot)
            vix_by_q = load_inputs_by_quarter(vix_files, normalize_vix) if vix_files else {q: pd.DataFrame() for q in TARGET_QUARTERS}

            results = {}
            errors = []
            for idx, q in enumerate(selected, start=1):
                status.info(f"{q}: processing {idx}/{len(selected)}")
                try:
                    m, p, f, patterns = process_quarter(q, opt_by_q[q], spot_by_q[q], vix_by_q[q])
                    results[q] = (m, p, f, patterns, opt_by_q[q], spot_by_q[q], vix_by_q[q])
                except Exception as exc:
                    errors.append((q, str(exc)))
                bar.progress(idx / len(selected), text=f"FRIDAY Reports: {idx/len(selected)*100:.1f}% — {q}")

            if not results:
                raise RuntimeError("No quarter completed successfully. " + " | ".join(f"{q}: {e}" for q, e in errors))

            for q, result in results.items():
                m, p, f, patterns, o, s, v = result
                report_text, safe = build_report(q, o, s, v, m, p, f, patterns)
                st.markdown(f"### {q} — Report ready")
                st.code(report_text[:4000], language="markdown")
                c1, c2 = st.columns(2)
                with c1:
                    st.download_button(f"DOWNLOAD {q} REPORT (.MD)", report_text.encode("utf-8"), f"FRIDAY_{safe}_REPORT.md", "text/markdown", key=f"md_{safe}", use_container_width=True)
                with c2:
                    qzip = io.BytesIO()
                    with zipfile.ZipFile(qzip, "w", zipfile.ZIP_DEFLATED) as z:
                        z.writestr(f"{safe}_REPORT.md", report_text)
                        z.writestr(f"{safe}_features.csv", f.to_csv(index=False))
                        z.writestr(f"{safe}_patterns.csv", patterns.to_csv(index=False))
                        z.writestr(f"{safe}_diagnostics.csv", diagnostics(o, s, v, m, p, f).to_csv(index=False))
                        z.writestr(f"{safe}_pairs.csv", p.to_csv(index=False))
                    st.download_button(f"DOWNLOAD {q} FULL PACKAGE (.ZIP)", qzip.getvalue(), f"FRIDAY_{safe}_RESEARCH.zip", "application/zip", key=f"zip_{safe}", use_container_width=True)

            if len(results) > 1:
                st.divider()
                all_zip = build_quarter_package(results)
                st.download_button("DOWNLOAD ALL QUARTER-WISE REPORTS (.ZIP)", all_zip, "FRIDAY_2025_2026_QUARTER_WISE_RESEARCH_REPORTS.zip", "application/zip", use_container_width=True)

            if errors:
                st.warning("Some selected quarters could not be processed: " + " | ".join(f"{q}: {e}" for q, e in errors))
            else:
                status.success(f"All selected reports completed in {time.time() - start:.1f}s")
            bar.progress(1.0, text="FRIDAY Reports: 100% ✅")
        except Exception as exc:
            status.error(f"Report generation stopped after {time.time() - start:.1f}s: {exc}")
            st.exception(exc)


with st.sidebar:
    st.subheader("FRIDAY")
    client_id = st.text_input("Dhan Client ID", value=DEFAULT_CLIENT_ID).strip()
    token = st.text_input("Dhan Access Token", value="", type="password").strip()
    if token:
        ok, msg = dhan_profile(token, client_id)
        (st.success if ok else st.error)(msg)
    module = st.radio("MODULE", ["Data Vault", "Pattern Research"], index=1)

if module == "Data Vault":
    render_data_vault(token, client_id)
else:
    render_pattern_research()
