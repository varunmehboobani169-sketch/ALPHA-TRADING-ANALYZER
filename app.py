# FRIDAY Cloud-safe launcher
# Loads the intact FRIDAY source and applies robust Data Vault fixes at runtime.
import re
import requests

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/361848fd675ecda841c8a6564c9f2caa4d57967c/app.py"

source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()
text = source.text

# ---------------------------------------------------------------------------
# 1) Make quarterly ZIP downloads cloud-safe: build them in memory.
# ---------------------------------------------------------------------------
text = text.replace("import math\n", "import math\nimport zipfile\n", 1) if "import zipfile" not in text[:1500] else text
text = text.replace(
    'buf = Path("/mnt/data") / f"FRIDAY_{ds.replace(\' \',\'_\')}_quarters.zip"\n            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:',
    'from io import BytesIO\n            zip_buffer = BytesIO()\n            zip_name = f"FRIDAY_{ds.replace(\' \',\'_\')}_quarters.zip"\n            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:'
)
text = text.replace('                data=buf.read_bytes(),', '                data=zip_buffer.getvalue(),')
text = text.replace('                file_name=buf.name,', '                file_name=zip_name,')
text = text.replace(
    'path = Path("/mnt/data/friday_quarterly_reports.zip")\n\n    master_rows = []\n    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:',
    'from io import BytesIO\n    zip_buffer = BytesIO()\n\n    master_rows = []\n    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:'
)
text = text.replace('    return path\n\n\ndef _read_quarterly_zip_files(files):', '    zip_buffer.seek(0)\n    return zip_buffer\n\n\ndef _read_quarterly_zip_files(files):')
text = text.replace('data=selected_zip.read_bytes(),', 'data=selected_zip.getvalue(),')
text = text.replace('data=all_zip.read_bytes(),', 'data=all_zip.getvalue(),')

# ---------------------------------------------------------------------------
# 2) Replace the fragile NIFTY-futures resolver.
#    The old logic often returned None for historical quarters because the
#    instrument-master columns/values differ across master versions.
# ---------------------------------------------------------------------------
historical_resolver = r'''

def _resolve_historical_nifty_future(master, year, quarter):
    x = master.copy()
    if "security_id" not in x.columns:
        return None

    # Normalize likely instrument-master column aliases.
    if "expiry_date" not in x.columns:
        for c in ["EXPIRY_DATE", "SEM_EXPIRY_DATE", "SM_EXPIRY_DATE", "expiry", "expiry_date"]:
            if c in x.columns:
                x["expiry_date"] = x[c]
                break
    if "expiry_date" not in x.columns:
        return None

    x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")
    x["security_id"] = pd.to_numeric(x["security_id"], errors="coerce")
    x = x.dropna(subset=["expiry_date", "security_id"]).copy()

    # Search all relevant text columns for NIFTY + FUTIDX/FUTURE rather than
    # assuming one exact master schema.
    text_cols = [c for c in x.columns if x[c].dtype == "object"]
    text_blob = pd.Series("", index=x.index, dtype="object")
    for c in text_cols:
        text_blob = text_blob + " " + x[c].astype(str).str.upper()

    nifty_mask = text_blob.str.contains("NIFTY", regex=False, na=False)
    futures_mask = text_blob.str.contains("FUTIDX|FUTURE|FUT", regex=True, na=False)

    if "exchange" in x.columns:
        nifty_mask &= x["exchange"].astype(str).str.upper().isin(["NSE", "NFO", "NSE_FO", "NSE_FNO"])

    candidates = x[nifty_mask & futures_mask].copy()
    if candidates.empty:
        # Fallback: look specifically at instrument field if available.
        if "instrument" in x.columns:
            instr = x["instrument"].astype(str).str.upper()
            candidates = x[instr.str.contains("FUT", regex=False, na=False) & nifty_mask].copy()

    if candidates.empty:
        return None

    q = pd.Period(f"{year}-Q{quarter}")
    q_start = q.start_time
    q_end = q.end_time

    # Select the nearest expiry on or after the quarter end. This keeps the
    # requested quarter attached to a real historical contract instead of the
    # current front month.
    after = candidates[candidates["expiry_date"] >= q_end].sort_values("expiry_date")
    if not after.empty:
        row = after.iloc[0]
    else:
        within = candidates[candidates["expiry_date"] <= q_end].sort_values("expiry_date")
        if within.empty:
            return None
        row = within.iloc[-1]

    return int(row["security_id"]), "NSE_FNO", "FUTIDX"

'''
text = re.sub(r'\ndef _resolve_basic_instrument\(master, dataset\):.*?\n\ndef _download_quarter_dataset\(', historical_resolver + '\ndef _download_quarter_dataset(', text, count=1, flags=re.S)

# ---------------------------------------------------------------------------
# 3) Patch Data Vault to use the historical futures resolver for the selected
#    year/quarter. Spot and VIX continue using the normal resolver.
# ---------------------------------------------------------------------------
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
                st.error(f"Unable to resolve {dataset} for {year} Q{quarter}.")
                st.stop()

            sid, segment, instrument = resolved
''', 1)

# Better diagnostics if the master is missing the necessary fields.
text = text.replace(
'if "expiry_date" not in x.columns:\n        return None',
'if "expiry_date" not in x.columns:\n        return None', 1)

# ---------------------------------------------------------------------------
# 4) Execute the fully patched source.
# ---------------------------------------------------------------------------
exec(compile(text, "friday_app.py", "exec"), globals(), globals())
