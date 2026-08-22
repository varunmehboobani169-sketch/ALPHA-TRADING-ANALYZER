# ALPHA ANALYZER FINAL MCX RESTORE

This version restores the MCX data-fetching path from V16, the last version that
showed MCX prices correctly.

Only two changes were made to the MCX section:
- Add live LTP coverage count.
- Display Entry and SL from the existing P&F result.

MCX data calls, contract selection, daily filter and intraday/positional logic
are otherwise unchanged from V16.

MCX visible columns:
Script | LTP | Bias | Entry | SL | Trade Recommendation
