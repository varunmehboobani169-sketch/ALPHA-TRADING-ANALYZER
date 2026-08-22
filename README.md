# ALPHA ANALYZER V15

Functional architecture before visual redesign.

Pages:
- Market Overview — 3 minute refresh
- NSE Intraday P&F — 1 minute refresh
- NSE Positional P&F — 15 minute refresh
- Bullish Stocks — 3 minute refresh (select Intraday/Positional)
- Bearish Stocks — 3 minute refresh (select Intraday/Positional)
- Sector Breadth — 15 minute refresh
- MCX Intraday — 1 minute refresh
- MCX Positional — 15 minute refresh
- Diagnostics — manual

NSE:
- P&F uses CASH/EQUITY price only.
- Universe = stocks that have active NSE FUTSTK contracts.
- Futures are used only for OI confirmation.
- All unique F&O stocks are scanned.

MCX:
- Positional: 0.25% / 3 box / daily close.
- Intraday: daily 0.25% direction filter + 0.15% / 3 box / 1-minute entry.
- OI is secondary.

Credentials:
- Client code and access token persist in Streamlit session state.
- Do not put access token in GitHub.
