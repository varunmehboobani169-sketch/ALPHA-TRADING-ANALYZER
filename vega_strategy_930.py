"""9:30 fixed-ATM Vega-difference strategy.

- Freeze nearest NIFTY ATM at 09:30.
- Use that exact strike all day.
- CE-PE Vega difference = CE Vega - PE Vega.
- Three strictly rising completed 1-minute differences -> BUY CE.
- Three strictly falling completed 1-minute differences -> BUY PE.
- First qualifying signal only; signal engine does not place broker orders.
"""

from dataclasses import dataclass
from datetime import time
from typing import Optional

import pandas as pd

ENTRY_START = time(9, 33)


@dataclass
class Vega930Signal:
    signal: str
    entry_side: Optional[str]
    reason: str
    difference: float
    previous_differences: list[float]
    streak: int


def minute_series(observations: list[dict]) -> pd.DataFrame:
    rows = []
    for obs in observations:
        rows.append({
            "time": pd.Timestamp(obs["time"]).floor("min"),
            "ce_vega": float(obs["ce_vega"]),
            "pe_vega": float(obs["pe_vega"]),
        })
    if not rows:
        return pd.DataFrame(columns=["time", "ce_vega", "pe_vega", "difference"])
    df = pd.DataFrame(rows).sort_values("time").drop_duplicates("time", keep="last")
    df["difference"] = df["ce_vega"] - df["pe_vega"]
    return df.reset_index(drop=True)


def evaluate_signal(observations: list[dict], already_traded: bool = False) -> Vega930Signal:
    df = minute_series(observations)
    if len(df) < 3:
        return Vega930Signal("WAIT", None, "Need 3 completed one-minute observations.", 0.0, [], 0)

    last = df.iloc[-1]
    diffs = df["difference"].tail(3).tolist()
    rising = diffs[0] < diffs[1] < diffs[2]
    falling = diffs[0] > diffs[1] > diffs[2]

    if already_traded:
        return Vega930Signal("LOCKED", None, "First-signal-per-day rule already triggered.", float(last["difference"]), diffs, 3 if (rising or falling) else 0)

    if pd.Timestamp(last["time"]).time() < ENTRY_START:
        return Vega930Signal("WAIT", None, "Waiting for the 09:33+ entry window.", float(last["difference"]), diffs, 0)

    if rising:
        return Vega930Signal("BUY CE", "CE", "CE-PE Vega difference rose for 3 consecutive completed minutes.", float(last["difference"]), diffs, 3)
    if falling:
        return Vega930Signal("BUY PE", "PE", "CE-PE Vega difference fell for 3 consecutive completed minutes.", float(last["difference"]), diffs, 3)

    return Vega930Signal("WATCH", None, "No 3-minute continuous Vega-difference move yet.", float(last["difference"]), diffs, 0)
