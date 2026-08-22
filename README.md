# ALPHA ANALYZER V16 — MCX Clean Display

MCX now uses the same simple display as NSE:

Script | LTP | Bias | Trade Recommendation

MCX Intraday:
- Daily 0.25% / 3-box P&F direction filter once per day.
- Only Bullish/Bearish commodities retained.
- 0.15% / 3-box / 1-minute P&F.
- Matching DTB/DBS gives BUY/SELL.

MCX Positional:
- 0.25% / 3-box / daily close.
- DTB = BUY; DBS = SELL.

BUY rows are green and SELL rows are red.
The old raw P&F JSON/debug output is removed.
