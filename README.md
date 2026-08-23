# ALPHA ANALYZER — FINAL SECTOR + NIFTY 50 RS FIX

Fixed the RS Matrix runtime error occurring when requesting NIFTY historical data.

Changes:
- NIFTY/BANKNIFTY Security IDs are resolved from Dhan's detailed instrument master.
- The Dhan master uses segment `I` for the `IDX_I` index segment.
- NIFTY 50 resolves to its current master Security ID instead of relying on a
  hard-coded number.
- Daily index requests use `exchangeSegment=IDX_I`, `instrument=INDEX`.
- For index daily history, `timeframe=1D` is included to handle Data API
  deployments that enforce the explicit daily timeframe.
- RS Matrix remains NIFTY 50 only and manual-refresh.
- Sector Analysis remains daily close-only, 1% box, 3-box reversal, manual-refresh.
- Other modules remain unchanged.

Dhan documentation references:
- Instrument List: Dhan Security IDs come from the instrument master.
- Annexure: `IDX_I` is the Index Value segment and `INDEX` is the instrument.
- Historical Data: `/charts/historical` is the daily OHLC endpoint.
