import io
import json
import math
import time
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

# Optional ML stack. FRIDAY will fail gracefully with a clear message if absent.
try:
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.ensemble import ExtraTreesRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, mean_absolute_error, r2_score
    from sklearn.feature_selection import mutual_info_regression
    SKLEARN_OK = True
except Exception:
    SKLEARN_OK = False

st.set_page_config(page_title="FRIDAY — Autonomous Research Engine", layout="wide")

QUARTERS = {
    "Q1 2024": ("2024-01-01", "2024-03-31 23:59:59"),
    "Q2 2024": ("2024-04-01", "2024-06-30 23:59:59"),
    "Q3 2024": ("2024-07-01", "2024-09-30 23:59:59"),
    "Q4 2024": ("2024-10-01", "2024-12-31 23:59:59"),
    "Q1 2025": ("2025-01-01", "2025-03-31 23:59:59"),
    "Q2 2025": ("2025-04-01", "2025-06-30 23:59:59"),
    "Q3 2025": ("2025-07-01", "2025-09-30 23:59:59"),
    "Q4 2025": ("2025-10-01", "2025-12-31 23:59:59"),
    "Q1 2026": ("2026-01-01", "2026-03-31 23:59:59"),
    "Q2 2026": ("2026-04-01", "2026-06-30 23:59:59"),
    "Q3 2026": ("2026-07-01", "2026-09-30 23:59:59"),
    "Q4 2026": ("2026-10-01", "2026-12-31 23:59:59"),
}
HORIZONS = [1, 3, 5, 10, 15, 30, 60, 120]
DISCOVERY_SAMPLE = 30000
MAX_BASE_FEATURES = 40
MAX_INTERACTION_SEEDS = 50
PAIR_MIN_N = 60


def _find_col(df, names):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    norm_names = [n.lower().replace(" ", "_") for n in names]
    for n in norm_names:
        if n in lookup:
            return lookup[n]
    for k, v in lookup.items():
        if any(n in k for n in norm_names):
            return v
    return None


