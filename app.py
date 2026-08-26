# FRIDAY Cloud-safe launcher
# Loads the intact FRIDAY source and applies the Streamlit Cloud ZIP fix in memory.
# V3.4: Data Vault now downloads each requested quarter reliably by resolving
# the correct historical contract security ID for that year/quarter.

import requests
import base64
import zlib

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/361848fd675ecda841c8a6564c9f2caa4d57967c/app.py"

source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()
text = source.text

# Make Data Vault ZIP downloads cloud-safe: build ZIPs in memory rather than
# reading/writing /mnt/data paths, which are not reliable across Streamlit reruns.
text = text.replace(
    'buf = Path("/mnt/data") / f"FRIDAY_{ds.replace(\' \',\'_\')}_quarters.zip"\n            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:',
    'from io import BytesIO\n            zip_buffer = BytesIO()\n            zip_name = f"FRIDAY_{ds.replace(\' \',\'_\')}_quarters.zip"\n            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:'
)
text = text.replace(
    '                file_name=buf.name,',
    '                file_name=zip_name,\n'
    '                '
)
text = text.replace(
    '                data=buf.read_bytes(),',
    '                data=zip_buffer.getvalue(),'
)

# If the older quarterly report builder writes a ZIP to disk, convert it to
# an in-memory BytesIO object as well.
text = text.replace(
    'path = Path("/mnt/data/friday_quarterly_reports.zip")\n\n    master_rows = []\n    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:',
    'from io import BytesIO\n    zip_buffer = BytesIO()\n\n    master_rows = []\n    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:'
)
text = text.replace(
    '    return path\n\n\ndef _read_quarterly_zip_files(files):',
    '    zip_buffer.seek(0)\n    return zip_buffer\n\n\ndef _read_quarterly_zip_files(files):'
)
text = text.replace('data=selected_zip.read_bytes(),','data=selected_zip.getvalue(),')
text = text.replace('data=all_zip.read_bytes(),','data=all_zip.getvalue(),')

# Add a resolver for the exact historical NIFTY futures contract. The previous
# Data Vault selected the nearest/current contract, which can produce empty data
# for an old quarter. We now select the contract whose expiry covers the quarter.
historical_resolver = r'''

def _resolve_historical_nifty_future(master, year, quarter):
    x = master.copy()
    if "exchange" in x.columns:
        x = x[x["exchange"].astype(str).str.upper().eq("NSE")]
    if "instrument" in x.columns:
        x = x[x["instrument"].astype(str).str.upper().eq("FUTIDX")]

    mask = pd.Series(False, index=x.index)
    for col in ["underlying_symbol", "symbol_name", "trading_symbol", "display_name"]:
        if col in x.columns:
            s = x[col].astype(str).str.upper().str.strip()
            mask |= s.eq("NIFTY") | s.str.startswith("NIFTY-", na=False)
    x = x[mask].copy()
    if x.empty:
        return None

    if "expiry_date" not in x.columns:
        return None
    x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")
    x = x.dropna(subset=["expiry_date"]).copy()
    q = pd.Period(f"{year}-Q{quarter}")
    q_end = q.end_time

    # Choose the first NIFTY futures expiry on/after the quarter end.
    after = x[x["expiry_date"] >= q_end].sort_values("expiry_date")
    if not after.empty:
        row = after.iloc[0]
    else:
        # Fallback: latest contract available in or before that quarter.
        within = x[x["expiry_date"] <= q_end].sort_values("expiry_date")
        if within.empty:
            return None
        row = within.iloc[-1]

    return int(pd.to_numeric(row["security_id"], errors="coerce")), "NSE_FNO", "FUTIDX"

'''
insert_at = text.find('def _download_quarter_dataset(')
if insert_at >= 0 and '_resolve_historical_nifty_future' not in text:
    text = text[:insert_at] + historical_resolver + text[insert_at:]

# In render_data_vault, resolve futures with the historical resolver.
old = '''            resolved = _resolve_basic_instrument(master, dataset)
            if not resolved:
                st.error(f"Unable to resolve {dataset}.")
                st.stop()

            sid, segment, instrument = resolved
'''
new = '''            if dataset == "NIFTY Futures":
                resolved = _resolve_historical_nifty_future(master, year, quarter)
            else:
                resolved = _resolve_basic_instrument(master, dataset)
            if not resolved:
                st.error(f"Unable to resolve {dataset} for {year} Q{quarter}.")
                st.stop()

            sid, segment, instrument = resolved
'''
text = text.replace(old, new, 1)

# Also make the Data Vault date range use inclusive quarter end without relying
# on timestamp precision unsupported by the API.
text = text.replace(
    'end = period.end_time\n    chunks = []',
    'end = period.end_time.floor("s")\n    chunks = []',
    1
)

# Ensure zipfile is available in the loaded source.
if 'import zipfile' not in text[:5000]:
    text = text.replace('import time\n', 'import time\nimport zipfile\n', 1)

# Execute the patched source.
exec(compile(text, "friday_app.py", "exec"), globals(), globals())
