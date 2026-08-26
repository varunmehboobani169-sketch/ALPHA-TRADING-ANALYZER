from __future__ import annotations

import io
import itertools
import zipfile
from dataclasses import dataclass
from typing import Iterable, List

import numpy as np
import pandas as pd

PAIRS_TOL = pd.Timedelta(minutes=1)
SPOT_TOL = pd.Timedelta(minutes=30)
HORIZONS = [1, 3, 5, 10, 15, 30, 60, 120]
QUARTERS = {
    "Q1 2024": (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-03-31 23:59:59")),
    "Q2 2024": (pd.Timestamp("2024-04-01"), pd.Timestamp("2024-06-30 23:59:59")),
    "Q3 2024": (pd.Timestamp("2024-07-01"), pd.Timestamp("2024-09-30 23:59:59")),
    "Q4 2024": (pd.Timestamp("2024-10-01"), pd.Timestamp("2024-12-31 23:59:59")),
    "Q1 2025": (pd.Timestamp("2025-01-01"), pd.Timestamp("2025-03-31 23:59:59")),
    "Q2 2025": (pd.Timestamp("2025-04-01"), pd.Timestamp("2025-06-30 23:59:59")),
    "Q3 2025": (pd.Timestamp("2025-07-01"), pd.Timestamp("2025-09-30 23:59:59")),
    "Q4 2025": (pd.Timestamp("2025-10-01"), pd.Timestamp("2025-12-31 23:59:59")),
    "Q1 2026": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-31 23:59:59")),
    "Q2 2026": (pd.Timestamp("2026-04-01"), pd.Timestamp("2026-06-30 23:59:59")),
    "Q3 2026": (pd.Timestamp("2026-07-01"), pd.Timestamp("2026-08-26 23:59:59")),
}
TARGET_QUARTERS = list(QUARTERS.keys())

@dataclass
class QuarterResult:
    quarter: str
    audit: pd.DataFrame
    features: pd.DataFrame
    fruitfulness: pd.DataFrame
    interactions: pd.DataFrame
    validation: pd.DataFrame
    pairs: pd.DataFrame
    report: str


def _find_col(df: pd.DataFrame, names: Iterable[str]):
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}
    names = [n.lower().replace(" ", "_") for n in names]
    for n in names:
        if n in lookup:
            return lookup[n]
    for key, original in lookup.items():
        if any(n in key for n in names):
            return original
    return None


def _num(df, names):
    c = _find_col(df, names)
    return pd.to_numeric(df[c], errors="coerce") if c is not None else None


def _parse_dt(s: pd.Series) -> pd.Series:
    raw = pd.Series(s)
    numeric = pd.to_numeric(raw, errors="coerce")
    if numeric.notna().mean() > 0.8 and numeric.notna().any():
        med = float(numeric.dropna().abs().median())
        unit = "ns" if med >= 1e18 else "us" if med >= 1e15 else "ms" if med >= 1e12 else "s" if med >= 1e9 else None
        dt = pd.to_datetime(numeric, unit=unit, errors="coerce", utc=True) if unit else pd.to_datetime(raw, errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(raw, errors="coerce", utc=True)
    return dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).astype("datetime64[ns]")


