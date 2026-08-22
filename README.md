# ALPHA ANALYZER FROM SCRATCH V1

A clean rebuild with only the essential data path.

NSE:
- Discover FUTSTK contracts.
- Use each future's UNDERLYING_SECURITY_ID as the cash security ID.
- Pull live NSE cash LTP in one batch via /marketfeed/ltp.
- Pull cash historical data via /charts/historical or /charts/intraday.
- Build the exact 3-column P&F pattern:
  >15 Anchor -> 1–5 opposite-column pullback -> third column -> DTB/DBS.
- Signal-only; no order execution.

MCX:
- Futures-based P&F.
- Positional 0.25%/3-box/daily.
- Intraday 0.15%/3-box/1-minute.

This rebuild intentionally omits sector, stars, OI ranking, and appearance enhancements until the base cash-data pipeline is verified.
