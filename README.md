# ALPHA ANALYZER — POSITIONAL OI + SECTOR CONFIRMATION FIX

Fixes the sector-confirmation layer so it can actually calculate sector breadth.

Positional logic:
- P&F decides all valid LONG/SHORT trades.
- OI confirmation can add ★.
- Sector confirmation can add ★.
- Both confirmations can produce ★★.
- No confirmation never removes the P&F trade.

The Sector column is backend-only and is not displayed to the client.
Intraday remains unchanged.