def normalize_spot(df: pd.DataFrame, label="Spot") -> pd.DataFrame:
    ts = _find_col(df, ["timestamp", "datetime", "date_time", "exchange_timestamp", "timestamp_ist", "time", "date"])
    px = _find_col(df, ["close", "ltp", "last_price", "nifty", "spot", "index_close", "price"])
    if ts is None or px is None:
        raise ValueError(f"{label}: timestamp/close column not found")
    out = pd.DataFrame({"timestamp": _parse_dt(df[ts]), "spot": pd.to_numeric(df[px], errors="coerce")})
    return out.dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def normalize_vix(df: pd.DataFrame) -> pd.DataFrame:
    ts = _find_col(df, ["timestamp", "datetime", "date_time", "exchange_timestamp", "timestamp_ist", "time", "date"])
    px = _find_col(df, ["close", "ltp", "last_price", "vix_close", "vix", "price"])
    if ts is None or px is None:
        raise ValueError("VIX: timestamp/close column not found")
    out = pd.DataFrame({"timestamp": _parse_dt(df[ts]), "vix": pd.to_numeric(df[px], errors="coerce")})
    return out.dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def _expiry_from_symbol(values: pd.Series) -> pd.Series:
    out = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    text = values.astype(str)
    for pat in [r"(20\d{2})[-_/](0\d|1[0-2])[-_/]([0-3]\d)", r"(20\d{2})(0\d|1[0-2])([0-3]\d)"]:
        m = text.str.extract(pat)
        cand = pd.to_datetime(m[0] + "-" + m[1] + "-" + m[2], errors="coerce")
        out = out.fillna(cand)
    return out


def normalize_options(df: pd.DataFrame) -> pd.DataFrame:
    ts = _find_col(df, ["timestamp", "datetime", "date_time", "exchange_timestamp", "timestamp_ist", "time", "date"])
    strike = _find_col(df, ["strike", "strike_price", "strikeprice", "strike_px"])
    side = _find_col(df, ["side", "option_type", "optiontype", "type", "cp", "ce_pe", "call_put"])
    expiry = _find_col(df, ["expiry", "expiry_date", "expirydate", "exp_date", "expiry_dt"])
    symbol = _find_col(df, ["symbol", "trading_symbol", "tradingsymbol", "instrument_name"])
    if ts is None or strike is None or side is None:
        raise ValueError("Options: timestamp, strike and CE/PE columns are mandatory")
    out = pd.DataFrame({
        "timestamp": _parse_dt(df[ts]),
        "strike": pd.to_numeric(df[strike], errors="coerce"),
        "side": df[side].astype(str).str.upper().str.strip().replace({"C": "CE", "CALL": "CE", "P": "PE", "PUT": "PE"}),
    })
    if expiry is not None:
        out["expiry"] = pd.to_datetime(df[expiry], errors="coerce").dt.normalize()
    elif symbol is not None:
        out["expiry"] = _expiry_from_symbol(df[symbol])
    else:
        out["expiry"] = pd.NaT
    for names, target in [
        (["open"], "open"), (["high"], "high"), (["low"], "low"), (["close", "ltp", "last_price", "price"], "close"),
        (["iv", "implied_volatility", "impliedvolatility", "implied_vol"], "iv"),
        (["oi", "open_interest", "openinterest"], "oi"), (["volume", "vol", "traded_volume"], "volume"),
    ]:
        v = _num(df, names)
        out[target] = v if v is not None else np.nan
    return out.dropna(subset=["timestamp", "strike"])[out["side"].isin(["CE", "PE"])].sort_values("timestamp").reset_index(drop=True)


def slice_quarter(df: pd.DataFrame, quarter: str) -> pd.DataFrame:
    a, b = QUARTERS[quarter]
    return df[(df.timestamp >= a) & (df.timestamp <= b)].copy()


def md_value(v):
    if pd.isna(v): return ""
    if isinstance(v, (float, np.floating)): return f"{float(v):.6g}"
    return str(v).replace("|", "\\|")


def md_table(df):
    if df is None or df.empty: return "_No rows._"
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for row in df.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(md_value(v) for v in row) + " |")
    return "\n".join(lines)


