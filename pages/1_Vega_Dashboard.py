import streamlit as st

# Shared Dhan session bridge: the login page and Vega Dashboard use the same credentials.
st.session_state.setdefault("dhan_client_id", "1113195747")
st.session_state.setdefault("dhan_access_token", "")
st.session_state.setdefault("dhan_token", st.session_state.dhan_access_token)
st.session_state.setdefault("dhan_verified", False)
st.session_state.setdefault("dhan_connected", st.session_state.dhan_verified)

# Keep both token aliases synchronized for compatibility with the existing Vega engine.
if st.session_state.dhan_access_token and not st.session_state.dhan_token:
    st.session_state.dhan_token = st.session_state.dhan_access_token
elif st.session_state.dhan_token and not st.session_state.dhan_access_token:
    st.session_state.dhan_access_token = st.session_state.dhan_token

if st.session_state.dhan_verified:
    st.session_state.dhan_connected = True

exec(open("vega_monitor_v8.py", encoding="utf-8").read(), globals())
