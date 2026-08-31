from __future__ import annotations

import io
import math
import time
from typing import Dict, Iterable, Optional

import pandas as pd
import requests

NIFTY_CONSTITUENT_URLS = {
    "NIFTY 50": "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "NIFTY MIDCAP 150": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
    "NIFTY SMALLCAP 250": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
    "NIFTY 500": "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "NIFTY MICROCAP 250": "https://www.niftyindices.com/IndexConstituent/ind_niftymicrocap250_list.csv",
}

UNIVERSES = {
    "NIFTY 50": ["NIFTY 50"],
    "NIFTY MIDCAP 150": ["NIFTY MIDCAP 150"],
    "NIFTY SMALLCAP 250": ["NIFTY SMALLCAP 250"],
    "NIFTY 500": ["NIFTY 500"],
    "NIFTY TOTAL MARKET": ["NIFTY 500", "NIFTY MICROCAP 250"],
}

# Four layers mirror the Matrix idea: short, medium, intermediate and long term.
TIMEFRAMES = {
    "ST": 21,
    "MT": 63,
    "IT": 126,
    "LT": 252,
}

DEFAULT_HISTORY_DAYS = 420
REQUEST_TIMEOUT = 30
CACHE_TTL_SECONDS = 12 * 60 * 60


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 MatrixEngine/1.0",
        "Referer": "https://www.niftyindices.com/",
        "Accept": "text/csv,application/octet-stream,text/plain,*/*",
    })
    return s


def fetch_constituent_csv(url: str, session: Optional[requests.Session] = None) -> pd.DataFrame:
    session = session or _session()
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    df = pd.read_csv(io.StringIO(text))
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _find_col(columns: Iterable[str], names: Iterable[str]) -> Optional[str]:
    normalized = {str(c).strip().lower().replace(" ", "_"): c for c in columns}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in normalized:
            return normalized[key]
    return None


def load_index_members(index_name: str) -> pd.DataFrame:
    parts = UNIVERSES.get(index_name)
    if not parts:
        raise ValueError(f"Unknown Matrix universe: {index_name}")

    session = _session()
    frames = []
    for part in parts:
        frame = fetch_constituent_csv(NIFTY_CONSTITUENT_URLS[part], session)
        sym_col = _find_col(frame.columns, ["Symbol", "SYMBOL"])
        name_col = _find_col(frame.columns, ["Company Name", "Company_Name"])
        isin_col = _find_col(frame.columns, ["ISIN Code", "ISIN_Code"])
        if not sym_col:
            raise RuntimeError(f"Could not identify Symbol column in {part} constituent file.")
        out = pd.DataFrame({
            "symbol": frame[sym_col].astype(str).str.strip().str.upper(),
            "company": frame[name_col].astype(str).str.strip() if name_col else "",
            "isin": frame[isin_col].astype(str).str.strip() if isin_col else "",
            "source_index": part,
        })
        frames.append(out)

    result = pd.concat(frames, ignore_index=True)
    result = result.drop_duplicates(subset=["symbol"], keep="first")
    result = result[result["symbol"].ne("") & result["symbol"].ne("NAN")].reset_index(drop=True)
    return result


