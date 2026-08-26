# FRIDAY — Autonomous Market Research Engine

FRIDAY is rebuilt from scratch around the agreed research specification.

## Research inputs
- NIFTY Options: 1-minute rolling expired-option surface, ATM-10 to ATM+10, CE/PE, OHLC, IV, OI, volume and option-source spot.
- NIFTY Spot: 15-minute OHLC/close.
- India VIX: 15-minute OHLC/close.

## Research pipeline
Raw audit → full option-surface feature factory → fruitfulness discovery → adaptive interactions → machine learning → holdout validation → downloadable research package.

The discovery stage uses only the chronological discovery segment. Validation and final-test segments stay untouched until after candidate selection, reducing look-ahead and selection leakage.

The system preserves Dhan rolling-option `expiry_flag` and `expiry_code` metadata. The supplied rolling sample does not contain an historical expiry-date field, so FRIDAY does not invent one.

## Outputs
Each selected quarter produces:
- REPORT.md
- audit.csv
- features.csv
- discovery.csv
- interactions.csv
- validation.csv
- ml.csv
- combined ZIP package

The current design is the foundation for later adversarial validation, research memory and the AI research-analyst layer.
