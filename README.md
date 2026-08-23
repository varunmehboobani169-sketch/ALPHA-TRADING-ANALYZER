# ALPHA ANALYZER — CLIENT-SECRET-SAFE BUILD

Core calculation methodology is kept backend-only.

Client UI does not expose:
- P&F terminology or construction rules
- box sizes or reversal settings
- DTB/DBS or anchor logic
- sector confirmation calculation
- RS ratio/construction methodology
- internal API endpoints/security IDs
- raw backend exception details

Client sees only:
- actionable trade/status outputs
- generic confirmation markers
- market/sector/relative-strength views
- alerts and notifications

All backend trading calculations remain unchanged.
