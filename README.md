# ALPHA ANALYZER V20 — Entry Display Fix

The previous version calculated Entry correctly but did not include the Entry
column in the Long/Short/Setup dashboard display.

V20 fixes this.

Every NSE and MCX dashboard now shows:
Script | LTP | Bias | Entry | SL | Recommendation

Entry:
- DTB / bullish setup = Anchor high
- DBS / bearish setup = Anchor low

SL:
- Existing P&F structural stop
- Fallback to previous opposite P&F column extreme when needed
