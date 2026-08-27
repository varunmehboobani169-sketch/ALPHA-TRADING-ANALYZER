import streamlit as st

# Shared Dhan login bridge. Token remains session-only and is never stored in GitHub.
st.session_state.setdefault("dhan_client_id", "1113195747")
st.session_state.setdefault("dhan_access_token", "")
st.session_state.setdefault("dhan_token", "")
st.session_state.setdefault("dhan_verified", False)
st.session_state.setdefault("dhan_connected", False)

if st.session_state.dhan_access_token and not st.session_state.dhan_token:
    st.session_state.dhan_token = st.session_state.dhan_access_token
if st.session_state.dhan_token and not st.session_state.dhan_access_token:
    st.session_state.dhan_access_token = st.session_state.dhan_token

st.session_state.cid = st.session_state.dhan_client_id
st.session_state.token = st.session_state.dhan_access_token or st.session_state.dhan_token
st.session_state.connected = bool(st.session_state.dhan_verified or st.session_state.dhan_connected) and bool(st.session_state.token)

exec(open("vega_monitor_v9.py", encoding="utf-8").read(), globals())
