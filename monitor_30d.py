from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")


def _safe_num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_at_10(body, day):
    timestamps = body.get("timestamp") or []
    closes = body.get("close") or []
    oi = body.get("open_interest") or []
    target = datetime.combine(day, dtime(10, 0), IST)
    for i, stamp in enumerate(timestamps):
        try:
            dt = datetime.fromtimestamp(int(stamp), IST)
        except (TypeError, ValueError, OSError):
            continue
        if dt.date() == day and dt >= target and i < len(closes) and closes[i] is not None:
            return {
                "timestamp": dt,
                "price": _safe_num(closes[i]),
                "oi": _safe_num(oi[i]) if i < len(oi) else None,
            }
    return None


def _daily_spot_history(post_api, client_id, token, index_name, security_id, start_day, end_day):
    body = post_api(
        client_id,
        token,
        "/charts/historical",
        {
            "securityId": str(security_id),
            "exchangeSegment": "IDX_I",
            "instrument": "INDEX",
            "expiryCode": 0,
            "oi": False,
            "fromDate": start_day.isoformat(),
            "toDate": (end_day + timedelta(days=1)).isoformat(),
        },
        timeout=60,
    )
    rows = []
    for stamp, close in zip(body.get("timestamp") or [], body.get("close") or []):
        if close is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(stamp), IST)
        except (TypeError, ValueError, OSError):
            continue
        if start_day <= dt.date() <= end_day:
            rows.append({"date": dt.date(), "spot_close": _safe_num(close)})
    return pd.DataFrame(rows).drop_duplicates("date", keep="last").sort_values("date")


def render_30d_module(post_api, get_expiries, get_option_chain, option_at_10am, calculate_iv, indexes):
    st.divider()
    st.header("30-Day Monitoring")
    st.caption(
        "Separate historical monitor • 10:00 observations • ATM/near-ATM context • "
        "does not alter the daily live dashboard above."
    )

    c1, c2 = st.columns(2)
    with c1:
        lookback = st.slider("Lookback (calendar days)", 7, 30, 30)
    with c2:
        index_choice = st.multiselect("Indexes", list(indexes.keys()), default=list(indexes.keys()))

    if not index_choice:
        st.info("Select at least one index.")
        return

    if not st.button("FETCH 30-DAY MONITOR", type="secondary", use_container_width=True):
        st.info("This module is on-demand so it does not add API traffic to the live monitor.")
        return

    today = datetime.now(IST).date()
    start_day = today - timedelta(days=lookback - 1)

    # This module intentionally backfills the index 10:00 series first.
    spot_frames = []
    errors = []
    for name in index_choice:
        try:
            spec = indexes[name]
            frame = _daily_spot_history(
                post_api, client_id, token, name, spec["security_id"], start_day, today
            )
            if not frame.empty:
                frame["index"] = name
                spot_frames.append(frame)
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if spot_frames:
        spot_df = pd.concat(spot_frames, ignore_index=True)
        st.subheader("10:00 / Daily Index History")
        st.dataframe(spot_df, use_container_width=True, hide_index=True)
        try:
            chart_df = spot_df.pivot(index="date", columns="index", values="spot_close")
            st.line_chart(chart_df)
        except Exception:
            pass
    else:
        st.warning("No historical index rows were returned.")

    st.subheader("30-Day Option Monitoring Cache")
    st.info(
        "The live IV/OI snapshot is intentionally kept separate. Historical option IV/OI requires "
        "contract-level historical series and expiry mapping; the module will display any daily "
        "snapshots captured by this monitor and will not substitute today's option-chain values for past dates."
    )

    history_key = "iv_monitor_30d_snapshots"
    history = st.session_state.get(history_key, [])
    cutoff = today - timedelta(days=lookback - 1)
    history = [x for x in history if x.get("date") and x["date"] >= cutoff]
    if history:
        hist_rows = []
        for item in history:
            row = {"date": item.get("date"), "index": item.get("index")}
            row.update(item.get("metrics", {}))
            hist_rows.append(row)
        hist_df = pd.DataFrame(hist_rows).sort_values(["date", "index"])
        st.dataframe(hist_df, use_container_width=True, hide_index=True)
    else:
        st.caption("No stored daily snapshots yet. Each successful daily lock can be retained here for the next 30 days.")

    if errors:
        st.warning("\n".join(errors))
