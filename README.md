# ALPHA ANALYZER FINAL V14

NSE architecture:
- P&F is built from NSE cash/spot EQUITY price only.
- Only stocks that have an active NSE FUTSTK are included.
- Futures are used only for OI confirmation.
- All unique F&O stocks are scanned.
- Positional: 0.25% box, 3-box reversal, daily cash closes.
- Intraday: 0.15% box, 3-box reversal, completed 1-minute cash closes.
- Anchor -> retracement -> DTB/DBS is explicit.
- Initial SL uses the latest completed opposite P&F column extreme.
- Futures OI is obtained in a batched Market Quote call and compared with a once-per-day cached previous OI baseline.
- Historical cash intraday data is cached for 3 minutes to align with the current auto-refresh while avoiding duplicate calls inside a rerun.
- Sector breadth uses cash P&F.
- MCX remains a separate futures-based engine.
- Client code/access token remain in Streamlit session state.

Important:
- This version is signal-only; it does not place orders.
- Use Diagnostics to verify the unique NSE F&O cash mapping before live use.
