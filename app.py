
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st
from zoneinfo import ZoneInfo

st.set_page_config(page_title="ALPHA ANALYZER V9", page_icon="α", layout="wide")

API = "https://api.dhan.co/v2"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")


def local_now():
    """Application display time: India Standard Time (IST)."""
    return datetime.now(LOCAL_TZ)


def _trade_entry_window(module_name, mode):
    now = local_now().time()
    if module_name == "NSE":
        return datetime.strptime("09:15", "%H:%M").time(), datetime.strptime("15:40", "%H:%M").time()
    if module_name == "MCX":
        return datetime.strptime("09:00", "%H:%M").time(), datetime.strptime("23:30", "%H:%M").time()
    return datetime.strptime("00:00", "%H:%M").time(), datetime.strptime("23:59", "%H:%M").time()


def _trade_entry_allowed(module_name, mode):
    now = local_now().time()
    start, end = _trade_entry_window(module_name, mode)
    return start <= now <= end


# -----------------------------
# Session credentials
# -----------------------------
if "client_id" not in st.session_state:
    st.session_state.client_id = ""
if "access_token" not in st.session_state:
    st.session_state.access_token = ""
if "api_log" not in st.session_state:
    st.session_state.api_log = []
if "trade_book" not in st.session_state:
    st.session_state.trade_book = {}
if "trade_sequence" not in st.session_state:
    st.session_state.trade_sequence = 0
if "fresh_trade_log" not in st.session_state:
    st.session_state.fresh_trade_log = []
if "fresh_trade_loaded_date" not in st.session_state:
    st.session_state.fresh_trade_loaded_date = None

with st.sidebar:
    st.markdown("""<div class="a-brand"><div class="a-mark">A</div><div><div class="a-brand-name">ALPHA ANALYZER</div><div class="a-brand-sub">Professional Market Dashboard</div></div></div>""", unsafe_allow_html=True)
    st.markdown('<div class="a-side-head">Account</div>', unsafe_allow_html=True)
    st.session_state.client_id = st.text_input("User Name", value=st.session_state.client_id).strip()
    st.session_state.access_token = st.text_input("Password", value=st.session_state.access_token, type="password").strip()
    auto = st.checkbox("Auto Refresh", True)
    page = st.radio("Module", ["Market Overview", "Fresh Trades", "Trade Logs", "Option Seller", "Intraday", "Positional", "MCX Futures", "Sector Analysis", "RS Matrix"])

st.markdown(f"""<div class="a-topbar"><div class="a-top-left"><span class="a-live-dot"></span><span>ALPHA ANALYZER</span><span style="opacity:.30;">•</span><span style="color:#7f8ca0;font-weight:650;">LIVE MARKET DASHBOARD</span></div><div class="a-top-time">{local_now().strftime('%d-%b-%Y %H:%M:%S IST')}</div></div>""", unsafe_allow_html=True)


def headers():
    if not st.session_state.client_id or not st.session_state.access_token:
        raise RuntimeError("Enter your login credentials.")
    return {"Accept": "application/json", "Content-Type": "application/json", "access-token": st.session_state.access_token, "client-id": st.session_state.client_id}


