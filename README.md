# FRIDAY — AI Option Strategist V2

## Required historical data structure

FRIDAY is designed around these exact base timeframes:

### 1. NIFTY Options — 1 minute
The option stream should contain, where available:
- timestamp
- expiry
- strike
- CE/PE or option_type
- open
- high
- low
- close/LTP
- IV
- OI
- volume

FRIDAY uses the 1-minute option stream to create synchronized ATM option features.

### 2. NIFTY Futures — 15 minutes
Required:
- timestamp
- open
- high
- low
- close

FRIDAY derives:
- 15-minute return
- futures momentum
- futures context
- spot-vs-futures spread

### 3. NIFTY Spot — 15 minutes
Required:
- timestamp
- open
- high
- low
- close

FRIDAY derives:
- 15-minute return
- market direction context
- spot-vs-futures relationship

### 4. India VIX
Preferred:
- 1-minute for detailed intraday features
or
- daily data for regime-level features

## Multi-year / multi-file workflow

You can upload multiple CSVs in each group, year-wise:

2024:
- NIFTY Options 1m
- NIFTY Futures 15m
- NIFTY Spot 15m
- India VIX

2025:
- NIFTY Options 1m
- NIFTY Futures 15m
- NIFTY Spot 15m
- India VIX

2026:
- NIFTY Options 1m
- NIFTY Futures 15m
- NIFTY Spot 15m
- India VIX

FRIDAY concatenates each group, aligns the data by timestamp, and builds a synchronized feature dataset.

## Timeframe validation

FRIDAY automatically estimates the median timestamp spacing of the uploaded files and warns when:
- NIFTY Spot is not approximately 15 minutes
- NIFTY Futures is not approximately 15 minutes
- NIFTY Options is not approximately 1 minute

The app does not silently pretend that a wrong timeframe is correct.

## Features

The prepared dataset can include:
- NIFTY 15-minute return
- Futures 15-minute return
- spot-vs-futures spread
- VIX change
- ATM CE IV
- ATM PE IV
- ATM IV
- ATM IV change
- ATM straddle
- straddle change
- PCR from OI
- time of day
- DTE

## ML training

The raw four-source dataset is the feature foundation. Supervised FRIDAY training still needs a historical strategy-outcome label such as:
- best_strategy
- target_strategy
- strategy
- label

The eventual strategy-outcome engine should generate those labels from the historical option stream rather than relying on manual labels.

## Controlled strategy set

FRIDAY can rank:
- BUY CE
- SELL PE
- BULL CALL SPREAD
- BULL PUT SPREAD
- BUY PE
- SELL CE
- BEAR PUT SPREAD
- BEAR CALL SPREAD
- SHORT STRADDLE
- SHORT STRANGLE
- IRON CONDOR
- NO TRADE

FRIDAY is a research/decision-support system and should be validated with time-ordered out-of-sample and walk-forward testing before live use.
