import traceback

import requests
import streamlit as st

# The production dashboard is stored in GitHub as legacy_app.py.
# This entry point deliberately does NOT call st.set_page_config().
LEGACY_URL = (
    "https://raw.githubusercontent.com/"
    "varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/"
    "main/legacy_app.py"
)


def patch_legacy_source(source: str) -> str:
    """Apply only safe, deterministic compatibility fixes."""

    # Prevent duplicate active trades across Streamlit reruns.
    source = source.replace(
        'if existing and existing.get("status") == "ACTIVE":',
        'if existing and existing.get("Status", existing.get("status")) == "ACTIVE":',
        1,
    )

    # Preserve the original trade creation timestamp.
    opened_line = '        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    if '"Entry Time": opened.strftime' not in source and opened_line in source:
        source = source.replace(
            opened_line,
            opened_line
            + '        "Entry Time": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n',
            1,
        )

    # Never delete legitimate overnight/positional history on refresh.
    source = source.replace(
        '_clean_invalid_trade_history_before_app_load()\n',
        '# historical cleanup intentionally disabled\n',
        1,
    )

    # Allow new trades only during live market hours.
    old_open = """        # No active trade: open only on a valid signal.
        if direction is not None and pd.notna(ltp):
"""
    new_open = """        # No active trade: create only during the live market.
        now_t = local_now().time()
        trade_entry_allowed = True

        if module_name == "NSE":
            trade_entry_allowed = (
                datetime.strptime("09:15", "%H:%M").time()
                <= now_t
                <= datetime.strptime("15:20", "%H:%M").time()
            )
        elif module_name == "MCX":
            trade_entry_allowed = (
                datetime.strptime("09:00", "%H:%M").time()
                <= now_t
                <= datetime.strptime("23:30", "%H:%M").time()
            )

        if trade_entry_allowed and direction is not None and pd.notna(ltp):
"""
    if old_open in source:
        source = source.replace(old_open, new_open, 1)

    # Use immutable Entry Time in the client-facing report columns.
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

    # Prevent an uninitialized exit_reason from breaking active-trade refreshes.
    source = source.replace(
        '            trail_moved = False\n\n            if (\n',
        '            trail_moved = False\n            exit_reason = None\n\n            if (\n',
        1,
    )

    return source


def main() -> None:
    try:
        response = requests.get(LEGACY_URL, timeout=30)
        response.raise_for_status()
        legacy_source = patch_legacy_source(response.text)

        # Compile before executing so a Python error is visible in the app.
        compiled = compile(legacy_source, "legacy_app.py", "exec")
        exec(compiled, globals(), globals())

    except Exception as exc:
        st.error("ALPHA ANALYZER startup error")
        st.write(f"**{type(exc).__name__}:** {exc}")
        st.code(traceback.format_exc(), language="text")
        st.stop()


if __name__ == "__main__":
    main()
