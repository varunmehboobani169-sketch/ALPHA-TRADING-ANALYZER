# FRIDAY fast launcher
# Current design: Options + NIFTY Spot + India VIX only.
# Futures are intentionally removed from the application design.
import requests

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/main/friday_fast.py"
source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()
exec(compile(source.text, "friday_fast.py", "exec"), {"__name__": "__main__"})
