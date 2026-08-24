# ALPHA ANALYZER — V8 ERROR FIX

Fixed the startup NameError shown in the screenshot.

Cause:
The sidebar daily-report button called `trade_report_dataframe()` before the
function was defined.

Fix:
The report button is now rendered after the trade-report helper is defined.

All V7 functionality remains:
- global option warning monitor
- background warning notifications/sound
- trade logging
- IST timestamps
- NSE/MCX/Option Seller/Market/RS/Sector modules
