# Dhan NIFTY Streamlit Dashboard

## 1. Install
```bash
pip install -r requirements.txt
```

## 2. Credentials
Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and enter your Dhan Client ID and Access Token.

Do not commit or share `secrets.toml`.

## 3. Run
```bash
streamlit run app.py
```

## What it does
- Loads active NIFTY option expiries from DhanHQ V2.
- Pulls the selected expiry's option chain.
- Shows spot, ATM, PCR, ATM IV, max Call/Put OI and an initial directional interpretation.
- Displays CE/PE LTP, OI, change in OI, volume, IV and delta.
- Shows OI and IV charts.
- Uses a 3-second cache for option-chain data to respect the documented Dhan request limit.