def _parse_dt(s):
    x = pd.Series(s)
    n = pd.to_numeric(x, errors="coerce")
    if n.notna().mean() > 0.8 and n.notna().any():
        med = float(n.dropna().abs().median())
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(n, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(x, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(x, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)


def _load_csvs(files, usecols=None):
    frames = []
    for f in files or []:
        d = pd.read_csv(f, usecols=usecols, low_memory=False)
        if not d.empty:
            frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_options(raw):
    ts = _find_col(raw, ["timestamp", "datetime_ist", "datetime", "exchange_timestamp", "time", "date"])
    side = _find_col(raw, ["option_type", "side", "optiontype", "type", "cp", "ce_pe", "call_put"])
    strike = _find_col(raw, ["strike_price", "strike", "strikeprice", "strike_px"])
    offset = _find_col(raw, ["strike_offset", "offset"])
    requested = _find_col(raw, ["requested_strike", "requestedstrike"])
    expiry_flag = _find_col(raw, ["expiry_flag", "expiryflag"])
    expiry_code = _find_col(raw, ["expiry_code", "expirycode"])
    spot = _find_col(raw, ["spot", "underlying_spot", "nifty_spot"])
    if ts is None or side is None or strike is None:
        raise ValueError(f"Options need timestamp, option_type and strike. Found: {list(raw.columns)}")
    out = pd.DataFrame({
        "timestamp": _parse_dt(raw[ts]),
        "side": raw[side].astype(str).str.upper().str.strip().replace({"C": "CALL", "CALL": "CALL", "P": "PUT", "PUT": "PUT"}),
        "strike": pd.to_numeric(raw[strike], errors="coerce"),
    })
    out["strike_offset"] = pd.to_numeric(raw[offset], errors="coerce") if offset else np.nan
    out["requested_strike"] = raw[requested].astype(str) if requested else ""
    out["expiry_flag"] = raw[expiry_flag].astype(str).str.upper() if expiry_flag else ""
    out["expiry_code"] = pd.to_numeric(raw[expiry_code], errors="coerce") if expiry_code else np.nan
    out["option_spot"] = pd.to_numeric(raw[spot], errors="coerce") if spot else np.nan
    for cands, target in [
        (["open"], "open"), (["high"], "high"), (["low"], "low"),
        (["close", "ltp", "last_price", "price"], "close"),
        (["volume", "vol", "traded_volume"], "volume"),
        (["oi", "open_interest", "openinterest"], "oi"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
    ]:
        c = _find_col(raw, cands)
        out[target] = pd.to_numeric(raw[c], errors="coerce") if c else np.nan
    out = out.dropna(subset=["timestamp", "strike"])
    out = out[out.side.isin(["CALL", "PUT"])].copy()
    return out.sort_values(["timestamp", "strike_offset", "side"]).reset_index(drop=True)


def normalize_market(raw, kind):
    ts = _find_col(raw, ["timestamp", "datetime_ist", "datetime", "exchange_timestamp", "time", "date"])
    px = _find_col(raw, ["close", "ltp", "last_price", "spot", "nifty", "vix", "index_close", "price"])
    if ts is None or px is None:
        raise ValueError(f"{kind}: timestamp/close column not found. Found: {list(raw.columns)}")
    out = pd.DataFrame({"timestamp": _parse_dt(raw[ts]), "value": pd.to_numeric(raw[px], errors="coerce")}).dropna()
    return out.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def quarter_slice(df, q):
    a = pd.Timestamp(QUARTERS[q][0])
    b = pd.Timestamp(QUARTERS[q][1])
    return df[(df.timestamp >= a) & (df.timestamp <= b)].copy()


def audit_options(o):
    rows = [
        ("Rows", len(o), "INFO"),
        ("CALL rows", int((o.side == "CALL").sum()), "INFO"),
        ("PUT rows", int((o.side == "PUT").sum()), "INFO"),
        ("Distinct 1m timestamps", o.timestamp.nunique(), "INFO"),
        ("Distinct strike offsets", int(o.strike_offset.nunique()), "INFO"),
        ("Missing offset", f"{o.strike_offset.isna().mean()*100:.2f}%", "WARN" if o.strike_offset.isna().any() else "PASS"),
        ("Missing IV", f"{o.iv.isna().mean()*100:.2f}%", "WARN" if o.iv.isna().any() else "PASS"),
        ("Missing OI", f"{o.oi.isna().mean()*100:.2f}%", "WARN" if o.oi.isna().any() else "PASS"),
        ("Missing volume", f"{o.volume.isna().mean()*100:.2f}%", "WARN" if o.volume.isna().any() else "PASS"),
    ]
    key = ["timestamp", "strike", "side", "strike_offset", "expiry_flag", "expiry_code"]
    dup = o.duplicated(key).mean() if len(o) else 0
    rows.append(("Duplicate contract rows", f"{dup*100:.2f}%", "WARN" if dup > 0.001 else "PASS"))
    counts = o.groupby("timestamp").size()
    rows.append(("Median rows/timestamp", float(counts.median()) if len(counts) else np.nan, "INFO"))
    rows.append(("Complete 42-row timestamps", f"{(counts.eq(42).mean()*100):.2f}%" if len(counts) else "0.00%", "INFO"))
    if o["expiry_code"].notna().any():
        rows.append(("Expiry code(s)", ",".join(map(str, sorted(o.expiry_code.dropna().unique())[:10])), "INFO"))
    if o["expiry_flag"].astype(str).str.len().gt(0).any():
        rows.append(("Expiry flag(s)", ",".join(sorted(o.expiry_flag.astype(str).unique())[:10]), "INFO"))
    return pd.DataFrame(rows, columns=["check", "value", "status"])


def audit_market(df, name, expected_minutes):
    gaps = df.timestamp.sort_values().diff().dropna().dt.total_seconds()/60
    rows = [
        ("Rows", len(df), "INFO"),
        ("Start", str(df.timestamp.min()), "INFO"),
        ("End", str(df.timestamp.max()), "INFO"),
        ("Median gap (min)", float(gaps.median()) if len(gaps) else np.nan, "INFO"),
        ("P95 gap (min)", float(gaps.quantile(.95)) if len(gaps) else np.nan, "INFO"),
    ]
    bad = int((gaps.round(6) != expected_minutes).sum()) if len(gaps) else 0
    rows.append(("Non-15m cadence gaps", bad, "WARN" if bad > 0 else "PASS"))
    return pd.DataFrame(rows, columns=["check", "value", "status"])


def build_option_matrix(o):
    gcols = ["timestamp", "strike_offset", "side"]
    dup = o.groupby(gcols).size()
    if (dup > 1).any():
        o = o.sort_values("timestamp").drop_duplicates(gcols, keep="last")
    value_cols = ["close", "iv", "oi", "volume", "open", "high", "low", "strike", "option_spot"]
    pieces = []
    for metric in value_cols:
        p = o.pivot(index="timestamp", columns=["side", "strike_offset"], values=metric)
        p.columns = [f"{metric}_{side}_{int(off)}" for side, off in p.columns]
        pieces.append(p)
    wide = pd.concat(pieces, axis=1).sort_index()
    meta = o.groupby("timestamp").agg(expiry_flag=("expiry_flag", "first"), expiry_code=("expiry_code", "first"), option_spot=("option_spot", "median"))
    return wide.join(meta).reset_index()


def add_surface_features(w):
    x = w.copy()
    def col(name):
        return x[name] if name in x else pd.Series(np.nan, index=x.index)
    additions = {}
    for off in range(-10, 11):
        c, p = col(f"close_CALL_{off}"), col(f"close_PUT_{off}")
        ci, pi = col(f"iv_CALL_{off}"), col(f"iv_PUT_{off}")
        co, po = col(f"oi_CALL_{off}"), col(f"oi_PUT_{off}")
        cv, pv = col(f"volume_CALL_{off}"), col(f"volume_PUT_{off}")
        additions[f"straddle_{off}"] = c + p
        additions[f"iv_mid_{off}"] = pd.concat([ci, pi], axis=1).mean(axis=1)
        additions[f"iv_skew_{off}"] = ci - pi
        additions[f"pcr_oi_{off}"] = po / co.replace(0, np.nan)
        additions[f"oi_imb_{off}"] = (po - co) / (po + co).replace(0, np.nan)
        additions[f"vol_imb_{off}"] = (pv - cv) / (pv + cv).replace(0, np.nan)
    y = pd.concat([x, pd.DataFrame(additions, index=x.index)], axis=1)
    core = [f"straddle_{i}" for i in range(-10, 11)]
    derived = {
        "atm_straddle": y["straddle_0"],
        "atm_iv": y["iv_mid_0"],
        "atm_pcr_oi": y["pcr_oi_0"],
        "atm_oi_imb": y["oi_imb_0"],
        "atm_vol_imb": y["vol_imb_0"],
        "atm_iv_skew": y["iv_skew_0"],
        "straddle_wing_avg": y[[f"straddle_{i}" for i in (-10,-8,-6,-4,-2,2,4,6,8,10)]].mean(axis=1),
    }
    d = pd.concat([y, pd.DataFrame(derived, index=y.index)], axis=1)
    d["straddle_curvature"] = d["straddle_wing_avg"] - d["atm_straddle"]
    d["call_put_iv_spread"] = d["iv_mid_10"] - d["iv_mid_-10"]
    d["straddle_range"] = d[core].max(axis=1) - d[core].min(axis=1)
    chg = {}
    for c in ["atm_straddle","atm_iv","atm_pcr_oi","atm_oi_imb","atm_vol_imb","atm_iv_skew","straddle_curvature"]:
        for n in (1,3,5,15):
            chg[f"{c}_chg_{n}m"] = d[c].pct_change(n, fill_method=None)
    return pd.concat([d, pd.DataFrame(chg, index=d.index)], axis=1)


def align_15m_state(option_features, spot15, vix15):
    left = option_features.sort_values("timestamp").copy()
    s = spot15.rename(columns={"value":"spot"}).sort_values("timestamp")
    v = vix15.rename(columns={"value":"vix"}).sort_values("timestamp")
    left = pd.merge_asof(left, s, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20))
    left = pd.merge_asof(left, v, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20))
    s2 = s.copy()
    s2["spot_ret_15"] = s2.spot.pct_change(1, fill_method=None)
    s2["spot_ret_30"] = s2.spot.pct_change(2, fill_method=None)
    s2["spot_ret_60"] = s2.spot.pct_change(4, fill_method=None)
    s2["spot_ret_120"] = s2.spot.pct_change(8, fill_method=None)
    s2["spot_vol_60"] = s2.spot.pct_change(fill_method=None).rolling(4).std()
    v2 = v.copy()
    v2["vix_chg_15"] = v2.vix.diff(1)
    v2["vix_chg_30"] = v2.vix.diff(2)
    v2["vix_chg_60"] = v2.vix.diff(4)
    v2["vix_ret_15"] = v2.vix.pct_change(1, fill_method=None)
    left = pd.merge_asof(left.sort_values("timestamp"), s2[["timestamp","spot_ret_15","spot_ret_30","spot_ret_60","spot_ret_120","spot_vol_60"]], on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20))
    left = pd.merge_asof(left.sort_values("timestamp"), v2[["timestamp","vix_chg_15","vix_chg_30","vix_chg_60","vix_ret_15"]], on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=20))
    left["spot_source_disagreement"] = left["option_spot"] - left["spot"]
    return left.sort_values("timestamp").reset_index(drop=True)


