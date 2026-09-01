from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


COLUMN_ALIASES = {
    "datetime": ["datetime", "date_time", "timestamp", "time_stamp", "dt"],
    "date": ["date", "trading_date", "trade_date"],
    "time": ["time", "trading_time", "trade_time"],
    "expiry": ["expiry", "expiry_date", "expiration", "expiration_date"],
    "strike": ["strike", "strike_price", "strikeprice"],
    "option_type": ["option_type", "optiontype", "type", "cp", "right", "instrument_type"],
    "symbol": ["symbol", "tradingsymbol", "trading_symbol", "security_name", "name"],
    "open": ["open", "open_price"],
    "high": ["high", "high_price"],
    "low": ["low", "low_price"],
    "close": ["close", "close_price", "ltp", "last_price", "price"],
    "spot": ["spot", "underlying", "underlying_price", "index_price", "nifty", "nifty_close"],
}


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    entry_time: str = "09:20"
    exit_time: str = "15:20"
    hold_trading_days: int = 5
    expiry_selection: str = "nearest_after_exit"
    strike_mode: str = "atm"
    wing_steps: int = 2
    short_steps: int = 1
    stop_loss_pct: float | None = None
    target_pct: float | None = None


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower().strip() if ch.isalnum())


def _find_col(df: pd.DataFrame, field: str) -> str | None:
    normalized = {_norm(c): c for c in df.columns}
    for alias in COLUMN_ALIASES[field]:
        if _norm(alias) in normalized:
            return normalized[_norm(alias)]
    return None


