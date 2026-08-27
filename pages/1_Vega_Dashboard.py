import streamlit as st

# Pre-fill the Dhan Client ID used by this dashboard. The access token remains session-only.
st.session_state.setdefault("dhan_client_id", "1113195747")
exec(open("vega_monitor_v6.py", encoding="utf-8").read(), globals())