def fetch_dhan_instruments() -> pd.DataFrame:
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    r = _session().get(url, timeout=60)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    df = pd.read_csv(io.StringIO(text), low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def resolve_security_ids(members: pd.DataFrame, instruments: pd.DataFrame) -> pd.DataFrame:
    x = instruments.copy()
    col_seg = _find_col(x.columns, ["SEM_EXM_EXCH_ID", "EXCH_ID", "exchange_segment"])
    col_seg2 = _find_col(x.columns, ["SEM_SEGMENT", "SEGMENT"])
    col_id = _find_col(x.columns, ["SEM_SMST_SECURITY_ID", "SECURITY_ID", "securityId"])
    col_sym = _find_col(x.columns, ["SEM_TRADING_SYMBOL", "TRADING_SYMBOL", "SYMBOL"])
    col_custom = _find_col(x.columns, ["SEM_CUSTOM_SYMBOL", "CUSTOM_SYMBOL"])
    col_inst = _find_col(x.columns, ["SEM_INSTRUMENT_NAME", "INSTRUMENT"])

    if not col_id or not col_sym:
        raise RuntimeError("Dhan scrip master did not contain expected security/symbol columns.")

    # Restrict to NSE cash instruments. Column conventions vary across scrip-master revisions.
    mask = pd.Series(True, index=x.index)
    if col_seg:
        mask &= x[col_seg].astype(str).str.upper().eq("NSE")
    if col_seg2:
        seg_text = x[col_seg2].astype(str).str.upper()
        if seg_text.ne("").any():
            mask &= seg_text.str.contains("EQUITY|NSE_EQ", regex=True, na=False)
    if col_inst:
        inst_text = x[col_inst].astype(str).str.upper()
        eq_mask = inst_text.eq("EQUITY") | inst_text.str.contains("EQUITY", na=False)
        if eq_mask.any():
            mask &= eq_mask

    x = x.loc[mask].copy()
    x["_trading_symbol"] = x[col_sym].astype(str).str.upper().str.strip()
    if col_custom:
        x["_custom_symbol"] = x[col_custom].astype(str).str.upper().str.strip()
    else:
        x["_custom_symbol"] = ""
    x["_security_id"] = x[col_id].astype(str).str.strip()

    left = members.copy()
    left["symbol"] = left["symbol"].str.upper().str.strip()
    merged = left.merge(x[["_trading_symbol", "_custom_symbol", "_security_id"]], left_on="symbol", right_on="_trading_symbol", how="left")
    missing = merged["_security_id"].isna() | merged["_security_id"].eq("")
    if missing.any():
        fallback = x[["_custom_symbol", "_security_id"]].drop_duplicates("_custom_symbol")
        repl = merged.loc[missing, ["symbol"]].merge(fallback, left_on="symbol", right_on="_custom_symbol", how="left")
        merged.loc[missing, "_security_id"] = repl["_security_id"].values
    merged = merged.drop(columns=[c for c in ["_trading_symbol", "_custom_symbol"] if c in merged.columns])
    return merged


def fetch_dhan_daily(client_id: str, access_token: str, security_id: str, from_date: str, to_date: str) -> pd.DataFrame:
    url = "https://api.dhan.co/v2/charts/historical"
    headers = {"Accept": "application/json", "Content-Type": "application/json", "access-token": access_token, "client-id": client_id}
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "expiryCode": 0,
        "oi": False,
        "fromDate": from_date,
        "toDate": to_date,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"Dhan historical {security_id}: HTTP {r.status_code} {r.text[:300]}")
    body = r.json()
    ts = body.get("timestamp") or body.get("start_Time") or []
    closes = body.get("close") or []
    highs = body.get("high") or []
    lows = body.get("low") or []
    if not ts or not closes:
        return pd.DataFrame()
    n = min(len(ts), len(closes), len(highs) or len(ts), len(lows) or len(ts))
    rows = []
    for i in range(n):
        try:
            dt = pd.to_datetime(int(ts[i]), unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None)
        except Exception:
            dt = pd.to_datetime(ts[i], errors="coerce")
        close = pd.to_numeric(closes[i], errors="coerce")
        high = pd.to_numeric(highs[i], errors="coerce") if highs else close
        low = pd.to_numeric(lows[i], errors="coerce") if lows else close
        if pd.isna(dt) or pd.isna(close):
            continue
        rows.append({"date": dt.normalize(), "close": float(close), "high": float(high) if not pd.isna(high) else float(close), "low": float(low) if not pd.isna(low) else float(close)})
    return pd.DataFrame(rows).drop_duplicates("date").sort_values("date").reset_index(drop=True)


def pattern_score(close: pd.Series, n: int) -> int:
    if len(close) < n + 5:
        return 0
    c = float(close.iloc[-1])
    prev = float(close.iloc[-n])
    ret = c / prev - 1.0 if prev else 0.0
    sma = float(close.iloc[-n:].mean())
    prior_window = close.iloc[-min(len(close), n + 1):-1]
    prior_hi = float(prior_window.max()) if not prior_window.empty else c
    prior_lo = float(prior_window.min()) if not prior_window.empty else c

    if c >= prior_hi * 0.997 and ret > 0:
        return 2
    if c <= prior_lo * 1.003 and ret < 0:
        return -2
    if c > sma and ret > 0.02:
        return 1
    if c < sma and ret < -0.02:
        return -1
    return 0


