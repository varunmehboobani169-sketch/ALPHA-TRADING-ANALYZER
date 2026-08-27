import streamlit as st

# Shared Dhan session bridge: Dhan Login and Vega Dashboard use one session.
st.session_state.setdefault("dhan_client_id", "1113195747")
st.session_state.setdefault("dhan_access_token", "")
st.session_state.setdefault("dhan_token", "")
st.session_state.setdefault("dhan_verified", False)
st.session_state.setdefault("dhan_connected", False)

# Keep token aliases synchronized.
if st.session_state.dhan_access_token and not st.session_state.dhan_token:
    st.session_state.dhan_token = st.session_state.dhan_access_token
elif st.session_state.dhan_token and not st.session_state.dhan_access_token:
    st.session_state.dhan_access_token = st.session_state.dhan_token

# Bridge the shared login state to the Vega engine's legacy session keys.
st.session_state.cid = st.session_state.dhan_client_id
st.session_state.token = st.session_state.dhan_access_token or st.session_state.dhan_token
st.session_state.connected = bool(st.session_state.dhan_verified or st.session_state.dhan_connected) and bool(st.session_state.token)

exec(open("vega_monitor_v8.py", encoding="utf-8").read(), globals())
