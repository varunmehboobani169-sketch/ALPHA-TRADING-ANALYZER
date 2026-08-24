import streamlit as st

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

# Execute the full original dashboard unchanged. Keeping it in a separate
# file prevents startup-order NameErrors while preserving every module.
from pathlib import Path
legacy = Path(__file__).resolve().parent / "legacy_app.py"
exec(compile(legacy.read_text(encoding="utf-8"), str(legacy), "exec"), globals(), globals())
