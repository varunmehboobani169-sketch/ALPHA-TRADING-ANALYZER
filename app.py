import io
import time
import zipfile
from typing import List, Tuple

import pandas as pd
import requests
import streamlit as st

DHAN_API = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

# Index options: exactly 21 ATM-relative strikes = ATM-10 through ATM+10.
INDEX_OFFSETS = tuple(range(-10, 11))
# Stock F&O: Dhan rolling historical API supports ATM-3 through ATM+3.
STOCK_OFFSETS = tuple(range(-3, 4))

st.set_page_config(page_title="Dhan Options Data Downloader", page_icon="📥", layout="wide")


def col(df, *names):
    m = {str(c).strip().upper(): c for c in df.columns}
    for n in names:
        if n.upper() in m:
            return m[n.upper()]
    return None


def load_master():
    d = pd.read_csv(MASTER_URL, low_memory=False)
    d.columns = [str(c).strip() for c in d.columns]
    return d


def build_universe(d):
    exch, seg, inst = col(d, "EXCH_ID"), col(d, "SEGMENT"), col(d, "INSTRUMENT")
    sid, usid, usym = col(d, "SECURITY_ID"), col(d, "UNDERLYING_SECURITY_ID"), col(d, "UNDERLYING_SYMBOL")
    sym, exp, flag = col(d, "SYMBOL_NAME"), col(d, "EXPIRY_DATE"), col(d, "EXPIRY_FLAG")
    needed = [exch, seg, inst, sid, usid, usym]
    if any(x is None for x in needed):
        raise RuntimeError(f"Unexpected Dhan master columns. Found: {list(d.columns)}")
    x = pd.DataFrame({
        "exchange": d[exch].astype(str).str.upper().str.strip(),
        "segment": d[seg].astype(str).str.upper().str.strip(),
        "instrument": d[inst].astype(str).str.upper().str.strip(),
        "security_id": pd.to_numeric(d[sid], errors="coerce"),
        "underlying_security_id": pd.to_numeric(d[usid], errors="coerce"),
        "underlying_symbol": d[usym].astype(str).str.strip(),
        "symbol": d[sym].astype(str).str.strip() if sym else "",
        "expiry_date": pd.to_datetime(d[exp], errors="coerce") if exp else pd.NaT,
        "expiry_flag": d[flag].astype(str).str.upper().str.strip() if flag else "",
    })
    x["exchange_segment"] = ""
    x.loc[(x.exchange == "NSE") & (x.segment == "D"), "exchange_segment"] = "NSE_FNO"
    x.loc[(x.exchange == "BSE") & (x.segment == "D"), "exchange_segment"] = "BSE_FNO"
    x = x[x.instrument.isin(["OPTIDX", "OPTSTK"])]
    x = x[x.exchange_segment.isin(["NSE_FNO", "BSE_FNO"])]
    x = x.dropna(subset=["underlying_security_id"])
    x["family"] = x.instrument.map({"OPTIDX": "INDEX", "OPTSTK": "STOCK"})
    return x


def get_underlyings(x):
    return (x.groupby(["exchange", "exchange_segment", "underlying_security_id", "underlying_symbol", "family"], dropna=False)
        .agg(first_expiry=("expiry_date", "min"), last_expiry=("expiry_date", "max"), contracts=("security_id", "count"))
        .reset_index().sort_values(["exchange", "family", "underlying_symbol"]))


def years(text):
    out = []
    for token in str(text).replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(token))
    return sorted(set(out))


def windows(y: int) -> List[Tuple[str, str]]:
    cur, end = pd.Timestamp(y, 1, 1), pd.Timestamp(y + 1, 1, 1)
    out = []
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=30), end)
        out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt
    return out


def post(client, token, payload):
    r = requests.post(DHAN_API + "/charts/rollingoption", headers={
        "Accept": "application/json", "Content-Type": "application/json",
        "access-token": token, "client-id": client,
    }, json=payload, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"Dhan HTTP {r.status_code}: {r.text[:700]}")
    j = r.json()
    status = str(j.get("status", "")).lower()
    if status and status not in ("success", "ok"):
        raise RuntimeError(str(j)[:700])
    return j


def fetch_one(client, token, row, year, offset, side, expiry_flag, expiry_code):
    frames = []
    instrument = "OPTIDX" if row.family == "INDEX" else "OPTSTK"
    for a, b in windows(year):
        payload = {
            "exchangeSegment": row.exchange_segment, "interval": "1",
            "securityId": str(int(row.underlying_security_id)), "instrument": instrument,
            "expiryFlag": expiry_flag, "expiryCode": int(expiry_code),
            "strike": offset, "drvOptionType": side,
            "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
            "fromDate": a, "toDate": b,
        }
        j = post(client, token, payload)
        leg = (j.get("data") or {}).get("ce" if side == "CALL" else "pe") or {}
        ts = leg.get("timestamp") or []
        if not ts:
            continue
        n = len(ts)
        def arr(k):
            z = list(leg.get(k) or [])
            return (z + [None] * n)[:n]
        f = pd.DataFrame({
            "timestamp": pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None),
            "open": arr("open"), "high": arr("high"), "low": arr("low"), "close": arr("close"),
            "iv": arr("iv"), "volume": arr("volume"), "strike": arr("strike"),
            "oi": arr("oi"), "spot": arr("spot"),
        })
        f["underlying_symbol"] = row.underlying_symbol
        f["underlying_security_id"] = int(row.underlying_security_id)
        f["exchange_segment"] = row.exchange_segment
        f["family"] = row.family
        f["option_type"] = side
        f["requested_strike"] = offset
        f["expiry_flag"] = expiry_flag
        f["expiry_code"] = expiry_code
        f["year"] = year
        frames.append(f)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def zip_by_year(df):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for y, g in df.groupby("year"):
            z.writestr(f"options_{int(y)}.csv", g.sort_values("timestamp").to_csv(index=False))
    return b.getvalue()