def audit_raw(options, spot, vix):
    rows=[]
    def add(name, value, status="INFO"): rows.append({"check":name,"value":value,"status":status})
    add("Option rows",f"{len(options):,}"); add("Spot rows",f"{len(spot):,}"); add("VIX rows",f"{len(vix):,}")
    add("Option CE rows",f"{int((options.side=='CE').sum()):,}"); add("Option PE rows",f"{int((options.side=='PE').sum()):,}")
    miss=options.expiry.isna().mean() if len(options) else 1.0; add("Missing expiry",f"{miss*100:.2f}%","FAIL" if miss>0 else "PASS")
    dup=options.duplicated(["timestamp","strike","side","expiry"]).mean() if len(options) else 0; add("Duplicate option rows",f"{dup*100:.2f}%","FAIL" if dup>0.01 else "PASS")
    for name,df in [("Options",options),("Spot",spot),("VIX",vix)]:
        if len(df):
            gaps=df.timestamp.sort_values().diff().dropna().dt.total_seconds(); add(f"{name} median gap (sec)",f"{gaps.median():.0f}"); add(f"{name} p95 gap (sec)",f"{gaps.quantile(.95):.0f}")
    return pd.DataFrame(rows)


def legacy_diagnostics(options, spot, vix, aligned, pairs, features):
    return pd.DataFrame({"Check":["Option rows","Spot rows","VIX rows","Option+Spot synced","CE rows","PE rows","CE/PE pairs","ATM observations","Option start","Option end","Spot start","Spot end"],"Value":[len(options),len(spot),len(vix),len(aligned),int((options.side=="CE").sum()) if not options.empty else 0,int((options.side=="PE").sum()) if not options.empty else 0,len(pairs),len(features),str(options.timestamp.min()) if not options.empty else "N/A",str(options.timestamp.max()) if not options.empty else "N/A",str(spot.timestamp.min()) if not spot.empty else "N/A",str(spot.timestamp.max()) if not spot.empty else "N/A"]})


def align_market(options, spot, vix):
    x=pd.merge_asof(options.sort_values("timestamp"),spot.sort_values("timestamp"),on="timestamp",direction="backward",tolerance=SPOT_TOL)
    if len(vix): x=pd.merge_asof(x.sort_values("timestamp"),vix.sort_values("timestamp"),on="timestamp",direction="backward",tolerance=SPOT_TOL)
    return x


def pair_ce_pe(aligned):
    x=aligned.copy()
    if x.empty: return pd.DataFrame()
    x["expiry"]=pd.to_datetime(x.expiry,errors="coerce").dt.normalize()
    if x.expiry.isna().any(): raise ValueError("Missing expiry detected. FRIDAY refuses to cross-pair different expiries.")
    ce=x[x.side=="CE"].copy(); pe=x[x.side=="PE"].copy()
    if ce.empty or pe.empty: return pd.DataFrame()
    pe=pe.rename(columns={"timestamp":"pe_timestamp","close":"pe_close","iv":"pe_iv","oi":"pe_oi","volume":"pe_volume"})
    ce=ce.sort_values(["timestamp","expiry","strike"],kind="mergesort"); pe=pe.sort_values(["pe_timestamp","expiry","strike"],kind="mergesort")
    out=pd.merge_asof(ce,pe,left_on="timestamp",right_on="pe_timestamp",by=["expiry","strike"],direction="nearest",tolerance=PAIRS_TOL)
    out=out.dropna(subset=["pe_timestamp","close","pe_close"])
    out["pair_gap_seconds"]=(out.timestamp-out.pe_timestamp).abs().dt.total_seconds()
    return out.sort_values("timestamp").reset_index(drop=True)


