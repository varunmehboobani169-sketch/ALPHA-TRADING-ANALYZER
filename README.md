# ALPHA ANALYZER V14 — Intraday 10-SMA Filter

NSE Intraday:
1. Last completed daily P&F column must be an Anchor:
   X >15 boxes = bullish; O >15 boxes = bearish.
2. Scan 0.15% / 3-box / 1-minute cash P&F.
3. BUY only on matching DTB AND Spot LTP > intraday 10-SMA.
4. SELL only on matching DBS AND Spot LTP < intraday 10-SMA.
5. No OI.
6. No sector analysis.
7. P&F remains the entry trigger; 10-SMA is a filter only.

Display remains: Script | LTP | Bias | Intraday Trade Recommendation.
