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

# Bridge the shared login state to the Vega engine's session keys.
st.session_state.cid = st.session_state.dhan_client_id
st.session_state.token = st.session_state.dhan_access_token or st.session_state.dhan_token
st.session_state.connected = bool(st.session_state.dhan_verified or st.session_state.dhan_connected) and bool(st.session_state.token)

# Defensive initialization: a Streamlit rerun can retain the lock IDs without the derived tuple.
if "instruments" not in st.session_state:
    ce_id = st.session_state.get("ce_id")
    pe_id = st.session_state.get("pe_id")
    rebuilt = [("IDX_I", "13")]
    if ce_id:
        rebuilt.append(("NSE_FNO", str(ce_id)))
    if pe_id:
        rebuilt.append(("NSE_FNO", str(pe_id)))
    st.session_state.instruments = tuple(rebuilt)

exec(open("vega_monitor_v9.py", encoding="utf-8").read(), globals())
