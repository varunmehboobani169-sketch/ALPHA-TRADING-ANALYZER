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

## Data handling
The 10:00 lock is reconstructed from minute historical data, so the monitor can still capture the 10:00 snapshot when opened later during the same trading day. Previous-day IV is reconstructed consistently from the previous close option premium, previous index close, strike, expiry and the configurable calculation rate.

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```

Enter the Client ID and Access Token in the sidebar. Credentials are kept in the Streamlit session.
