# ALPHA ANALYZER — OPTION SELLER STRIKE FIX

Fixed the Option Seller runtime error:
`too many values to unpack (expected 3, got 7)`

Cause:
The strategy-suggestion function returns a dictionary with multiple fields,
but the dashboard was trying to unpack it into three variables.

Fix:
The dashboard now reads the returned strategy dictionary explicitly and keeps
the exact strike/premium suggestions.

All other option-chain, expiry, IV/OI and alert logic is unchanged.
