import streamlit as st

# Default Dhan Client ID; access token remains session-only.
st.session_state.setdefault("cid", "1113195747")
exec(open("vega_monitor_v8.py", encoding="utf-8").read(), globals())
