"""FRIDAY deep audit engine.

Designed to sit between raw quarter CSVs and the Pattern Research report.
It does not discover trading strategies; it audits research inputs and outputs
for data integrity, expiry identity, timestamp alignment, duplicates, missing
periods, pairing quality, return sanity and robustness statistics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class AuditConfig:
    spot_tolerance: pd.Timedelta = pd.Timedelta(minutes=20)
    pair_tolerance: pd.Timedelta = pd.Timedelta(minutes=2)
    market_open: str = "09:15"
    market_close: str = "15:30"
    min_obs_pattern: int = 30
    max_abs_forward_return: float = 0.25


def _pct(x: pd.Series) -> pd.Series:
    return pd.to_numeric(x, errors="coerce")


def audit_options(options: pd.DataFrame, cfg: AuditConfig) -> pd.DataFrame:
    checks = []
    if options.empty:
        return pd.DataFrame([{"check": "options_present", "status": "FAIL", "value": 0, "note": "No option rows."}])

    expiry_present = options.get("expiry", pd.Series(index=options.index, dtype="datetime64[ns]")).notna().mean()
    checks.append({"check": "expiry_coverage", "status": "PASS" if expiry_present >= 0.999 else "FAIL", "value": expiry_present, "note": "Required for safe CE/PE pairing."})

    valid_side = options.get("side", pd.Series(index=options.index, dtype=str)).isin(["CE", "PE"]).mean()
    checks.append({"check": "ce_pe_labels", "status": "PASS" if valid_side >= 0.999 else "WARN", "value": valid_side, "note": "CE/PE labels recognized."})

    dup_cols = [c for c in ["timestamp", "expiry", "strike", "side"] if c in options.columns]
    dup_rate = options.duplicated(dup_cols).mean() if dup_cols else np.nan
    checks.append({"check": "duplicate_option_rows", "status": "PASS" if dup_rate <= 0.001 else "WARN", "value": dup_rate, "note": "Duplicate rate on timestamp+expiry+strike+side."})

    for col in ["close", "iv", "oi", "volume"]:
        if col in options.columns:
            coverage = options[col].notna().mean()
            checks.append({"check": f"{col}_coverage", "status": "PASS" if coverage >= 0.95 else "WARN", "value": coverage, "note": f"Non-null {col} share."})
    return pd.DataFrame(checks)


def audit_market_timestamps(df: pd.DataFrame, name: str, cfg: AuditConfig) -> pd.DataFrame:
    if df.empty or "timestamp" not in df:
        return pd.DataFrame([{"check": f"{name}_timestamps", "status": "FAIL", "value": 0, "note": "No usable timestamp column."}])
    ts = pd.to_datetime(df["timestamp"], errors="coerce").dropna().sort_values()
    if ts.empty:
        return pd.DataFrame([{"check": f"{name}_timestamps", "status": "FAIL", "value": 0, "note": "No parseable timestamps."}])
    dup = ts.duplicated().mean()
    outside = ((ts.dt.strftime("%H:%M") < cfg.market_open) | (ts.dt.strftime("%H:%M") > cfg.market_close)).mean()
    return pd.DataFrame([
        {"check": f"{name}_duplicate_timestamps", "status": "PASS" if dup == 0 else "WARN", "value": dup, "note": "Duplicate timestamp share."},
        {"check": f"{name}_outside_session", "status": "PASS" if outside < 0.01 else "WARN", "value": outside, "note": "Share outside 09:15–15:30; inspect source semantics."},
        {"check": f"{name}_start", "status": "INFO", "value": str(ts.min()), "note": "Observed start."},
        {"check": f"{name}_end", "status": "INFO", "value": str(ts.max()), "note": "Observed end."},
    ])


def audit_interval(df: pd.DataFrame, name: str) -> pd.DataFrame:
    if df.empty or "timestamp" not in df:
        return pd.DataFrame()
    ts = pd.to_datetime(df["timestamp"], errors="coerce").dropna().sort_values().drop_duplicates()
    delta = ts.diff().dropna().dt.total_seconds() / 60.0
    if delta.empty:
        return pd.DataFrame()
    return pd.DataFrame([
        {"check": f"{name}_median_interval_min", "status": "INFO", "value": float(delta.median()), "note": "Median observed spacing."},
        {"check": f"{name}_p95_interval_min", "status": "INFO", "value": float(delta.quantile(0.95)), "note": "95th percentile spacing; large gaps merit review."},
        {"check": f"{name}_largest_gap_min", "status": "WARN" if delta.max() > 30 else "PASS", "value": float(delta.max()), "note": "Largest timestamp gap."},
    ])


def audit_pairing(pairs: pd.DataFrame, options: pd.DataFrame, cfg: AuditConfig) -> pd.DataFrame:
    rows = []
    if options.empty:
        return pd.DataFrame()
    expiry_ok = options.get("expiry", pd.Series(index=options.index, dtype="datetime64[ns]")).notna().mean()
    rows.append({"check": "pairing_expiry_integrity", "status": "PASS" if expiry_ok >= 0.999 else "FAIL", "value": expiry_ok, "note": "Expiry must survive normalization before pairing."})
    if pairs.empty:
        rows.append({"check": "ce_pe_pair_count", "status": "FAIL", "value": 0, "note": "No pairs produced."})
        return pd.DataFrame(rows)
    gap = pd.to_numeric(pairs.get("pair_gap_seconds"), errors="coerce") if "pair_gap_seconds" in pairs else pd.Series(dtype=float)
    if not gap.empty:
        rows.append({"check": "pair_gap_median_sec", "status": "INFO", "value": float(gap.median()), "note": "CE/PE timestamp gap."})
        rows.append({"check": "pair_gap_p95_sec", "status": "INFO", "value": float(gap.quantile(0.95)), "note": "95th percentile CE/PE gap."})
        rows.append({"check": "pair_gap_max_sec", "status": "PASS" if gap.max() <= cfg.pair_tolerance.total_seconds() else "WARN", "value": float(gap.max()), "note": "Maximum permitted gap is 120 seconds."})
    if "expiry_key" in pairs:
        no_exp = (pairs["expiry_key"].astype(str) == "NO_EXPIRY").mean()
        rows.append({"check": "pairs_without_expiry", "status": "PASS" if no_exp == 0 else "FAIL", "value": no_exp, "note": "A non-zero value can create cross-expiry contamination."})
    return pd.DataFrame(rows)


def audit_forward_returns(features: pd.DataFrame, cfg: AuditConfig) -> pd.DataFrame:
    rows = []
    for col in [c for c in features.columns if c.startswith("forward_")]:
        x = _pct(features[col]).replace([np.inf, -np.inf], np.nan).dropna()
        if x.empty:
            continue
        abs_bad = (x.abs() > cfg.max_abs_forward_return).mean()
        rows.append({"check": f"{col}_outlier_share", "status": "PASS" if abs_bad < 0.001 else "WARN", "value": abs_bad, "note": f"Share with |return| > {cfg.max_abs_forward_return:.0%}."})
        rows.append({"check": f"{col}_median", "status": "INFO", "value": float(x.median()), "note": "Median forward return."})
        rows.append({"check": f"{col}_mean", "status": "INFO", "value": float(x.mean()), "note": "Mean forward return; compare with median for tail sensitivity."})
    return pd.DataFrame(rows)


def robust_pattern_table(features: pd.DataFrame, min_obs: int = 30) -> pd.DataFrame:
    rules = {
        "IV rising + spot flat": (features["iv_change"] > 0) & (features["spot_ret_4"].abs() < 0.001),
        "IV falling + spot flat": (features["iv_change"] < 0) & (features["spot_ret_4"].abs() < 0.001),
        "PCR rising": features["pcr_change"] > 0,
        "PCR falling": features["pcr_change"] < 0,
        "VIX rising": features["vix_change"] > 0,
        "VIX falling": features["vix_change"] < 0,
        "Straddle expanding": features["straddle_change"] > 0,
        "Straddle contracting": features["straddle_change"] < 0,
        "Spot uptrend": features["spot_trend"] > 0,
        "Spot downtrend": features["spot_trend"] < 0,
    }
    result = []
    for name, mask in rules.items():
        d = features.loc[mask].copy()
        d = d.dropna(subset=["forward_spot_4", "forward_spot_16", "forward_straddle_4", "forward_straddle_16"])
        if len(d) < min_obs:
            continue
        r4 = d.forward_spot_4
        r16 = d.forward_spot_16
        s4 = d.forward_straddle_4
        result.append({
            "pattern": name,
            "observations": len(d),
            "mean_4_spot": r4.mean(),
            "median_4_spot": r4.median(),
            "mean_16_spot": r16.mean(),
            "median_16_spot": r16.median(),
            "mean_4_straddle": s4.mean(),
            "median_4_straddle": s4.median(),
            "spot_4_up_rate": (r4 > 0).mean(),
            "spot_4_down_rate": (r4 < 0).mean(),
            "tail_robust_mean_4_spot": r4.clip(r4.quantile(0.01), r4.quantile(0.99)).mean(),
        })
    return pd.DataFrame(result).sort_values("observations", ascending=False) if result else pd.DataFrame()


def build_deep_audit(options: pd.DataFrame, spot: pd.DataFrame, vix: pd.DataFrame, pairs: pd.DataFrame, features: pd.DataFrame, cfg: Optional[AuditConfig] = None) -> pd.DataFrame:
    cfg = cfg or AuditConfig()
    pieces = [
        audit_options(options, cfg),
        audit_market_timestamps(spot, "spot", cfg),
        audit_market_timestamps(vix, "vix", cfg) if not vix.empty else pd.DataFrame([{"check": "vix_present", "status": "INFO", "value": 0, "note": "India VIX was not uploaded."}]),
        audit_interval(spot, "spot"),
        audit_interval(vix, "vix") if not vix.empty else pd.DataFrame(),
        audit_pairing(pairs, options, cfg),
        audit_forward_returns(features, cfg),
    ]
    return pd.concat([p for p in pieces if p is not None and not p.empty], ignore_index=True)
