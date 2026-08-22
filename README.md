# ALPHA ANALYZER FROM SCRATCH V2 — Ranking

Adds a 4-slot ranking display:
- ⭐ P&F directional pattern
- ⭐ Futures OI buildup (current OI > previous daily OI)
- ⭐ Sector P&F breadth support
- 🟢★ New 3-column pattern (>15 Anchor -> 1–5 Pullback -> 3rd column)

P&F DTB/DBS remains the actual entry trigger. The stars rank/confirm the setup.

The ranking build keeps the base data path working and uses batch live quotes for cash and futures.
Previous OI history is only requested for currently directional candidates.
