from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests
from scipy.special import ndtr

API = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
NIFTY_ID = "13"
STRIKE_STEP = 50
MAX_DAYS = 90
IST = "Asia/Kolkata"

@dataclass
class JobConfig:
    start: str
    end: str
    risk_free_rate: float = 0.065
    chunk_days: int = 85

def headers(client_id: str, token: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "access-token": token.strip(), "client-id": client_id.strip()}

def post(path: str, client_id: str, token: str, payload: dict[str, Any], retries: int = 4) -> dict[str, Any]:
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{API}{path}", headers=headers(client_id, token), json=payload, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 10)); last = RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}"); continue
            r.raise_for_status()
            body = r.json()
            if isinstance(body, dict) and body.get("status") == "failure": raise RuntimeError(str(body))
            return body
        except Exception as exc:
            last = exc
            if attempt + 1 < retries: time.sleep(min(2 ** attempt, 10))
    raise last or RuntimeError("Dhan request failed")

def date_chunks(start: str, end: str, days: int = MAX_DAYS) -> Iterable[tuple[str, str]]:
    s, e = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    while s < e:
        n = min(s + pd.Timedelta(days=days), e)
        yield s.strftime("%Y-%m-%d"), n.strftime("%Y-%m-%d")
        s = n

def fetch_master() -> pd.DataFrame:
    df = pd.read_csv(MASTER_URL, low_memory=False)
    ren = {}
    for c in df.columns:
        k = str(c).upper().strip()
        if k in {"SECURITY_ID", "SEM_SECURITY_ID"}: ren[c] = "SECURITY_ID"
        elif k == "UNDERLYING_SECURITY_ID": ren[c] = "UNDERLYING_SECURITY_ID"
        elif k == "SM_SYMBOL_NAME": ren[c] = "SYMBOL_NAME"
        elif k == "SM_EXPIRY_DATE": ren[c] = "EXPIRY"
        elif k == "SEM_EXPIRY_FLAG": ren[c] = "EXPIRY_FLAG"
        elif k == "SEM_STRIKE_PRICE": ren[c] = "STRIKE"
        elif k == "SEM_OPTION_TYPE": ren[c] = "OPTION_TYPE"
    df = df.rename(columns=ren)
    needed = {"SECURITY_ID", "UNDERLYING_SECURITY_ID", "EXPIRY", "STRIKE", "OPTION_TYPE", "EXPIRY_FLAG"}
    missing = needed - set(df.columns)
    if missing: raise RuntimeError(f"Instrument master missing columns: {sorted(missing)}")
    df["SECURITY_ID"] = df["SECURITY_ID"].astype(str)
    df["UNDERLYING_SECURITY_ID"] = df["UNDERLYING_SECURITY_ID"].astype(str)
    df["EXPIRY"] = pd.to_datetime(df["EXPIRY"], errors="coerce").dt.date
    df["STRIKE"] = pd.to_numeric(df["STRIKE"], errors="coerce")
    return df

def weekly_nifty_options(master: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    q = master[(master["UNDERLYING_SECURITY_ID"] == NIFTY_ID) & master["OPTION_TYPE"].isin(["CE", "PE"]) & master["EXPIRY_FLAG"].astype(str).str.upper().eq("W") & master["EXPIRY"].notna()].copy()
    return q[q["EXPIRY"].apply(lambda x: s <= x < e)].sort_values(["EXPIRY", "STRIKE", "OPTION_TYPE"]).reset_index(drop=True)

def candles(client_id: str, token: str, security_id: str, segment: str, instrument: str, start: str, end: str, oi: bool = False) -> pd.DataFrame:
    frames = []
    for a, b in date_chunks(start, end, MAX_DAYS):
        body = post("/charts/intraday", client_id, token, {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument, "interval": "1", "oi": oi, "fromDate": f"{a} 09:15:00", "toDate": f"{b} 15:31:00"})
        ts = body.get("timestamp", [])
        if not ts: continue
        n = len(ts)
        def arr(k):
            x = body.get(k); return x if isinstance(x, list) else [np.nan] * n
        frames.append(pd.DataFrame({"timestamp": pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST), "open": arr("open"), "high": arr("high"), "low": arr("low"), "close": arr("close"), "volume": arr("volume"), "oi": arr("open_interest")}))
        time.sleep(0.2)
    if not frames: return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")

