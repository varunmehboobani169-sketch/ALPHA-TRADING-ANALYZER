# FRIDAY V3 — AI Strategist + Data Vault

FRIDAY now has two modules.

## AI Strategist

Upload multiple year-wise files in four groups:
- NIFTY Options — 1-minute
- NIFTY Futures — 15-minute OHLC
- NIFTY Spot — 15-minute OHLC
- India VIX

After preparation, FRIDAY automatically creates quarter-wise downloadable reports.

Each quarter includes:
- quarterly_summary.csv
- daily_analysis.csv
- decision_features.csv

You can download:
- selected quarterly reports
- all quarterly reports
- a later Master 3-Year Review

You can also upload previously downloaded quarterly ZIPs back into FRIDAY for the master review.

## Data Vault

Separate quarter-wise data downloader for:
- NIFTY Spot
- NIFTY Futures
- India VIX

Select:
- year
- quarter
- timeframe
- OI for futures where supported

Each quarter is downloaded automatically in safe historical chunks and can be downloaded as a CSV. Accumulated quarters can also be downloaded as a ZIP.

Dhan's current historical API supports 1/5/15/25/60-minute intraday candles, and intraday history can be requested up to 90 days at a time; the module therefore splits a quarter into multiple requests automatically. citeturn404852search0turn404852search3

This Data Vault is intended to create the exact quarter-wise NIFTY / Futures / VIX files needed for FRIDAY.