def build_atm_features(pairs, spot, vix):
    if pairs.empty: return pd.DataFrame()
    p=pairs.copy(); p["distance_to_spot"]=(p.strike-p.spot).abs(); p["dte"]=(p.expiry-p.timestamp.dt.normalize()).dt.total_seconds()/86400
    atm=p.sort_values(["timestamp","expiry","distance_to_spot"],kind="mergesort").drop_duplicates(["timestamp","expiry"])
    primary=atm.sort_values(["timestamp","dte","distance_to_spot"],kind="mergesort").drop_duplicates("timestamp")
    f=primary[["timestamp","expiry","strike","spot","vix","close","pe_close","iv","pe_iv","oi","pe_oi","volume","pe_volume","pair_gap_seconds","dte"]].copy().rename(columns={"strike":"atm_strike","close":"ce_close","iv":"ce_iv","oi":"ce_oi","volume":"ce_volume"})
    f["pcr_oi"]=f.pe_oi/f.ce_oi.replace(0,np.nan); f["straddle"]=f.ce_close+f.pe_close; f["iv_mid"]=f[["ce_iv","pe_iv"]].mean(axis=1); f["iv_skew"]=f.ce_iv-f.pe_iv
    f["oi_imbalance"]=(f.pe_oi-f.ce_oi)/(f.pe_oi+f.ce_oi).replace(0,np.nan); f["volume_imbalance"]=(f.pe_volume-f.ce_volume)/(f.pe_volume+f.ce_volume).replace(0,np.nan)
    s=spot.copy(); s["spot_ret_15"]=s.spot.pct_change(); s["spot_ret_30"]=s.spot.pct_change(2); s["spot_ret_60"]=s.spot.pct_change(4); s["spot_vol_60"]=s.spot.pct_change().rolling(4).std()
    v=vix.copy(); v["vix_chg_15"]=v.vix.diff(); v["vix_ret_15"]=v.vix.pct_change(); v["vix_chg_60"]=v.vix.diff(4)
    f=pd.merge_asof(f.sort_values("timestamp"),s.sort_values("timestamp"),on="timestamp",direction="backward",tolerance=SPOT_TOL,suffixes=("","_15m"))
    f=pd.merge_asof(f.sort_values("timestamp"),v.sort_values("timestamp"),on="timestamp",direction="backward",tolerance=SPOT_TOL,suffixes=("","_15m"))
    for c in ["ce_close","pe_close","straddle","ce_iv","pe_iv","iv_mid","pcr_oi","oi_imbalance","volume_imbalance"]: f[f"{c}_chg"]=f[c].pct_change()
    f["time_minute"]=f.timestamp.dt.hour*60+f.timestamp.dt.minute; f["is_expiry_day"]=f.timestamp.dt.normalize().eq(f.expiry)
    return f.sort_values("timestamp").reset_index(drop=True)


def add_forward_outcomes(f):
    x=f.copy().sort_values("timestamp").reset_index(drop=True); idx=x.timestamp; future=x[["timestamp","spot","straddle"]].rename(columns={"timestamp":"future_timestamp","spot":"future_spot","straddle":"future_straddle"})
    for h in HORIZONS:
        q=pd.DataFrame({"target":idx+pd.Timedelta(minutes=h)})
        q=pd.merge_asof(q.sort_values("target"),future.sort_values("future_timestamp"),left_on="target",right_on="future_timestamp",direction="forward",tolerance=pd.Timedelta(minutes=2))
        q=q.reindex(x.index)
        x[f"fwd_spot_{h}m"]=q.future_spot.to_numpy()/x.spot.to_numpy()-1; x[f"fwd_straddle_{h}m"]=q.future_straddle.to_numpy()/x.straddle.to_numpy()-1
    return x


def _winsor(s):
    s=pd.to_numeric(s,errors="coerce").dropna()
    if s.empty: return s
    return s.clip(s.quantile(.01),s.quantile(.99))


def single_factor_screen(f):
    candidates=[c for c in f.columns if c not in {"timestamp","expiry"} and pd.api.types.is_numeric_dtype(f[c])]
    rows=[]
    for c in candidates:
        d=f[[c,"fwd_spot_15m","fwd_straddle_15m"]].dropna()
        if len(d)<50 or d[c].nunique()<5: continue
        ic=d[c].corr(d.fwd_spot_15m,method="spearman"); hi=d.loc[d[c]>=d[c].quantile(.8),"fwd_spot_15m"]; lo=d.loc[d[c]<=d[c].quantile(.2),"fwd_spot_15m"]; spread=hi.mean()-lo.mean(); wspread=_winsor(hi).mean()-_winsor(lo).mean()
        rows.append({"feature":c,"observations":len(d),"spearman_ic":ic,"top_bottom_20_spread":spread,"winsor_spread":wspread,"outlier_sensitivity":abs(spread-wspread),"useful_score":abs(ic)+2*abs(wspread)})
    return pd.DataFrame(rows).sort_values("useful_score",ascending=False) if rows else pd.DataFrame()


