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


def _patch_legacy_source(source: str) -> str:
    # Preserve the same active trade across reruns: the trade record uses
    # capitalized Status, so do not create a new trade on every refresh.
    source = source.replace(
        'if existing and existing.get("status") == "ACTIVE":',
        'if existing and existing.get("Status", existing.get("status")) == "ACTIVE":',
        1,
    )

    # Add an immutable Entry Time to each new trade. It is set once, when the
    # scanner creates the trade, and is never rewritten during refreshes.
    opened_line = '        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    entry_time_line = opened_line + '        "Entry Time": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    if '"Entry Time": opened.strftime' not in source:
        if opened_line not in source:
            raise RuntimeError("Trade creation timestamp anchor not found")
        source = source.replace(opened_line, entry_time_line, 1)

    # Expose Entry Time in the report schema.
    report_cols_old = '''        "Opened","Closed","Duration (min)","SL Trails","Last SL Update"\n'''
    report_cols_new = '''        "Opened","Entry Time","Closed","Duration (min)","SL Trails","Last SL Update"\n'''
    if '"Entry Time","Closed","Duration (min)"' not in source and report_cols_old in source:
        source = source.replace(report_cols_old, report_cols_new, 1)

    # Show Entry Time in client-facing trade tables.
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

    # Historical rows loaded from SQLite keep the immutable Opened timestamp
    # as their Entry Time if the field predates this patch.
    df_anchor = '''    df = pd.DataFrame(rows)\n    for col in columns:\n'''
    df_patch = '''    df = pd.DataFrame(rows)\n    if "Entry Time" not in df.columns:\n        df["Entry Time"] = df["Opened"]\n    for col in columns:\n'''
    if 'df["Entry Time"] = df["Opened"]' not in source and df_anchor in source:
        source = source.replace(df_anchor, df_patch, 1)

    # Only create NEW trades while the appropriate market is open. Existing
    # active trades continue to be monitored outside this block.
    old_open = '''        # No active trade: open only on a valid signal.\n        if direction is not None and pd.notna(ltp):\n'''
    new_open = '''        # No active trade: create a fresh trade only during live market hours.\n        now_t = local_now().time()\n        trade_entry_allowed = True\n\n        if module_name == "NSE":\n            trade_entry_allowed = _dt_time(9, 15) <= now_t <= _dt_time(15, 20)\n        elif module_name == "MCX":\n            trade_entry_allowed = _dt_time(9, 0) <= now_t <= _dt_time(23, 30)\n\n        if trade_entry_allowed and direction is not None and pd.notna(ltp):\n'''
    if old_open in source:
        source = source.replace(old_open, new_open, 1)

    return source


def _entry_time_dt(value):
    try:
        return datetime.strptime(
            str(value).replace(" IST", ""),
            "%d-%b-%Y %H:%M:%S",
        )
    except Exception:
        return None


def _is_invalid_entry_time(module, opened):
    dt = _entry_time_dt(opened)
    if dt is None:
        return False
    module = str(module).upper()
    if module == "NSE":
        return not (_dt_time(9, 15) <= dt.time() <= _dt_time(15, 30))
    if module == "MCX":
        return not (_dt_time(9, 0) <= dt.time() <= _dt_time(23, 55))
    return False


def _clean_invalid_trade_history_before_app_load():
    """Remove legacy fake after-hours NSE/MCX entries before loading reports."""
    try:
        db = Path(__file__).resolve().parent / "alpha_trade_history.db"
        if db.exists():
            conn = sqlite3.connect(str(db), timeout=10)
            rows = conn.execute(
                "SELECT id,module,opened FROM trades"
            ).fetchall()
            bad_ids = [
                (int(row_id),)
                for row_id, module, opened in rows
                if _is_invalid_entry_time(module, opened)
            ]
            if bad_ids:
                conn.executemany("DELETE FROM trades WHERE id=?", bad_ids)
                conn.commit()
            conn.close()

        trade_book = st.session_state.get("trade_book", {})
        for key, trade in list(trade_book.items()):
            if _is_invalid_entry_time(
                trade.get("Module"),
                trade.get("Entry Time", trade.get("Opened")),
            ):
                del trade_book[key]

        history = st.session_state.get("trade_history", [])
        st.session_state.trade_history = [
            trade
            for trade in history
            if not _is_invalid_entry_time(
                trade.get("Module"),
                trade.get("Entry Time", trade.get("Opened")),
            )
        ]
    except Exception:
        # History cleanup is defensive and must never break the dashboard.
        pass


# Prevent the duplicate sidebar report widget from crashing Streamlit.
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
_clean_invalid_trade_history_before_app_load()

legacy = Path(__file__).resolve().parent / "legacy_app.py"
legacy_source = _patch_legacy_source(
    legacy.read_text(encoding="utf-8")
)
exec(compile(legacy_source, str(legacy), "exec"), globals(), globals())
