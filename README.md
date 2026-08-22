# ALPHA ANALYZER V15

NSE Intraday:
- The daily 0.25% / 3-box P&F Anchor filter is built once per trading day and stored in Streamlit session state.
- It is NOT recalculated on every 1-minute refresh.
- Only stocks whose last daily P&F column is an Anchor (>15 X boxes or >15 O boxes) are retained.
- Retained stocks are rescanned every minute using 0.15% / 3-box cash P&F.
- Existing intraday 10-SMA filter remains: BUY above SMA10, SELL below SMA10.
- P&F DTB/DBS remains the entry trigger.
- BUY rows are highlighted green; SELL rows are highlighted red.
- A manual Rebuild Daily Filter button is provided.

No intraday OI or sector analysis.
