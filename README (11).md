# ALPHA ANALYZER — Client-Safe V3 Option Seller

Added a functional Option Seller module for:
- NIFTY
- BANKNIFTY
- SENSEX

User chooses:
- Intraday
- Positional

The dashboard evaluates:
- Live index spot
- ATM strike
- ATM call/put premium
- ATM IV
- Expected range from ATM straddle
- Highest Call OI resistance
- Highest Put OI support

Recommendations:
- SELL STRADDLE
- CAUTION
- DON'T SELL

The client-facing UI intentionally does not disclose internal trading methodology.

This is the first live option-chain implementation; historical IV-open-vs-previous-close
comparison and continuous IV/OI alerting can be added as the next layer.
