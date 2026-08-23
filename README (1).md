# ALPHA ANALYZER — POSITIONAL + INTRADAY LOGIC

POSITIONAL:
- Daily cash/spot closes only.
- Current 0.25% structure with 3-box reversal.
- Latest completed X column = Bullish / LONG.
- Latest completed O column = Bearish / SHORT.
- Entry = latest 3-box reversal level that started the current column.
- SL = previous opposite column extreme.
- No requirement for a fresh breakout to keep showing the running position.

INTRADAY:
- Daily eligibility is calculated once per trading day.
- A stock is eligible only when the latest completed daily column is an Anchor >15 boxes.
  X >15 = Bullish candidate.
  O >15 = Bearish candidate.
- Only eligible stocks are scanned intraday.
- Intraday uses 0.15% / 3-box / 1-minute close data.
- BUY requires bullish daily eligibility + intraday BUY trigger + price above trend filter.
- SELL requires bearish daily eligibility + intraday SELL trigger + price below trend filter.
- No OI or sector filter.

Client-facing methodology labels are kept hidden.
MCX remains removed from the dashboard.
