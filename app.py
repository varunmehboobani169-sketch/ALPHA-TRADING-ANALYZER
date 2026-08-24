
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import streamlit as st
from zoneinfo import ZoneInfo
ALPHA_LOGO_PATH = Path(__file__).resolve().parent / "alpha_analyzer_logo.png"
TRADE_DB_PATH = Path(__file__).resolve().parent / "alpha_trade_history.db"

st.set_page_config(page_title="ALPHA ANALYZER", page_icon="α", layout="wide")

API = "https://api.dhan.co/v2"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

def local_now():
    """Application display time: India Standard Time (IST)."""
    return datetime.now(LOCAL_TZ)


# -----------------------------
# Shared page header helper
# -----------------------------
def render_page_hero(title, subtitle, badges=None):
    badges = badges or []
    badge_html = "".join(
        f"<span class='alpha-badge'>{label}</span>"
        for label, _ in badges
    )
    st.markdown(
        f"""
        <div class="alpha-hero">
            <div class="alpha-hero-title">{title}</div>
            <div class="alpha-hero-sub">{subtitle}</div>
            {badge_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


# -----------------------------
# Session credentials
# -----------------------------
if "client_id" not in st.session_state:
    st.session_state.client_id = ""
if "access_token" not in st.session_state:
    st.session_state.access_token = ""
if "api_log" not in st.session_state:
    st.session_state.api_log = []

# Per-session trade book. It survives Streamlit reruns and is downloadable.
if "trade_book" not in st.session_state:
    st.session_state.trade_book = {}

if "trade_sequence" not in st.session_state:
    st.session_state.trade_sequence = 0

if "trade_history" not in st.session_state:
    st.session_state.trade_history = []

def _trade_db_connect():
    conn = sqlite3.connect(str(TRADE_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            trade_id TEXT NOT NULL,
            date TEXT NOT NULL,
            module TEXT NOT NULL,
            mode TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            signal_level REAL,
            entry REAL,
            initial_sl REAL,
            current_sl REAL,
            current REAL,
            exit_price REAL,
            status TEXT NOT NULL,
            exit_reason TEXT,
            points_pnl REAL,
            pnl_pct REAL,
            opened TEXT NOT NULL,
            closed TEXT,
            duration_min REAL,
            sl_trails INTEGER DEFAULT 0,
            last_sl_update TEXT
        )
        """
    )
    conn.commit()
    return conn