def forward_join(current_ts, future_ts, future_values, horizon_minutes, tolerance_minutes):
    left = pd.DataFrame({"base_ts": pd.to_datetime(current_ts)})
    left["target_ts"] = left.base_ts + pd.Timedelta(minutes=horizon_minutes)
    right = pd.DataFrame({"future_ts": pd.to_datetime(future_ts), "future_value": pd.to_numeric(future_values, errors="coerce")}).sort_values("future_ts")
    j = pd.merge_asof(left.sort_values("target_ts"), right, left_on="target_ts", right_on="future_ts", direction="forward", tolerance=pd.Timedelta(minutes=tolerance_minutes))
    return j["future_value"].to_numpy()


def add_targets(x, spot15):
    out = x.sort_values("timestamp").reset_index(drop=True).copy()
    option_ts = out.timestamp
    atm_series = out[["timestamp","atm_straddle"]].dropna().sort_values("timestamp")
    spot_series = spot15.rename(columns={"value":"spot"}).sort_values("timestamp")
    for h in HORIZONS:
        f_str = forward_join(option_ts, atm_series.timestamp, atm_series.atm_straddle, h, 2)
        f_spot = forward_join(option_ts, spot_series.timestamp, spot_series.spot, h, 16)
        cur_str = pd.to_numeric(out.atm_straddle, errors="coerce").to_numpy()
        cur_spot = pd.to_numeric(out.spot, errors="coerce").to_numpy()
        out[f"y_straddle_{h}m"] = np.where(np.isfinite(cur_str) & (cur_str != 0), f_str / cur_str - 1, np.nan)
        out[f"y_spot_{h}m"] = np.where(np.isfinite(cur_spot) & (cur_spot != 0), f_spot / cur_spot - 1, np.nan)
        out[f"y_dir_{h}m"] = np.where(np.isfinite(out[f"y_spot_{h}m"]), (out[f"y_spot_{h}m"] > 0).astype(int), np.nan)
    return out


