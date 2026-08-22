# ALPHA ANALYZER V6 — Same-Contract Futures OI

OI is now calculated directly from the exact active futures contract selected for each stock.

For the same contract:
- Current OI
- Previous OI
- Change in OI
- Change in OI %
- Futures price change %
- OI interpretation

Classification:
- Price up + OI up = LONG BUILDUP
- Price down + OI up = SHORT BUILDUP
- Price up + OI down = SHORT COVERING
- Price down + OI down = LONG UNWINDING

OI can award the OI star when it confirms the P&F direction.
P&F remains the actual entry trigger.
If OI retrieval fails, P&F still works and the row shows OI UNAVAILABLE.
