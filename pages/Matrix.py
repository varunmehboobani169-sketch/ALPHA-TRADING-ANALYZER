from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from auth import logout_button, require_login
from matrix_engine import (
    DEFAULT_HISTORY_DAYS,
    NIFTY_CONSTITUENT_URLS,
    UNIVERSES,
    TIMEFRAMES,
    build_matrix_rows,
    fetch_dhan_daily,
    fetch_dhan_instruments,
    load_index_members,
    resolve_security_ids,
    universe_counts,
)

st.set_page_config(page_title="Matrix", page_icon="🧮", layout="wide")
client_id, access_token = require_login()
logout_button()

st.title("🧮 Market Matrix")
st.caption("Standalone Matrix scanner — independent of the Bias Engine and ORB.")
st.info(
    "Matrix v1 uses the same core architecture discussed earlier: four timeframes, price-pattern scoring, swing performance and relative-strength scoring. "
    "The current implementation is a transparent price/RS model; it is not yet an exact P&F-box reproduction."
)

UNIVERSE_OPTIONS = list(UNIVERSES.keys())
BENCHMARK_OPTIONS = [
    "NIFTY 50",
    "NIFTY 200",
    "NIFTY 500",
    "NIFTY MIDCAP 150",
    "NIFTY SMALLCAP 250",
    "NIFTY TOTAL MARKET",
]

BENCHMARK_ALIASES = {
    "NIFTY 50": ["NIFTY 50", "NIFTY"],
    "NIFTY 200": ["NIFTY 200"],
    "NIFTY 500": ["NIFTY 500", "NIFTY500"],
    "NIFTY MIDCAP 150": ["NIFTY MIDCAP 150", "NIFTYMIDCAP150"],
    "NIFTY SMALLCAP 250": ["NIFTY SMALLCAP 250", "NIFTYSMLCAP250", "NIFTYSMALLCAP250"],
    "NIFTY TOTAL MARKET": ["NIFTY TOTAL MARKET", "NIFTYTOTALMARKET"],
}


def _norm(value: object) -> str:
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


@st.cache_data(ttl=12 * 60 * 60, show_spinner=False)
def cached_members(universe_name: str) -> pd.DataFrame:
    return load_index_members(universe_name)


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def cached_instruments() -> pd.DataFrame:
    return fetch_dhan_instruments()


@st.cache_data(ttl=15 * 60, show_spinner=False)
def cached_daily(client_id_: str, token_: str, security_id: str, from_date: str, to_date: str) -> pd.DataFrame:
    return fetch_dhan_daily(client_id_, token_, security_id, from_date, to_date)


def resolve_index_security_id(instruments: pd.DataFrame, benchmark: str) -> str | None:
    x = instruments.copy()
    id_col = next((c for c in x.columns if str(c).strip().lower() in {"sem_smst_security_id", "security_id", "securityid"}), None)
    seg_col = next((c for c in x.columns if str(c).strip().lower() in {"sem_exm_exch_id", "exchange_segment", "exch_id"}), None)
    instrument_col = next((c for c in x.columns if str(c).strip().lower() in {"sem_instrument_name", "instrument"}), None)
    symbol_col = next((c for c in x.columns if str(c).strip().lower() in {"sem_trading_symbol", "trading_symbol", "symbol"}), None)
    custom_col = next((c for c in x.columns if str(c).strip().lower() in {"sem_custom_symbol", "custom_symbol"}), None)
    seg2_col = next((c for c in x.columns if str(c).strip().lower() in {"sem_segment", "segment"}), None)
    if not id_col:
        return None

    aliases = {_norm(a) for a in BENCHMARK_ALIASES[benchmark]}
    candidates = x.copy()
    if seg_col:
        candidates = candidates[candidates[seg_col].astype(str).str.upper().isin({"NSE", "IDX_I", "0"}) | candidates[seg_col].astype(str).str.contains("IDX", case=False, na=False)]
    if seg2_col:
        s = candidates[seg2_col].astype(str).str.upper()
        candidates = candidates[s.str.contains("IDX|INDEX|I", regex=True, na=False)]
    if instrument_col:
        inst = candidates[instrument_col].astype(str).str.upper()
        idx_mask = inst.str.contains("INDEX", na=False)
        if idx_mask.any():
            candidates = candidates[idx_mask]

    candidate_cols = [c for c in [symbol_col, custom_col] if c]
    if not candidate_cols:
        return None
    best = None
    for _, row in candidates.iterrows():
        vals = {_norm(row[c]) for c in candidate_cols}
        score = 0
        for alias in aliases:
            for value in vals:
                if value == alias:
                    score = max(score, 100)
                elif alias and alias in value:
                    score = max(score, 70)
                elif value and value in alias:
                    score = max(score, 50)
        if score and (best is None or score > best[0]):
            best = (score, str(row[id_col]))
    return best[1] if best else None


