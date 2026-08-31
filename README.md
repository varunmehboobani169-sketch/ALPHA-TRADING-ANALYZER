# IV Monitor

A lightweight Streamlit market monitor focused on NIFTY and SENSEX.

## Monitor behavior
- At 10:00 IST, lock the day's ATM strike for NIFTY and SENSEX.
- Compare 10:00 ATM CE and PE implied volatility with the previous trading day's ATM close.
- Track open-interest movement for ATM-2, ATM-1, ATM, ATM+1 and ATM+2.
- Show current OI, change since the 10:00 lock, change versus the previous close, LTP and current IV.
- Refresh the OI monitor every 60 seconds while the dashboard is open.
- Use the nearest active expiry for each index.
- Use the exact NSE/BSE derivatives segment internally for option history.

## Vega Monitor
`pages/Vega Monitor.py` is a separate live module inspired by the supplied Vega script.

- Reads live strike-wise Vega directly from the option-chain Greeks.
- Combines Vega across a configurable ATM−N to ATM+N strike band; default is ATM−2 to ATM+2.
- Displays Call Vega, Put Vega and a transparent Vega Difference defined as Put Vega − Call Vega.
- Tracks Vega change between refreshes and keeps a rolling signal history in the Streamlit session.
- Tracks ATM Call Vega current/high/low and ATM Put Vega current/high/low for the trading day.
- Shows current Vega, day high and day low for every monitored option leg.
- On an expiry day, Auto expiry switches to the next available expiry, matching the behavior described in the supplied script.
- Expiry discovery is cached for the trading day and option-chain requests are spaced by at least 3.2 seconds.

## Vega Move Engine
`vega_direction_engine.py` and `pages/Vega Move Engine.py` implement the new movement-first research engine.

### Objective
First answer: **Is a meaningful market move building?**
Only after that answer is strong do we use Vega asymmetry to estimate direction.

### Live outputs
- Movement Score: 0–100.
- Movement state: Quiet / Elevated / Movement Building / High Probability Move / Extreme Expansion.
- Call-side Vega pressure and acceleration.
- Put-side Vega pressure and acceleration.
- IV asymmetry between calls and puts.
- Call and put Vega position within the observed session range.
- Direction Score: −100 to +100.
- Direction: Neutral / Mild Bullish / Bullish / Mild Bearish / Bearish.
- Confidence: Low / Medium / High.
- Call Vega, Put Vega, ATM Vega and per-leg current/day-high/day-low values.

The movement score intentionally treats simultaneous Call + Put Vega expansion as volatility expansion first, rather than automatically labeling it bullish or bearish.

### Research interpretation
The engine is a live research detector, not yet a validated trading edge. The next research step is to backtest each movement alert against forward NIFTY returns (5m, 10m, 15m, 30m and 60m) and measure how often different movement thresholds precede statistically meaningful moves.

## Theta Vega Ratio
`theta_vega_ratio.py` contains the strategy core and `pages/Theta Vega Ratio.py` is the live dashboard module.

### Current strategy rules
- At 09:16 IST, use the first completed 1-minute NIFTY candle to select the nearest ATM strike.
- Freeze that ATM strike for the whole day.
- Use the near weekly expiry.
- Start looking for entries after 09:36 IST.
- Entry requires Theta/Vega Ratio >= 1.5.
- Entry also requires 15-minute IV change <= +2%.
- Sell the fixed ATM CE + PE straddle.
- Target is 30% decay in combined premium from actual entry.
- Stop is 15% expansion in combined premium from actual entry.
- Maximum 2 trades per day.
- Enforce a 10-minute cooldown after an exit.
- Square off all open positions at 15:05 IST.
- No overnight position.

### Vega monitoring added to the strategy module
The strategy core records Vega separately for the ATM Call and ATM Put and exposes:
- Current CE Vega / Day High CE Vega / Day Low CE Vega.
- Current PE Vega / Day High PE Vega / Day Low PE Vega.
- Current combined ATM Vega / combined high / combined low.

These Vega high/low values are monitoring information only and do not currently change the entry/exit rules.

### Strategy note
The current research version reconstructs Theta/Vega with a Black-Scholes-style calculation. The exact expiry calendar, IV convention, risk-free assumption, lot size, transaction costs and execution/slippage should be validated before production use.

### Vega calculation note
The supplied original Vega script does not disclose its exact proprietary strike-weighting formula. The dashboard therefore keeps the combined-band aggregation transparent rather than claiming to reproduce an undisclosed formula exactly.

## Authentication
The Vega modules use a fixed Client ID configured as `1113195747`. The user enters only the Dhan Access Token; the Client ID is not manually changeable in those modules.

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```
