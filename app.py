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
    # ------------------------------------------------------------------
    # 1) Never create a second copy of an already-active trade.
    # ------------------------------------------------------------------
    source = source.replace(
        'if existing and existing.get("status") == "ACTIVE":',
        'if existing and existing.get("Status", existing.get("status")) == "ACTIVE":',
        1,
    )

    # ------------------------------------------------------------------
    # 2) Persist the first detection timestamp as Entry Time.
    #    It is identical to Opened and is never rewritten on reruns.
    # ------------------------------------------------------------------
    opened_line = '        "Opened": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    entry_time_line = opened_line + '        "Entry Time": opened.strftime("%d-%b-%Y %H:%M:%S IST"),\n'
    if '"Entry Time": opened.strftime' not in source:
        if opened_line in source:
            source = source.replace(opened_line, entry_time_line, 1)

    # ------------------------------------------------------------------
    # 3) Make report tables use Entry Time.
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 4) IMPORTANT: do NOT delete after-hours positional trades.
    #    An NSE positional trade may remain active overnight. The previous
    #    cleanup was deleting legitimate trades when the user refreshed at
    #    night, which made Fresh Trades/Trade Logs appear empty.
    # ------------------------------------------------------------------
    # Replace the old cleanup call with a harmless compatibility call.
    source = source.replace(
        '_clean_invalid_trade_history_before_app_load()\n',
        '# Do not delete historical/positional trades on refresh.\n',
        1,
    )

    # ------------------------------------------------------------------
    # 5) Load persisted ACTIVE trades from SQLite into trade_book on every
    #    new Streamlit session. This prevents a refresh/reconnect from
    #    generating a new Trade ID and a new Entry Time for the same signal.
    # ------------------------------------------------------------------
    marker = '    _reload_trade_history()\n'
    inject = '''    _reload_trade_history()\n\n    # Rehydrate active trades from SQLite after a browser/session restart.\n    # The stored Opened timestamp is the original signal detection time.\n    try:\n        owner = str(st.session_state.get("client_id", "default") or "default")\n        conn = _trade_db_connect()\n        active_rows = conn.execute(\n            """\n            SELECT trade_id,date,module,mode,symbol,direction,\n                   signal_level,entry,initial_sl,current_sl,current,exit_price,\n                   status,exit_reason,points_pnl,pnl_pct,opened,closed,\n                   duration_min,sl_trails,last_sl_update\n            FROM trades\n            WHERE owner=? AND status='ACTIVE'\n            ORDER BY opened DESC\n            """,\n            (owner,),\n        ).fetchall()\n        conn.close()\n\n        for r in active_rows:\n            trade = dict(zip(\n                [\n                    "Trade ID","Date","Module","Mode","Symbol","Direction",\n                    "Signal Level","Entry","Initial SL","SL","Current","Exit",\n                    "Status","Exit Reason","Points P&L","P&L %",\n                    "Opened","Closed","Duration (min)","SL Trails","Last SL Update"\n                ],\n                r,\n            ))\n            trade["Entry Time"] = trade.get("Opened", "")\n            key = _trade_key(\n                trade["Module"], trade["Mode"], trade["Symbol"]\n            )\n            st.session_state.trade_book[key] = trade\n\n            # Keep the sequence ahead of persisted IDs.\n            try:\n                seq = int(str(trade["Trade ID"])[1:])\n                st.session_state.trade_sequence = max(\n                    int(st.session_state.get("trade_sequence", 0)), seq\n                )\n            except Exception:\n                pass\n    except Exception:\n        # Persistence must never prevent the live scanner from running.\n        pass\n'''
    if marker in source and 'Rehydrate active trades from SQLite' not in source:
        source = source.replace(marker, inject, 1)

    # ------------------------------------------------------------------
    # 6) Fresh-trade creation must only happen during the relevant market
    #    session. Existing active trades continue to be managed outside it.
    #    Positional and Intraday both create only during market hours.
    # ------------------------------------------------------------------
    old_open = '''        # No active trade: open only on a valid signal.\n        if direction is not None and pd.notna(ltp):\n'''
    new_open = '''        # No active trade: create only on a valid live-market signal.\n        # Existing ACTIVE trades are handled above and are never recreated.\n        now_t = local_now().time()\n        trade_entry_allowed = True\n\n        if module_name == "NSE":\n            trade_entry_allowed = _dt_time(9, 15) <= now_t <= _dt_time(15, 20)\n        elif module_name == "MCX":\n            trade_entry_allowed = _dt_time(9, 0) <= now_t <= _dt_time(23, 30)\n\n        if trade_entry_allowed and direction is not None and pd.notna(ltp):\n'''
    if old_open in source:
        source = source.replace(old_open, new_open, 1)

    # ------------------------------------------------------------------
    # 7) Fix the active-trade manager's uninitialised exit_reason. Without
    #    this, an active trade could throw on refresh before normal logging.
    # ------------------------------------------------------------------
    target = '            trail_moved = False\n\n            if (\n'
    replacement = '            trail_moved = False\n            exit_reason = None\n\n            if (\n'
    if target in source and 'trail_moved = False\n            exit_reason = None' not in source:
        source = source.replace(target, replacement, 1)

    # ------------------------------------------------------------------
    # 8) Load historical Entry Time for legacy rows.
    # ------------------------------------------------------------------
    df_anchor = '''    df = pd.DataFrame(rows)\n    for col in columns:\n'''
    df_patch = '''    df = pd.DataFrame(rows)\n    if "Entry Time" not in df.columns:\n        df["Entry Time"] = df["Opened"]\n    for col in columns:\n'''
    if 'df["Entry Time"] = df["Opened"]' not in source and df_anchor in source:
        source = source.replace(df_anchor, df_patch, 1)

    return source


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

legacy = Path(__file__).resolve().parent / "legacy_app.py"
legacy_source = _patch_legacy_source(
    legacy.read_text(encoding="utf-8")
)
exec(compile(legacy_source, str(legacy), "exec"), globals(), globals())
