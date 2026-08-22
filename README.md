# ALPHA ANALYZER V17 — Long / Short / Setup Dashboards + SL

Each NSE and MCX module now has:
- 🟢 LONG TRADES
- 🔴 SHORT TRADES
- 🟡 SETUPS FORMING
- All scanned instruments

Each trade row displays:
Script | LTP | Bias | SL | Recommendation

SL is taken from the P&F structure:
- Long = pullback O-column low
- Short = pullback X-column high

NSE Intraday:
- Daily 0.25% Anchor filter remains once per trading day.
- Intraday 0.15% P&F + existing 10-SMA filter.
- No OI or sector analysis.

NSE Positional:
- 0.25% / 3-box / daily P&F.

MCX:
- Intraday: daily 0.25% direction filter -> 0.15% intraday P&F.
- Positional: 0.25% / 3-box / daily P&F.