def _numpy_robust(arr):
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return None
    lo, hi = np.quantile(a, [0.05, 0.95])
    clipped = np.clip(a, lo, hi)
    return {"n": int(a.size), "mean": float(a.mean()), "median": float(np.median(a)), "trimmed_mean": float(clipped.mean()), "up_rate": float((a > 0).mean()), "p05": float(np.quantile(a, .05)), "p95": float(np.quantile(a, .95))}


def robust_stats(s):
    stt = _numpy_robust(pd.to_numeric(s, errors="coerce").to_numpy(dtype=float))
    return stt or {"n":0,"mean":np.nan,"median":np.nan,"trimmed_mean":np.nan,"up_rate":np.nan,"p05":np.nan,"p95":np.nan}


def time_split(df, train=.60, val=.20):
    n = len(df)
    a = max(1, int(n*train))
    b = max(a+1, int(n*(train+val)))
    return df.iloc[:a].copy(), df.iloc[a:b].copy(), df.iloc[b:].copy()


def candidate_discovery(train, target="y_spot_15m"):
    if not SKLEARN_OK:
        raise RuntimeError("scikit-learn is required for FRIDAY discovery/ML.")
    if target not in train.columns:
        return pd.DataFrame()
    numeric = [c for c in train.columns if c != target and not c.startswith("y_") and pd.api.types.is_numeric_dtype(train[c]) and not pd.api.types.is_bool_dtype(train[c])]
    if not numeric:
        return pd.DataFrame()
    sample = train[numeric + [target]].replace([np.inf, -np.inf], np.nan)
    if len(sample) > DISCOVERY_SAMPLE:
        sample = sample.sample(DISCOVERY_SAMPLE, random_state=42)
    y_full = pd.to_numeric(sample[target], errors="coerce")
    base = robust_stats(y_full)
    if base["n"] < 200:
        return pd.DataFrame()
    rows = []
    for c in numeric:
        s = pd.to_numeric(sample[c], errors="coerce")
        valid = s.notna() & y_full.notna()
        if valid.sum() < 200 or s[valid].nunique() < 5:
            continue
        sv, yv = s[valid].to_numpy(float), y_full[valid].to_numpy(float)
        qs = np.quantile(sv, [.10, .20, .80, .90])
        best = None
        for qv, label, upper in [(qs[0],"q10",True),(qs[1],"q20",True),(qs[2],"q80",False),(qs[3],"q90",False)]:
            stt = _numpy_robust(yv[sv <= qv if upper else sv >= qv])
            if stt is None or stt["n"] < 100:
                continue
            effect = abs(stt["trimmed_mean"] - base["trimmed_mean"])
            if best is None or effect > best[0]:
                best = (effect, label, stt)
        if best is not None:
            rows.append({"feature":c,"mutual_information":np.nan,"best_region":best[1],"n":best[2]["n"],"effect":best[0],"trimmed_mean":best[2]["trimmed_mean"],"up_rate":best[2]["up_rate"]})
    r = pd.DataFrame(rows)
    if r.empty:
        return r
    mi_candidates = r.nlargest(min(100,len(r)),"effect")["feature"].tolist()
    mi_df = sample[mi_candidates].replace([np.inf,-np.inf], np.nan)
    mi_df = mi_df.fillna(mi_df.median(numeric_only=True))
    good = y_full.notna()
    try:
        if good.sum() >= 300:
            mi_vals = mutual_info_regression(mi_df.loc[good].to_numpy(float), y_full.loc[good].to_numpy(float), random_state=42)
            r["mutual_information"] = r["feature"].map(dict(zip(mi_candidates,mi_vals))).fillna(0.0)
        else:
            r["mutual_information"] = 0.0
    except Exception:
        r["mutual_information"] = 0.0
    r["discovery_score"] = r["mutual_information"].fillna(0.0) * (1 + 100*r["effect"].fillna(0.0)) * np.sqrt(r["n"])
    return r.sort_values(["discovery_score","effect","n"],ascending=False).reset_index(drop=True)


