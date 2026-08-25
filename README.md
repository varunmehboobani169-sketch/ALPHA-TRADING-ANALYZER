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


## Option Seller UI
Detailed IV/OI/chain/diagnostic calculations are backend-only. The client sees only the decision, ATM, premium, mode, active status, entry/current premium, P&L and concise risk status.


## ALPHA PRO SELLER
Separate NIFTY intraday theta-decay strategy module.

Rules:
- Freeze NIFTY ATM using the 09:16 index price for the whole day.
- Freeze the exact ATM CE + PE contracts.
- Build the synthetic straddle from 1-minute option closes.
- 2% P&F box size, 3-box reversal.
- Fresh X-to-O reversal = SELL ATM straddle.
- Structural SL = preceding X-column high.
- Maximum 2 trades per day.
- 4-box X reversal or structural SL = exit.
- 15:05 IST = mandatory hard exit.

The existing general Option Seller module remains separate.


## P&F Fusion Matrix
The existing Matrix has been upgraded using the source-inspired scoring concept:
- Performance score: DTB +2, DTB retracement +1, DBS -2, DBS retracement -1, otherwise 0.
- Ranking score: current active P&F column box magnitude; X positive, O negative.
- Separate Price and Relative Strength scores.
- Total Performance/Ranking = Price + RS.
- Net Performance/Ranking = RS - Price.
- User can rank any numeric column High → Low or Low → High.
- Detailed per-box scores are available in an expander.


## Display Cleanup
Core P&F calculations remain in the backend, but the client-facing Matrix no
longer displays "P&F" terminology or the detailed per-box score expander.


## Matrix Enhancements
- Four box sizes are user-configurable from the Matrix screen.
- Default values remain 3%, 2%, 1%, 0.25%.
- The latest 0.25% price-chart DTB with a perfect +2 score is highlighted green.
- The latest 0.25% price-chart DBS with a perfect -2 score is highlighted red.
- Core calculations remain backend-driven.
