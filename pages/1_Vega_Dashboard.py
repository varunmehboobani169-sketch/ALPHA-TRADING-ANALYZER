import streamlit as st

# Shared session bridge. Provider branding is hidden from the visible dashboard.
st.session_state.setdefault("dhan_client_id", "1113195747")
st.session_state.setdefault("dhan_access_token", "")
st.session_state.setdefault("dhan_token", "")
st.session_state.setdefault("dhan_verified", False)
st.session_state.setdefault("dhan_connected", False)

if st.session_state.dhan_access_token and not st.session_state.dhan_token:
    st.session_state.dhan_token = st.session_state.dhan_access_token
if st.session_state.dhan_token and not st.session_state.dhan_access_token:
    st.session_state.dhan_access_token = st.session_state.dhan_access_token

st.session_state.cid = st.session_state.dhan_client_id
st.session_state.token = st.session_state.dhan_access_token or st.session_state.dhan_token
st.session_state.connected = bool(st.session_state.dhan_verified or st.session_state.dhan_connected) and bool(st.session_state.token)

for key, value in {
    "locked": False, "strike": None, "spot10": None, "expiry": None,
    "ce_id": "", "pe_id": "", "wing_ids": {}, "instruments": (("IDX_I", "13"),),
    "feed": None, "feed_key": "", "prev_ce": None, "prev_pe": None,
    "prev_atm_iv": None, "history": [], "last_alert": None,
    "sample": {"atm_iv": None, "legs": {}}, "sample_bucket": None,
    "day": "", "setup_error": "",
}.items():
    st.session_state.setdefault(key, value)

exec(open("vega_monitor_v13.py", encoding="utf-8").read(), globals())
