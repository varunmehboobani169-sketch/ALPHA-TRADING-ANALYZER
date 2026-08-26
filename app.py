import requests

# Minimal Cloud-safe FRIDAY launcher.
# The launcher itself contains no analysis logic.
SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/main/friday_stable.py"

source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()
exec(compile(source.text, "friday_stable.py", "exec"), {"__name__": "__main__"})