def interaction_screen(f, screen, top_n=15):
    if screen.empty: return pd.DataFrame()
    feats=[x for x in screen.feature.head(top_n).tolist() if x in f.columns]; rows=[]
    for a,b in itertools.combinations(feats,2):
        qa,la=f[a].quantile(.8),f[a].quantile(.2); qb,lb=f[b].quantile(.8),f[b].quantile(.2)
        for state,mask in {"HH":(f[a]>=qa)&(f[b]>=qb),"HL":(f[a]>=qa)&(f[b]<=lb),"LH":(f[a]<=la)&(f[b]>=qb),"LL":(f[a]<=la)&(f[b]<=lb)}.items():
            d=f.loc[mask,["fwd_spot_15m","fwd_straddle_15m"]].dropna()
            if len(d)<30: continue
            rows.append({"feature_a":a,"feature_b":b,"state":state,"observations":len(d),"mean_spot_15m":d.fwd_spot_15m.mean(),"median_spot_15m":d.fwd_spot_15m.median(),"mean_straddle_15m":d.fwd_straddle_15m.mean(),"up_rate":(d.fwd_spot_15m>0).mean()})
    return pd.DataFrame(rows).sort_values("mean_spot_15m",key=lambda s:s.abs(),ascending=False) if rows else pd.DataFrame()


