# ALPHA ANALYZER V19 — Positional Bias -> Intraday P&F

NSE Intraday is restructured:

1. Positional bias:
   - 0.25% box
   - 3-box reversal
   - daily close
   - latest completed X column = Bullish bias
   - latest completed O column = Bearish bias
   - bias is calculated once per trading day

2. Intraday entry:
   - 0.15% box
   - 3-box reversal
   - completed 1-minute closes
   - BUY only when intraday DTB agrees with Bullish positional bias
     AND price is above intraday 10-SMA
   - SELL only when intraday DBS agrees with Bearish positional bias
     AND price is below intraday 10-SMA

No OI or Sector filter.

Positional NSE display also uses the latest daily P&F X/O direction
as the running Long/Short bias.
