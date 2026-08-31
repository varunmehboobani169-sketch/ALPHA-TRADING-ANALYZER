"""Theta Vega Ratio strategy core.

Intraday NIFTY fixed-ATM short straddle research/live signal helpers.

Rules:
- 09:16 completed 1-minute candle fixes ATM strike for the day.
- Near weekly expiry.
- Look for entries after 09:36 IST.
- Entry: Theta/Vega ratio >= 1.5 and 15-minute IV change <= +2%.
- Sell fixed ATM CE + PE.
- Target: 30% decay from actual entry premium.
- Stop: 15% expansion from actual entry premium.
- Max 2 trades/day.
- 10-minute cooldown after an exit.
- 15:05 IST mandatory exit.

Vega monitoring:
- Track current, day-high and day-low Vega separately for CE and PE.
- Also expose combined straddle Vega current/high/low.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional


@dataclass(frozen=True)
class ThetaVegaConfig:
    setup_time: time = time(9, 16)
    entry_time: time = time(9, 36)
    hard_exit_time: time = time(15, 5)
    theta_vega_min: float = 1.5
    iv_change_15m_max: float = 0.02
    target_decay: float = 0.30
    stop_expansion: float = 0.15
    max_trades_per_day: int = 2
    cooldown_minutes: int = 10


@dataclass
class VegaLegState:
    current: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None

    def update(self, value: Optional[float]) -> None:
        if value is None:
            return
        value = float(value)
        self.current = value
        self.day_high = value if self.day_high is None else max(self.day_high, value)
        self.day_low = value if self.day_low is None else min(self.day_low, value)


@dataclass
class VegaState:
    call: VegaLegState
    put: VegaLegState

    @property
    def combined_current(self) -> Optional[float]:
        if self.call.current is None or self.put.current is None:
            return None
        return self.call.current + self.put.current

    @property
    def combined_high(self) -> Optional[float]:
        if self.call.day_high is None or self.put.day_high is None:
            return None
        return self.call.day_high + self.put.day_high

    @property
    def combined_low(self) -> Optional[float]:
        if self.call.day_low is None or self.put.day_low is None:
            return None
        return self.call.day_low + self.put.day_low


@dataclass
class Trade:
    trade_number: int
    strike: float
    expiry: object
    entry_time: datetime
    entry_premium: float
    target_premium: float
    stop_premium: float
    exit_time: Optional[datetime] = None
    exit_premium: Optional[float] = None
    exit_reason: Optional[str] = None

    @property
    def pnl_points(self) -> Optional[float]:
        if self.exit_premium is None:
            return None
        return self.entry_premium - self.exit_premium


def entry_signal_allowed(
    timestamp: datetime,
    theta_vega_ratio: float,
    iv_change_15m: float,
    config: ThetaVegaConfig | None = None,
) -> bool:
    cfg = config or ThetaVegaConfig()
    return (
        cfg.entry_time <= timestamp.time() < cfg.hard_exit_time
        and theta_vega_ratio >= cfg.theta_vega_min
        and iv_change_15m <= cfg.iv_change_15m_max
    )


def make_trade(
    trade_number: int,
    strike: float,
    expiry: object,
    timestamp: datetime,
    combined_premium: float,
    config: ThetaVegaConfig | None = None,
) -> Trade:
    cfg = config or ThetaVegaConfig()
    if combined_premium <= 0:
        raise ValueError("Combined premium must be positive.")
    return Trade(
        trade_number=trade_number,
        strike=strike,
        expiry=expiry,
        entry_time=timestamp,
        entry_premium=combined_premium,
        target_premium=combined_premium * (1.0 - cfg.target_decay),
        stop_premium=combined_premium * (1.0 + cfg.stop_expansion),
    )


def exit_reason(
    timestamp: datetime,
    combined_premium: float,
    trade: Trade,
    config: ThetaVegaConfig | None = None,
) -> Optional[str]:
    cfg = config or ThetaVegaConfig()
    if timestamp.time() >= cfg.hard_exit_time:
        return "15:05"
    if combined_premium >= trade.stop_premium:
        return "STOP"
    if combined_premium <= trade.target_premium:
        return "TARGET"
    return None


def start_new_cooldown(exit_time: datetime, config: ThetaVegaConfig | None = None) -> datetime:
    cfg = config or ThetaVegaConfig()
    return exit_time + timedelta(minutes=cfg.cooldown_minutes)
