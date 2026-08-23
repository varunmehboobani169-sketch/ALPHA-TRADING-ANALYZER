# ALPHA ANALYZER — CLIENT INTRADAY ANCHOR BUILD

Intraday eligibility is now:

1. Daily cash/spot close data.
2. 0.25% box size.
3. 3-box reversal.
4. The latest completed daily column itself must be an Anchor with >15 boxes.
   - X column >15 boxes = Bullish intraday candidate.
   - O column >15 boxes = Bearish intraday candidate.
5. Stocks without such a qualifying Anchor are NOT scanned intraday.
6. The eligible universe is built once per trading day and reused on each 1-minute refresh.
7. Intraday scan:
   - 0.15% box
   - 3-box reversal
   - completed 1-minute closes
   - BUY only when bullish daily candidate + intraday BUY setup + price above 10-period trend filter
   - SELL only when bearish daily candidate + intraday SELL setup + price below 10-period trend filter

No OI or sector filter is used.

Client-facing UI remains methodology-safe.
MCX remains removed.
Option Seller remains available.
