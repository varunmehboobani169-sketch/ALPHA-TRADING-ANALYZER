# Positional Option Selling — Historical Data

Put the historical NIFTY option CSV files in this folder.

Recommended filenames include:
- nifty option data 2022.csv
- nifty 2024.csv
- nifty december 2024.csv
- nifty 2025.csv
- nifty 2026.csv

The dashboard can also read the same files through its upload control, so filenames do not have to match exactly.

For a defensible positional backtest, the data should contain:
- Date or Datetime
- Expiry
- Strike
- CE/PE (or equivalent option-type field)
- Close/LTP
- Spot/Underlying price is strongly recommended

Do not alter the raw data to fit the engine. The loader is designed to normalize common column-name variations.
