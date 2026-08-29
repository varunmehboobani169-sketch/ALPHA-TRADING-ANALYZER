# Dhan Options Data Downloader

A dedicated Streamlit dashboard for downloading year-wise NSE/BSE options data from DhanHQ v2.

## What it downloads
- NSE and BSE index options: ATM-10 through ATM+10.
- NSE and BSE F&O stocks: ATM-3 through ATM+3, matching Dhan's documented rolling expired-options limit for non-index contracts.
- CE and PE legs.
- 1-minute OHLC, IV, volume, OI, actual strike, underlying spot, requested ATM-relative strike, expiry flag/code and timestamps.
- One CSV per calendar year inside a ZIP package.

## Important Dhan limits
Dhan's `/charts/rollingoption` API supports historical expired options on a rolling basis for up to five years and accepts at most 30 days in one request. Index options support ATM±10; other contracts support ATM±3. The app therefore splits every selected year into 30-day requests and assembles the result by year.

## Universe discovery
The app downloads Dhan's detailed instrument master and builds the underlying universe from option contracts, preserving exchange, segment, underlying security ID, symbol, instrument family, expiry information and contract counts.

## Authentication
Enter the Dhan Client ID and Access Token in the Streamlit sidebar. Credentials are held only in the app session and are never written to the repository. Dhan documents access tokens and API-key based authentication separately; use the current token/authentication method available for your account.

## Run
```bash
pip install -r requirements.txt
streamlit run app_new.py
```

See the DhanHQ v2 documentation for the authoritative API contract:
https://dhanhq.co/docs/v2/

The current app is intentionally focused on data acquisition. Strategy research, ML, backtesting and the previous FRIDAY dashboard have been removed from the active data-downloader workflow.