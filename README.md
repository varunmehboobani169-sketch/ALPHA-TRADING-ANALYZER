# ALPHA ANALYZER V18

NSE and MCX modules now have three separate dashboards:
- 🟢 BULLISH / LONG TRADES
- 🔴 BEARISH / SHORT TRADES
- 🟡 SETUPS FORMING

Every row contains:
Script | LTP | Bias | SL | Recommendation

SL is always populated when a completed P&F pullback/reversal structure provides a structural level; a fallback uses the previous opposite P&F column extreme.

NSE Intraday:
- Morning daily 0.25% last-column Anchor filter.
- 0.15% / 3-box / 1-minute P&F.
- Intraday 10-SMA filter remains.
- No OI or sector analysis.

NSE Positional:
- 0.25% / 3-box / daily P&F.

MCX Intraday:
- Daily 0.25% last-column Anchor filter once per day.
- 0.15% / 3-box / 1-minute P&F.

MCX Positional:
- 0.25% / 3-box / daily P&F.
