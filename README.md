# ALPHA ANALYZER — MCX FUTURES FINAL

MCX is a futures-trading module only. No MCX option-selling module is included.

Same Dhan Client Code + Access Token are reused for NSE and MCX.

Supported:
GOLD, GOLDM, SILVER, SILVERM, CRUDEOIL, CRUDEOILM, NATURALGAS, NATURALGASMINI

POSITIONAL:
- Daily close-only
- 0.25% / 3-box
- Active DTB + X -> LONG
- Active DBS + O -> SHORT
- Every valid P&F trade is displayed
- Futures OI confirmation adds ★ only
- OI never removes a P&F trade
- Entry and SL displayed

INTRADAY:
- Daily >15-box eligibility gate
- 1-minute / 0.15% / 3-box
- Trend filter
- BUY / SELL / SETUP
- No OI confirmation scan every minute

Dhan:
- exchangeSegment = MCX_COMM
- instrument = FUTCOM
- same login as NSE
