import streamlit as st
from datetime import time as _dt_time
from pathlib import Path

# Compatibility bootstrap: the legacy dashboard calls render_page_hero()
# before its original definition. Define it before executing the legacy app.
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

# V12/V13 added the same same-day report shortcut in two places inside the
# legacy dashboard. Streamlit raises StreamlitDuplicateElementKey when the
# same explicit key is rendered twice in one run. Keep the first report button
# and safely suppress later duplicates of that one legacy key.
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


def _patch_legacy_source(source):
    """Apply safe runtime patches without rewriting the large legacy file."""

    # ---------------------------------------------------------
    # 1) Preserve a dedicated immutable Entry Time in every trade.
    #    This is the first time the signal is actually detected by the
    #    live scanner. It is NOT updated when the report page is refreshed.
    # ---------------------------------------------------------
    opened_line = '        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    entry_time_line = opened_line + '        "Entry Time": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    if '"Entry Time": opened.strftime' not in source:
        if opened_line not in source:
            raise RuntimeError("Trade creation timestamp anchor not found")
        source = source.replace(opened_line, entry_time_line, 1)

    # ---------------------------------------------------------
    # 2) Make the report expose Entry Time explicitly.
    # ---------------------------------------------------------
    report_cols_old = '''        "Opened","Closed","Duration (min)","SL Trails","Last SL Update"\n'''
    report_cols_new = '''        "Opened","Entry Time","Closed","Duration (min)","SL Trails","Last SL Update"\n'''
    if '"Entry Time","Closed","Duration (min)"' not in source:
        if report_cols_old not in source:
            raise RuntimeError("Trade report columns anchor not found")
        source = source.replace(report_cols_old, report_cols_new, 1)

    # Populate Entry Time from the immutable Opened timestamp on old records
    # loaded from SQLite, so historical trades also display correctly.
    df_anchor = '''    df = pd.DataFrame(rows)\n    for col in columns:\n'''
    df_patch = '''    df = pd.DataFrame(rows)\n    if "Entry Time" not in df.columns:\n        df["Entry Time"] = df["Opened"]\n    for col in columns:\n'''
    if 'df["Entry Time"] = df["Opened"]' not in source:
        if df_anchor not in source:
            raise RuntimeError("Trade report dataframe anchor not found")
        source = source.replace(df_anchor, df_patch, 1)

    # Client-facing tables: show Entry Time instead of the internal field name.
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

    # ---------------------------------------------------------
    # 3) Only create NEW live trades while the relevant market is open.
    #    This prevents a late-night dashboard refresh from turning an old
    #    positional signal into a brand-new trade at 19:54/20:03 etc.
    #    Existing active trades continue to be managed/reported normally.
    # ---------------------------------------------------------
    old_open = '''        # No active trade: open only on a valid signal.\n        if direction is not None and pd.notna(ltp):\n'''
    new_open = '''        # No active trade: open only on a valid signal DURING MARKET HOURS.\n        now_t = local_now().time()\n        trade_entry_allowed = True\n\n        if module_name == "NSE":\n            # NSE cash/F&O: use the live trading window for fresh entries.\n            trade_entry_allowed = _dt_time(9, 15) <= now_t <= _dt_time(15, 20)\n        elif module_name == "MCX":\n            # Broad MCX window; individual contract logic remains in the module.\n            trade_entry_allowed = _dt_time(9, 0) <= now_t <= _dt_time(23, 30)\n\n        if trade_entry_allowed and direction is not None and pd.notna(ltp):\n'''
    if old_open not in source:
        raise RuntimeError("Fresh trade opening anchor not found")
    source = source.replace(old_open, new_open, 1)

    # ---------------------------------------------------------
    # 4) Rename the report download label so clients understand this is a
    #    timestamped trade log, not the time the page was refreshed.
    # ---------------------------------------------------------
    source = source.replace(
        '"⬇️ Download Today\'s Fresh Trades"',
        '"⬇️ Download Today\'s Timestamped Trades"',
    )

    return source


# Execute the full original dashboard with the safe runtime patches above.
legacy = Path(__file__).resolve().parent / "legacy_app.py"
legacy_source = _patch_legacy_source(legacy.read_text(encoding="utf-8"))
exec(compile(legacy_source, str(legacy), "exec"), globals(), globals())