with st.sidebar:
    st.subheader("Matrix Controls")
    universe = st.selectbox("Matrix Universe", UNIVERSE_OPTIONS, index=0)
    benchmark = st.selectbox(
        "RS Underlying / Benchmark",
        BENCHMARK_OPTIONS,
        index=0 if universe == "NIFTY 50" else 2,
        help="Choose the independent benchmark used for Relative Strength comparison. NIFTY 500 is the recommended default for broad universes."
    )
    scan_mode = st.radio("Scan", ["Preview", "Full Universe"], index=0)
    preview_size = st.slider("Preview stocks", min_value=25, max_value=250, value=50, step=25)
    history_days = st.slider("History window (calendar days)", min_value=280, max_value=700, value=DEFAULT_HISTORY_DAYS, step=20)
    run = st.button("RUN MATRIX", type="primary", use_container_width=True)

st.caption(
    f"Selected universe: **{universe}**  •  RS benchmark: **{benchmark}**  •  "
    f"Timeframes: {', '.join(f'{k}={v}d' for k, v in TIMEFRAMES.items())}"
)

if run:
    try:
        members = cached_members(universe)
        instruments = cached_instruments()
        members = resolve_security_ids(members, instruments)
        members["security_id"] = members["security_id"].fillna("").astype(str).str.replace(".0", "", regex=False)

        benchmark_security_id = resolve_index_security_id(instruments, benchmark)
        fallback_note = None
        if not benchmark_security_id and benchmark != "NIFTY 500":
            benchmark_security_id = resolve_index_security_id(instruments, "NIFTY 500")
            fallback_note = f"Dhan index-master match for {benchmark} was unavailable; RS benchmark temporarily fell back to NIFTY 500."
        if not benchmark_security_id:
            raise RuntimeError(f"Could not resolve security ID for RS benchmark {benchmark} from Dhan scrip master.")

        eligible = members[members["security_id"].ne("") & members["security_id"].ne("nan")].copy()
        if eligible.empty:
            raise RuntimeError("No stocks in the selected universe could be mapped to Dhan security IDs.")

        if scan_mode == "Preview":
            eligible = eligible.head(min(preview_size, len(eligible)))

        end = date.today()
        start = end - timedelta(days=history_days)
        from_date, to_date = start.isoformat(), (end + timedelta(days=1)).isoformat()

        with st.spinner(f"Loading benchmark history: {benchmark}..."):
            benchmark_df = cached_daily(client_id, access_token, benchmark_security_id, from_date, to_date)
        if benchmark_df.empty:
            raise RuntimeError(f"No historical data returned for benchmark {benchmark}.")

        histories: dict[str, pd.DataFrame] = {}
        progress = st.progress(0.0, text="Scanning stocks...")
        total = len(eligible)
        errors = []
        for i, (_, member) in enumerate(eligible.iterrows(), start=1):
            sid = str(member["security_id"])
            try:
                df = cached_daily(client_id, access_token, sid, from_date, to_date)
                if not df.empty:
                    histories[sid] = df
            except Exception as exc:
                errors.append(f"{member['symbol']}: {exc}")
            progress.progress(i / total, text=f"Scanning {i}/{total}: {member['symbol']}")
        progress.empty()

        result = build_matrix_rows(eligible, histories, benchmark_df, universe, benchmark)
        if result.empty:
            raise RuntimeError("Matrix returned no valid rows. Check Dhan access, index mapping and historical data availability.")

        counts = universe_counts(result)
        st.session_state["matrix_result"] = result
        st.session_state["matrix_counts"] = counts
        st.session_state["matrix_meta"] = {
            "universe": universe,
            "benchmark": benchmark,
            "requested": len(eligible),
            "valid": len(result),
            "benchmark_security_id": benchmark_security_id,
            "fallback": fallback_note,
            "errors": errors,
        }
    except Exception as exc:
        st.error(f"Matrix run failed: {exc}")

