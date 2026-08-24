# ALPHA ANALYZER — V12

Added:
- Universal P&F/structure-based dynamic SL across all trade types:
  Applies to NSE Intraday, NSE Positional, MCX Intraday and MCX Positional.
  LONG SL trails upward to the latest more-favorable DBS/structural support.
  SHORT SL trails downward to the latest more-favorable DTB/structural resistance.
  SL never moves backwards.
  SL never moves backward.
- Dynamic SL movements are timestamped and counted.
- Fresh Trades module for all trades opened today.
- Actual detected LTP is stored as Entry; original P&F breakout is stored as Signal Level.
- Persistent SQLite trade history separated by client code.
- Trade Logs module with a trading-date selector for day-wise performance.
- Selected-day CSV report plus today's quick report download.
- Existing notifications, sounds, IST timestamps, NSE/MCX/Option Seller, sector and RS functionality retained.
