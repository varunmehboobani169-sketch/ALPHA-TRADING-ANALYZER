# ALPHA ANALYZER — P&F TRADES + OPTIONAL OI STAR

Exact positional behavior:

1. P&F is the ONLY trade gate.
   - Active DTB + X -> LONG
   - Active DBS + O -> SHORT
   - Everything else -> no directional trade.

2. Every P&F-approved trade is displayed.
   Example: 13 P&F Long trades -> all 13 are shown.

3. F&O OI is checked only as an additional confirmation.
   - LONG + Long Buildup -> add ★
   - SHORT + Short Buildup -> add ★

4. OI NEVER removes, rejects, or changes a P&F trade.
   If OI is unavailable or does not confirm, the trade remains in the list
   without a star.

5. Intraday does not run this positional OI confirmation layer.

Client display remains:
Script | LTP | Bias | Entry | SL | Recommendation

Only the ★ is visible; the detailed OI confirmation stays backend-only.
