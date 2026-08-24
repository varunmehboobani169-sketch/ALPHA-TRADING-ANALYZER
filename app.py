import sqlite3
from pathlib import Path
from datetime import time as _dt_time

import streamlit as st


def render_page_hero(title, subtitle, badges=None):
    badges = badges or []
    badge_html = "".join(
        f"<span class='alpha-badge'>{label}</span>" for label, _ in badges
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
    """Apply only small, deterministic patches to the production dashboard."""

    # Fix active-trade detection. Trade records store Status="ACTIVE".
    source = source.replace(
        'if existing and existing.get("status") == "ACTIVE":',
        'if existing and existing.get("Status", existing.get("status")) == "ACTIVE":',
        1,
    )

    # Add immutable Entry Time at the moment the trade is actually created.
    opened_line = '        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    if '"Entry Time": opened.strftime' not in source and opened_line in source:
        source = source.replace(
            opened_line,
            opened_line + '        "Entry Time": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n',
            1,
        )

    # Do NOT delete historical/overnight positional trades during refresh.
    source = source.replace(
        '_clean_invalid_trade_history_before_app_load()\n',
        '# Historical cleanup disabled; positional trades may remain overnight.\n',
        1,
    )

    # New trades are created only while the relevant live market is open.
    # Existing active trades are handled above and remain intact.
    old_open = '''        # No active trade: open only on a valid signal.\n        if direction is not None and pd.notna(ltp):\n'''
    new_open = '''        # No active trade: create only on a valid live-market signal.\n        now_t = local_now().time()\n        trade_entry_allowed = True\n\n        if module_name == "NSE":\n            trade_entry_allowed = _dt_time(9, 15) <= now_t <= _dt_time(15, 20)\n        elif module_name == "MCX":\n            trade_entry_allowed = _dt_time(9, 0) <= now_t <= _dt_time(23, 30)\n\n        if trade_entry_allowed and direction is not None and pd.notna(ltp):\n'''
    if old_open in source:
        source = source.replace(old_open, new_open, 1)

    # Initialize the exit reason before the stop/reversal checks.
    target = '            trail_moved = False\n\n            if (\n'
    if target in source:
        source = source.replace(
            target,
            '            trail_moved = False\n            exit_reason = None\n\n            if (\n',
            1,
        )

    # Add Entry Time to the report schema and use Opened as a fallback for
    # older rows which were created before the field existed.
    cols_old = '        "Opened","Closed","Duration (min)","SL Trails","Last SL Update"\n'
    cols_new = '        "Opened","Entry Time","Closed","Duration (min)","SL Trails","Last SL Update"\n'
    if cols_old in source and '"Entry Time","Closed","Duration (min)"' not in source:
        source = source.replace(cols_old, cols_new, 1)

    df_anchor = '    df = pd.DataFrame(rows)\n    for col in columns:\n'
    if df_anchor in source and 'df["Entry Time"] = df["Opened"]' not in source:
        source = source.replace(
            df_anchor,
            '    df = pd.DataFrame(rows)\n    if "Entry Time" not in df.columns:\n        df["Entry Time"] = df["Opened"]\n    for col in columns:\n',
            1,
        )

    # Client-facing tables explicitly show Entry Time.
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

    return source


# Avoid the historical duplicate sidebar report-widget key crash.
_original_download_button = st.download_button
_seen_keys = set()


def _safe_download_button(*args, **kwargs):
    key = kwargs.get("key")
    if key == "sidebar_today_trade_report":
        if key in _seen_keys:
            return None
        _seen_keys.add(key)
    return _original_download_button(*args, **kwargs)


st.download_button = _safe_download_button

# Execute the existing full production dashboard with the safe patches above.
legacy = Path(__file__).resolve().parent / "legacy_app.py"
legacy_source = _patch_legacy_source(legacy.read_text(encoding="utf-8"))
exec(compile(legacy_source, str(legacy), "exec"), globals(), globals())
