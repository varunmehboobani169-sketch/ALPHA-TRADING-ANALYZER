# ALPHA ANALYZER V11 — CLEAN TRADE LEDGER

Changes from the working V10 base:
- One Fresh Trade per Symbol + Module + Mode + Date.
- Existing duplicate ledger rows are automatically collapsed.
- Active duplicate rows are preferred over stale duplicate rows.
- Entry Time is immutable and shown as the first column.
- Legacy rows use First Logged/Open date only when available; otherwise the UI
  explicitly shows TIME UNAVAILABLE (LEGACY) rather than inventing a time.
- Simplified visible Fresh Trades and Trade Logs columns.
- CSV downloads retain the full detailed ledger.
- Dynamic SL logic and trading strategy are otherwise preserved.


## Deployment
Upload `app.py`, `requirements.txt`, and `alpha_analyzer_logo.png` to Streamlit.
`README.md` is optional.
