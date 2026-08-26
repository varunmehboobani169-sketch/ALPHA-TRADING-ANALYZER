# FRIDAY Cloud-safe launcher
# Robust runtime patch for the FRIDAY Data Vault historical futures resolver.
import re
import requests

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/361848fd675ecda841c8a6564c9f2caa4d57967c/app.py"

source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()
text = source.text

# Ensure required ZIP module exists in the loaded source.
if "import zipfile" not in text[:2000]:
    text = text.replace("import math\n", "import math\nimport zipfile\n", 1)

# ---------------------------------------------------------------------------
# Cloud-safe accumulated ZIP downloads.
# ---------------------------------------------------------------------------
text = re.sub(
    r'\s*buf = Path\("/mnt/data"\).*?with zipfile\.ZipFile\(buf, "w", zipfile\.ZIP_DEFLATED\) as z:',
    '''\n            from io import BytesIO\n            zip_buffer = BytesIO()\n            zip_name = f"FRIDAY_{ds.replace(' ','_')}_quarters.zip"\n            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:''',
    text,
    count=1,
    flags=re.S,
)
text = text.replace("data=buf.read_bytes(),", "data=zip_buffer.getvalue(),")
text = text.replace("file_name=buf.name,", "file_name=zip_name,")

# ---------------------------------------------------------------------------
# Replace quarterly-report ZIP creation with an in-memory ZIP.
# ---------------------------------------------------------------------------
text = text.replace(
    'path = Path("/mnt/data/friday_quarterly_reports.zip")\n\n    master_rows = []\n    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:',
    'from io import BytesIO\n    zip_buffer = BytesIO()\n\n    master_rows = []\n    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:',
)
text = text.replace(
    '    return path\n\n\ndef _read_quarterly_zip_files(files):',
    '    zip_buffer.seek(0)\n    return zip_buffer\n\n\ndef _read_quarterly_zip_files(files):',
)
text = text.replace("data=selected_zip.read_bytes(),", "data=selected_zip.getvalue(),")
text = text.replace("data=all_zip.read_bytes(),", "data=all_zip.getvalue(),")

# ---------------------------------------------------------------------------
# Historical NIFTY Futures resolver.
# The previous resolver was too strict about instrument-master column values.
# This version discovers expiry columns dynamically and identifies NIFTY
# futures by scanning all text columns for NIFTY + FUT.
# ---------------------------------------------------------------------------
historical_resolver = r'''

def _resolve_historical_nifty_future(master, year, quarter):
    x = master.copy()

    if "security_id" not in x.columns:
        return None

    x["security_id"] = pd.to_numeric(x["security_id"], errors="coerce")
    x = x.dropna(subset=["security_id"]).copy()

    # Discover any expiry-date column used by the current Dhan master schema.
    expiry_candidates = [
        c for c in x.columns
        if any(token in str(c).upper() for token in ["EXPIRY_DATE", "EXPIRYDATE", "EXPIRY"])
    ]
    if not expiry_candidates:
        return None

    expiry_col = expiry_candidates[0]
    x["_friday_expiry"] = pd.to_datetime(x[expiry_col], errors="coerce")
    x = x.dropna(subset=["_friday_expiry"]).copy()

    # Build a searchable text blob from every object/string column.
    text_cols = [c for c in x.columns if x[c].dtype == "object"]
    if text_cols:
        blob = x[text_cols].astype(str).agg(" ".join, axis=1).str.upper()
    else:
        blob = pd.Series("", index=x.index)

    nifty_mask = blob.str.contains("NIFTY", regex=False, na=False)
    futures_mask = blob.str.contains(r"FUTIDX|FUTURE|FUT", regex=True, na=False)
    candidates = x[nifty_mask & futures_mask].copy()

    if candidates.empty:
        # Less restrictive fallback: use the explicit instrument/symbol fields.
        mask = pd.Series(False, index=x.index)
        for c in ["underlying_symbol", "symbol_name", "trading_symbol", "display_name"]:
            if c in x.columns:
                s = x[c].astype(str).str.upper().str.strip()
                mask |= s.eq("NIFTY") | s.str.startswith("NIFTY", na=False)
        if "instrument" in x.columns:
            instr = x["instrument"].astype(str).str.upper()
            candidates = x[mask & instr.str.contains("FUT", regex=False, na=False)].copy()
        else:
            candidates = x[mask].copy()

    if candidates.empty:
        return None

    q = pd.Period(f"{year}-Q{quarter}")
    q_end_date = pd.Timestamp(q.end_time.date())

    # Choose the first monthly NIFTY futures contract expiring on/after the
    # quarter end. This gives an actual historical contract rather than today's.
    after = candidates[candidates["_friday_expiry"].dt.normalize() >= q_end_date].sort_values("_friday_expiry")
    if not after.empty:
        row = after.iloc[0]
    else:
        before = candidates[candidates["_friday_expiry"].dt.normalize() <= q_end_date].sort_values("_friday_expiry")
        if before.empty:
            return None
        row = before.iloc[-1]

    return int(row["security_id"]), "NSE_FNO", "FUTIDX"

'''
text = re.sub(
    r'\ndef _resolve_basic_instrument\(master, dataset\):.*?\n\ndef _download_quarter_dataset\(',
    historical_resolver + '\ndef _download_quarter_dataset(',
    text,
    count=1,
    flags=re.S,
)

# Data Vault: use historical resolver for futures.
text = text.replace(
'''            resolved = _resolve_basic_instrument(master, dataset)
            if not resolved:
                st.error(f"Unable to resolve {dataset}.")
                st.stop()

            sid, segment, instrument = resolved
''',
'''            if dataset == "NIFTY Futures":
                resolved = _resolve_historical_nifty_future(master, year, quarter)
            else:
                resolved = _resolve_basic_instrument(master, dataset)

            if not resolved:
                st.error(f"Unable to resolve {dataset} for {year} Q{quarter}. Please refresh the Dhan master and retry.")
                st.stop()

            sid, segment, instrument = resolved
''',
    count=1,
)

exec(compile(text, "friday_app.py", "exec"), globals(), globals())