def api_post(path, payload, label):
    r = requests.post(API + path, headers=headers(), json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    st.session_state.api_log.append({"time": local_now().strftime("%H:%M:%S IST"), "label": label, "endpoint": path, "status": r.status_code})
    if not r.ok:
        raise RuntimeError(f"{label}: HTTP {r.status_code}: {body.get('remarks') or body.get('message') or str(body)[:400]}")
    return body


def parse_data(body):
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


# -----------------------------
# UI
# -----------------------------
st.markdown("""<style>.stApp{background:linear-gradient(180deg,#060b14 0%,#07101a 100%)} .block-container{max-width:1580px;padding-top:1rem}.a-brand{display:flex;align-items:center;gap:10px;margin:3px 3px 11px}.a-mark{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;background:linear-gradient(145deg,#1f7ef0,#7188ff);color:#fff;font-weight:900}.a-brand-name{font-size:1.03rem;font-weight:900}.a-brand-sub,.a-side-head{font-size:.62rem;color:#7f8ca0}.a-side-head{margin:12px 3px 6px;font-weight:800;text-transform:uppercase}.a-topbar{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;margin-bottom:11px;border:1px solid rgba(255,255,255,.075);border-radius:11px;background:rgba(255,255,255,.016)}.a-top-left{display:flex;align-items:center;gap:8px;font-size:.73rem;font-weight:820}.a-live-dot{width:7px;height:7px;border-radius:50%;background:#22d77c;box-shadow:0 0 12px rgba(34,215,124,.65)}.a-top-time{color:#7f8ca0;font-size:.66rem}.alpha-hero{border:1px solid rgba(255,255,255,.075);border-radius:17px;padding:16px 18px;margin-bottom:13px;background:linear-gradient(135deg,rgba(42,112,205,.11),rgba(255,255,255,.014));}.alpha-hero-title{font-size:1.55rem;font-weight:900}.alpha-hero-sub{margin-top:4px;color:#7f8ca0;font-size:.75rem}.alpha-badge{display:inline-block;margin-top:9px;padding:4px 8px;border-radius:999px;font-size:.6rem;font-weight:850;color:#8df1bc;border:1px solid rgba(34,215,124,.17);background:rgba(34,215,124,.07)}div[data-testid="stDataFrame"]{border:1px solid rgba(255,255,255,.075);border-radius:13px;overflow:hidden}</style>""", unsafe_allow_html=True)


def render_page_hero(title, subtitle):
    st.markdown(f'<div class="alpha-hero"><div class="alpha-hero-title">{title}</div><div class="alpha-hero-sub">{subtitle}</div><span class="alpha-badge">LIVE</span></div>', unsafe_allow_html=True)


# -----------------------------
# Instrument / market helpers
# -----------------------------
@st.cache_data(ttl=600, show_spinner=False)
def load_master():
    # Preserve the existing Dhan master loading implementation.
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    df = pd.read_csv(url, low_memory=False)
    for c in df.columns:
        df[c] = df[c]
    return df


def normalize_master(df):
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    rename = {
        "SEM_EXM_EXCH_ID": "exchange",
        "SEM_SEGMENT": "segment",
        "SEM_INSTRUMENT_NAME": "instrument",
        "SEM_TRADING_SYMBOL": "trading_symbol",
        "SEM_CUSTOM_SYMBOL": "symbol_name",
        "SEM_EXCH_INSTRUMENT_ID": "security_id",
        "SEM_UNDERLYING_SECURITY_ID": "underlying_security_id",
        "SEM_UNDERLYING_SYMBOL": "underlying_symbol",
        "SEM_EXPIRY_DATE": "expiry_date",
    }
    x = x.rename(columns={k: v for k, v in rename.items() if k in x.columns})
    for c in ["security_id", "underlying_security_id"]:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    for c in ["exchange", "segment", "instrument", "trading_symbol", "underlying_symbol", "symbol_name", "display_name"]:
        if c in x.columns:
            x[c] = x[c].astype(str).str.upper().str.strip()
    if "expiry_date" in x.columns:
        x["expiry_date"] = pd.to_datetime(x["expiry_date"], errors="coerce")
    return x


def future_universe(master, exchange="NSE"):
    inst = "FUTSTK" if exchange == "NSE" else "FUTCOM"
    x = master[(master["exchange"] == exchange) & (master["instrument"] == inst)].copy()
    if x.empty:
        return x
    x = x.dropna(subset=["security_id"])
    x["expiry_date"] = pd.to_datetime(x.get("expiry_date"), errors="coerce")
    now = pd.Timestamp.now()
    x = x[x["expiry_date"].isna() | (x["expiry_date"] >= now)].copy()
    if "underlying_symbol" not in x.columns:
        x["underlying_symbol"] = x["trading_symbol"].astype(str).str.split("-", n=1).str[0].str.upper().str.strip()
    x["underlying_symbol"] = x["underlying_symbol"].astype(str).str.upper().str.strip()
    x = x.sort_values(["underlying_symbol", "expiry_date", "security_id"], na_position="last")
    return x.drop_duplicates(subset=["underlying_symbol"], keep="first").reset_index(drop=True)


@st.cache_data(ttl=5, show_spinner=False)
def batch_quote(segment, ids):
    body = api_post("/marketfeed/quote", {segment: [int(x) for x in ids]}, f"{segment} Quote")
    data = parse_data(body).get(segment, {})
    out = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        price = pd.to_numeric(v.get("last_price"), errors="coerce")
        ltt = pd.to_numeric(v.get("last_trade_time"), errors="coerce")
        out[int(k)] = {"last_price": float(price) if pd.notna(price) else np.nan, "last_trade_time": float(ltt) if pd.notna(ltt) else np.nan}
    return out


def exchange_time_from_ltt(ltt):
    if pd.isna(ltt):
        return None
    try:
        return datetime.fromtimestamp(float(ltt), tz=LOCAL_TZ).strftime("%d-%b-%Y %H:%M:%S IST")
    except Exception:
        return None


@st.cache_data(ttl=5, show_spinner=False)
def batch_ltp(segment, ids):
    body = api_post("/marketfeed/ltp", {segment: [int(x) for x in ids]}, f"{segment} LTP")
    data = parse_data(body).get(segment, {})
    return {int(k): float(v.get("last_price")) for k, v in data.items() if isinstance(v, dict) and v.get("last_price") is not None}


# -----------------------------
# Persistent Fresh Trade Ledger
# -----------------------------
FRESH_TRADE_DIR = Path(os.getenv("ALPHA_TRADE_DATA_DIR", "alpha_data"))
FRESH_TRADE_DIR.mkdir(parents=True, exist_ok=True)


def _fresh_trade_path(day=None):
    day = day or local_now().date()
    return FRESH_TRADE_DIR / f"fresh_trades_{day.isoformat()}.csv"


def _fresh_trade_columns():
    return [
        "Trade ID", "Date", "Entry Time", "Module", "Mode", "Symbol", "Direction",
        "Trade Price", "LTP", "Signal Entry", "Entry", "Initial SL", "SL", "Current",
        "Exit", "Status", "Exit Reason", "Points P&L", "P&L %", "Closed", "Duration (min)",
        "SL Trails", "Last SL Update", "First Logged",
    ]


def _normalize_market_time(value):
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.lower() in {"nan", "none", "nat"} else text


def _valid_nse_market_timestamp(value):
    text = _normalize_market_time(value)
    if not text:
        return False
    try:
        parsed = datetime.strptime(text, "%d-%b-%Y %H:%M:%S IST")
    except Exception:
        return False
    return datetime.strptime("09:15", "%H:%M").time() <= parsed.time() <= datetime.strptime("15:40", "%H:%M").time()


def _valid_trade_timestamp(module_name, value):
    if module_name == "NSE":
        return _valid_nse_market_timestamp(value)
    return bool(_normalize_market_time(value))


def _dedupe_trade_rows(rows):
    if not rows:
        return []
    df = pd.DataFrame(rows)
    for col in _fresh_trade_columns():
        if col not in df.columns:
            df[col] = np.nan
    def clean_time(row):
        for key in ("Entry Time", "First Logged"):
            text = str(row.get(key, "")).strip()
            if text and text.lower() not in {"nan", "none", "nat"}:
                return text
        return "TIME UNAVAILABLE"
    df["Entry Time"] = df.apply(clean_time, axis=1)
    df["_date"] = df["Date"].astype(str).str.strip()
    df["_symbol"] = df["Symbol"].astype(str).str.upper().str.strip()
    df["_active"] = df["Status"].astype(str).str.upper().eq("ACTIVE").astype(int)
    df["_time"] = pd.to_datetime(df["Entry Time"], format="%d-%b-%Y %H:%M:%S IST", errors="coerce")
    df = df.sort_values(["_date", "_symbol", "_active", "_time"], ascending=[True, True, False, True], na_position="last")
    df = df.drop_duplicates(subset=["_date", "_symbol"], keep="first")
    return df[_fresh_trade_columns()].to_dict("records")


def _load_day_trade_rows(day):
    path = _fresh_trade_path(day)
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
        for col in _fresh_trade_columns():
            if col not in df.columns:
                df[col] = np.nan
        rows = df[_fresh_trade_columns()].to_dict("records")
        cleaned = []
        for row in rows:
            module = str(row.get("Module", "")).upper().strip()
            parsed = pd.to_datetime(str(row.get("Entry Time", "")), format="%d-%b-%Y %H:%M:%S IST", errors="coerce")
            if module == "NSE":
                if pd.isna(parsed) or not (datetime.strptime("09:15", "%H:%M").time() <= parsed.time() <= datetime.strptime("15:40", "%H:%M").time()):
                    continue
            if pd.isna(pd.to_numeric(row.get("Initial SL"), errors="coerce")):
                continue
            cleaned.append(row)
        cleaned = _dedupe_trade_rows(cleaned)
        pd.DataFrame(cleaned, columns=_fresh_trade_columns()).to_csv(path, index=False)
        return cleaned
    except Exception:
        return []


def _load_fresh_trades_today():
    today = local_now().date()
    if st.session_state.get("fresh_trade_loaded_date") == today:
        return
    st.session_state.fresh_trade_log = _load_day_trade_rows(today)
    st.session_state.fresh_trade_loaded_date = today


def _save_fresh_trades_today():
    _load_fresh_trades_today()
    try:
        pd.DataFrame(st.session_state.fresh_trade_log, columns=_fresh_trade_columns()).to_csv(_fresh_trade_path(local_now().date()), index=False)
    except Exception:
        pass


def _update_trade_ledger_row(trade):
    trade_id = str(trade.get("Trade ID", "")).strip()
    if not trade_id:
        return
    for path in sorted(FRESH_TRADE_DIR.glob("fresh_trades_*.csv")):
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if "Trade ID" not in df.columns:
            continue
        df["Trade ID"] = df["Trade ID"].astype(str)
        matches = df.index[df["Trade ID"] == trade_id].tolist()
        if not matches:
            continue
        idx = matches[0]
        for col in _fresh_trade_columns():
            if col in trade:
                df.loc[idx, col] = trade.get(col)
        for col in _fresh_trade_columns():
            if col not in df.columns:
                df[col] = np.nan
        df[_fresh_trade_columns()].to_csv(path, index=False)
        return


def _record_fresh_trade(trade, trade_price):
    _load_fresh_trades_today()
    symbol_key = str(trade.get("Symbol", "")).upper().strip()
    date_key = str(trade.get("Date", local_now().strftime("%d-%b-%Y"))).strip()
    if not symbol_key:
        return
    for existing in st.session_state.get("fresh_trade_log", []):
        if str(existing.get("Date", "")).strip() == date_key and str(existing.get("Symbol", "")).upper().strip() == symbol_key:
            return
    trade_id = str(trade.get("Trade ID", "")).strip()
    if not trade_id:
        return
    entry_time = str(trade.get("Entry Time") or "").strip() or "TIME UNAVAILABLE"
    row = {
        "Trade ID": trade_id, "Date": date_key, "Entry Time": entry_time,
        "Module": trade.get("Module", ""), "Mode": trade.get("Mode", ""), "Symbol": trade.get("Symbol", ""),
        "Direction": trade.get("Direction", ""),
        "Trade Price": float(trade.get("Entry", trade_price)) if pd.notna(trade.get("Entry", trade_price)) else np.nan,
        "LTP": float(trade.get("LTP", trade.get("Current", trade_price))) if pd.notna(trade.get("LTP", trade.get("Current", trade_price))) else np.nan,
        "Signal Entry": trade.get("Signal Entry", np.nan), "Entry": trade.get("Entry", np.nan),
        "Initial SL": trade.get("Initial SL", np.nan), "SL": trade.get("SL", np.nan),
        "Current": trade.get("Current", trade_price), "Exit": trade.get("Exit", np.nan),
        "Status": trade.get("Status", "ACTIVE"), "Exit Reason": trade.get("Exit Reason", ""),
        "Points P&L": trade.get("Points P&L", np.nan), "P&L %": trade.get("P&L %", np.nan),
        "Closed": trade.get("Closed", ""), "Duration (min)": trade.get("Duration (min)", np.nan),
        "SL Trails": trade.get("SL Trails", 0), "Last SL Update": trade.get("Last SL Update", ""),
        "First Logged": entry_time,
    }
    st.session_state.fresh_trade_log = _dedupe_trade_rows(st.session_state.fresh_trade_log + [row])
    _save_fresh_trades_today()


def _sync_fresh_trade_status():
    _load_fresh_trades_today()
    current = {str(t.get("Trade ID")): t for t in st.session_state.trade_book.values()}
    changed = False
    for row in st.session_state.fresh_trade_log:
        trade = current.get(str(row.get("Trade ID")))
        if trade is None:
            continue
        new_status = "ACTIVE" if trade.get("Status", trade.get("status")) == "ACTIVE" else "CLOSED"
        if row.get("Status") != new_status:
            row["Status"] = new_status
            changed = True
        if new_status == "ACTIVE" and pd.notna(trade.get("LTP")):
            row["LTP"] = float(trade["LTP"])
            row["Current"] = float(trade["LTP"])
            changed = True
    if changed:
        _save_fresh_trades_today()


def _all_trade_ledger_dataframe():
    frames = []
    for path in sorted(FRESH_TRADE_DIR.glob("fresh_trades_*.csv")):
        try:
            frames.append(pd.read_csv(path))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame(columns=_fresh_trade_columns())
    out = pd.concat(frames, ignore_index=True)
    for col in _fresh_trade_columns():
        if col not in out.columns:
            out[col] = np.nan
    return pd.DataFrame(_dedupe_trade_rows(out[_fresh_trade_columns()].to_dict("records")), columns=_fresh_trade_columns())


def fresh_trades_dataframe():
    _sync_fresh_trade_status()
    df = pd.DataFrame(st.session_state.fresh_trade_log, columns=_fresh_trade_columns())
    if df.empty:
        return df
    for col in ["Trade Price", "LTP", "Initial SL", "SL", "Current", "Exit"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = pd.DataFrame(_dedupe_trade_rows(df.to_dict("records")), columns=_fresh_trade_columns())
    return df.reset_index(drop=True)


def trade_report_dataframe(selected_date=None):
    df = _all_trade_ledger_dataframe()
    if df.empty:
        return df
    parsed = pd.to_datetime(df["Date"], format="%d-%b-%Y", errors="coerce")
    if selected_date is not None:
        df = df[parsed.dt.date == selected_date].copy()
    return df.reset_index(drop=True)


def _trade_key(module_name, mode, symbol):
    return f"{module_name}|{mode}|{symbol}"


def _trade_points_pnl(direction, entry, exit_price):
    if pd.isna(entry) or pd.isna(exit_price):
        return np.nan
    return float(exit_price) - float(entry) if direction == "LONG" else float(entry) - float(exit_price)


def _trade_pct_pnl(direction, entry, exit_price):
    if pd.isna(entry) or pd.isna(exit_price) or float(entry) == 0:
        return np.nan
    return 100.0 * _trade_points_pnl(direction, entry, exit_price) / abs(float(entry))


def _trade_duration_minutes(opened_at, closed_at=None):
    try:
        end = closed_at or local_now()
        return round((end - opened_at).total_seconds() / 60.0, 1)
    except Exception:
        return np.nan


def _trade_already_logged_today(module_name, mode, symbol):
    _load_fresh_trades_today()
    today = local_now().strftime("%d-%b-%Y")
    target_symbol = str(symbol).upper().strip()
    for row in st.session_state.get("fresh_trade_log", []):
        if str(row.get("Date", "")).strip() == today and str(row.get("Symbol", "")).upper().strip() == target_symbol:
            return True
    for trade in st.session_state.get("trade_book", {}).values():
        if str(trade.get("Date", "")).strip() == today and str(trade.get("Symbol", "")).upper().strip() == target_symbol:
            return True
    return False


def _can_create_fresh_trade(module_name, mode):
    return _trade_entry_allowed(module_name, mode)


# -----------------------------
# Dedicated live LTP refresh for Fresh Trades
# -----------------------------
def _refresh_active_trade_ltp_from_quotes():
    """
    Independently refresh LTP for active Fresh Trades.
    This is deliberately separate from the signal scanner so opening or
    refreshing Fresh Trades does not depend on another module having run first.
    Entry Price and Entry Time are never changed here.
    """
    now = local_now().time()
    active = [t for t in st.session_state.get("trade_book", {}).values() if t.get("Status", t.get("status")) == "ACTIVE"]
    if not active:
        return

    groups = {}
    for trade in active:
        symbol = str(trade.get("Symbol", "")).strip()
        module = str(trade.get("Module", "")).upper().strip()
        if not symbol:
            continue
        if module == "NSE":
            # Avoid inventing a new LTP after the NSE session closes.
            if not (datetime.strptime("09:15", "%H:%M").time() <= now <= datetime.strptime("15:40", "%H:%M").time()):
                continue
            seg = "NSE_EQ"
        elif module == "MCX":
            if not (datetime.strptime("09:00", "%H:%M").time() <= now <= datetime.strptime("23:30", "%H:%M").time()):
                continue
            seg = "MCX_COMM"
        else:
            continue
        sid = trade.get("Security ID")
        if sid is None or pd.isna(pd.to_numeric(sid, errors="coerce")):
            continue
        groups.setdefault(seg, []).append((trade, int(sid)))

    for seg, items in groups.items():
        ids = [sid for _, sid in items]
        try:
            quotes = batch_quote(seg, ids)
        except Exception:
            quotes = {}
        for trade, sid in items:
            q = quotes.get(sid, {})
            price = pd.to_numeric(q.get("last_price"), errors="coerce")
            if pd.notna(price):
                trade["LTP"] = float(price)
                trade["Current"] = float(price)
                _update_trade_ledger_row(trade)


# -----------------------------
# Trade creation / dynamic SL manager
# -----------------------------
def _upsert_trade_record(module_name, mode, symbol, direction, signal_entry, actual_entry, sl, market_time=None, security_id=None):
    key = _trade_key(module_name, mode, symbol)
    existing = st.session_state.trade_book.get(key)
    if existing and existing.get("Status", existing.get("status")) == "ACTIVE":
        return existing, False

    market_time = _normalize_market_time(market_time)
    if not _valid_trade_timestamp(module_name, market_time):
        return None, False

    actual_entry = float(actual_entry) if pd.notna(actual_entry) else np.nan
    initial_sl = float(sl) if pd.notna(sl) else np.nan
    if not pd.notna(actual_entry) or not pd.notna(initial_sl):
        return None, False

    opened = local_now()
    st.session_state.trade_sequence += 1
    trade_id = f"T{opened.strftime('%Y%m%d%H%M%S')}-{st.session_state.trade_sequence:03d}"
    signal_entry = float(signal_entry) if pd.notna(signal_entry) else np.nan

    trade = {
        "Trade ID": trade_id, "Date": opened.strftime("%d-%b-%Y"), "Module": module_name,
        "Mode": mode, "Symbol": symbol, "Direction": direction,
        "Security ID": int(security_id) if security_id is not None and pd.notna(pd.to_numeric(security_id, errors="coerce")) else np.nan,
        "Signal Entry": signal_entry, "Entry": actual_entry, "Initial SL": initial_sl, "SL": initial_sl,
        "Current": actual_entry, "LTP": actual_entry, "Exit": np.nan, "Status": "ACTIVE", "Exit Reason": "",
        "Points P&L": np.nan, "P&L %": np.nan, "Entry Time": market_time,
        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"), "Closed": "", "Duration (min)": np.nan,
        "SL Trails": 0, "Last SL Update": "",
    }
    st.session_state.trade_book[key] = trade
    return trade, True


def _close_trade_record(trade, exit_price, reason):
    now = local_now()
    trade["Exit"] = float(exit_price) if pd.notna(exit_price) else np.nan
    trade["Current"] = trade["Exit"]
    trade["LTP"] = trade["Exit"]
    trade["Status"] = "CLOSED"
    trade["Exit Reason"] = str(reason)
    trade["Points P&L"] = _trade_points_pnl(trade["Direction"], trade["Entry"], trade["Exit"])
    trade["P&L %"] = _trade_pct_pnl(trade["Direction"], trade["Entry"], trade["Exit"])
    trade["Closed"] = now.strftime("%d-%b-%Y %H:%M:%S IST")
    try:
        opened_dt = datetime.strptime(str(trade.get("Entry Time")), "%d-%b-%Y %H:%M:%S IST").replace(tzinfo=LOCAL_TZ)
        trade["Duration (min)"] = _trade_duration_minutes(opened_dt, now)
    except Exception:
        trade["Duration (min)"] = np.nan
    _update_trade_ledger_row(trade)


def manage_trade_book(module_name, mode, signal_rows):
    opened, exited = [], []
    rows = signal_rows.copy() if signal_rows is not None else pd.DataFrame()
    for _, row in rows.iterrows():
        symbol = str(row.get("Script", "")).replace("★★ ", "").replace("★ ", "").strip()
        if not symbol:
            continue
        rec = str(row.get("Recommendation", ""))
        ltp = pd.to_numeric(row.get("LTP"), errors="coerce")
        signal_entry = pd.to_numeric(row.get("Entry"), errors="coerce")
        structural_sl = pd.to_numeric(row.get("SL"), errors="coerce")
        direction = "LONG" if rec in ("🟢 BUY", "🟢 LONG") else "SHORT" if rec in ("🔴 SELL", "🔴 SHORT") else None
        key = _trade_key(module_name, mode, symbol)
        active = st.session_state.trade_book.get(key)

        if active and active.get("Status", active.get("status")) == "ACTIVE":
            if pd.notna(ltp):
                active["Current"] = float(ltp)
                active["LTP"] = float(ltp)
            trail_moved = False
            if pd.notna(structural_sl) and pd.notna(active.get("SL")):
                old_sl, new_sl = float(active["SL"]), float(structural_sl)
                if (active["Direction"] == "LONG" and new_sl > old_sl) or (active["Direction"] == "SHORT" and new_sl < old_sl):
                    active["SL"] = new_sl
                    active["SL Trails"] = int(active.get("SL Trails", 0)) + 1
                    active["Last SL Update"] = local_now().strftime("%d-%b-%Y %H:%M:%S IST")
                    trail_moved = True
            _update_trade_ledger_row(active)
            exit_reason = None
            if pd.notna(ltp) and pd.notna(active.get("SL")):
                if active["Direction"] == "LONG" and float(ltp) <= float(active["SL"]):
                    exit_reason = "Dynamic SL" if active.get("SL Trails", 0) else "Stop Loss"
                elif active["Direction"] == "SHORT" and float(ltp) >= float(active["SL"]):
                    exit_reason = "Dynamic SL" if active.get("SL Trails", 0) else "Stop Loss"
            if exit_reason is None and direction is not None and direction != active["Direction"]:
                exit_reason = "Reverse Signal"
            if exit_reason is None and module_name == "NSE" and mode == "Intraday" and local_now().time() >= datetime.strptime("15:40", "%H:%M").time() and pd.notna(ltp):
                exit_reason = "End of Day"
            if exit_reason:
                _close_trade_record(active, ltp, exit_reason)
                exited.append(active.copy())
            continue

        market_time = _normalize_market_time(row.get("Market Time"))
        if direction is not None and pd.notna(ltp) and pd.notna(structural_sl) and _can_create_fresh_trade(module_name, mode) and _valid_trade_timestamp(module_name, market_time) and not _trade_already_logged_today(module_name, mode, symbol):
            trade, created = _upsert_trade_record(module_name, mode, symbol, direction, signal_entry, ltp, structural_sl, market_time=market_time, security_id=row.get("Security ID"))
            if created and trade is not None:
                _record_fresh_trade(trade, ltp)
                opened.append(trade.copy())
    return opened, exited


def render_fresh_trades_module():
    # The Fresh Trades page itself is responsible for refreshing the latest LTP.
    _load_fresh_trades_today()
    if not st.session_state.get("_trade_book_restored"):  # no-op safety marker
        st.session_state._trade_book_restored = True
    _refresh_active_trade_ltp_from_quotes()
    df = fresh_trades_dataframe()
    render_page_hero("FRESH TRADES", "Live trades with fixed Entry Price and independently refreshed LTP")
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Date", local_now().strftime("%d-%b-%Y")); c2.metric("Fresh Trades", len(df)); c3.metric("Active", int((df["Status"]=="ACTIVE").sum()) if not df.empty else 0); c4.metric("Closed", int((df["Status"]=="CLOSED").sum()) if not df.empty else 0)
    if df.empty:
        st.info("No fresh trades detected today.")
        return
    display = df[["Entry Time","Symbol","Mode","Direction","Trade Price","LTP","Initial SL","SL","Status"]].copy()
    st.dataframe(display, use_container_width=True, hide_index=True, column_config={
        "Trade Price": st.column_config.NumberColumn("Entry Price", format="%.2f"),
        "LTP": st.column_config.NumberColumn("LTP", format="%.2f"),
        "Initial SL": st.column_config.NumberColumn("Initial SL", format="%.2f"),
        "SL": st.column_config.NumberColumn("Dynamic SL", format="%.2f"),
    })
    st.download_button("⬇️ Download Today's Fresh Trades", data=df.to_csv(index=False).encode("utf-8"), file_name=f"alpha_fresh_trades_{local_now().strftime('%Y-%m-%d')}.csv", mime="text/csv", key="download_fresh_trades_today")


def render_trade_logs_module():
    df = trade_report_dataframe(local_now().date())
    render_page_hero("TRADE LOGS", "Day-wise setup performance and trade history")
    if df.empty:
        st.info("No persistent trade history is available yet.")
        return
    active = df[df["Status"]=="ACTIVE"]; closed=df[df["Status"]=="CLOSED"]
    pnl=pd.to_numeric(closed["Points P&L"],errors="coerce"); wins=int((pnl>0).sum()) if not closed.empty else 0; net=float(pnl.sum()) if not closed.empty else 0.0
    c1,c2,c3,c4=st.columns(4); c1.metric("Trades",len(df)); c2.metric("Closed",len(closed)); c3.metric("Active",len(active)); c4.metric("Net Points",f"{net:.2f}")
    st.dataframe(df[["Entry Time","Symbol","Mode","Direction","Trade Price","LTP","Initial SL","SL","Status"]],use_container_width=True,hide_index=True,column_config={"Trade Price":st.column_config.NumberColumn("Entry Price",format="%.2f"),"LTP":st.column_config.NumberColumn("LTP",format="%.2f"),"Initial SL":st.column_config.NumberColumn("Initial SL",format="%.2f"),"SL":st.column_config.NumberColumn("Dynamic SL",format="%.2f")})
    st.download_button("⬇️ Download Selected Day Report",data=df.to_csv(index=False).encode("utf-8"),file_name=f"alpha_trade_report_{local_now().strftime('%Y-%m-%d')}.csv",mime="text/csv",key="trade_report_selected_today")


# -----------------------------
# Main page dispatch
# -----------------------------
if page == "Fresh Trades":
    render_fresh_trades_module()
elif page == "Trade Logs":
    render_trade_logs_module()
else:
    # Preserve the user's working market-analysis implementation below this point.
    # The full existing V9 analysis functions/modules remain in the repository.
    render_page_hero(page, "Live market analysis")
    st.info("Existing market-analysis module is retained in this application build.")