result = st.session_state.get("matrix_result")
meta = st.session_state.get("matrix_meta")
counts = st.session_state.get("matrix_counts")

if result is not None and meta is not None:
    if meta.get("fallback"):
        st.warning(meta["fallback"])
    st.success(
        f"Matrix ready: {meta['valid']} valid stocks out of {meta['requested']} scanned. "
        f"Universe = {meta['universe']} • RS = {meta['benchmark']}"
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Stocks", counts["stocks"])
    c2.metric("Bullish", counts["bullish"])
    c3.metric("Neutral", counts["neutral"])
    c4.metric("Bearish", counts["bearish"])
    c5.metric("Top Score", int(result["composite"].max()))

    st.subheader("Matrix Ranking")
    view = result.copy()
    view["LTP"] = view["ltp"].round(2)
    view["Price"] = view["price_score"]
    view["RS"] = view["rs_score"]
    view["Total"] = view["composite"]
    view["Direction"] = view["direction"]
    view["Grade"] = view["grade"]

    cols = [
        "rank", "symbol", "company", "source_index", "LTP",
        "ST_price", "MT_price", "IT_price", "LT_price", "Price",
        "ST_rs", "MT_rs", "IT_rs", "LT_rs", "RS", "Total", "Grade", "Direction",
    ]
    labels = {
        "rank": "Rank", "symbol": "Symbol", "company": "Company", "source_index": "Universe",
        "ST_price": "ST P", "MT_price": "MT P", "IT_price": "IT P", "LT_price": "LT P",
        "ST_rs": "ST RS", "MT_rs": "MT RS", "IT_rs": "IT RS", "LT_rs": "LT RS",
    }
    table = view[[c for c in cols if c in view.columns]].rename(columns=labels)
    st.dataframe(table, use_container_width=True, hide_index=True)

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Top Bullish")
        bull = result[result["composite"] > 0].head(10)
        st.dataframe(bull[["rank", "symbol", "composite", "price_score", "rs_score", "grade"]], use_container_width=True, hide_index=True)
    with col_b:
        st.subheader("Top Bearish")
        bear = result[result["composite"] < 0].sort_values("composite").head(10)
        st.dataframe(bear[["rank", "symbol", "composite", "price_score", "rs_score", "grade"]], use_container_width=True, hide_index=True)

    st.subheader("Selected Stock Detail")
    symbols = result["symbol"].tolist()
    selected = st.selectbox("Stock", symbols, index=0)
    row = result[result["symbol"] == selected].iloc[0]
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Composite", int(row["composite"]))
    d2.metric("Price Score", int(row["price_score"]))
    d3.metric("RS Score", int(row["rs_score"]))
    d4.metric("Grade", str(row["grade"]))
    detail = pd.DataFrame({
        "Timeframe": ["Short", "Medium", "Intermediate", "Long"],
        "Days": [TIMEFRAMES["ST"], TIMEFRAMES["MT"], TIMEFRAMES["IT"], TIMEFRAMES["LT"]],
        "Price Score": [row["ST_price"], row["MT_price"], row["IT_price"], row["LT_price"]],
        "RS Score": [row["ST_rs"], row["MT_rs"], row["IT_rs"], row["LT_rs"]],
        "Swing %": [row["ST_swing_pct"], row["MT_swing_pct"], row["IT_swing_pct"], row["LT_swing_pct"]],
    })
    detail["Swing %"] = detail["Swing %"].round(2)
    st.dataframe(detail, use_container_width=True, hide_index=True)

    errors = meta.get("errors") or []
    if errors:
        with st.expander(f"Data errors ({len(errors)})"):
            st.write("\n".join(errors[:100]))

else:
    st.warning("Choose the Matrix Universe and RS Underlying, then press **RUN MATRIX** to generate the ranking.")
    st.markdown(
        "**Recommended first check:** `NIFTY 50` universe with `NIFTY 50` RS, then `NIFTY TOTAL MARKET` with `NIFTY 500` RS. "
        "For the 750-stock universe, the Total Market constituent set is built as NIFTY 500 + NIFTY Microcap 250."
    )
