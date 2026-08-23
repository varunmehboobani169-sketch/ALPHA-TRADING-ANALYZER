# ALPHA ANALYZER — OPTION SELLER WITH STRIKE SUGGESTIONS

The Option Seller module now provides exact strikes in addition to the strategy.

Examples:
- SELL STRADDLE -> current ATM CE + ATM PE
- SELL PUT -> highest available put strike at/below the detected support
- SELL CALL -> lowest available call strike at/above the detected resistance

The dashboard also shows the live premium for the recommended strike(s).

The strategy reason remains visible to the client without exposing proprietary
calculation details.