st.title("📥 Dhan NSE / BSE Options Data Downloader")
st.caption("Dedicated data-download dashboard • no trading analytics")

with st.sidebar:
    st.header("Dhan Connection")
    client = st.text_input("Dhan Client ID")
    token = st.text_input("Dhan Access Token", type="password")
    st.divider()
    years_text = st.text_input("Years", "2022-2026")
    stock_mode = st.selectbox("F&O stock strikes", ["ATM-3 to ATM+3", "ATM only"])
    expiry_codes = st.multiselect("Expiry codes", [0, 1, 2], default=[0, 1, 2])
    delay = st.number_input("Delay between API requests (sec)", 0.0, 5.0, 0.25, 0.25)

st.info("INDEX OPTIONS: 21 ATM-relative strikes — ATM-10 through ATM+10, inclusive. Both CALL and PUT are downloaded for every requested offset. STOCK F&O: ATM-3 through ATM+3.")

if not client or not token:
    st.warning("Enter Dhan Client ID and Access Token in the sidebar.")
    st.stop()

if st.button("LOAD NSE + BSE F&O UNIVERSE", type="primary"):
    try:
        with st.spinner("Loading Dhan instrument master..."):
            raw = load_master()
            u = build_universe(raw)
            st.session_state["contracts"] = u
            st.session_state["underlyings"] = get_underlyings(u)
        st.success(f"Loaded {len(st.session_state['underlyings']):,} option underlyings.")
        st.caption(f"Master rows: {len(raw):,} | option contracts: {len(u):,} | segments: {sorted(u.exchange_segment.unique())}")
    except Exception as e:
        st.error(str(e))

u = st.session_state.get("underlyings", pd.DataFrame())
if u.empty:
    st.stop()

c1, c2 = st.columns(2)
with c1:
    ex = st.multiselect("Exchange", sorted(u.exchange.unique()), default=sorted(u.exchange.unique()))
with c2:
    fam = st.multiselect("Type", ["INDEX", "STOCK"], default=["INDEX", "STOCK"])
f = u[u.exchange.isin(ex) & u.family.isin(fam)].copy()

st.subheader("Available underlyings")
st.dataframe(f, use_container_width=True, height=380)

symbols = st.multiselect("Select underlyings", sorted(f.underlying_symbol.unique()), default=sorted(f.underlying_symbol.unique())[:5])
selected = f[f.underlying_symbol.isin(symbols)]
st.metric("Selected", len(selected))

if st.button("DOWNLOAD YEAR-WISE DATA", type="primary", use_container_width=True):
    ys = years(years_text)
    if not ys or not expiry_codes or selected.empty:
        st.error("Select valid years, expiry codes and at least one underlying.")
        st.stop()

    idx_offsets = [f"ATM{n:+d}" if n else "ATM" for n in INDEX_OFFSETS]
    stk_offsets = [f"ATM{n:+d}" if n else "ATM" for n in (STOCK_OFFSETS if stock_mode.startswith("ATM-3") else [0])]

    # Safety check: an index job must always contain exactly the 21 offsets ATM-10..ATM+10.
    if len(idx_offsets) != 21 or idx_offsets[0] != "ATM-10" or idx_offsets[-1] != "ATM+10":
        st.error("Internal strike-range configuration error: index range must be ATM-10 through ATM+10.")
        st.stop()

    jobs = []
    for _, r in selected.iterrows():
        offs = idx_offsets if r.family == "INDEX" else stk_offsets
        flags = ["WEEK", "MONTH"] if r.family == "INDEX" else ["MONTH"]
        for y in ys:
            for ef in flags:
                for ec in expiry_codes:
                    for off in offs:
                        # Every ATM-relative index strike is downloaded for BOTH option legs.
                        for side in ["CALL", "PUT"]:
                            jobs.append((r, y, off, side, ef, ec))

    st.caption(f"Index strike range: ATM-10 … ATM+10 ({len(idx_offsets)} strikes) × CALL + PUT. Stock range: {len(stk_offsets)} strikes when enabled. Total request groups: {len(jobs):,}. Historical requests are split into 30-day windows.")
    prog = st.progress(0.0)
    status = st.empty()
    frames, errors = [], []
    for i, (r, y, off, side, ef, ec) in enumerate(jobs, 1):
        status.write(f"{r.underlying_symbol} | {y} | {ef} {ec} | {off} | {side}")
        try:
            z = fetch_one(client, token, r, y, off, side, ef, ec)
            if not z.empty:
                frames.append(z)
        except Exception as e:
            errors.append({"underlying": r.underlying_symbol, "year": y, "expiry_flag": ef, "expiry_code": ec, "strike": off, "side": side, "error": str(e)})
        if delay:
            time.sleep(delay)
        prog.progress(i / len(jobs))
    if not frames:
        st.error("No data returned. Check Dhan API/data subscription, token validity, date range and expiry parameters.")
        if errors:
            st.dataframe(pd.DataFrame(errors), use_container_width=True)
    else:
        data = pd.concat(frames, ignore_index=True).drop_duplicates()
        st.success(f"Downloaded {len(data):,} rows across {data.year.nunique()} year(s).")
        st.download_button("DOWNLOAD YEAR-WISE ZIP", zip_by_year(data), "dhan_options_yearwise.zip", "application/zip", use_container_width=True)
        if errors:
            st.warning(f"{len(errors):,} request groups failed.")
            st.dataframe(pd.DataFrame(errors), use_container_width=True)
