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
- Uses the script's observable interpretation: rising Vega Difference = bearish pressure; falling Vega Difference = bullish pressure.
- Implements the script's continuous-count idea: after the configured number of prior consecutive changes, the next same-direction change produces a signal. Default prior count is 2, so the signal appears on the third consecutive change.
- On an expiry day, Auto expiry switches to the next available expiry, matching the behavior described in the supplied script.
- Expiry discovery is cached for the trading day and option-chain requests are spaced by at least 3.2 seconds to stay above the documented 3-second request interval.

### Vega calculation note
The supplied script describes a proprietary combined-strike Vega calculation but does not disclose its exact weighting formula. The dashboard therefore uses a transparent simple sum by default, with an optional OI-weighted aggregation for research comparison. It does not claim to reproduce an undisclosed proprietary formula exactly.

## Data handling
The 10:00 lock is reconstructed from minute historical data, so the monitor can still capture the 10:00 snapshot when opened later during the same trading day. Previous-day IV is reconstructed consistently from the previous close option premium, previous index close, strike, expiry and the configurable calculation rate.

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```

Enter the Client ID and Access Token in the sidebar. Credentials are kept in the Streamlit session.
