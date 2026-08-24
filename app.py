import sqlite3
from pathlib import Path
from datetime import datetime, time as _dt_time

import streamlit as st


def render_page_hero(title, subtitle, badges=None):
    """Shared page header used by the legacy dashboard."""
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


# Earlier revisions stored the trade timestamp correctly but used a lowercase
# Status check while the trade record uses the capitalized "Status" key. That
# caused every Streamlit rerun to create another copy of the same active trade,
# making the displayed Entry Time jump to the refresh time.
def _patch_legacy_source(source: str) -> str:
    # 1) IMPORTANT: preserve the same active trade across all reruns/modules.
    source = source.replace(
        'if existing and existing.get("status") == "ACTIVE":',
        'if existing and existing.get("Status", existing.get("status")) == "ACTIVE":',
        1,
    )

    # 2) Preserve / expose the immutable Entry Time field.
    opened_line = '        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    entry_time_line = opened_line + '        "Entry Time": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    if '"Entry Time": opened.strftime' not in source:
        if opened_line not in source:
            raise RuntimeError("Trade creation timestamp anchor not found")
        source = source.replace(opened_line, entry_time_line, 1)

    report_cols_old = '''        "Opened","Closed","Duration (min)","SL Trails","Last SL Update"\n'''
    report_cols_new = '''        "Opened","Entry Time","Closed","Duration (min)","SL Trails","Last SL Update"\n'''
    if '"Entry Time","Closed","Duration (min)"' not in source:
        if report_cols_old in source:
            source = source.replace(report_cols_old, report_cols_new, 1)

    # 3) Do not create a fresh trade simply because an old positional signal is
    # still visible after market close or because the user switched modules.
    old_open = '''        # No active trade: open only on a valid signal.\n        if direction is not None and pd.notna(ltp):\n'''
    new_open = '''        # No active trade: open only on a valid signal during live market hours.\n        now_t = local_now().time()\n        trade_entry_allowed = True\n\n        if module_name == "NSE":\n            trade_entry_allowed = _dt_time(9, 15) <= now_t <= _dt_time(15, 20)\n        elif module_name == "MCX":\n            trade_entry_allowed = _dt_time(9, 0) <= now_t <= _dt_time(23, 30)\n\n        if trade_entry_allowed and direction is not None and pd.notna(ltp):\n'''
    if old_open in source:
        source = source.replace(old_open, new_open, 1)

    # 4) Report tables should display Entry Time rather than the internal field
    # name Opened. The underlying timestamp remains immutable.
    source = source.replace(
        '"Opened","Signal Level","Entry","Initial SL","SL","Status"',
        '"Entry Time","Signal Level","Entry","Initial SL","SL","Status"',
    )
    source = source.replace(
        '"Opened","Closed","SL Trails"',
        '"Entry Time","Closed","SL Trails"',
    )
    source = source.replace(
        '"Opened","Closed","Duration (min)","SL Trails","Last SL Update"',
        '"Entry Time","Closed","Duration (min)","SL Trails","Last SL Update"',
    )

    # 5) Make old SQLite rows expose Entry Time too, without changing Opened.
    df_anchor = '''    df = pd.DataFrame(rows)\n    for col in columns:\n'''
    df_patch = '''    df = pd.DataFrame(rows)\n    if "Entry Time" not in df.columns:\n        df["Entry Time"] = df["Opened"]\n    for col in columns:\n'''
    if 'df["Entry Time"] = df["Opened"]' not in source and df_anchor in source:
        source = source.replace(df_anchor, df_patch, 1)

    return source


def _is_invalid_entry_time(module, opened):
    try:
        dt = datetime.strptime(str(opened).replace(" IST", ""), "%d-%b-%Y %H:%M:%S")
    except Exception:
        return False
    module = str(module).upper()
    if module == "NSE":
        return not (_dt_time(9, 15) <= dt.time() <= _dt_time(15, 30))
    if module == "MCX":
        return not (_dt_time(9, 0) <= dt.time() <= _dt_time(23, 55))
    return False


def _clean_invalid_trade_history_before_app_load():
    """Remove legacy NSE/MCX after-hours records before the dashboard reloads them."""
    try:
        db = Path(__file__).resolve().parent / "alpha_trade_history.db"
        if db.exists():
            conn = sqlite3.connect(str(db), timeout=10)
            rows = conn.execute("SELECT id,module,opened FROM trades").fetchall()
            bad = [(int(row_id),) for row_id, module, opened in rows if _is_invalid_entry_time(module, opened)]
            if bad:
                conn.executemany("DELETE FROM trades WHERE id=?", bad)
                conn.commit()
            conn.close()

        # Also clear invalid in-memory active/history rows from previous reruns.
        tb = st.session_state.get("trade_book", {})
        for key, trade in list(tb.items()):
            if _is_invalid_entry_time(trade.get("Module"), trade.get("Opened")):
                del tb[key]

        hist = st.session_state.get("trade_history", [])
        st.session_state.trade_history = [
            trade for trade in hist
            if not _is_invalid_entry_time(trade.get("Module"), trade.get("Opened"))
        ]
    except Exception:
        # History cleanup must never stop the trading dashboard.
        pass


# Prevent duplicate same-day report widgets from crashing Streamlit.
_original_download_button = st.download_button
_download_button_keys_seen = set()


def _safe_download_button(*args, **kwargs):
    key = kwargs.get("key")
    if key == "sidebar_today_trade_report":
        if key in _download_button_keys_seen:
            return None
        _download_button_keys_seen.add(key)
    return _original_download_button(*args, **kwargs)


st.download_button = _safe_download_button

# Clean legacy after-hours records BEFORE legacy_app loads the trade history.
_clean_invalid_trade_history_before_app_load()

# Execute the full production dashboard with the safety patches above.
legacy = Path(__file__).resolve().parent / "legacy_app.py"
legacy_source = _patch_legacy_source(legacy.read_text(encoding="utf-8"))
exec(compile(legacy_source, str(legacy), "exec"), globals(), globals())
