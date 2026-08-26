import io
import json
import math
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

DEFAULT_CLIENT_ID = "1113195747"
DHAN_API = "https://api.dhan.co/v2"
SPOT_TOLERANCE = pd.Timedelta(minutes=20)
PAIR_TOLERANCE = pd.Timedelta(minutes=1)
FUTURE_SPOT_TOLERANCE = pd.Timedelta(minutes=8)
FUTURE_OPTION_TOLERANCE = pd.Timedelta(minutes=2)
HORIZONS_MIN = [1, 3, 5, 10, 15, 30, 60, 120]
QUARTERS = {
    "Q1 2025": (pd.Timestamp("2025-01-01"), pd.Timestamp("2025-03-31 23:59:59")),
    "Q2 2025": (pd.Timestamp("2025-04-01"), pd.Timestamp("2025-06-30 23:59:59")),
    "Q3 2025": (pd.Timestamp("2025-07-01"), pd.Timestamp("2025-09-30 23:59:59")),
    "Q4 2025": (pd.Timestamp("2025-10-01"), pd.Timestamp("2025-12-31 23:59:59")),
    "Q1 2026": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-31 23:59:59")),
    "Q2 2026": (pd.Timestamp("2026-04-01"), pd.Timestamp("2026-06-30 23:59:59")),
    "Q3 2026": (pd.Timestamp("2026-07-01"), pd.Timestamp("2026-08-26 23:59:59")),
}

st.set_page_config(page_title="FRIDAY", layout="wide")


def parse_dt(values):
    s = pd.Series(values)
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.8 and n.notna().any():
        med = float(n.dropna().abs().median())
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(s, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).astype("datetime64[ns]")


def find_col(df, names):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in lookup:
            return lookup[key]
    for key, original in lookup.items():
        if any(name.lower().replace(" ", "_") in key for name in names):
            return original
    return None


def num_col(df, names):
    c = find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c else None