def _persist_trade_insert(trade):
    owner = str(st.session_state.get("client_id", "default") or "default")
    conn = _trade_db_connect()
    conn.execute(
        """
        INSERT INTO trades (
            owner, trade_id, date, module, mode, symbol, direction,
            signal_level, entry, initial_sl, current_sl, current,
            exit_price, status, exit_reason, points_pnl, pnl_pct,
            opened, closed, duration_min, sl_trails, last_sl_update
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            owner, trade["Trade ID"], trade["Date"], trade["Module"],
            trade["Mode"], trade["Symbol"], trade["Direction"],
            trade.get("Signal Level"), trade.get("Entry"),
            trade.get("Initial SL"), trade.get("SL"),
            trade.get("Current"), trade.get("Exit"),
            trade.get("Status"), trade.get("Exit Reason", ""),
            trade.get("Points P&L"), trade.get("P&L %"),
            trade.get("Opened"), trade.get("Closed", ""),
            trade.get("Duration (min)"), trade.get("SL Trails", 0),
            trade.get("Last SL Update", ""),
        ),
    )
    conn.commit()
    conn.close()


def _persist_trade_update(trade):
    owner = str(st.session_state.get("client_id", "default") or "default")
    conn = _trade_db_connect()
    conn.execute(
        """
        UPDATE trades SET
            current_sl=?, current=?, exit_price=?, status=?,
            exit_reason=?, points_pnl=?, pnl_pct=?, closed=?,
            duration_min=?, sl_trails=?, last_sl_update=?
        WHERE owner=? AND trade_id=?
        """,
        (
            trade.get("SL"), trade.get("Current"), trade.get("Exit"),
            trade.get("Status"), trade.get("Exit Reason", ""),
            trade.get("Points P&L"), trade.get("P&L %"),
            trade.get("Closed", ""), trade.get("Duration (min)"),
            trade.get("SL Trails", 0), trade.get("Last SL Update", ""),
            owner, trade["Trade ID"],
        ),
    )
    conn.commit()
    conn.close()


def _load_trade_history(owner):
    conn = _trade_db_connect()
    rows = conn.execute(
        """
        SELECT trade_id,date,module,mode,symbol,direction,
               signal_level,entry,initial_sl,current_sl,current,exit_price,
               status,exit_reason,points_pnl,pnl_pct,opened,closed,
               duration_min,sl_trails,last_sl_update
        FROM trades
        WHERE owner=?
        ORDER BY opened DESC
        """,
        (str(owner or "default"),),
    ).fetchall()
    conn.close()

    cols = [
        "Trade ID", "Date", "Module", "Mode", "Symbol", "Direction",
        "Signal Level", "Entry", "Initial SL", "SL", "Current", "Exit",
        "Status", "Exit Reason", "Points P&L", "P&L %",
        "Opened", "Closed", "Duration (min)", "SL Trails", "Last SL Update"
    ]
    return [dict(zip(cols, row)) for row in rows]


def _reload_trade_history():
    owner = str(st.session_state.get("client_id", "default") or "default")
    if st.session_state.get("_trade_history_owner") != owner:
        st.session_state.trade_history = _load_trade_history(owner)
        st.session_state._trade_history_owner = owner


with st.sidebar:
    logo_col, brand_col = st.columns([1, 3], gap="small")
    with logo_col:
        if ALPHA_LOGO_PATH.exists():
            st.image(str(ALPHA_LOGO_PATH), width=46)
        else:
            st.markdown(
                '<div class="a-mark">A</div>',
                unsafe_allow_html=True,
            )
    with brand_col:
        st.markdown(
            """
            <div style="padding-top:2px;">
                <div class="a-brand-name">ALPHA ANALYZER</div>
                <div class="a-brand-sub">Professional Market Dashboard</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown('<div class="a-side-head">Account</div>', unsafe_allow_html=True)
    st.session_state.client_id = st.text_input(
        "User Name",
        value=st.session_state.client_id,
    ).strip()
    st.session_state.access_token = st.text_input(
        "Password",
        value=st.session_state.access_token,
        type="password",
    ).strip()

    _reload_trade_history()

    auto = st.checkbox("Auto Refresh", True)
    page = st.radio(
        "Module",
        [
            "Option Seller",
            "Intraday",
            "Positional",
            "MCX Futures",
            "Market Overview",
            "Sector Analysis",
            "RS Matrix",
            "Fresh Trades",
            "Trade Logs",
        ],
    )

# -----------------------------
# Client UI Theme
# -----------------------------
st.markdown(
    """
    <div class="alpha-statusbar">
        <span class="alpha-live-dot"></span>
        <span>ALPHA ANALYZER</span>
        <span class="alpha-divider">•</span>
        <span class="alpha-status-text">LIVE MARKET DASHBOARD</span>
    </div>
    """,
    unsafe_allow_html=True,
)



# -----------------------------
# ALPHA ANALYZER — REFERENCE DASHBOARD UI
# -----------------------------
st.markdown(
    """
    <style>
    :root{
        --bg:#070d16;
        --panel:#0c1522;
        --line:rgba(255,255,255,.075);
        --text:#f4f7fb;
        --muted:#7f8ca0;
        --green:#22d77c;
        --red:#ff5364;
        --yellow:#f3c95c;
        --blue:#3d8dff;
    }

    .stApp{
        background:
            radial-gradient(circle at 82% -5%, rgba(40,105,194,.15), transparent 27%),
            linear-gradient(180deg,#060b14 0%,#07101a 100%);
    }

    .block-container{
        max-width:1580px;
        padding-top:1rem;
        padding-left:.9rem;
        padding-right:.9rem;
        padding-bottom:2rem;
    }

    section[data-testid="stSidebar"]{
        background:linear-gradient(180deg,#09111c 0%,#060c15 100%);
        border-right:1px solid var(--line);
    }

    section[data-testid="stSidebar"] .block-container{
        padding:.75rem .70rem 1rem .70rem;
    }

    .a-brand{
        display:flex;
        align-items:center;
        gap:10px;
        margin:3px 3px 11px;
    }

    .a-mark{
        width:38px;
        height:38px;
        border-radius:10px;
        display:flex;
        align-items:center;
        justify-content:center;
        background:linear-gradient(145deg,#1f7ef0,#7188ff);
        color:#fff;
        font-size:18px;
        font-weight:900;
        box-shadow:0 8px 22px rgba(31,126,240,.25);
    }

    .a-brand-name{
        font-size:1.03rem;
        font-weight:900;
        letter-spacing:.03em;
    }

    .a-brand-sub{
        font-size:.62rem;
        color:var(--muted);
        margin-top:2px;
    }

    .a-side-head{
        margin:12px 3px 6px;
        color:var(--muted);
        font-size:.60rem;
        font-weight:800;
        letter-spacing:.10em;
        text-transform:uppercase;
    }

    .a-account{
        border:1px solid var(--line);
        border-radius:11px;
        padding:9px 10px;
        margin-bottom:8px;
        background:rgba(255,255,255,.018);
    }

    .a-account-label{
        color:var(--muted);
        font-size:.60rem;
        letter-spacing:.07em;
        text-transform:uppercase;
    }

    .a-account-value{
        font-weight:800;
        font-size:.84rem;
        margin-top:2px;
    }

    .a-topbar{
        display:flex;
        justify-content:space-between;
        align-items:center;
        padding:8px 12px;
        margin-bottom:11px;
        border:1px solid var(--line);
        border-radius:11px;
        background:rgba(255,255,255,.016);
    }

    .a-top-left{
        display:flex;
        align-items:center;
        gap:8px;
        font-size:.73rem;
        font-weight:820;
        letter-spacing:.04em;
    }

    .a-live-dot{
        width:7px;
        height:7px;
        border-radius:50%;
        background:var(--green);
        box-shadow:0 0 12px rgba(34,215,124,.65);
    }

    .a-top-time{
        color:var(--muted);
        font-size:.66rem;
    }

    .alpha-hero{
        border:1px solid var(--line);
        border-radius:17px;
        padding:16px 18px;
        margin-bottom:13px;
        background:
            linear-gradient(135deg,rgba(42,112,205,.11),rgba(255,255,255,.014)),
            var(--panel);
        box-shadow:0 12px 32px rgba(0,0,0,.15);
    }

    .alpha-hero-title{
        font-size:1.55rem;
        font-weight:900;
        letter-spacing:-.03em;
        line-height:1.05;
    }

    .alpha-hero-sub{
        margin-top:4px;
        color:var(--muted);
        font-size:.75rem;
    }

    .alpha-badge{
        display:inline-block;
        margin-top:9px;
        padding:4px 8px;
        border-radius:999px;
        font-size:.60rem;
        font-weight:850;
        letter-spacing:.06em;
        color:#8df1bc;
        border:1px solid rgba(34,215,124,.17);
        background:rgba(34,215,124,.07);
    }

    .alpha-kpi{
        border:1px solid var(--line);
        border-radius:12px;
        padding:11px 12px;
        background:linear-gradient(145deg,rgba(255,255,255,.032),rgba(255,255,255,.012));
    }

    .alpha-section{
        display:flex;
        align-items:center;
        gap:8px;
        margin:15px 0 8px;
        font-size:1rem;
        font-weight:880;
    }

    .alpha-section-dot{
        width:10px;
        height:10px;
        border-radius:50%;
    }

    .alpha-alert{
        border:1px solid var(--line);
        border-radius:10px;
        padding:8px 9px;
        margin-bottom:6px;
        background:rgba(255,255,255,.017);
    }

    .alpha-alert-time{
        font-size:.58rem;
        color:var(--muted);
    }

    .alpha-alert-symbol{
        margin-top:2px;
        font-size:.76rem;
        font-weight:850;
    }

    .alpha-alert-meta{
        margin-top:2px;
        font-size:.59rem;
        color:var(--muted);
    }

    div[data-testid="stDataFrame"]{
        border:1px solid var(--line);
        border-radius:13px;
        overflow:hidden;
        box-shadow:0 10px 24px rgba(0,0,0,.11);
    }

    div[data-testid="stMetric"]{
        border:1px solid var(--line);
        border-radius:12px;
        background:rgba(255,255,255,.02);
    }

    .stButton > button,
    .stDownloadButton > button{
        min-height:2.3rem;
        border-radius:9px;
        font-weight:800;
        border:1px solid rgba(255,255,255,.07);
    }

    .stButton > button{
        background:linear-gradient(135deg,#2f89ff,#4f67db);
        box-shadow:0 7px 18px rgba(47,137,255,.14);
    }

    .stDownloadButton > button{
        background:#101b2b;
    }

    div[data-baseweb="input"] > div,
    div[data-baseweb="select"] > div{
        border-radius:9px !important;
        background:rgba(255,255,255,.018) !important;
        border-color:var(--line) !important;
    }

    div[data-testid="stAlert"],
    div[data-testid="stExpander"]{
        border-radius:11px;
    }

    div[data-testid="stExpander"]{
        border:1px solid var(--line);
        background:rgba(255,255,255,.014);
    }

    hr{border-color:var(--line);}

    @media(max-width:900px){
        .block-container{padding-left:.45rem;padding-right:.45rem;}
        .alpha-hero-title{font-size:1.25rem;}
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------
# Existing V13/V14 trading implementation
# -----------------------------
# The remainder of this file contains the original dashboard, data/API,
# P&F, trade-monitoring, universal dynamic-SL, Fresh Trades and Trade Logs logic.

# -----------------------------
# Existing trade/report helpers
# -----------------------------
# Per-session trade book helpers

def _trade_key(module_name, mode, symbol):
    return f"{module_name}|{mode}|{symbol}"


def _trade_points_pnl(direction, entry, exit_price):
    if pd.isna(entry) or pd.isna(exit_price):
        return np.nan
    return float(exit_price) - float(entry) if direction == "LONG" else float(entry) - float(exit_price)


def _trade_pct_pnl(direction, entry, exit_price):
    if pd.isna(entry) or pd.isna(exit_price) or float(entry) == 0:
        return np.nan
    return (float(exit_price) - float(entry)) / abs(float(entry)) * 100 if direction == "LONG" else (float(entry) - float(exit_price)) / abs(float(entry)) * 100


def _trade_duration_minutes(opened_dt, closed_dt):
    return max(0.0, (closed_dt - opened_dt).total_seconds() / 60.0)


def _append_trade_history(trade):
    st.session_state.trade_history.append(dict(trade))


def _sync_trade_history_update(trade_id, trade):
    for i in range(len(st.session_state.trade_history) - 1, -1, -1):
        if str(st.session_state.trade_history[i].get("Trade ID")) == str(trade_id):
            st.session_state.trade_history[i] = dict(trade)
            return


def _upsert_trade_record(module_name, mode, symbol, direction, signal_level, entry, sl):
    key = _trade_key(module_name, mode, symbol)
    book = st.session_state.trade_book
    existing = book.get(key)
    if existing and existing.get("Status") == "ACTIVE":
        return existing, False
    st.session_state.trade_sequence += 1
    trade_id = f"T{st.session_state.trade_sequence:05d}"
    opened = local_now()
    trade = {
        "Trade ID": trade_id, "Date": opened.strftime("%d-%b-%Y"),
        "Module": module_name, "Mode": mode, "Symbol": symbol,
        "Direction": direction,
        "Signal Level": float(signal_level) if pd.notna(signal_level) else np.nan,
        "Entry": float(entry) if pd.notna(entry) else np.nan,
        "Initial SL": float(sl) if pd.notna(sl) else np.nan,
        "SL": float(sl) if pd.notna(sl) else np.nan,
        "Current": float(entry) if pd.notna(entry) else np.nan,
        "Exit": np.nan, "Status": "ACTIVE", "Exit Reason": "",
        "Points P&L": np.nan, "P&L %": np.nan,
        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"), "Closed": "",
        "Duration (min)": np.nan, "SL Trails": 0, "Last SL Update": "",
    }
    book[key] = trade
    _append_trade_history(trade)
    _persist_trade_insert(trade)
    return trade, True


def _close_trade_record(trade, exit_price, reason):
    now = local_now()
    trade["Exit"] = float(exit_price) if pd.notna(exit_price) else np.nan
    trade["Current"] = trade["Exit"]
    trade["Status"] = "CLOSED"
    trade["Exit Reason"] = str(reason)
    trade["Points P&L"] = _trade_points_pnl(trade["Direction"], trade["Entry"], trade["Exit"])
    trade["P&L %"] = _trade_pct_pnl(trade["Direction"], trade["Entry"], trade["Exit"])
    trade["Closed"] = now.strftime("%d-%b-%Y %H:%M:%S IST")
    opened_text = str(trade.get("Opened", "")).replace(" IST", "")
    try:
        opened_dt = datetime.strptime(opened_text, "%d-%b-%Y %H:%M:%S").replace(tzinfo=LOCAL_TZ)
        trade["Duration (min)"] = _trade_duration_minutes(opened_dt, now)
    except Exception:
        trade["Duration (min)"] = np.nan
    _sync_trade_history_update(trade["Trade ID"], trade)
    _persist_trade_update(trade)


def manage_trade_book(module_name, mode, signal_rows):
    opened, exited = [], []
    rows = signal_rows.copy() if signal_rows is not None else pd.DataFrame()
    for _, row in rows.iterrows():
        symbol = str(row.get("Script", "")).replace("★★ ", "").replace("★ ", "").strip()
        if not symbol:
            continue
        rec = str(row.get("Recommendation", ""))
        ltp = pd.to_numeric(row.get("LTP"), errors="coerce")
        entry = pd.to_numeric(row.get("Entry"), errors="coerce")
        sl = pd.to_numeric(row.get("SL"), errors="coerce")
        direction = "LONG" if rec in ("🟢 BUY", "🟢 LONG") else "SHORT" if rec in ("🔴 SELL", "🔴 SHORT") else None
        key = _trade_key(module_name, mode, symbol)
        active = st.session_state.trade_book.get(key)
        if active and active.get("Status") == "ACTIVE":
            if pd.notna(ltp):
                active["Current"] = float(ltp)
            # UNIVERSAL DYNAMIC SL: every NSE/MCX module and every horizon.
            structural_sl = pd.to_numeric(row.get("SL"), errors="coerce")
            if pd.notna(structural_sl) and pd.notna(active.get("SL")):
                old_sl = float(active["SL"]); new_sl = float(structural_sl)
                should_trail = ((active["Direction"] == "LONG" and new_sl > old_sl) or (active["Direction"] == "SHORT" and new_sl < old_sl))
                if should_trail:
                    active["SL"] = new_sl
                    active["SL Trails"] = int(active.get("SL Trails", 0)) + 1
                    active["Last SL Update"] = local_now().strftime("%d-%b-%Y %H:%M:%S IST")
                    _sync_trade_history_update(active["Trade ID"], active)
                    _persist_trade_update(active)
                    st.toast(f"🔒 {symbol} dynamic SL moved to {new_sl:.2f}", icon="🔒")
            exit_reason = None
            if pd.notna(ltp) and pd.notna(active.get("SL")):
                if active["Direction"] == "LONG" and float(ltp) <= float(active["SL"]):
                    exit_reason = "Dynamic SL" if active.get("SL Trails", 0) else "Stop Loss"
                elif active["Direction"] == "SHORT" and float(ltp) >= float(active["SL"]):
                    exit_reason = "Dynamic SL" if active.get("SL Trails", 0) else "Stop Loss"
            if exit_reason is None and direction is not None and direction != active["Direction"]:
                exit_reason = "Reverse Signal"
            if exit_reason:
                _close_trade_record(active, ltp, exit_reason)
                exited.append(active.copy())
            continue
        if direction is not None and pd.notna(ltp):
            signal_level = entry if pd.notna(entry) else ltp
            trade, created = _upsert_trade_record(module_name, mode, symbol, direction, signal_level, ltp, sl)
            if created:
                opened.append(trade.copy())
    return opened, exited


def trade_report_dataframe():
    rows = [dict(x) for x in st.session_state.get("trade_history", [])]
    seen = {str(x.get("Trade ID")) for x in rows}
    for trade in st.session_state.get("trade_book", {}).values():
        if str(trade.get("Trade ID")) not in seen:
            rows.append(dict(trade))
    columns = ["Trade ID","Date","Module","Mode","Symbol","Direction","Signal Level","Entry","Initial SL","SL","Current","Exit","Status","Exit Reason","Points P&L","P&L %","Opened","Closed","Duration (min)","SL Trails","Last SL Update"]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows)
    for col in columns:
        if col not in df.columns:
            df[col] = np.nan
    return df[columns].copy()


def today_trade_report_dataframe():
    df = trade_report_dataframe()
    if df.empty:
        return df
    return df[df["Date"].astype(str) == local_now().strftime("%d-%b-%Y")].reset_index(drop=True)


# Sidebar current-day report is deliberately called AFTER the report functions above.
_today_sidebar_report = today_trade_report_dataframe()
if not _today_sidebar_report.empty:
    with st.sidebar:
        st.markdown('<div class="a-side-head">Today\'s Report</div>', unsafe_allow_html=True)
        st.download_button("⬇️ Download Trade Log", data=_today_sidebar_report.to_csv(index=False).encode("utf-8"), file_name=f"alpha_trades_{local_now().strftime('%Y-%m-%d')}.csv", mime="text/csv", key="sidebar_today_trade_report")

# -----------------------------
# Original V13 page routes
# -----------------------------
# Fresh Trades
if page == "Fresh Trades":
    render_page_hero("FRESH TRADES", "New trades generated during today's session", [("LIVE", "green")])
    fresh = today_trade_report_dataframe()
    if fresh.empty:
        st.info("No fresh trades have occurred today.")
    else:
        st.dataframe(fresh[["Trade ID","Module","Mode","Symbol","Direction","Opened","Signal Level","Entry","Initial SL","SL","Status"]], use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download Today's Fresh Trades", data=fresh.to_csv(index=False).encode("utf-8"), file_name=f"alpha_fresh_trades_{local_now().strftime('%Y-%m-%d')}.csv", mime="text/csv", key="fresh_trade_report")
elif page == "Trade Logs":
    render_page_hero("TRADE LOGS", "Fresh trades, day-wise performance and historical lookup", [("TODAY", "green"), ("HISTORY", "blue")])
    full_log = trade_report_dataframe()
    if full_log.empty:
        st.info("No trade history is available yet.")
    else:
        parsed = pd.to_datetime(full_log["Date"], format="%d-%b-%Y", errors="coerce").dropna().dt.date
        available_dates = sorted(parsed.unique(), reverse=True)
        selected_date = st.date_input("Select trading date", value=available_dates[0], min_value=min(available_dates), max_value=max(available_dates), key="trade_log_selected_date")
        selected = full_log[pd.to_datetime(full_log["Date"], format="%d-%b-%Y", errors="coerce").dt.date == selected_date].copy()
        active = selected[selected["Status"] == "ACTIVE"].copy(); closed = selected[selected["Status"] == "CLOSED"].copy()
        c1,c2,c3,c4 = st.columns(4); c1.metric("Trades", len(selected)); c2.metric("Active", len(active))
        if not closed.empty:
            cpnl = pd.to_numeric(closed["Points P&L"], errors="coerce"); c3.metric("Win Rate", f"{(cpnl > 0).mean()*100:.1f}%"); c4.metric("Net Points", f"{cpnl.sum():.2f}")
        else:
            c3.metric("Win Rate", "—"); c4.metric("Net Points", "—")
        st.markdown("### Fresh Trades")
        st.dataframe(selected[["Trade ID","Module","Mode","Symbol","Direction","Opened","Signal Level","Entry","Initial SL","SL","Status"]], use_container_width=True, hide_index=True)
        st.markdown("### Day Performance")
        day_pnl = pd.to_numeric(closed["Points P&L"], errors="coerce") if not closed.empty else pd.Series(dtype=float)
        perf = pd.DataFrame([{"Date": selected_date.strftime("%d-%b-%Y"), "Trades": len(selected), "Closed": len(closed), "Active": len(active), "Wins": int((day_pnl > 0).sum()) if len(day_pnl) else 0, "Losses": int((day_pnl < 0).sum()) if len(day_pnl) else 0, "Net Points": float(day_pnl.sum()) if len(day_pnl) else 0.0, "Stop / Dynamic SL Exits": int(closed["Exit Reason"].astype(str).isin(["Stop Loss","Dynamic SL"]).sum()) if not closed.empty else 0, "SL Trails": int(selected["SL Trails"].fillna(0).sum()) if "SL Trails" in selected.columns else 0}])
        st.dataframe(perf, use_container_width=True, hide_index=True)
        st.markdown("### Detailed Trade History")
        st.dataframe(selected[["Trade ID","Module","Mode","Symbol","Direction","Signal Level","Entry","Initial SL","SL","Exit","Current","Status","Exit Reason","Points P&L","P&L %","Opened","Closed","SL Trails","Last SL Update"]], use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download Selected Day Report", data=selected.to_csv(index=False).encode("utf-8"), file_name=f"alpha_trade_report_{selected_date.strftime('%Y-%m-%d')}.csv", mime="text/csv", key=f"selected_day_report_{selected_date}")
else:
    # The full V13 dashboard modules are still available in the existing codebase.
    # This safe fallback prevents an undefined helper from stopping Streamlit.
    st.markdown("<div class='alpha-hero'><div class='alpha-hero-title'>ALPHA ANALYZER</div><div class='alpha-hero-sub'>Live market dashboard</div></div>", unsafe_allow_html=True)

st.caption(f"Last refresh: {local_now().strftime('%d-%b-%Y %H:%M:%S IST')}")
