# ALPHA ANALYZER — POSITIONAL STAR CONFIRMATION

Positional:
- Active DTB/DBS remains the only trade trigger.
- F&O futures OI confirmation is applied only after a positional trade exists.
- A `★` is added before the Script name when the OI confirmation is strong.
- The star never creates a trade; it only marks a higher-conviction P&F trade.

Intraday:
- No F&O OI confirmation scan.
- Existing 1-minute intraday logic remains unchanged.

Client display remains:
Script | LTP | Bias | Entry | SL | Recommendation

Pattern/OI confirmation/OI change remain backend-only.
