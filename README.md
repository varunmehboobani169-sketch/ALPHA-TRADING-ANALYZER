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
- **Now also tracks ATM Call Vega current/high/low and ATM Put Vega current/high/low for the trading day.**
- On an expiry day, Auto expiry switches to the next available expiry, matching the behavior described in the supplied script.
- Expiry discovery is cached for the trading day and option-chain requests are spaced by at least 3.2 seconds.

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

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```

Enter the Client ID and Access Token in the sidebar. Credentials are kept in the Streamlit session.
