# FRIDAY emergency launcher: restores the validated FRIDAY V3.1 source from the last intact GitHub commit.
# The previous GitHub update accidentally replaced app.py with a redacted placeholder.
import requests

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/361848fd675ecda841c8a6564c9f2caa4d57967c/app.py"
response = requests.get(SOURCE_URL, timeout=30)
response.raise_for_status()
source = response.text
if "import zipfile" not in source.splitlines()[:40]:
    source = source.replace("import time\n", "import time\nimport zipfile\n", 1)
exec(compile(source, "friday_app.py", "exec"), globals(), globals())
