# JARVIS — Option Seller Environment

Dedicated single-purpose dashboard using the same Dhan API and instrument-master source.

Fixes in this build:
- Dhan rate-limit protection with 1-second request throttling and exponential retry/backoff.
- 5-minute auto-refresh instead of 1-minute refresh.
- Longer caching for VIX and IV history.
- Last successful cached values are retained during transient rate-limit/API errors.
- India VIX security ID is resolved from the same Dhan instrument master, with 26 as fallback.
- JARVIS logo added to the sidebar.
- VIX controls remain user configurable: timeframe, box size, reversal.
- Default VIX calculation: Day / 0.25% / 3-box reversal / Close Only.


## V2 Bug Fix
Fixed the `unsupported format string passed to tuple.__format__` error caused by
the nested tuple returned by the live India VIX request. Added defensive numeric
formatting and safe expiry formatting.


## V3 IV Behaviour Fix
- Today's IV row is retained so JARVIS can calculate today's Open IV vs current IV.
- The historical baseline uses ONLY the previous 30 completed sessions.
- Today's session is never included in the 30-session average or standard deviation.
- API throttle increased to 3.2 seconds between requests to stay below Dhan's documented 3-second option-chain request limit.
