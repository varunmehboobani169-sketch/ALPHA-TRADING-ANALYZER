# ALPHA ANALYZER — DHAN V2 OPTION SELLER V3

Fixes the live Option Seller data path.

Critical correction:
- Index LTP request uses `IDX_I`, the documented Dhan Index Value segment,
  instead of `NSE_IDX`.

The option-chain flow remains:
Index Security ID -> expiry list -> selected expiry -> option chain.

The data-status expander now shows the exact API/data error, which makes any
remaining Dhan entitlement or instrument-resolution issue directly visible.

Client modules remain:
- Option Seller
- Intraday
- Positional
- Market Overview

MCX remains excluded from the client navigation.
