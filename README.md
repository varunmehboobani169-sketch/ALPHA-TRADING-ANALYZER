# Dhan NSE / BSE Options Data Downloader

The repository's default Streamlit application is a dedicated DhanHQ v2 historical options-data downloader.

## Data coverage
- NSE and BSE index options: ATM-10 through ATM+10.
- NSE and BSE F&O stock options: ATM-3 through ATM+3, matching Dhan's documented rolling-options range for non-index contracts.
- CE and PE legs.
- 1-minute OHLC, IV, volume, OI, actual strike, underlying spot, requested ATM-relative strike, expiry metadata and timestamps.
- One CSV per calendar year inside a downloadable ZIP.

## Year-wise downloads
Enter years such as `2022-2026`, `2024,2025`, or a single year. Each selected calendar year is exported separately inside the final ZIP.

The downloader automatically splits historical requests into 30-day windows to stay within the Dhan rolling-options request limit.

## Universe discovery
The app downloads Dhan's detailed instrument master and builds the option-underlying universe from the master itself, preserving exchange, segment, underlying security ID, symbol, instrument family, expiry information and contract counts.

## Authentication
Enter the Dhan Client ID and Access Token in the sidebar. Credentials remain in the Streamlit session and are not written to the repository.

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```

`app.py` is the only Streamlit application entry point in the current repository. The legacy FRIDAY/Vega application and its multipage files/workflows have been removed from the current repository tree.

Authoritative API reference: https://dhanhq.co/docs/v2/
