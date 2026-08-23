# ALPHA ANALYZER — GITHUB DEPLOY BUILD

Main entry point:
- app.py

Required:
- requirements.txt

Client-facing modules:
1. Option Seller
2. Intraday
3. Positional
4. Market Overview

MCX is not exposed in the client navigation.

POSitional trade logic:
- Active DTB + latest X = LONG
- Active DBS + latest O = SHORT
- Anything else = Sideways / No Position
- Active LONG rows are highlighted green
- Active SHORT rows are highlighted red
- Entry and SL are shown

Intraday:
- Daily eligibility is built once per trading day.
- Only instruments passing the daily eligibility gate are scanned intraday.
- Intraday signals are shown with Entry and SL.

Option Seller:
- NIFTY
- BANKNIFTY
- SENSEX
- Intraday / Positional
- recommendation, ATM, premium, IV, expected range,
  support/resistance and option-chain monitor.

Deploy with Streamlit using:
app.py