def interaction_beam(train, seeds, target="y_spot_15m"):
    if train.empty or not seeds:
        return pd.DataFrame()
    cols=[c for c in seeds if c in train.columns][:MAX_BASE_FEATURES]
    base=robust_stats(train[target]); rows=[]
    for i in range(len(cols)):
        for j in range(i+1,len(cols)):
            a,b=cols[i],cols[j]; sa,sb=train[a],train[b]
            if pd.api.types.is_bool_dtype(sa) or pd.api.types.is_bool_dtype(sb):
                continue
            qa,qb=sa.quantile([.2,.8]),sb.quantile([.2,.8])
            masks={"LL":(sa<=qa.iloc[0])&(sb<=qb.iloc[0]),"LH":(sa<=qa.iloc[0])&(sb>=qb.iloc[1]),"HL":(sa>=qa.iloc[1])&(sb<=qb.iloc[0]),"HH":(sa>=qa.iloc[1])&(sb>=qb.iloc[1])}
            for regime,mask in masks.items():
                stt=robust_stats(train.loc[mask,target])
                if stt["n"]>=PAIR_MIN_N:
                    effect=abs(stt["trimmed_mean"]-base["trimmed_mean"])
                    rows.append({"formula":f"{a}[{regime}] + {b}[{regime}]","feature_a":a,"feature_b":b,"n":stt["n"],"trimmed_mean":stt["trimmed_mean"],"effect":effect,"up_rate":stt["up_rate"]})
    r=pd.DataFrame(rows)
    return r.sort_values(["effect","n"],ascending=False).head(MAX_INTERACTION_SEEDS).reset_index(drop=True) if not r.empty else r


