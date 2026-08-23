# ALPHA ANALYZER V25 — MCX Historical Diagnostic

MCX live LTP is confirmed to work. V25 adds a direct single-contract
historical-data test so we can see the actual Dhan response.

Use the MCX page:
1. Open "MCX historical-data diagnostic".
2. Select GOLD or GOLDM.
3. Click "Test Historical Data".
4. The app shows:
   - security ID
   - expiry
   - exact endpoint
   - exact payload
   - HTTP/API error if any
   - response keys/preview if successful

This diagnostic does not change the trading logic.

Dhan's current documentation lists MCX as `MCX_COMM` and commodity futures
as `FUTCOM`, and supports both `/charts/historical` and `/charts/intraday`
historical endpoints. 
