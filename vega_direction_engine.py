"""Vega-first market movement detection engine.

The engine answers two separate questions:
1) Is the option chain entering an abnormal volatility/Vega expansion state?
2) If so, which side has the stronger asymmetry?

It deliberately does not treat raw Vega level as bullish/bearish. Direction
comes from changes, acceleration, IV asymmetry, and price confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass
class VegaSignal:
    movement_score: float
    movement_state: str
    direction_score: float
    direction: str
    confidence: str
    call_pressure: float
    put_pressure: float
    call_acceleration: float
    put_acceleration: float
    iv_asymmetry: float
    call_range_position: float
    put_range_position: float
    expansion: bool


def _safe_float(value, default=0.0) -> float:
    try:
        value = float(value)
        return default if not np.isfinite(value) else value
    except (TypeError, ValueError):
        return default


def _pct_change(now: float, old: float) -> float:
    now, old = _safe_float(now), _safe_float(old)
    denom = max(abs(old), 1e-9)
    return (now - old) / denom * 100.0


def _range_position(current: float, low: float, high: float) -> float:
    current, low, high = map(_safe_float, (current, low, high))
    if high <= low:
        return 0.5
    return float(np.clip((current - low) / (high - low), 0.0, 1.0))


def _recent_delta(series: pd.Series, bars: int) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) <= bars:
        return 0.0
    return _pct_change(values.iloc[-1], values.iloc[-1 - bars])


def _acceleration(series: pd.Series, fast_bars: int = 1, slow_bars: int = 3) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) <= slow_bars + fast_bars:
        return 0.0
    fast = _pct_change(values.iloc[-1], values.iloc[-1 - fast_bars])
    slow = _pct_change(values.iloc[-1 - fast_bars], values.iloc[-1 - fast_bars - slow_bars])
    return float(fast - slow)


def _side_metrics(history: pd.DataFrame, side: str) -> dict:
    side_df = history[history["side"] == side].copy()
    if side_df.empty:
        return {"current": 0.0, "low": 0.0, "high": 0.0, "range_pos": 0.5,
                "change": 0.0, "acceleration": 0.0, "iv_change": 0.0}
    side_df = side_df.sort_values("time")
    current = _safe_float(side_df["vega"].iloc[-1])
    low = _safe_float(side_df["vega"].min(), current)
    high = _safe_float(side_df["vega"].max(), current)
    # Use one aggregated side series at each observation, not strike-level noise.
    agg = side_df.groupby("time", as_index=False).agg({"vega": "sum", "iv": "mean"}).sort_values("time")
    change = _recent_delta(agg["vega"], 1)
    acceleration = _acceleration(agg["vega"], 1, 3)
    iv_change = _recent_delta(agg["iv"], 1)
    return {
        "current": current,
        "low": low,
        "high": high,
        "range_pos": _range_position(current, low, high),
        "change": change,
        "acceleration": acceleration,
        "iv_change": iv_change,
    }


def calculate_vega_signal(snapshot_history: Iterable[dict]) -> VegaSignal:
    """Calculate a movement-first Vega signal from timestamped chain snapshots."""
    rows = []
    for item in snapshot_history:
        time_value = item.get("time")
        for side, payload in (("CE", item.get("call", {})), ("PE", item.get("put", {}))):
            rows.append({
                "time": time_value,
                "side": side,
                "vega": _safe_float(payload.get("vega")),
                "iv": _safe_float(payload.get("iv")),
            })
    history = pd.DataFrame(rows)
    if history.empty:
        return VegaSignal(0, "QUIET", 0, "NEUTRAL", "LOW", 0, 0, 0, 0, 0, .5, .5, False)

    ce = _side_metrics(history, "CE")
    pe = _side_metrics(history, "PE")

    # Movement score: abnormal expansion/acceleration is the core signal.
    total_change = abs(ce["change"]) + abs(pe["change"])
    total_accel = abs(ce["acceleration"]) + abs(pe["acceleration"])
    iv_expansion = max(0.0, ce["iv_change"]) + max(0.0, pe["iv_change"])
    range_energy = max(0.0, ce["range_pos"] - 0.60) + max(0.0, pe["range_pos"] - 0.60)

    movement_score = min(100.0, (
        min(total_change, 20.0) / 20.0 * 35.0
        + min(total_accel, 20.0) / 20.0 * 25.0
        + min(iv_expansion, 10.0) / 10.0 * 25.0
        + min(range_energy, 0.8) / 0.8 * 15.0
    ))

    expansion = movement_score >= 55.0 and (ce["change"] > 0 or pe["change"] > 0)
    if movement_score >= 90:
        movement_state = "EXTREME EXPANSION"
    elif movement_score >= 75:
        movement_state = "HIGH PROBABILITY MOVE"
    elif movement_score >= 55:
        movement_state = "MOVEMENT BUILDING"
    elif movement_score >= 35:
        movement_state = "ELEVATED"
    else:
        movement_state = "QUIET"

    # Direction: positive means CE side stronger; negative means PE side stronger.
    pressure = (ce["change"] - pe["change"]) + 0.75 * (ce["acceleration"] - pe["acceleration"])
    iv_asymmetry = ce["iv_change"] - pe["iv_change"]
    range_asymmetry = (ce["range_pos"] - 0.5) - (pe["range_pos"] - 0.5)
    direction_score = float(np.clip(pressure * 1.4 + iv_asymmetry * 3.0 + range_asymmetry * 30.0, -100.0, 100.0))

    if direction_score >= 55:
        direction = "BULLISH"
    elif direction_score <= -55:
        direction = "BEARISH"
    elif direction_score >= 20:
        direction = "MILD BULLISH"
    elif direction_score <= -20:
        direction = "MILD BEARISH"
    else:
        direction = "NEUTRAL"

    # Confidence is deliberately conditional on a movement regime.
    magnitude = abs(direction_score)
    if movement_score < 45:
        confidence = "LOW"
    elif magnitude >= 60 and movement_score >= 75:
        confidence = "HIGH"
    elif magnitude >= 35 and movement_score >= 55:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return VegaSignal(
        movement_score=round(float(movement_score), 1),
        movement_state=movement_state,
        direction_score=round(direction_score, 1),
        direction=direction,
        confidence=confidence,
        call_pressure=round(ce["change"], 2),
        put_pressure=round(pe["change"], 2),
        call_acceleration=round(ce["acceleration"], 2),
        put_acceleration=round(pe["acceleration"], 2),
        iv_asymmetry=round(iv_asymmetry, 2),
        call_range_position=round(ce["range_pos"] * 100.0, 1),
        put_range_position=round(pe["range_pos"] * 100.0, 1),
        expansion=bool(expansion),
    )