def model_fit_predict(train, val, test, features, target):
    if not SKLEARN_OK:
        raise RuntimeError("scikit-learn is required for the ML module.")
    if not features:
        return pd.DataFrame()
    Xtr=train[features].replace([np.inf,-np.inf],np.nan); Xv=val[features].replace([np.inf,-np.inf],np.nan); Xt=test[features].replace([np.inf,-np.inf],np.nan)
    ytr=pd.to_numeric(train[target],errors="coerce"); yv=pd.to_numeric(val[target],errors="coerce"); yt=pd.to_numeric(test[target],errors="coerce")
    masktr=ytr.notna(); maskv=yv.notna(); maskt=yt.notna(); Xtr,Xv,Xt=Xtr.loc[masktr],Xv.loc[maskv],Xt.loc[maskt]; ytr,yv,yt=ytr.loc[masktr],yv.loc[maskv],yt.loc[maskt]
    rows=[]; ytr_cls=(ytr>0).astype(int); yv_cls=(yv>0).astype(int); yt_cls=(yt>0).astype(int)
    if ytr_cls.nunique() < 2:
        rows.append({"model":"LogisticRegression","target":target,"status":"SKIPPED — training segment contains one direction only"})
    else:
        clf=Pipeline([("imp",SimpleImputer(strategy="median")),("scale",StandardScaler()),("model",LogisticRegression(max_iter=600,C=0.5,class_weight="balanced"))])
        clf.fit(Xtr,ytr_cls); pv=clf.predict(Xv); pt=clf.predict(Xt)
        rows.append({"model":"LogisticRegression","target":target,"val_accuracy":accuracy_score(yv_cls,pv),"val_balanced_accuracy":balanced_accuracy_score(yv_cls,pv),"test_accuracy":accuracy_score(yt_cls,pt),"test_balanced_accuracy":balanced_accuracy_score(yt_cls,pt)})
    reg=Pipeline([("imp",SimpleImputer(strategy="median")),("model",ExtraTreesRegressor(n_estimators=160,max_depth=8,min_samples_leaf=30,n_jobs=-1,random_state=42))]); reg.fit(Xtr,ytr); rv=reg.predict(Xv); rt=reg.predict(Xt)
    rows.append({"model":"ExtraTreesRegressor","target":target,"val_mae":mean_absolute_error(yv,rv),"val_r2":r2_score(yv,rv),"test_mae":mean_absolute_error(yt,rt),"test_r2":r2_score(yt,rt)})
    ridge=Pipeline([("imp",SimpleImputer(strategy="median")),("scale",StandardScaler()),("model",Ridge(alpha=10.0))]); ridge.fit(Xtr,ytr); rv2=ridge.predict(Xv); rt2=ridge.predict(Xt)
    rows.append({"model":"Ridge","target":target,"val_mae":mean_absolute_error(yv,rv2),"val_r2":r2_score(yv,rv2),"test_mae":mean_absolute_error(yt,rt2),"test_r2":r2_score(yt,rt2)})
    return pd.DataFrame(rows)


def validation_table(train,val,test,target="y_spot_15m"):
    rows=[]
    for name,d in [("discovery",train),("validation",val),("final_test",test)]:
        stt=robust_stats(d[target]); stt["sample"]=name; rows.append(stt)
    return pd.DataFrame(rows)[["sample","n","mean","median","trimmed_mean","up_rate","p05","p95"]]