def normalize_option_data(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize common NIFTY option-data schemas into a canonical table.

    Required: date/time (or datetime), strike, option type, close.
    Expiry and spot are strongly recommended for positional tests.
    """
    if raw is None or raw.empty:
        raise ValueError("The supplied data is empty.")

    df = raw.copy()
    mapping = {f: _find_col(df, f) for f in COLUMN_ALIASES}

    dt = None
    if mapping["datetime"]:
        dt = pd.to_datetime(df[mapping["datetime"]], errors="coerce", dayfirst=False)
    else:
        if not mapping["date"]:
            raise ValueError("Could not find a date/datetime column.")
        date_part = df[mapping["date"]].astype(str)
        time_part = df[mapping["time"]].astype(str) if mapping["time"] else "09:15:00"
        dt = pd.to_datetime(date_part + " " + time_part, errors="coerce", dayfirst=False)

    required = {"strike": mapping["strike"], "option_type": mapping["option_type"], "close": mapping["close"]}
    missing = [k for k, v in required.items() if v is None]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    out = pd.DataFrame(index=df.index)
    out["datetime"] = dt
    out["date"] = pd.to_datetime(dt, errors="coerce").dt.normalize()
    out["expiry"] = pd.to_datetime(df[mapping["expiry"]], errors="coerce", dayfirst=False) if mapping["expiry"] else pd.NaT
    out["strike"] = pd.to_numeric(df[mapping["strike"]], errors="coerce")
    out["option_type"] = (
        df[mapping["option_type"]].astype(str).str.upper().str.extract(r"(CE|PE|CALL|PUT)", expand=False)
        .replace({"CALL": "CE", "PUT": "PE"})
    )
    out["close"] = pd.to_numeric(df[mapping["close"]], errors="coerce")
    for field in ["open", "high", "low"]:
        out[field] = pd.to_numeric(df[mapping[field]], errors="coerce") if mapping[field] else np.nan
    out["spot"] = pd.to_numeric(df[mapping["spot"]], errors="coerce") if mapping["spot"] else np.nan

    for col in ["symbol"]:
        out[col] = df[mapping[col]].astype(str) if mapping[col] else ""

    out = out.dropna(subset=["datetime", "date", "strike", "option_type", "close"])
    out = out[out["option_type"].isin(["CE", "PE"])]
    out = out[out["close"] >= 0]
    out = out.sort_values(["datetime", "expiry", "strike", "option_type"]).reset_index(drop=True)

    # If expiry is absent from the file but symbols contain a parseable expiry, leave it blank
    # rather than inventing an expiry. Positional expiry selection requires real expiry dates.
    return out


def combine_csvs(files: Iterable) -> pd.DataFrame:
    frames = []
    for f in files:
        name = getattr(f, "name", "uploaded.csv")
        raw = pd.read_csv(f)
        norm = normalize_option_data(raw)
        norm["source_file"] = name
        frames.append(norm)
    if not frames:
        raise ValueError("No CSV files supplied.")
    combined = pd.concat(frames, ignore_index=True)
    return combined.drop_duplicates(subset=["datetime", "expiry", "strike", "option_type", "close", "source_file"])


def trading_dates(df: pd.DataFrame) -> list[pd.Timestamp]:
    return sorted(pd.to_datetime(df["date"].dropna().unique()))


def choose_expiry(df: pd.DataFrame, entry_date: pd.Timestamp, exit_date: pd.Timestamp) -> pd.Timestamp:
    candidates = pd.to_datetime(df["expiry"].dropna().unique())
    candidates = sorted(x for x in candidates if x.normalize() >= exit_date.normalize())
    if not candidates:
        candidates = sorted(x for x in pd.to_datetime(df["expiry"].dropna().unique()) if x.normalize() >= entry_date.normalize())
    if not candidates:
        raise ValueError("No usable expiry dates were found in the dataset.")
    return candidates[0]


def _nearest_strike(strikes: np.ndarray, spot: float) -> float:
    return float(strikes[np.argmin(np.abs(strikes - spot))])


def _strike_by_steps(strikes: np.ndarray, atm: float, steps: int) -> float:
    strikes = np.array(sorted(set(map(float, strikes))))
    idx = int(np.argmin(np.abs(strikes - atm)))
    target = max(0, min(len(strikes) - 1, idx + steps))
    return float(strikes[target])


def _spot_at(df: pd.DataFrame, day: pd.Timestamp, clock: str) -> float | None:
    rows = df[df["date"] == day]
    if rows.empty:
        return None
    t = pd.to_datetime(clock).time()
    # Prefer an actual spot column if available; otherwise estimate spot from the closest CE/PE strike pair is not safe.
    spot_rows = rows.dropna(subset=["spot"])
    if spot_rows.empty:
        return None
    spot_rows = spot_rows.assign(_dist=(spot_rows["datetime"].dt.time.map(lambda x: abs((x.hour*60+x.minute) - (t.hour*60+t.minute)))))
    return float(spot_rows.sort_values("_dist").iloc[0]["spot"])


def _bar_at(df: pd.DataFrame, day: pd.Timestamp, clock: str, expiry: pd.Timestamp, strike: float, option_type: str) -> pd.Series | None:
    rows = df[(df["date"] == day) & (df["strike"] == strike) & (df["option_type"] == option_type)]
    if pd.notna(expiry):
        rows = rows[rows["expiry"].dt.normalize() == expiry.normalize()]
    if rows.empty:
        return None
    target = pd.to_datetime(clock).time()
    rows = rows.copy()
    target_minutes = target.hour * 60 + target.minute
    rows["_dist"] = rows["datetime"].dt.hour * 60 + rows["datetime"].dt.minute
    rows["_dist"] = (rows["_dist"] - target_minutes).abs()
    return rows.sort_values(["_dist", "datetime"]).iloc[0]


def _entry_exit_dates(all_dates: list[pd.Timestamp], entry_date: pd.Timestamp, hold_days: int) -> pd.Timestamp | None:
    try:
        idx = all_dates.index(entry_date)
    except ValueError:
        return None
    j = idx + hold_days
    if j >= len(all_dates):
        return None
    return all_dates[j]


def _legs_for_strategy(strategy: StrategyConfig, strikes: np.ndarray, atm: float) -> list[tuple[str, float, int]]:
    if strategy.name == "ATM Short Straddle":
        return [("CE", atm, -1), ("PE", atm, -1)]
    if strategy.name == "OTM Short Strangle":
        return [
            ("CE", _strike_by_steps(strikes, atm, strategy.short_steps), -1),
            ("PE", _strike_by_steps(strikes, atm, -strategy.short_steps), -1),
        ]
    if strategy.name == "Iron Condor":
        return [
            ("PE", _strike_by_steps(strikes, atm, -strategy.short_steps), -1),
            ("CE", _strike_by_steps(strikes, atm, strategy.short_steps), -1),
            ("PE", _strike_by_steps(strikes, atm, -strategy.wing_steps), +1),
            ("CE", _strike_by_steps(strikes, atm, strategy.wing_steps), +1),
        ]
    if strategy.name == "Wide OTM Strangle":
        return [
            ("CE", _strike_by_steps(strikes, atm, strategy.wing_steps), -1),
            ("PE", _strike_by_steps(strikes, atm, -strategy.wing_steps), -1),
        ]
    raise ValueError(f"Unknown strategy: {strategy.name}")


def backtest(df: pd.DataFrame, strategy: StrategyConfig, initial_capital: float = 1_000_000.0, lot_size: int = 75) -> tuple[pd.DataFrame, dict]:
    if df.empty:
        raise ValueError("No normalized option data supplied.")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
    dates = trading_dates(df)
    trades = []

    for entry_date in dates:
        exit_date = _entry_exit_dates(dates, entry_date, strategy.hold_trading_days)
        if exit_date is None:
            break
        spot = _spot_at(df, entry_date, strategy.entry_time)
        if spot is None:
            continue

        try:
            expiry = choose_expiry(df, entry_date, exit_date)
        except ValueError:
            continue

        universe = df[(df["expiry"].dt.normalize() == expiry.normalize()) & (df["date"] == entry_date)]
        strikes = universe["strike"].dropna().unique()
        if len(strikes) < 3:
            continue
        atm = _nearest_strike(strikes, spot)
        legs = _legs_for_strategy(strategy, np.array(strikes), atm)

        entry_premium = 0.0
        entry_rows = []
        valid = True
        for side, strike, qty_sign in legs:
            row = _bar_at(df, entry_date, strategy.entry_time, expiry, strike, side)
            if row is None:
                valid = False
                break
            px = float(row["close"])
            entry_premium += qty_sign * px
            entry_rows.append((side, strike, qty_sign, px))
        if not valid:
            continue

        exit_value = 0.0
        for side, strike, qty_sign, entry_px in entry_rows:
            row = _bar_at(df, exit_date, strategy.exit_time, expiry, strike, side)
            if row is None:
                # fallback to the last available bar for that leg on exit date
                rows = df[(df["date"] == exit_date) & (df["expiry"].dt.normalize() == expiry.normalize()) & (df["strike"] == strike) & (df["option_type"] == side)]
                row = rows.sort_values("datetime").iloc[-1] if not rows.empty else None
            if row is None:
                valid = False
                break
            exit_value += qty_sign * float(row["close"])
        if not valid:
            continue

        pnl_points = entry_premium - exit_value
        pnl_rupees = pnl_points * lot_size
        trades.append({
            "Entry Date": entry_date.date(),
            "Exit Date": exit_date.date(),
            "Expiry": expiry.date(),
            "Spot": spot,
            "ATM": atm,
            "Entry Premium": entry_premium,
            "Exit Value": exit_value,
            "PnL Points": pnl_points,
            "PnL": pnl_rupees,
            "Holding Days": strategy.hold_trading_days,
        })

    result = pd.DataFrame(trades)
    if result.empty:
        return result, {"trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "max_drawdown": 0.0, "return_pct": 0.0}

    result["Equity"] = initial_capital + result["PnL"].cumsum()
    peak = result["Equity"].cummax()
    result["Drawdown"] = result["Equity"] - peak
    max_dd = float(result["Drawdown"].min())
    net = float(result["PnL"].sum())
    wins = int((result["PnL"] > 0).sum())
    stats = {
        "trades": int(len(result)),
        "net_pnl": net,
        "win_rate": wins / len(result) * 100.0,
        "avg_pnl": float(result["PnL"].mean()),
        "max_drawdown": max_dd,
        "return_pct": net / initial_capital * 100.0,
    }
    return result, stats


STRATEGY_PRESETS = {
    "ATM Short Straddle": StrategyConfig(name="ATM Short Straddle", short_steps=0),
    "OTM Short Strangle": StrategyConfig(name="OTM Short Strangle", short_steps=1),
    "Wide OTM Strangle": StrategyConfig(name="Wide OTM Strangle", wing_steps=2),
    "Iron Condor": StrategyConfig(name="Iron Condor", short_steps=1, wing_steps=2),
}
