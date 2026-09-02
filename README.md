# NIFTY Options Data Collector

A clean Streamlit dashboard dedicated to collecting NIFTY option-chain data for research.

## Collection universe
- NIFTY nearest active option expiry (or manually selected expiry)
- ATM determined from the NIFTY underlying using a 50-point strike step
- ATM−20 through ATM+20 = 41 strikes
- Both CE and PE = 82 option rows per snapshot

## Collected fields
Capture timestamp, trading date/time, expiry, NIFTY spot, ATM, strike offset, strike, option type, security ID, LTP, previous close, price change, OI, previous OI, OI change, volume, IV, Delta, Theta, Gamma, Vega, best bid/ask and quantities.

## Dashboard
The Dhan login/token panel is permanently on the left sidebar. After connection, the dashboard lets you select expiry, set the polling interval, start/stop collection, inspect the complete ATM ±20 chain, monitor collection quality, and export the collected dataset as CSV.

## Dhan API
The collector uses DhanHQ v2 Option Chain and Expiry List endpoints. Dhan documents the NIFTY underlying security ID as 13 for the example Option Chain request, and the Option Chain API supplies LTP, OI, IV, Greeks, volume and bid/ask data across strikes. The documented Option Chain rate limit is one unique request every 3 seconds, so the dashboard enforces a minimum 3-second polling interval.

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```

Keep the Dhan Access Token private and never commit it to GitHub.
