# ALPHA ANALYZER V11 — NSE Intraday + MCX Fix

Fixed:
- NSE Intraday no longer references `fut` before it is created.
- NSE Intraday retains the daily 0.25% P&F direction filter, 1-minute P&F, nearest contract only, and no intraday OI.
- MCX has explicit daily/intraday historical requests using `MCX_COMM/FUTCOM`.
- MCX shows live LTP and returned historical candle count.
- MCX errors now expose full API details in an expandable panel.
- No ZIP is required; this is a normal folder with individual files.
