# ALPHA ANALYZER V13 — Last Daily Column Must Be Anchor

NSE Intraday:
- First filter every F&O stock using the latest DAILY cash/spot P&F.
- 0.25% box, 3-box reversal, daily close.
- The LAST daily P&F column itself must be the Anchor:
  - X column with >15 boxes = bullish candidate.
  - O column with >15 boxes = bearish candidate.
- If the last daily column is not >15 boxes, the stock is excluded.
- No OI, sector, SMA, or other intraday filters.
- Then run 0.15% / 3-box / 1-minute cash P&F.
- BUY only when intraday DTB agrees with daily bullish Anchor.
- SELL only when intraday DBS agrees with daily bearish Anchor.
