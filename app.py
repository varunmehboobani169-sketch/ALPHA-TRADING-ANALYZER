import traceback
import requests
import streamlit as st

# Safe manual-upload entry point.
# The full dashboard remains in legacy_app.py and is downloaded from GitHub.
# We intentionally do NOT call st.set_page_config() here because the legacy
# dashboard owns page configuration.
LEGACY_URL = (
    "https://raw.githubusercontent.com/"
    "varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/"
    "main/legacy_app.py"
)


def _patch_legacy_source(source: str) -> str:
    # Prevent duplicate active trades after a Streamlit rerun.
    source = source.replace(
        'if existing and existing.get("status") == "ACTIVE":',
        'if existing and existing.get("Status", existing.get("status")) == "ACTIVE":',
        1,
    )

    # Preserve the original trade-creation time.
    opened_line = '        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    if '"Entry Time": opened.strftime' not in source and opened_line in source:
        source = source.replace(
            opened_line,
            opened_line + '        "Entry Time": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n',
            1,
        )

    # Never delete legitimate overnight positional history.
    source = source.replace(
        '_clean_invalid_trade_history_before_app_load()\n',
        '# historical cleanup intentionally disabled\n',
        1,
    )

    # New trades may only be CREATED while the corresponding market is live.
    old_open = '''        # No active trade: open only on a valid signal.\n        if direction is not None and pd.notna(ltp):\n'''
    new_open = '''        # No active trade: create only during live-market hours.\n        now_t = local_now().time()\n        trade_entry_allowed = True\n\n        if module_name == "NSE":\n            trade_entry_allowed = datetime.strptime("09:15", "%H:%M").time() <= now_t <= datetime.strptime("15:20", "%H:%M").time()\n        elif module_name == "MCX":\n            trade_entry_allowed = datetime.strptime("09:00", "%H:%M").time() <= now_t <= datetime.strptime("23:30", "%H:%M").time()\n\n        if trade_entry_allowed and direction is not None and pd.notna(ltp):\n'''
    if old_open in source:
        source = source.replace(old_open, new_open, 1)

    # Avoid uninitialized exit_reason on active-trade refresh.
    source = source.replace(
        '            trail_moved = False\n\n            if (\n',
        '            trail_moved = False\n            exit_reason = None\n\n            if (\n',
        1,
    )

    # Display immutable Entry Time in client-facing reports.
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


def main():
    try:
        r = requests.get(LEGACY_URL, timeout=30)
        r.raise_for_status()
        legacy_source = _patch_legacy_source(r.text)
        compiled = compile(legacy_source, "legacy_app.py", "exec")
        exec(compiled, globals(), globals())
    except Exception as exc:
        st.error("ALPHA ANALYZER startup error")
        st.write(f"**{type(exc).__name__}:** {exc}")
        st.code(traceback.format_exc(), language="text")
        st.stop()


if __name__ == "__main__":
    main()
