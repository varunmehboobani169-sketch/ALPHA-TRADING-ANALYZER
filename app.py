import requests

# Minimal Cloud-safe FRIDAY launcher.
# Current design: Options + NIFTY Spot + India VIX only.
# Futures are intentionally excluded.
SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/main/friday_final.py"
source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()
exec(compile(source.text, "friday_final.py", "exec"), {"__name__": "__main__"})
