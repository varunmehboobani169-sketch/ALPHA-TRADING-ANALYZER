# ALPHA ANALYZER V11 — CLEAN TRADE LEDGER

Changes from the working V10 base:
- One Fresh Trade per Symbol + Module + Mode + Date.
- Existing duplicate ledger rows are automatically collapsed.
- Active duplicate rows are preferred over stale duplicate rows.
- Entry Time is immutable and shown as the first column.
- Legacy rows use First Logged/Open date only when available; otherwise the UI
  explicitly shows TIME UNAVAILABLE (LEGACY) rather than inventing a time.
- Simplified visible Fresh Trades and Trade Logs columns.
- CSV downloads retain the full detailed ledger.
- Dynamic SL logic and trading strategy are otherwise preserved.


## Deployment
Upload `app.py`, `requirements.txt`, and `alpha_analyzer_logo.png` to Streamlit.
`README.md` is optional.


## Entry Time rule
Fresh Trade `Entry Time` uses Dhan market-data `last_trade_time` (LTT) from
the quote feed when the signal is first detected. The dashboard refresh time
is not used as the primary timestamp. NSE fresh entries are restricted to
09:15–15:40 IST.


## V17 Trade Report Fix
- Canonicalizes old trade reports before display/download.
- Exactly one trade per symbol per day.
- Removes invalid NSE rows outside 09:15–15:40.
- Entry Time never falls back to dashboard refresh time.
- Client-facing Trade Logs remain: Entry Time, Symbol, Mode, Direction,
  Trade Price, Initial SL, Dynamic SL, Status.


## V18 Entry Price / LTP
- `Trade Price` is the immutable actual entry price captured at signal detection.
- `LTP` is the latest live market price and updates as the dashboard refreshes.
- NSE/MCX quote `last_price` is preferred over cached batch LTP when available.
- Client-facing Fresh Trades and Trade Logs show both Entry Price and LTP.


## V19 Trade-logging safety
- A Fresh Trade is created only if Dhan exchange timestamp is valid.
- NSE entries are strictly 09:15–15:40 IST.
- A complete trade must have a valid Initial SL.
- Entry Price is captured once from live quote price.
- LTP is stored separately and updates with the live market.
- Missing exchange time/SL will prevent the trade from being logged rather than
  creating a false trade.
- One symbol/day remains the hard duplicate key.

## Manual upload
Upload/replace `app.py` and `requirements.txt`. Keep the existing `alpha_analyzer_logo.png` if already present. `README.md` is optional.


## Positional Table Entry Date/Time
The NSE Positional LONG/SHORT tables now include `Entry Date/Time`.
This value is taken from the original immutable trade record created when the
trade was triggered, not from the current dashboard refresh.


## UI Label
The main NSE `Intraday` module is displayed as **Momentum**.
Internally, it still runs with `mode="Intraday"` so the existing signal logic,
market-hours rules, trade logging, and dynamic SL logic are unchanged.


## Final Positional Table
The NSE Positional LONG/SHORT table does not display Entry Date/Time.
Entry Time remains available in Fresh Trades and Trade Logs.


## KeyError Fix
Removed the stale `Entry Date/Time` column reference from the NSE Positional
LONG/SHORT display. The Positional table is now exactly:
Script, LTP, Bias, Entry, SL, Recommendation.


## Momentum Sticky Trade State
A logged Momentum trade stays visible in the active LONG/SHORT table until an
actual exit is recorded. A temporary loss of the live signal does not hide or
create a replacement trade. Duplicate prevention remains one symbol per day.
