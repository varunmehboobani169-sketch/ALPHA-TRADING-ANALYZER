import json
import urllib.error
import urllib.request

import streamlit as st

# Internal connection endpoint; provider name is intentionally hidden from UI.
DATA_API = "https://api.dhan.co/v2"
DEFAULT_CLIENT_ID = "1113195747"

st.set_page_config(page_title="FRIDAY — Login", layout="wide")

st.title("🔐 FRIDAY — Login")
st.caption("Enter your market-data Client ID and Access Token. Credentials stay in this Streamlit session and are not written to GitHub.")

st.session_state.setdefault("dhan_client_id", DEFAULT_CLIENT_ID)
st.session_state.setdefault("dhan_access_token", "")
st.session_state.setdefault("dhan_token", "")
st.session_state.setdefault("dhan_connected", False)
st.session_state.setdefault("dhan_verified", False)

if st.session_state.dhan_access_token and not st.session_state.dhan_token:
    st.session_state.dhan_token = st.session_state.dhan_access_token
elif st.session_state.dhan_token and not st.session_state.dhan_access_token:
    st.session_state.dhan_access_token = st.session_state.dhan_token

with st.form("login_form", clear_on_submit=False):
    client_id = st.text_input("Client ID", value=st.session_state.dhan_client_id, placeholder="1113195747").strip()
    token = st.text_input("Access Token", value=st.session_state.dhan_access_token, type="password", placeholder="Paste your access token").strip()
    submitted = st.form_submit_button("LOGIN / VERIFY", use_container_width=True, type="primary")

if submitted:
    if not client_id or not token:
        st.session_state.dhan_verified = False
        st.session_state.dhan_connected = False
        st.error("Please enter both Client ID and Access Token.")
    else:
        req = urllib.request.Request(
            DATA_API + "/profile",
            method="GET",
            headers={"Accept": "application/json", "access-token": token, "client-id": client_id},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            st.session_state.dhan_client_id = client_id
            st.session_state.dhan_access_token = token
            st.session_state.dhan_token = token
            st.session_state.dhan_connected = True
            st.session_state.dhan_verified = True
            st.success("✅ Login verified. The same session is now available to the Vega Dashboard.")
            validity = None
            if isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, dict):
                    validity = data.get("tokenValidity")
                validity = validity or payload.get("tokenValidity")
            if validity:
                st.info(f"Token validity: {validity}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            st.session_state.dhan_verified = False
            st.session_state.dhan_connected = False
            st.error(f"Authentication failed (HTTP {exc.code}).")
            st.code(detail)
        except Exception as exc:
            st.session_state.dhan_verified = False
            st.session_state.dhan_connected = False
            st.error(f"Could not verify credentials: {exc}")

st.divider()

if st.session_state.dhan_verified and st.session_state.dhan_token:
    st.success("✅ Connection is active and shared across the app.")
    st.write(f"Client ID: `{st.session_state.dhan_client_id}`")
    st.caption("The access token is masked and remains session-only.")
    if st.button("LOGOUT / CLEAR CREDENTIALS", use_container_width=True):
        st.session_state.dhan_client_id = DEFAULT_CLIENT_ID
        st.session_state.dhan_access_token = ""
        st.session_state.dhan_token = ""
        st.session_state.dhan_connected = False
        st.session_state.dhan_verified = False
        st.cache_resource.clear()
        st.rerun()
else:
    st.warning("Not logged in.")

st.caption("Security note: credentials are not printed, committed, or stored in the repository.")
