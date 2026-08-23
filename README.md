# ALPHA ANALYZER — DHAN V2 OPTION SELLER V4

Dhan v2 verified index handling:

NIFTY = Security ID 13
BANKNIFTY = Security ID 25
SENSEX = Security ID 51

Underlying segment:
IDX_I

Option-chain flow:
1. Select the documented underlying Security ID.
2. Get active expiries from /optionchain/expirylist.
3. Choose the expiry based on Intraday / Positional.
4. Get /optionchain for that expiry.
5. Use the documented option-chain `last_price` as the underlying spot.

OI:
Change OI = `oi - previous_oi`.

The client module also shows:
- ATM premium
- ATM IV
- expected range
- OI support/resistance
- volatility risk alert
- one-sided OI buildup alert
- spoken option alert
- option-chain details

MCX remains removed from the client dashboard.
