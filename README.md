# ALPHA ANALYZER FROM SCRATCH V3 — Futures Quote Fix

Fixes the ranking-version runtime error in the futures quote call.

Root cause:
- Dhan's `/marketfeed/ltp` endpoint returns LTP/ticker data.
- Futures OI is returned by `/marketfeed/quote`.
- The previous version incorrectly requested `/marketfeed/ltp` while expecting `oi`.

V3:
- Uses `/marketfeed/quote` for NSE futures LTP + OI.
- Chunks futures requests in groups of 500.
- Shows live futures quote coverage.
- Keeps the clean from-scratch NSE cash/P&F data path.
- P&F entry rules are unchanged.
