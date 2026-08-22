# ALPHA ANALYZER V9 — Nearest Futures Only

NSE rules:
- Cash/spot price is used for all P&F calculations.
- Daily 0.25%/3-box P&F filters the intraday universe.
- Intraday 0.15%/3-box/1-minute P&F is run only on daily-direction stocks.
- For each stock, ONLY the nearest active futures contract is retained.
- 2nd and 3rd futures expiries are never scanned.
- Futures OI confirmation uses that same nearest contract.

The contract-selection function is robust to missing expiry_date fields and
never crashes the entire scan because of a missing expiry column.
