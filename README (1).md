# ALPHA ANALYZER V22 — MCX Entry/SL Fix

MCX has been rebuilt as a self-contained module.

Fix:
- Removes the undefined `entry` variable that caused every MCX row to fall into DATA ERROR.
- Entry is explicitly taken from P&F `entry_level`.
- SL is taken from P&F `sl`, with a structural fallback from the previous opposite column.
- Intraday uses the daily MCX Anchor filter once per day.
- Positional uses 0.25% / 3-box / daily P&F.
- Displays separate Bullish, Bearish and Setup dashboards.

MCX dashboard columns:
Script | LTP | Bias | Entry | SL | Recommendation
