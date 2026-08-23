# ALPHA ANALYZER — MCX OPTION SELLER

Added MCX Option Seller using the same simple framework as NSE.

MCX symbols:
GOLD, GOLDM, SILVER, SILVERM, CRUDEOIL, CRUDEOILM, NATURALGAS, NATURALGASMINI.

Dhan v2:
- MCX options use `MCX_COMM` as the underlying segment.
- The underlying is the nearest active `FUTCOM` contract Security ID.
- Active expiries are fetched from Dhan.
- Option-chain data is fetched for the selected expiry.
- Analysis is limited to ATM +/-20 available strikes.

Logic:
- Overall OI -> support/resistance.
- OI change -> one-sided buildup.
- ATM IV -> volatility monitoring.
- Expected range -> strategy context.
- SELL STRADDLE / SELL PUT / SELL CALL / WAIT.

Notifications:
- New IV risk state -> spoken alert.
- New one-sided OI buildup -> spoken alert.

Refresh:
- Intraday -> 1 minute.
- Positional -> 3 minutes.