def bs_price(S, K, T, r, sigma, call):
    T = np.maximum(T, 1e-8); sigma = np.maximum(sigma, 1e-8); sqrtT = np.sqrt(T)
    d1 = (np.log(np.maximum(S, 1e-12) / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT; df = np.exp(-r * T)
    return np.where(call, S * ndtr(d1) - K * df * ndtr(d2), K * df * ndtr(-d2) - S * ndtr(-d1))

def implied_vol(price, S, K, T, r, call):
    price, S, K, T = map(lambda x: np.asarray(x, float), (price, S, K, T)); call = np.asarray(call, bool)
    T = np.maximum(T, 1e-8)
    intrinsic = np.where(call, np.maximum(S - K * np.exp(-r*T), 0), np.maximum(K*np.exp(-r*T) - S, 0))
    valid = np.isfinite(price) & np.isfinite(S) & (S > 0) & np.isfinite(K) & (K > 0) & (price > intrinsic + 1e-7)
    lo, hi = np.full(price.shape, 1e-5), np.full(price.shape, 6.0)
    valid &= price < bs_price(S, K, T, r, hi, call)
    for _ in range(42):
        mid = (lo + hi) / 2; too_low = bs_price(S, K, T, r, mid, call) < price
        lo = np.where(too_low, mid, lo); hi = np.where(too_low, hi, mid)
    out = (lo + hi) / 2; out[~valid] = np.nan
    return out

def add_greeks(df: pd.DataFrame, r: float) -> pd.DataFrame:
    x = df.copy(); S = x["spot"].to_numpy(float); K = x["strike"].to_numpy(float); T = x["time_to_expiry_years"].to_numpy(float); price = x["close"].to_numpy(float); call = x["option_type"].eq("CE").to_numpy()
    iv = implied_vol(price, S, K, T, r, call); sqrtT = np.sqrt(np.maximum(T, 1e-8)); sig = np.maximum(iv, 1e-8)
    d1 = (np.log(np.maximum(S,1e-12)/K) + (r + .5*sig*sig)*T) / (sig*sqrtT); d2 = d1 - sig*sqrtT
    pdf = np.exp(-.5*d1*d1)/math.sqrt(2*math.pi); dfact = np.exp(-r*T)
    dc = ndtr(d1); gamma = pdf/(np.maximum(S,1e-12)*sig*sqrtT); vega = S*pdf*sqrtT/100.0
    tc = (-(S*pdf*sig)/(2*sqrtT) - r*K*dfact*ndtr(d2))/365.0; tp = (-(S*pdf*sig)/(2*sqrtT) + r*K*dfact*ndtr(-d2))/365.0
    x["iv"], x["delta"], x["gamma"], x["vega"], x["theta"] = iv*100, np.where(call, dc, dc-1), gamma, vega, np.where(call, tc, tp)
    return x

def build_expiry_data(client_id, token, expiry, contracts, spot, r, out_dir, progress):
    expiry_dt = pd.Timestamp(expiry, tz=IST) + pd.Timedelta(hours=15, minutes=30)
    day_start = pd.Timestamp(expiry, tz=IST).normalize() - pd.Timedelta(days=7); day_end = pd.Timestamp(expiry, tz=IST).normalize() + pd.Timedelta(days=1)
    sp = spot[(spot.timestamp >= day_start) & (spot.timestamp < day_end)].copy()
    if sp.empty: return out_dir / f"nifty_weekly_{expiry}.parquet", 0, len(contracts)
    sp["atm"] = np.round(sp["close"] / STRIKE_STEP) * STRIKE_STEP; sp = sp[["timestamp","close","atm"]].rename(columns={"close":"spot"})
    lo = math.floor(float(sp.spot.min())/STRIKE_STEP)*STRIKE_STEP - 20*STRIKE_STEP; hi = math.ceil(float(sp.spot.max())/STRIKE_STEP)*STRIKE_STEP + 20*STRIKE_STEP
    cands = contracts[(contracts.STRIKE >= lo) & (contracts.STRIKE <= hi)].copy(); frames=[]; failed=[]
    for i, row in enumerate(cands.itertuples(index=False), 1):
        key = str(row.SECURITY_ID)
        try:
            d = candles(client_id, token, key, "NSE_FNO", "OPTIDX", str(day_start.date()), str(day_end.date()), oi=True)
            if d.empty: failed.append(key); continue
            d["expiry"], d["strike"], d["option_type"], d["security_id"] = expiry, float(row.STRIKE), str(row.OPTION_TYPE), key
            frames.append(d); progress["contracts_done"] = i
        except Exception as exc: failed.append(f"{key}: {exc}")
    if not frames: return out_dir / f"nifty_weekly_{expiry}.parquet", 0, len(failed)
    opt = pd.concat(frames, ignore_index=True).merge(sp, on="timestamp", how="inner")
    opt = opt[(opt.strike >= opt.atm - 20*STRIKE_STEP) & (opt.strike <= opt.atm + 20*STRIKE_STEP)]
    opt["strike_offset"] = ((opt.strike-opt.atm)/STRIKE_STEP).round().astype("Int64")
    opt["moneyness"] = opt.strike_offset.map(lambda z: "ATM" if z==0 else (f"ATM+{int(z)}" if z>0 else f"ATM{int(z)}"))
    opt["time_to_expiry_years"] = np.maximum((expiry_dt-opt.timestamp).dt.total_seconds()/(365*24*3600), 1/365000)
    opt = add_greeks(opt, r); opt["date"] = opt.timestamp.dt.date.astype(str); opt["time"] = opt.timestamp.dt.strftime("%H:%M:%S")
    opt = opt.rename(columns={"open":"open_price","high":"high_price","low":"low_price","close":"close_price","oi":"open_interest"})
    cols=["timestamp","date","time","expiry","security_id","spot","atm","strike_offset","moneyness","strike","option_type","open_price","high_price","low_price","close_price","volume","open_interest","iv","delta","gamma","theta","vega","time_to_expiry_years"]
    path=out_dir/f"nifty_weekly_{expiry}.parquet"; opt[cols].sort_values(["timestamp","strike","option_type"]).to_parquet(path,index=False)
    progress["failed_contracts"] = len(failed); return path, len(opt), len(failed)

def run_job(client_id: str, token: str, cfg: JobConfig, out_dir: Path, progress_cb=None):
    out_dir.mkdir(parents=True, exist_ok=True); master=fetch_master(); contracts=weekly_nifty_options(master,cfg.start,cfg.end)
    if contracts.empty: raise RuntimeError("No NIFTY weekly option contracts found for the selected period.")
    progress={"status":"running","start":cfg.start,"end":cfg.end,"expiry_total":0,"expiry_done":0,"rows":0,"failed_contracts":0,"contracts_done":0}
    def emit():
        if progress_cb: progress_cb(progress.copy())
    emit(); spot=candles(client_id,token,NIFTY_ID,"IDX_I","INDEX",cfg.start,cfg.end,oi=False); expiries=sorted(contracts.EXPIRY.dropna().unique().tolist()); progress["expiry_total"]=len(expiries); emit(); outputs=[]
    for expiry in expiries:
        path=out_dir/f"nifty_weekly_{expiry}.parquet"
        if path.exists() and path.stat().st_size>0:
            try: progress["rows"]+=len(pd.read_parquet(path,columns=["timestamp"])); progress["expiry_done"]+=1; emit(); continue
            except Exception: pass
        progress["expiry"]=str(expiry); progress["contracts_done"]=0; progress["failed_contracts"]=0; emit()
        path, rows, failed=build_expiry_data(client_id,token,expiry,contracts[contracts.EXPIRY==expiry],spot,cfg.risk_free_rate,out_dir,progress); outputs.append(str(path)); progress["rows"]+=rows; progress["failed_contracts"]+=failed; progress["expiry_done"]+=1; emit()
    progress["status"]="complete"; progress["outputs"]=outputs; emit(); (out_dir/"job_summary.json").write_text(json.dumps(progress,indent=2),encoding="utf-8"); return progress
