from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ResearchFinding:
    title: str
    evidence: str
    implication: str
    strength: float


def _daily_option_stats(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["date"] = pd.to_datetime(x["date"], errors="coerce").dt.normalize()
    x["expiry"] = pd.to_datetime(x["expiry"], errors="coerce").dt.normalize()
    rows = []
    for day, g in x.groupby("date"):
        if g.empty:
            continue
        expiries = g["expiry"].dropna()
        if expiries.empty:
            continue
        near = expiries[expiries >= day].min()
        u = g[g["expiry"] == near]
        if u.empty:
            continue
        strikes = np.sort(u["strike"].dropna().unique())
        if len(strikes) < 5:
            continue
        ce = u[u["option_type"] == "CE"].set_index("strike")["close"]
        pe = u[u["option_type"] == "PE"].set_index("strike")["close"]
        common = ce.index.intersection(pe.index)
        if len(common) == 0:
            continue
        straddle = (ce.loc[common] + pe.loc[common]).dropna()
        if straddle.empty:
            continue
        # ATM proxy from minimum combined premium; use spot when available for a better anchor.
        spot = pd.to_numeric(u["spot"], errors="coerce").dropna()
        if not spot.empty:
            anchor = float(spot.median())
            atm = float(common[np.argmin(np.abs(common.to_numpy(dtype=float) - anchor))])
        else:
            atm = float(straddle.idxmin())
        atm_premium = float(straddle.loc[atm])
        near_otm = [s for s in common if s < atm]
        near_otm_ce = [s for s in common if s > atm]
        rows.append({"date": day, "expiry": near, "atm": atm, "atm_straddle": atm_premium,
                     "atm_ce": float(ce.get(atm, np.nan)), "atm_pe": float(pe.get(atm, np.nan)),
                     "lower_strike": float(max(near_otm)) if near_otm else np.nan,
                     "upper_strike": float(min(near_otm_ce)) if near_otm_ce else np.nan,
                     "skew": float(pe.get(atm, np.nan) - ce.get(atm, np.nan))})
    return pd.DataFrame(rows).sort_values("date")


def analyze_patterns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[ResearchFinding]]:
    daily = _daily_option_stats(df)
    findings: list[ResearchFinding] = []
    if daily.empty:
        return daily, [ResearchFinding("Insufficient structure", "No complete nearest-expiry daily option snapshots could be reconstructed.", "Do not trust strategy conclusions until expiry/strike/time fields are complete.", 1.0)]

    premium = daily["atm_straddle"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(premium) >= 10:
        q25, q50, q75 = premium.quantile([0.25, 0.50, 0.75])
        high = int((premium >= q75).sum())
        low = int((premium <= q25).sum())
        findings.append(ResearchFinding(
            "Premium regime exists",
            f"ATM straddle premium spans {premium.min():.2f} to {premium.max():.2f}; Q25={q25:.2f}, median={q50:.2f}, Q75={q75:.2f} across {len(premium):,} reconstructed days.",
            "A regime-aware seller should distinguish cheap-premium days from expensive-premium days instead of selling every session.",
            min(1.0, (q75 - q25) / max(abs(q50), 1e-9)),
        ))
        if high > 0 and low > 0:
            findings.append(ResearchFinding(
                "High-premium days are a distinct bucket",
                f"{high:,} days fall in the top quartile of ATM premium and {low:,} in the bottom quartile.",
                "A candidate strategy should test whether entries restricted to elevated premium regimes improve expectancy and drawdown.",
                0.75,
            ))

    skew = daily["skew"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(skew) >= 10:
        pos = int((skew > 0).sum()); neg = int((skew < 0).sum())
        findings.append(ResearchFinding(
            "Call/put premium asymmetry is measurable",
            f"ATM PE-CE premium difference is positive on {pos:,} days and negative on {neg:,} days out of {len(skew):,}.",
            "The engine should test skew-aware strike selection rather than assuming symmetric wings are always optimal.",
            0.65,
        ))

    daily["premium_change"] = daily["atm_straddle"].pct_change()
    change = daily["premium_change"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(change) >= 20:
        vol = change.std()
        shocks = int((change.abs() >= change.abs().quantile(0.9)).sum())
        findings.append(ResearchFinding(
            "Premium shock clustering",
            f"Daily ATM premium change volatility is {vol:.3f}; {shocks:,} observations are in the largest 10% of absolute premium changes.",
            "Stops and holding-period rules should be tested specifically around premium shocks; simple fixed holding periods may be fragile.",
            0.70,
        ))

    return daily, sorted(findings, key=lambda x: x.strength, reverse=True)


def discovery_candidates(daily: pd.DataFrame) -> pd.DataFrame:
    """Produce data-driven research buckets, not a claimed optimal strategy."""
    if daily.empty:
        return pd.DataFrame()
    x = daily.copy()
    x["premium_rank"] = x["atm_straddle"].rank(pct=True)
    x["skew_abs"] = x["skew"].abs()
    buckets = []
    for label, mask in {
        "Low premium": x["premium_rank"] <= 0.25,
        "Middle premium": x["premium_rank"].between(0.25, 0.75, inclusive="right"),
        "High premium": x["premium_rank"] > 0.75,
        "Strong positive skew": x["skew"] > x["skew"].quantile(0.75),
        "Strong negative skew": x["skew"] < x["skew"].quantile(0.25),
    }.items():
        g = x[mask]
        if len(g) < 5:
            continue
        buckets.append({"Research bucket": label, "Days": len(g), "Median ATM premium": g["atm_straddle"].median(), "Median skew": g["skew"].median()})
    return pd.DataFrame(buckets)


def research_report(daily: pd.DataFrame, findings: list[ResearchFinding]) -> str:
    if daily.empty:
        return "The bot could not reconstruct enough complete daily option snapshots to form a defensible pattern report."
    top = findings[:3]
    lines = [
        f"The research bot reconstructed {len(daily):,} daily nearest-expiry snapshots from the uploaded dataset.",
        "",
        "Most important observed patterns:",
    ]
    for i, f in enumerate(top, 1):
        lines.append(f"{i}. {f.title}: {f.evidence}")
        lines.append(f"   Research implication: {f.implication}")
    lines.append("")
    lines.append("The bot does not declare a strategy profitable from these patterns alone. Each candidate must pass an out-of-sample backtest with costs, slippage and drawdown checks.")
    return "\n".join(lines)
