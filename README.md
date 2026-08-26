# JARVIS — Option Seller Environment

Dedicated volatility-environment dashboard using the same Dhan API source and instrument-master source as ALPHA ANALYZER.

## Scope
JARVIS is NOT a trade execution engine and does not use Momentum, Positional, Matrix, ALPHA PRO SELLER, Historical Data Lab, or ML logic.

## Core inputs
1. India VIX
2. NIFTY ATM CE IV
3. NIFTY ATM PE IV
4. ATM average IV
5. Last 30 completed trading sessions of ATM IV open/close/change
6. India VIX close-only P&F state

## VIX settings
Default:
- Timeframe: Day
- Box size: 0.25%
- Reversal: 3 boxes
- Close only

The user can change VIX timeframe, box size and reversal, then the state and chart recalculate.

## 30-session IV baseline
The dashboard automatically fetches enough rolling-option history to obtain the last 30 completed sessions. Today's session is excluded from the historical baseline.

For every historical session:
IV Change = Close IV - Open IV

The dashboard calculates:
- 30-session average IV change
- Standard deviation
- Today's difference from average
- Z-score
- Expansion / contraction state

## Environment interpretation
FAVOURABLE:
VIX active sell + ATM IV contracting/stable

CAUTION:
Mixed conditions or VIX active sell with IV expansion

NOT FAVOURABLE:
VIX active long with IV expansion/non-contracting conditions

## Data source
DhanHQ v2 API and Dhan instrument master, using the same API source architecture as the existing Alpha Analyzer.

The rolling expired-option endpoint supports minute-level data, IV, OI, volume, strike and spot, and historical windows of up to 30 days per request; JARVIS combines chunks automatically. Dhan documents up to five years of rolling expired-option history.
