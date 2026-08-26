# FRIDAY launcher
# Current design: Options + NIFTY Spot + India VIX only.
# Futures are intentionally removed from the application design for now.
import requests

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/main/friday_app_v2.py"
source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()
exec(compile(source.text, "friday_app_v2.py", "exec"), {"__name__": "__main__"})
