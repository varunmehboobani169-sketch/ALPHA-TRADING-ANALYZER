# ALPHA ANALYZER FINAL BASELINE

This final version restores the last confirmed MCX-working data path from V16.
Only the MCX display layer was extended with Entry and SL.

NSE:
- Intraday: morning daily 0.25% last-column Anchor filter -> 0.15%/3-box/1-minute P&F + intraday 10-SMA.
- Positional: 0.25%/3-box/daily P&F.
- No OI or Sector.

MCX:
- Intraday: daily 0.25% Anchor filter -> 0.15%/3-box/1-minute P&F.
- Positional: 0.25%/3-box/daily P&F.
- Uses the V16 MCX data-fetching and contract-selection logic unchanged.
- Adds Entry and SL to the dashboard only.

Displays:
- Separate Long/Bullish
- Separate Short/Bearish
- Separate Setup sections
- Columns: Script | LTP | Bias | Entry | SL | Recommendation
