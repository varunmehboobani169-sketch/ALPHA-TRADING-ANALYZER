# ALPHA ANALYZER — DHAN V2 OPTION SELLER V5

Added a client-facing expiry selector to the Option Seller module.

Behavior:
- Fetches the active expiry list from Dhan.
- Shows all available expiries in a dropdown.
- Defaults to:
  - Intraday: nearest active expiry.
  - Positional: preferred current-month expiry.
- Changing the expiry reloads the option chain for that expiry.

The rest of the Dhan v2 option-chain logic is unchanged.
