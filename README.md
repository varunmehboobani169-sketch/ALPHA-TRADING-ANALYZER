# ALPHA ANALYZER — SIMPLE OPTION SELLER

Simple live decision logic:

1. IV
   - Current IV versus the session opening IV.
   - Sharp IV increase -> WAIT / risk.

2. OI
   - Overall OI gives support and resistance.
   - OI change gives fresh one-sided buildup.
   - Heavy call buildup can favor SELL PUT.
   - Heavy put buildup can favor SELL CALL.

3. Expected range
   - ATM CE premium + ATM PE premium.
   - Compare expected range with OI support/resistance.

4. Output
   - SELL STRADDLE
   - SELL PUT
   - SELL CALL
   - WAIT

Analysis remains restricted to ATM +/- 20 strikes.
