# Positional Option Selling — Historical Data

This folder is the repository-side staging area for the Positional Option Selling research tab.

Recommended historical files:
- nifty option data 2022.csv
- nifty 2024.csv
- nifty december 2024.csv
- nifty 2025.csv
- nifty 2026.csv

## Preferred upload method
Use the **Positional Option Selling** dashboard and upload **one ZIP containing all NIFTY option CSV files**. The dashboard extracts every CSV, normalizes the fields, combines the files, and retains the loaded dataset in the Streamlit session so it survives normal page reruns.

A direct CSV upload is also supported as a fallback.

For a defensible positional backtest, the data should contain:
- Date or Datetime
- Expiry
- Strike
- CE/PE (or equivalent option-type field)
- Close/LTP
- Spot/Underlying price is strongly recommended

Do not alter the raw data to fit the engine. The loader is designed to normalize common column-name variations and will surface files that fail validation rather than silently discarding them.

Important: Streamlit session retention is runtime/session persistence, not permanent storage in GitHub. The raw ZIP/CSV files should remain available to the dashboard session or be staged in this `data/` folder for repeatable runs.
