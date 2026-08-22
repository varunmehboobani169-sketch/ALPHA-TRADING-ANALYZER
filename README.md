# ALPHA ANALYZER V28 — LIVE SPOT LTP CHECK

Adds live NSE cash data diagnostics to the scanner:
- Spot LTP
- Spot Day %
- Futures LTP
- Futures OI

The cash LTP is fetched in one batched Market Quote request for the mapped
NSE cash securities.

Purpose:
- Confirm whether cash/spot data is actually reaching the app.
- If Spot LTP is populated, the live cash mapping is working and we can focus on historical/P&F logic.
- If Spot LTP is missing, the issue is upstream of the P&F engine.

No P&F trading rules were changed.
