# ALPHA ANALYZER — V9 DESIGN 2 + FRESH TRADES

This is the V9 build of Alpha Analyzer based on the previous V8 application.

## V9 changes
- Design 2 dashboard visual direction for the Market Overview.
- New **Fresh Trades** module.
- Every newly opened analyzer trade is automatically logged with:
  - Date
  - First-seen IST time
  - Symbol
  - Module
  - Mode
  - Direction
  - Market price at first detection
  - Signal entry
  - Stop loss
  - Active/Closed status
- Once a trade is logged, it remains in the day's Fresh Trade ledger even after the trade closes.
- Daily ledger is stored as `alpha_data/fresh_trades_YYYY-MM-DD.csv`.
- A CSV download is available from the Fresh Trades module.

## Preserved V8 functionality
- Global option warning monitor
- Background warning notifications/sound
- Trade logging and active/completed trade monitoring
- IST timestamps
- NSE / MCX / Option Seller / Market / RS / Sector modules
- Existing P&F, OI, option-chain and confirmation logic

## Files
- `app.py` — Streamlit application
- `requirements.txt` — Python dependencies
- `README.md` — project notes

## Important persistence note
The Fresh Trade CSV is local to the Streamlit runtime. It is designed to persist across normal Streamlit reruns, but cloud/container restarts or redeployments can remove local files. For permanent history across restarts, use a persistent database or external storage.
