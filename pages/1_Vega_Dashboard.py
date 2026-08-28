import streamlit as st

# Shared session bridge. Provider branding is intentionally hidden from the visible dashboard.
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

st.session_state.setdefault("locked", False)
st.session_state.setdefault("strike", 0.0)
st.session_state.setdefault("spot10", 0.0)
st.session_state.setdefault("expiry", "")
st.session_state.setdefault("ce_id", "")
st.session_state.setdefault("pe_id", "")
st.session_state.setdefault("wing_ids", {})
st.session_state.setdefault("instruments", (("IDX_I", "13"),))
st.session_state.setdefault("prev_ce", None)
st.session_state.setdefault("prev_pe", None)
st.session_state.setdefault("prev_atm_iv", None)
st.session_state.setdefault("history", [])
st.session_state.setdefault("last_alert", None)
st.session_state.setdefault("sample", {"atm_iv": None, "legs": {}})
st.session_state.setdefault("sample_bucket", None)

exec(open("vega_monitor_v9.py", encoding="utf-8").read(), globals())