def build_report(q,audit,discovery,interactions,validation,ml,warnings):
    def md(df):
        if df is None or df.empty:return "_No rows._"
        cols=[str(c) for c in df.columns]; out=["| "+" | ".join(cols)+" |","| "+" | ".join(["---"]*len(cols))+" |"]
        for row in df.itertuples(index=False,name=None):
            vals=[]
            for v in row:
                if pd.isna(v):vals.append("")
                elif isinstance(v,(float,np.floating)):vals.append(f"{float(v):.6g}")
                else:vals.append(str(v).replace("|","\\|"))
            out.append("| "+" | ".join(vals)+" |")
        return "\n".join(out)
    lines=[f"# FRIDAY — AUTONOMOUS RESEARCH REPORT — {q}","","## 1. Research Clock","Options: 1-minute event clock. NIFTY Spot: 15-minute state clock. India VIX: 15-minute state clock.","Spot/VIX states are joined backward only. No future market-state observation is used.","","## 2. Raw Data Audit",md(audit),"","## 3. Fruitfulness Discovery","Discovery is trained only on the discovery segment; validation and final test remain untouched until after candidate selection.",md(discovery.head(100)),"","## 4. Interaction Discovery",md(interactions.head(100)),"","## 5. Holdout Validation",md(validation),"","## 6. Machine Learning Results",md(ml),"","## 7. Research Gates"]
    lines += [f"- {w}" for w in warnings] if warnings else ["- No blocking warnings."]
    lines += ["","## 8. Promotion Rule","A relationship is not promoted to FRIDAY research memory merely because it looks good in discovery. It must remain credible in validation and final test."]
    return "\n".join(lines)


def run_quarter(q,options,spot,vix):
    o=quarter_slice(options,q); s=quarter_slice(spot,q); vv=quarter_slice(vix,q)
    if o.empty or s.empty or vv.empty: raise ValueError(f"{q}: Options, NIFTY Spot and India VIX are all required.")
    audit=pd.concat([audit_options(o).assign(source="Options"),audit_market(s,"NIFTY Spot",15).assign(source="Spot"),audit_market(vv,"India VIX",15).assign(source="VIX")],ignore_index=True)[["source","check","value","status"]]
    w=build_option_matrix(o); f=add_surface_features(w); f=align_15m_state(f,s,vv); f=add_targets(f,s); f=f.replace([np.inf,-np.inf],np.nan).sort_values("timestamp").reset_index(drop=True)
    train,val,test=time_split(f); disc=candidate_discovery(train); top=disc.head(MAX_BASE_FEATURES)["feature"].tolist() if not disc.empty else []; inter=interaction_beam(train,top); ml=model_fit_predict(train,val,test,top,"y_spot_15m") if top else pd.DataFrame(); valtab=validation_table(train,val,test,"y_spot_15m")
    warnings=["Historical expiry DATE is not present in this rolling sample; expiry_flag/expiry_code are preserved as contract-rank metadata."]
    if (o["expiry_flag"].astype(str).str.strip()=="").mean()>0.01: warnings.append("Expiry flag missing on a material share of option rows.")
    if o["expiry_code"].isna().mean()>0.01: warnings.append("Expiry code missing on a material share of option rows.")
    return {"audit":audit,"features":f,"discovery":disc,"interactions":inter,"validation":valtab,"ml":ml,"report":build_report(q,audit,disc,inter,valtab,ml,warnings)}


def make_zip(results):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        for q,r in results.items():
            safe=q.replace(" ","_"); z.writestr(f"FRIDAY_{safe}_REPORT.md",r["report"])
            for key in ["audit","discovery","interactions","validation","ml","features"]: z.writestr(f"FRIDAY_{safe}_{key}.csv",r[key].to_csv(index=False))
        z.writestr("README.txt","FRIDAY rebuilt from scratch. Options=1m; Spot=15m; VIX=15m. Discovery is train-only; validation and final test are held out. Rolling expiry metadata is preserved as supplied by the Dhan rolling-options dataset.")
    return buf.getvalue()