def read_csv_files(files):
    frames = []
    for f in files or []:
        d = pd.read_csv(f, low_memory=False)
        if not d.empty:
            frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_options(df):
    ts = find_col(df, ["timestamp", "datetime", "date_time", "exchange_timestamp", "timestamp_ist", "time", "date"])
    strike = find_col(df, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = find_col(df, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = find_col(df, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    symbol = find_col(df, ["symbol", "trading_symbol", "tradingsymbol", "instrument_name"])
    if ts is None or strike is None or side is None:
        raise ValueError(f"Options require timestamp, strike and CE/PE columns. Found: {list(df.columns)}")
    out = pd.DataFrame({
        "timestamp": parse_dt(df[ts]),
        "strike": pd.to_numeric(df[strike], errors="coerce"),
        "side": df[side].astype(str).str.upper().str.strip().replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"}),
    })
    if expiry is not None:
        out["expiry"] = pd.to_datetime(df[expiry], errors="coerce").dt.normalize()
    elif symbol is not None:
        text = df[symbol].astype(str)
        out["expiry"] = pd.to_datetime(text.str.extract(r"(20\d{2}[-/]\d{2}[-/]\d{2})")[0], errors="coerce").dt.normalize()
    else:
        out["expiry"] = pd.NaT
    for names, target in [
        (["open"], "open"), (["high"], "high"), (["low"], "low"),
        (["close", "ltp", "last_price", "price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["volume", "vol", "traded_volume"], "volume"),
    ]:
        v = num_col(df, names)
        out[target] = v if v is not None else np.nan
    out = out.dropna(subset=["timestamp", "strike"])
    out = out[out.side.isin(["CE", "PE"])].copy()
    return out.sort_values("timestamp").reset_index(drop=True)


def normalize_spot(df):
    ts = find_col(df, ["timestamp", "datetime", "date_time", "exchange_timestamp", "timestamp_ist", "time", "date"])
    px = find_col(df, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if ts is None or px is None:
        raise ValueError("NIFTY Spot: timestamp/close not found")
    return pd.DataFrame({"timestamp": parse_dt(df[ts]), "spot": pd.to_numeric(df[px], errors="coerce")}).dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df):
    ts = find_col(df, ["timestamp", "datetime", "date_time", "exchange_timestamp", "timestamp_ist", "time", "date"])
    px = find_col(df, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if ts is None or px is None:
        raise ValueError("India VIX: timestamp/close not found")
    return pd.DataFrame({"timestamp": parse_dt(df[ts]), "vix": pd.to_numeric(df[px], errors="coerce")}).dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def dhan_get(path, token, client_id):
    req = urllib.request.Request(DHAN_API + path, method="GET", headers={"Accept": "application/json", "access-token": token, "client-id": client_id})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def dhan_profile(token, client_id):
    if not token:
        return False, "No token entered"
    try:
        body = dhan_get("/profile", token, client_id)
        validity = body.get("data", {}).get("tokenValidity") or body.get("tokenValidity")
        return True, f"Token verified{f' | Valid until: {validity}' if validity else ''}"
    except Exception as exc:
        return False, str(exc)


def slice_quarter(df, q):
    a, b = QUARTERS[q]
    return df[(df.timestamp >= a) & (df.timestamp <= b)].copy()


def attach_expiry(options):
    out = options.copy()
    missing_before = float(out.expiry.isna().mean()) if len(out) else 0.0
    has_expiry = missing_before == 0.0
    out["expiry_source"] = np.where(out["expiry"].notna(), "file", "rolling_contract")
    out["expiry_key"] = out["expiry"].dt.strftime("%Y-%m-%d")
    return out, missing_before, has_expiry


def align_market(options, spot, vix):
    x = pd.merge_asof(options.sort_values("timestamp"), spot.sort_values("timestamp"), on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE)
    x = x.dropna(subset=["spot"])
    if not vix.empty:
        x = pd.merge_asof(x.sort_values("timestamp"), vix.sort_values("timestamp"), on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE, suffixes=("", "_state"))
        if "vix_state" in x.columns:
            x["vix"] = x["vix_state"].combine_first(x.get("vix", np.nan))
            x = x.drop(columns=["vix_state"])
    return x.sort_values("timestamp").reset_index(drop=True)


def pair_ce_pe(aligned, has_expiry):
    x = aligned.copy()
    if x.empty:
        return pd.DataFrame()
    ce = x[x.side == "CE"].copy()
    pe = x[x.side == "PE"].copy()
    if ce.empty or pe.empty:
        return pd.DataFrame()
    pe = pe.rename(columns={"timestamp": "pe_timestamp", "close": "pe_close", "iv": "pe_iv", "oi": "pe_oi", "volume": "pe_volume"})
    by_cols = ["expiry", "strike"] if has_expiry else ["strike"]
    ce_sort = ["timestamp"] + by_cols
    pe_sort = ["pe_timestamp"] + by_cols
    ce = ce.sort_values(ce_sort, kind="mergesort")
    pe = pe.sort_values(pe_sort, kind="mergesort")
    out = pd.merge_asof(ce, pe, left_on="timestamp", right_on="pe_timestamp", by=by_cols, direction="nearest", tolerance=PAIR_TOLERANCE, suffixes=("", "_pe_dup"))
    out = out.dropna(subset=["pe_timestamp", "close", "pe_close"]).copy()
    if out.empty:
        return out
    out["pair_gap_seconds"] = (out.timestamp - out.pe_timestamp).abs().dt.total_seconds()
    out["expiry_pairing_mode"] = "explicit_expiry" if has_expiry else "rolling_contract_same_strike"
    # merge_asof may rename overlapping state columns; guarantee a canonical spot column.
    if "spot" not in out.columns:
        for candidate in ["spot_pe_dup", "spot_pe", "spot_dup"]:
            if candidate in out.columns:
                out["spot"] = out[candidate]
                break
    if "vix" not in out.columns:
        for candidate in ["vix_pe_dup", "vix_pe", "vix_dup"]:
            if candidate in out.columns:
                out["vix"] = out[candidate]
                break
    return out.sort_values("timestamp").reset_index(drop=True)


def build_market_features(pairs, spot, vix):
    if pairs.empty:
        return pd.DataFrame()
    p = pairs.copy()
    # Never assume the CE-side merge preserved the underlying state columns.
    if "spot" not in p.columns:
        p = pd.merge_asof(p.sort_values("timestamp"), spot.sort_values("timestamp"), on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE, suffixes=("", "_state"))
        if "spot_state" in p.columns and "spot" not in p.columns:
            p["spot"] = p["spot_state"]
    if "spot" not in p.columns:
        raise ValueError("NIFTY Spot could not be attached to paired option observations.")
    p["spot"] = pd.to_numeric(p["spot"], errors="coerce")
    if "vix" not in p.columns and not vix.empty:
        p = pd.merge_asof(p.sort_values("timestamp"), vix.sort_values("timestamp"), on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE, suffixes=("", "_state"))
        if "vix_state" in p.columns and "vix" not in p.columns:
            p["vix"] = p["vix_state"]
    p = p.dropna(subset=["spot"]).copy()
    p["distance_to_spot"] = (p["strike"] - p["spot"]).abs()
    p["dte"] = np.nan
    if "expiry" in p.columns and p["expiry"].notna().any():
        p["dte"] = (p["expiry"] - p["timestamp"].dt.normalize()).dt.total_seconds() / 86400.0
        atm = p.sort_values(["timestamp", "expiry", "distance_to_spot"], kind="mergesort").drop_duplicates(["timestamp", "expiry"])
    else:
        atm = p.sort_values(["timestamp", "distance_to_spot"], kind="mergesort").drop_duplicates("timestamp")
    primary = atm.sort_values(["timestamp", "dte", "distance_to_spot"], kind="mergesort", na_position="last").drop_duplicates("timestamp")
    keep = ["timestamp", "strike", "spot", "vix", "close", "pe_close", "iv", "pe_iv", "oi", "pe_oi", "volume", "pe_volume", "pair_gap_seconds", "dte", "expiry_pairing_mode"]
    keep = [c for c in keep if c in primary.columns]
    f = primary[keep].copy().rename(columns={"strike": "atm_strike", "close": "ce_close", "iv": "ce_iv", "oi": "ce_oi", "volume": "ce_volume"})
    if "expiry" in primary.columns:
        f["expiry"] = primary["expiry"].values
    f["pcr_oi"] = f["pe_oi"] / f["ce_oi"].replace(0, np.nan)
    f["straddle"] = f["ce_close"] + f["pe_close"]
    f["iv_mid"] = f[["ce_iv", "pe_iv"]].mean(axis=1)
    f["iv_skew"] = f["ce_iv"] - f["pe_iv"]
    f["oi_imbalance"] = (f["pe_oi"] - f["ce_oi"]) / (f["pe_oi"] + f["ce_oi"]).replace(0, np.nan)
    f["volume_imbalance"] = (f["pe_volume"] - f["ce_volume"]) / (f["pe_volume"] + f["ce_volume"]).replace(0, np.nan)

    s = spot.copy().sort_values("timestamp")
    s["spot_ret_15"] = s["spot"].pct_change(1); s["spot_ret_30"] = s["spot"].pct_change(2); s["spot_ret_60"] = s["spot"].pct_change(4); s["spot_vol_60"] = s["spot"].pct_change().rolling(4).std()
    v = vix.copy().sort_values("timestamp")
    if not v.empty:
        v["vix_chg_15"] = v["vix"].diff(1); v["vix_ret_15"] = v["vix"].pct_change(1); v["vix_chg_30"] = v["vix"].diff(2); v["vix_chg_60"] = v["vix"].diff(4)
    f = pd.merge_asof(f.sort_values("timestamp"), s[["timestamp", "spot_ret_15", "spot_ret_30", "spot_ret_60", "spot_vol_60"]], on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE)
    if not v.empty:
        f = pd.merge_asof(f.sort_values("timestamp"), v[["timestamp", "vix", "vix_chg_15", "vix_ret_15", "vix_chg_30", "vix_chg_60"]], on="timestamp", direction="backward", tolerance=SPOT_TOLERANCE, suffixes=("", "_state"))
        if "vix_state" in f.columns:
            f["vix"] = f["vix_state"].combine_first(f.get("vix", np.nan)); f = f.drop(columns=["vix_state"])
    for c in ["ce_close", "pe_close", "straddle", "ce_iv", "pe_iv", "iv_mid", "pcr_oi", "oi_imbalance", "volume_imbalance"]:
        f[f"{c}_chg_1"] = f[c].pct_change(1); f[f"{c}_chg_5"] = f[c].pct_change(5); f[f"{c}_chg_15"] = f[c].pct_change(15)
    f["time_minute"] = f["timestamp"].dt.hour * 60 + f["timestamp"].dt.minute
    f["minute_bucket"] = pd.cut(f["time_minute"], bins=[0, 570, 630, 720, 840, 960, 1440], labels=["open", "morning", "midday", "afternoon", "close", "other"], right=False)
    if "expiry" in f.columns:
        f["is_expiry_day"] = f["timestamp"].dt.normalize().eq(f["expiry"])
    f["straddle_level_log"] = np.log(f["straddle"].clip(lower=1e-6))
    return f.sort_values("timestamp").reset_index(drop=True)


def forward_values(base, target, source_col, horizon_min, tolerance):
    left = base[["timestamp"]].copy().sort_values("timestamp").reset_index(drop=True)
    left["target_ts"] = left["timestamp"] + pd.Timedelta(minutes=horizon_min)
    right = target[["timestamp", source_col]].rename(columns={"timestamp": "future_timestamp", source_col: "future_value"}).sort_values("future_timestamp").reset_index(drop=True)
    joined = pd.merge_asof(left, right, left_on="target_ts", right_on="future_timestamp", direction="forward", tolerance=tolerance)
    return joined["future_value"]


def add_forward_outcomes(f, spot):
    x = f.copy().sort_values("timestamp").reset_index(drop=True)
    opt_series = x[["timestamp", "straddle"]].copy()
    spot_series = spot[["timestamp", "spot"]].copy().sort_values("timestamp")
    for h in HORIZONS_MIN:
        fut_str = forward_values(x, opt_series, "straddle", h, FUTURE_OPTION_TOLERANCE)
        fut_spot = forward_values(x, spot_series, "spot", h, FUTURE_SPOT_TOLERANCE)
        x[f"future_straddle_ret_{h}m"] = fut_str.to_numpy() / x["straddle"].to_numpy() - 1
        x[f"future_spot_ret_{h}m"] = fut_spot.to_numpy() / x["spot"].to_numpy() - 1
    return x


def robust_stats(series):
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) == 0:
        return {"n": 0, "mean": np.nan, "median": np.nan, "trimmed_mean": np.nan, "up_rate": np.nan, "p05": np.nan, "p95": np.nan}
    lo, hi = s.quantile([0.05, 0.95]); clipped = s.clip(lo, hi)
    return {"n": int(len(s)), "mean": float(s.mean()), "median": float(s.median()), "trimmed_mean": float(clipped.mean()), "up_rate": float((s > 0).mean()), "p05": float(s.quantile(.05)), "p95": float(s.quantile(.95))}


def build_candidate_rules(df):
    target_col = "future_spot_ret_15m"
    if target_col not in df.columns: return pd.DataFrame()
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and c not in {"atm_strike", "time_minute"}]
    base_stats = robust_stats(df[target_col]); rows = []
    for c in numeric:
        s = pd.to_numeric(df[c], errors="coerce")
        if s.notna().sum() < 100: continue
        q10, q20, q80, q90 = s.quantile([.1, .2, .8, .9])
        for name, mask in [(f"{c} <= q10", s <= q10), (f"{c} >= q90", s >= q90), (f"{c} <= q20", s <= q20), (f"{c} >= q80", s >= q80)]:
            d = df.loc[mask & df[target_col].notna(), target_col]; stt = robust_stats(d)
            if stt["n"] < 50: continue
            effect = abs(stt["trimmed_mean"] - base_stats["trimmed_mean"]); score = effect * math.sqrt(stt["n"]) if pd.notna(effect) else 0.0
            rows.append({"rule": name, "feature": c, "n": stt["n"], "mean": stt["mean"], "median": stt["median"], "trimmed_mean": stt["trimmed_mean"], "up_rate": stt["up_rate"], "baseline_trimmed_mean": base_stats["trimmed_mean"], "effect_vs_baseline": effect, "priority_score": score})
    return pd.DataFrame(rows).sort_values(["priority_score", "n"], ascending=False).reset_index(drop=True) if rows else pd.DataFrame()


def build_interactions(df, top_features):
    target = "future_spot_ret_15m"
    if target not in df.columns: return pd.DataFrame()
    cols = [c for c in top_features if c in df.columns][:20]; rows = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]; sa, sb = pd.to_numeric(df[a], errors="coerce"), pd.to_numeric(df[b], errors="coerce"); qa, qb = sa.quantile([.2, .8]), sb.quantile([.2, .8])
            masks = {f"{a} low + {b} low": (sa <= qa[.2]) & (sb <= qb[.2]), f"{a} high + {b} high": (sa >= qa[.8]) & (sb >= qb[.8]), f"{a} high + {b} low": (sa >= qa[.8]) & (sb <= qb[.2]), f"{a} low + {b} high": (sa <= qa[.2]) & (sb >= qb[.8])}
            for name, mask in masks.items():
                stt = robust_stats(df.loc[mask & df[target].notna(), target])
                if stt["n"] >= 40: rows.append({"interaction": name, "feature_a": a, "feature_b": b, **stt})
    return pd.DataFrame(rows).sort_values(["trimmed_mean", "n"], ascending=[False, False]).reset_index(drop=True) if rows else pd.DataFrame()


def audit_table(options, spot, vix, aligned, pairs, features, missing_before, has_expiry):
    rows = [("Option rows", len(options), "INFO"), ("Spot rows", len(spot), "INFO"), ("VIX rows", len(vix), "INFO"), ("Option+Spot synced", len(aligned), "INFO"), ("CE rows", int((options.side == "CE").sum()), "INFO"), ("PE rows", int((options.side == "PE").sum()), "INFO"), ("CE/PE pairs", len(pairs), "PASS" if len(pairs) else "FAIL"), ("ATM observations", len(features), "PASS" if len(features) else "FAIL"), ("Expiry mode", "explicit" if has_expiry else "rolling-contract same-strike", "PASS"), ("Missing expiry in raw file", f"{missing_before*100:.2f}%", "INFO"), ("Option start", str(options.timestamp.min()) if not options.empty else "N/A", "INFO"), ("Option end", str(options.timestamp.max()) if not options.empty else "N/A", "INFO"), ("Spot start", str(spot.timestamp.min()) if not spot.empty else "N/A", "INFO"), ("Spot end", str(spot.timestamp.max()) if not spot.empty else "N/A", "INFO")]
    return pd.DataFrame(rows, columns=["check", "value", "status"])


def md_table(df):
    if df is None or df.empty: return "_No rows._"
    cols = [str(c) for c in df.columns]; lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for row in df.itertuples(index=False, name=None):
        vals = []
        for v in row:
            if pd.isna(v): vals.append("")
            elif isinstance(v, float): vals.append(f"{v:.6g}")
            else: vals.append(str(v).replace("|", "\\|"))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def build_report(q, audit, fruit, interactions, validation, warnings):
    lines = [f"# FRIDAY — DEEP RESEARCH AUDIT — {q}", "", "## Scope", f"Analysis period: **{q}**", "Sources: NIFTY Options + NIFTY Spot + India VIX. Futures excluded.", "", "## Research Clock", "Options: 1-minute event clock. NIFTY Spot: 15-minute state clock. India VIX: 15-minute state clock.", "Spot/VIX state is aligned backward only; future state information is never used.", "", "## Data Integrity / Diagnostics", md_table(audit), "", "## Fruitfulness Discovery", "FRIDAY tests raw and derived numeric variables across multiple threshold regions before interaction research.", md_table(fruit.head(100)), "", "## Interaction Discovery", md_table(interactions.head(100)), "", "## Train/Test Diagnostic", md_table(validation), "", "## Research Gates / Warnings"]
    lines.extend([f"- {w}" for w in warnings] if warnings else ["- No blocking warnings."])
    lines += ["", "## Important", "This is a discovery/audit engine. A candidate is not a trading strategy until it survives out-of-sample and multi-period validation."]
    return "\n".join(lines)


def process_quarter(q, options_all, spot_all, vix_all):
    options = slice_quarter(options_all, q); spot = slice_quarter(spot_all, q); vix = slice_quarter(vix_all, q) if not vix_all.empty else pd.DataFrame(columns=["timestamp", "vix"])
    if options.empty or spot.empty: raise ValueError(f"{q}: options and NIFTY Spot are mandatory and must contain data for this period.")
    options, missing_before, has_expiry = attach_expiry(options)
    aligned = align_market(options, spot, vix)
    if aligned.empty: raise ValueError(f"{q}: no option/spot synchronization within {SPOT_TOLERANCE}.")
    pairs = pair_ce_pe(aligned, has_expiry)
    if pairs.empty: raise ValueError(f"{q}: no valid CE/PE pairs after pairing.")
    features = build_market_features(pairs, spot, vix)
    if features.empty: raise ValueError(f"{q}: no ATM observations produced.")
    features = add_forward_outcomes(features, spot)
    fruit = build_candidate_rules(features)
    top_features = fruit.sort_values("priority_score", ascending=False).feature.drop_duplicates().head(20).tolist() if not fruit.empty else []
    interactions = build_interactions(features, top_features)
    split = max(1, int(len(features) * 0.6)); train = features.iloc[:split]; test = features.iloc[split:]
    validation = pd.DataFrame([{"sample": "train", **robust_stats(train.get("future_spot_ret_15m", pd.Series(dtype=float)))}, {"sample": "test", **robust_stats(test.get("future_spot_ret_15m", pd.Series(dtype=float)))}])
    warnings = []
    if not has_expiry: warnings.append("Options file has no historical expiry field; FRIDAY used rolling-contract same-strike pairing and did not infer expiry from a current calendar.")
    if fruit.empty: warnings.append("No single-variable candidate met the minimum quality threshold.")
    audit = audit_table(options, spot, vix, aligned, pairs, features, missing_before, has_expiry)
    report = build_report(q, audit, fruit, interactions, validation, warnings)
    return {"audit": audit, "features": features, "fruitfulness": fruit, "interactions": interactions, "validation": validation, "pairs": pairs, "report": report}


def make_zip(results):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for q, r in results.items():
            safe = q.replace(" ", "_")
            z.writestr(f"FRIDAY_{safe}_DEEP_AUDIT.md", r["report"])
            for key in ["features", "fruitfulness", "interactions", "validation", "pairs", "audit"]:
                z.writestr(f"FRIDAY_{safe}_{key}.csv", r[key].to_csv(index=False))
        z.writestr("README.txt", "FRIDAY research engine. Options 1m, NIFTY Spot 15m, India VIX 15m. Expiry is preserved when present; otherwise rolling-contract same-strike mode is used without inventing expiry.")
    return buf.getvalue()


def main():
    with st.sidebar:
        st.subheader("FRIDAY")
        client_id = st.text_input("Dhan Client ID", DEFAULT_CLIENT_ID).strip()
        token = st.text_input("Dhan Access Token", type="password").strip()
        if token:
            ok, msg = dhan_profile(token, client_id); (st.success if ok else st.error)(msg)
    st.title("FRIDAY — AUTONOMOUS MARKET RESEARCH ENGINE")
    st.caption("Options 1m + NIFTY Spot 15m + India VIX 15m. Raw-data fruitfulness discovery with safe pairing.")
    selected = st.multiselect("Quarters", list(QUARTERS.keys()), default=["Q1 2025"])
    opt_files = st.file_uploader("NIFTY Options CSV(s)", type=["csv"], accept_multiple_files=True)
    spot_files = st.file_uploader("NIFTY Spot CSV(s)", type=["csv"], accept_multiple_files=True)
    vix_files = st.file_uploader("India VIX CSV(s)", type=["csv"], accept_multiple_files=True)
    if st.button("RUN FRIDAY DEEP AUDIT", use_container_width=True):
        if not selected or not opt_files or not spot_files:
            st.error("Select at least one quarter and upload Options + NIFTY Spot data."); return
        bar = st.progress(0, text="FRIDAY: 0%"); status = st.empty(); started = time.time()
        try:
            options_all = normalize_options(read_csv_files(opt_files)); spot_all = normalize_spot(read_csv_files(spot_files)); vix_all = normalize_vix(read_csv_files(vix_files)) if vix_files else pd.DataFrame(columns=["timestamp", "vix"])
            results = {}; errors = []
            for i, q in enumerate(selected, start=1):
                status.info(f"FRIDAY: {q} — auditing → aligning → discovering → testing interactions...")
                try: results[q] = process_quarter(q, options_all, spot_all, vix_all)
                except Exception as exc: errors.append((q, str(exc)))
                bar.progress(i / len(selected), text=f"FRIDAY: {i/len(selected)*100:.1f}% — {q}")
            if not results: raise RuntimeError("No quarter completed. " + " | ".join(f"{q}: {e}" for q, e in errors))
            for q, r in results.items():
                st.markdown(f"### {q} — complete"); st.success(f"ATM observations: {len(r['features']):,} | CE/PE pairs: {len(r['pairs']):,} | fruitful single-factor candidates: {len(r['fruitfulness']):,}")
                st.download_button(f"DOWNLOAD {q} REPORT", r["report"].encode(), f"FRIDAY_{q.replace(' ','_')}_DEEP_AUDIT.md", "text/markdown", key=f"report_{q}", use_container_width=True)
                st.download_button(f"DOWNLOAD {q} FULL PACKAGE", make_zip({q: r}), f"FRIDAY_{q.replace(' ','_')}_FULL_RESEARCH.zip", "application/zip", key=f"zip_{q}", use_container_width=True)
                st.dataframe(r["fruitfulness"].head(25), use_container_width=True, hide_index=True)
            if len(results) > 1: st.download_button("DOWNLOAD ALL SELECTED QUARTERS", make_zip(results), "FRIDAY_ALL_SELECTED_QUARTERS_RESEARCH.zip", "application/zip", use_container_width=True)
            if errors: st.warning("Some quarters failed: " + " | ".join(f"{q}: {e}" for q, e in errors))
            status.success(f"FRIDAY finished in {time.time() - started:.1f}s"); bar.progress(1.0, text="FRIDAY: 100% ✅")
        except Exception as exc:
            status.error(f"FRIDAY stopped after {time.time() - started:.1f}s: {exc}"); st.exception(exc)


if __name__ == "__main__":
    main()
