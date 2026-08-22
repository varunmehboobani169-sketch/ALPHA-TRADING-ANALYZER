# ALPHA ANALYZER V27 — Futures Universe Error Fix

The screenshot showed a KeyError on `expiry_date` inside `nearest_fno`.
V27 fixes this by:
- Always creating an `expiry_date` column even if the detailed master omits it.
- Accepting alternate expiry field names.
- Parsing expiry before filtering/sorting.
- Falling back to the futures trading symbol when underlying_symbol is unavailable.
- Keeping the V26 underlying-security-ID cash mapping and all trading logic unchanged.

This is a data/universe bug fix only.
