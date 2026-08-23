# ALPHA ANALYZER V18 — MCX Running Positional Positions

MCX Positional now reports the CURRENTLY RUNNING daily P&F position instead
of waiting for a fresh DTB/DBS.

Rules:
- Latest completed daily column X -> 🟢 LONG position is running.
- Latest completed daily column O -> 🔴 SHORT position is running.
- Entry = latest 3-box reversal level that started the current column.
- SL = previous opposite P&F column extreme.
- No new trade is invented from current LTP.
- MCX data fetching is otherwise unchanged.

Example:
GOLD latest daily column = X
-> LONG
-> Entry = most recent 3-box reversal price
-> SL = previous O-column low
