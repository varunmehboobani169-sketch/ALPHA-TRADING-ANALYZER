# ALPHA ANALYZER V24 — MCX Historical Data Fallback

MCX LTP was already working. V24 isolates the problem to historical/P&F data.

Changes:
- Uses a dedicated MCX historical fetch.
- Tries the standard FUTCOM request with expiryCode=0.
- If rejected, retries the same request without expiryCode.
- Supports both daily and 1-minute MCX data.
- Shows live LTP coverage separately.
- Shows the number of historical candles returned.
- If historical data still fails, the actual error is shown in an MCX diagnostics table.

Trading logic is not changed.