def main():
    with st.sidebar:
        st.title("FRIDAY")
        st.caption("Autonomous Market Research Engine")
        st.info("Pipeline: Audit → Feature Factory → Fruitfulness → Interactions → ML → Holdout Validation → Research Memory")
    st.title("FRIDAY — AUTONOMOUS MARKET RESEARCH ENGINE")
    st.caption("Built from scratch around the agreed specification. No legacy FRIDAY modules are used.")
    if not SKLEARN_OK: st.warning("Machine-learning dependencies are not available in this environment. requirements.txt will install them on deployment.")
    selected=st.multiselect("Research quarters",list(QUARTERS.keys()),default=["Q1 2025"])
    opt_files=st.file_uploader("1. NIFTY Options — 1-minute CSV",type=["csv"],accept_multiple_files=True)
    spot_files=st.file_uploader("2. NIFTY Spot — 15-minute CSV",type=["csv"],accept_multiple_files=True)
    vix_files=st.file_uploader("3. India VIX — 15-minute CSV",type=["csv"],accept_multiple_files=True)
    run=st.button("RUN FRIDAY — FULL RESEARCH",use_container_width=True,type="primary")
    if not run:return
    if not selected or not opt_files or not spot_files or not vix_files:
        st.error("Upload Options + NIFTY Spot + India VIX and select at least one quarter."); return
    if not SKLEARN_OK:
        st.error("scikit-learn is required for the full FRIDAY build."); return
    bar=st.progress(0,text="FRIDAY: 0% — loading raw data"); status=st.empty(); started=time.time()
    try:
        opt_cols=["timestamp","datetime_ist","open","high","low","close","volume","oi","iv","strike_price","spot","option_type","requested_strike","strike_offset","expiry_flag","expiry_code"]
        market_cols=["timestamp","datetime_ist","open","high","low","close","spot","vix"]
        raw_o=_load_csvs(opt_files,usecols=lambda c:c in opt_cols); raw_s=_load_csvs(spot_files,usecols=lambda c:c in market_cols); raw_v=_load_csvs(vix_files,usecols=lambda c:c in market_cols)
        bar.progress(10,text="FRIDAY: 10% — normalizing raw inputs"); status.info("FRIDAY: normalizing and auditing raw inputs")
        o=normalize_options(raw_o); s=normalize_market(raw_s,"NIFTY Spot"); v=normalize_market(raw_v,"India VIX")
        results={}; errors=[]
        for i,q in enumerate(selected,1):
            try:
                status.info(f"FRIDAY: {q} — audit → feature factory → discovery → interactions → ML → validation")
                results[q]=run_quarter(q,o,s,v)
            except Exception as exc: errors.append((q,str(exc)))
            pct=10+int(90*i/len(selected)); bar.progress(pct,text=f"FRIDAY: {pct}% — {q}")
        if not results: raise RuntimeError("No quarter completed. "+" | ".join(f"{q}: {e}" for q,e in errors))
        for q,r in results.items():
            st.markdown(f"### {q}"); st.success(f"Completed — features {len(r['features']):,} | candidates {len(r['discovery']):,} | interactions {len(r['interactions']):,}")
            st.dataframe(r["discovery"].head(30),use_container_width=True,hide_index=True)
            st.download_button(f"DOWNLOAD {q} REPORT",r["report"].encode(),f"FRIDAY_{q.replace(' ','_')}_REPORT.md","text/markdown",key=f"report_{q}",use_container_width=True)
            st.download_button(f"DOWNLOAD {q} FULL PACKAGE",make_zip({q:r}),f"FRIDAY_{q.replace(' ','_')}_FULL_RESEARCH.zip","application/zip",key=f"zip_{q}",use_container_width=True)
        if len(results)>1: st.download_button("DOWNLOAD ALL QUARTERS",make_zip(results),"FRIDAY_ALL_RESEARCH.zip","application/zip",use_container_width=True)
        if errors: st.warning("Some quarters failed: "+" | ".join(f"{q}: {e}" for q,e in errors))
        status.success(f"FRIDAY completed in {time.time()-started:.1f}s"); bar.progress(100,text="FRIDAY: 100% ✅")
    except Exception as exc:
        status.error(f"FRIDAY stopped after {time.time()-started:.1f}s: {exc}"); st.exception(exc)

if __name__=="__main__": main()