def robustness_report(f, screen):
    rows=[]
    for feat in screen.feature.head(25) if not screen.empty else []:
        d=f[[feat,"fwd_spot_15m"]].dropna()
        if len(d)<80: continue
        first=d.iloc[:len(d)//2]; second=d.iloc[len(d)//2:]
        def spread(z): return z.loc[z[feat]>=z[feat].quantile(.75),"fwd_spot_15m"].mean()-z.loc[z[feat]<=z[feat].quantile(.25),"fwd_spot_15m"].mean() if len(z)>=40 else np.nan
        a,b=spread(first),spread(second); rows.append({"feature":feat,"full_spread":spread(d),"first_half_spread":a,"second_half_spread":b,"stable_sign":bool(np.isfinite(a) and np.isfinite(b) and np.sign(a)==np.sign(b)),"observations":len(d)})
    return pd.DataFrame(rows).sort_values(["stable_sign","observations"],ascending=[False,False]) if rows else pd.DataFrame()


def cross_quarter_validation(results: List[QuarterResult], top_n=20):
    if len(results)<2: return pd.DataFrame()
    feats=set()
    for r in results: feats.update(r.fruitfulness.feature.head(top_n).tolist() if not r.fruitfulness.empty else [])
    rows=[]
    for feat in feats:
        vals=[]
        for r in results:
            if feat not in r.features.columns: continue
            d=r.features[[feat,"fwd_spot_15m"]].dropna()
            if len(d)<50 or d[feat].nunique()<5: continue
            ic=d[feat].corr(d.fwd_spot_15m,method="spearman"); spread=d.loc[d[feat]>=d[feat].quantile(.8),"fwd_spot_15m"].mean()-d.loc[d[feat]<=d[feat].quantile(.2),"fwd_spot_15m"].mean(); vals.append((r.quarter,ic,spread,len(d)))
        if len(vals)>=2:
            signs=[np.sign(v[2]) for v in vals if np.isfinite(v[2]) and v[2]!=0]; mean_sp=np.mean([v[2] for v in vals]); stability=np.mean(np.array(signs)==np.sign(mean_sp)) if signs else np.nan
            rows.append({"feature":feat,"quarters_tested":len(vals),"mean_ic":np.mean([v[1] for v in vals]),"median_ic":np.median([v[1] for v in vals]),"mean_top_bottom_spread":mean_sp,"sign_stability":stability,"min_observations":min(v[3] for v in vals)})
    return pd.DataFrame(rows).sort_values(["sign_stability","mean_ic"],key=lambda s:s.abs(),ascending=False) if rows else pd.DataFrame()


def build_report(q,audit,fruit,inter,robust,features,options,spot,vix,aligned,pairs):
    fail=int((audit.status=="FAIL").sum()); best=fruit.head(12) if not fruit.empty else pd.DataFrame(); legacy=legacy_diagnostics(options,spot,vix,aligned,pairs,features)
    return "\n".join([f"# FRIDAY — OPTION PATTERN RESEARCH — {q}","","## Scope",f"Analysis period: **{q}**","Sources: 1-minute NIFTY Options + 15-minute NIFTY Spot + 15-minute India VIX. Futures excluded.","","## Input Diagnostics",md_table(legacy),"","## Deep Data Audit",md_table(audit),"",f"Audit failures: **{fail}**","","## Fruitfulness Audit — Single Factors",md_table(best) if not best.empty else "_No usable factors._","","## Fruitfulness Audit — Interactions",md_table(inter.head(20)) if not inter.empty else "_No usable interactions._","","## Robustness",md_table(robust.head(20)) if not robust.empty else "_No robustness results._","","## Research Method","Options are the 1-minute event clock. Spot and India VIX retain their native 15-minute state clock and are joined only backward, never forward. Forward outcomes are measured strictly after each observation. Expiry remains part of the CE/PE identity.","","## Research Guardrails","FRIDAY does not silently repair missing expiry, cross-pair different expiries, or use future Spot/VIX information. Results are discovery evidence only; they are not causal conclusions or trading recommendations.","","## Next Stage","Only candidates that survive data-quality, sample-size, outlier, temporal-stability and cross-period validation should enter FRIDAY research memory."])


def run_quarter(q, options, spot, vix):
    options_q,spot_q,vix_q=slice_quarter(options,q),slice_quarter(spot,q),slice_quarter(vix,q)
    audit=audit_raw(options_q,spot_q,vix_q); aligned=align_market(options_q,spot_q,vix_q); pairs=pair_ce_pe(aligned); features=add_forward_outcomes(build_atm_features(pairs,spot_q,vix_q)); fruit=single_factor_screen(features); inter=interaction_screen(features,fruit); robust=robustness_report(features,fruit)
    report=build_report(q,audit,fruit,inter,robust,features,options_q,spot_q,vix_q,aligned,pairs)
    return QuarterResult(q,audit,features,fruit,inter,robust,pairs,report)


def package_results(results: List[QuarterResult]) -> bytes:
    bio=io.BytesIO()
    with zipfile.ZipFile(bio,"w",zipfile.ZIP_DEFLATED) as z:
        for r in results:
            safe=r.quarter.replace(" ","_")
            for name,data in [(f"{safe}_DEEP_AUDIT.md",r.report),(f"{safe}_audit.csv",r.audit.to_csv(index=False)),(f"{safe}_features.csv",r.features.to_csv(index=False)),(f"{safe}_fruitfulness.csv",r.fruitfulness.to_csv(index=False)),(f"{safe}_interactions.csv",r.interactions.to_csv(index=False)),(f"{safe}_robustness.csv",r.validation.to_csv(index=False)),(f"{safe}_pairs.csv",r.pairs.to_csv(index=False))]: z.writestr(name,data if isinstance(data,bytes) else data.encode())
        z.writestr("README.txt","FRIDAY rebuilt research engine: 1m Options + native 15m Spot/VIX; strict expiry identity; raw-data fruitfulness screening; interactions; robustness; cross-quarter validation. No AI conclusions are generated.")
    return bio.getvalue()
