# NIFTY Historical Options Data Collector

This repository now focuses on historical NIFTY weekly-options data collection for research.

## Dashboard
- Dhan login in the left sidebar.
- Select a six-month block from 2024 onward.
- Click **DOWNLOAD SIX MONTHS**.
- The backend automatically splits the requested range into Dhan-compliant chunks of no more than 90 days.
- NIFTY weekly expiries are discovered from Dhan's detailed instrument master.
- NIFTY 1-minute spot data is used to determine the ATM strike for every minute.
- The collector keeps contracts from ATM−20 through ATM+20 and both CE and PE.
- Every completed weekly expiry is stored as an individual Parquet file and skipped on a future run, allowing the job to resume.

## Stored data
`timestamp`, `date`, `time`, `expiry`, `security_id`, `spot`, `atm`, `strike_offset`, `moneyness`, `strike`, `option_type`, `open_price`, `high_price`, `low_price`, `close_price`, `volume`, `open_interest`, `iv`, `delta`, `gamma`, `theta`, `vega`, `time_to_expiry_years`.

## Greeks
Dhan's historical intraday endpoint provides 1-minute OHLC, volume and OI. Historical IV/Delta/Gamma/Theta/Vega are reconstructed locally from option close, NIFTY spot, strike, time to expiry and the selected risk-free rate using Black-Scholes-style calculations. Dhan's live Option Chain endpoint provides current OI, Greeks, volume, LTP, bid/ask and IV, but it is not a historical Greek series.

## Important data limitation
Dhan's dedicated expired-options rolling endpoint supplies historical minute-level OHLC, IV, OI, volume and spot, but its documented strike range for index options is ATM±10. The collector therefore attempts the requested ATM±20 universe through individual option Security IDs using the historical intraday endpoint. Any contract that Dhan does not make available historically is explicitly counted as a failed/missing contract rather than silently filled.

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```

Never commit a Dhan Access Token to the repository.
