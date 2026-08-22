# ALPHA ANALYZER V11

Critical data-pipeline fixes:
- Correctly maps Dhan's compact instrument master `SEM_SMST_SECURITY_ID`.
- Builds the underlying symbol from `SEM_TRADING_SYMBOL` (e.g. RELIANCE-Aug2026-FUT -> RELIANCE).
- Filters to active/future contracts and chooses the nearest expiry per underlying.
- Handles historical timestamps defensively.
- Adds visible NSE/MCX active-futures counts.
- Adds Diagnostics quick-test buttons for one NSE future and one MCX future.
- Shows actual API exceptions instead of silently hiding them.

Replace app.py and requirements.txt in GitHub. Keep the access token out of GitHub.