def rs_score(stock: pd.Series, benchmark: pd.Series, n: int) -> int:
    if len(stock) < n + 1 or len(benchmark) < n + 1:
        return 0
    s0, s1 = float(stock.iloc[-n]), float(stock.iloc[-1])
    b0, b1 = float(benchmark.iloc[-n]), float(benchmark.iloc[-1])
    if min(s0, s1, b0, b1) <= 0:
        return 0
    rel = (s1 / s0 - 1.0) - (b1 / b0 - 1.0)
    if rel >= 0.08:
        return 2
    if rel >= 0.02:
        return 1
    if rel <= -0.08:
        return -2
    if rel <= -0.02:
        return -1
    return 0


def swing_pct(close: pd.Series, n: int) -> float:
    if len(close) < n + 1:
        return float("nan")
    base = float(close.iloc[-n])
    return ((float(close.iloc[-1]) / base) - 1.0) * 100.0 if base else float("nan")


def score_stock(stock: pd.DataFrame, benchmark: pd.DataFrame) -> Dict[str, object]:
    s = stock.set_index("date")["close"].astype(float).dropna().sort_index()
    b = benchmark.set_index("date")["close"].astype(float).dropna().sort_index()
    joined = pd.concat([s.rename("stock"), b.rename("benchmark")], axis=1).dropna()
    if joined.empty:
        return {"valid": False}
    s = joined["stock"]
    b = joined["benchmark"]
    row: Dict[str, object] = {"valid": True, "last_date": str(s.index[-1].date()), "ltp": float(s.iloc[-1])}
    price_total = 0
    rs_total = 0
    for label, n in TIMEFRAMES.items():
        ps = pattern_score(s, n)
        rs = rs_score(s, b, n)
        sp = swing_pct(s, n)
        row[f"{label}_price"] = ps
        row[f"{label}_rs"] = rs
        row[f"{label}_swing_pct"] = sp
        price_total += ps
        rs_total += rs
    row["price_score"] = price_total
    row["rs_score"] = rs_total
    row["composite"] = price_total + rs_total
    row["rank"] = None
    if row["composite"] >= 12:
        row["grade"] = "A+"
    elif row["composite"] >= 8:
        row["grade"] = "A"
    elif row["composite"] >= 4:
        row["grade"] = "B"
    elif row["composite"] <= -12:
        row["grade"] = "C-"
    elif row["composite"] <= -8:
        row["grade"] = "C"
    elif row["composite"] <= -4:
        row["grade"] = "B-"
    else:
        row["grade"] = "NEUTRAL"
    row["direction"] = "BULLISH" if row["composite"] > 0 else "BEARISH" if row["composite"] < 0 else "NEUTRAL"
    return row


def build_matrix_rows(members: pd.DataFrame, histories: Dict[str, pd.DataFrame], benchmark: pd.DataFrame, universe_name: str, benchmark_name: str) -> pd.DataFrame:
    rows = []
    for _, member in members.iterrows():
        sid = str(member.get("security_id", ""))
        if not sid or sid.lower() == "nan" or sid not in histories:
            continue
        try:
            result = score_stock(histories[sid], benchmark)
        except Exception:
            continue
        if not result.get("valid"):
            continue
        result.update({
            "symbol": member["symbol"],
            "company": member.get("company", ""),
            "universe": universe_name,
            "rs_benchmark": benchmark_name,
            "source_index": member.get("source_index", ""),
            "security_id": sid,
        })
        rows.append(result)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values(["composite", "price_score", "rs_score"], ascending=False, kind="mergesort").reset_index(drop=True)
    df["rank"] = df.index + 1
    return df


def universe_counts(df: pd.DataFrame) -> Dict[str, int]:
    if df.empty:
        return {"stocks": 0, "bullish": 0, "neutral": 0, "bearish": 0}
    return {
        "stocks": int(len(df)),
        "bullish": int((df["composite"] > 0).sum()),
        "neutral": int((df["composite"] == 0).sum()),
        "bearish": int((df["composite"] < 0).sum()),
    }
