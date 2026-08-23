# ALPHA ANALYZER — DHAN V2 OPTION SELLER

DhanHQ v2-compliant option-selling module.

Verified against the official Dhan v2 docs:
- `/optionchain`
- `/optionchain/expirylist`
- `IDX_I` as the Index Value segment
- `UnderlyingScrip` resolved from the instrument master
- `Expiry` selected from the active expiry list
- `previous_oi` used to calculate current OI change
- 3-second option-chain request cache

Client functions:
- NIFTY
- BANKNIFTY
- SENSEX
- Intraday / Positional
- Current ATM straddle recommendation
- Current ATM IV
- Session-opening IV baseline
- Expected range
- OI support/resistance
- One-sided OI buildup alert
- Intraday IV expansion alert
- Spoken option alert on new risk state
- Full option-chain view

Important:
The opening IV shown is the first successful observation after market open
within the active Streamlit session. The Dhan Option Chain response provides
current IV and previous-day option price/OI, but does not expose previous-day
IV directly.
