# ALPHA ANALYZER V26 — NSE CASH DATA FIX

The screenshot showed DATA ERROR for every NSE stock. The likely root cause was
mapping the futures symbol to the wrong NSE cash security ID.

V26 fixes this by:
- Loading Dhan's detailed instrument master.
- Using FUTSTK `UNDERLYING_SECURITY_ID` directly as the NSE cash security ID.
- Falling back to exact cash-symbol matching only when the underlying ID is absent.
- Keeping NSETEST instruments excluded.
- Retaining the existing P&F, OI, sector and star logic.

Diagnostics now reports how many NSE FUTSTK rows have an underlying_security_id.
