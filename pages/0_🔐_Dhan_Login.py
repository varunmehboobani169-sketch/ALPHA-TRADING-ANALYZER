import json
import urllib.error
import urllib.request

import streamlit as st

DHAN_API = "https://api.dhan.co/v2"
DEFAULT_CLIENT_ID = "1113195747"

st.set_page_config(page_title="FRIDAY — Dhan Login", layout="wide")

st.title("🔐 FRIDAY — Dhan Login")
st.caption("Enter your Dhan Client ID and Access Token. The credentials are kept in this Streamlit session and are shared with the Vega Dashboard; they are not written to GitHub.")

# Shared session keys used by both FRIDAY and the Vega Dashboard.
st.session_state.setdefault("dhan_client_id", DEFAULT_CLIENT_ID)
st.session_state.setdefault("dhan_access_token", "")
st.session_state.setdefault("dhan_token", st.session_state.dhan_access_token)
st.session_state.setdefault("dhan_connected", False)
st.session_state.setdefault("dhan_verified", False)

# Keep aliases synchronized so either page can consume the same credentials.
if st.session_state.dhan_access_token and not st.session_state.dhan_token:
    st.session_state.dhan_token = st.session_state.dhan_access_token
elif st.session_state.dhan_token and not st.session_state.dhan_access_token:
    st.session_state.dhan_access_token = st.session_state.dhan_token

with st.form("dhan_login_form", clear_on_submit=False):
    client_id = st.text_input(
        "Dhan Client ID",
        value=st.session_state.dhan_client_id,
        placeholder="1113195747",
    ).strip()
    token = st.text_input(
        "Dhan Access Token",
        value=st.session_state.dhan_access_token,
        type="password",
        placeholder="Paste your Dhan access token",
    ).strip()
    submitted = st.form_submit_button("LOGIN / VERIFY", use_container_width=True, type="primary")

if submitted:
    if not client_id or not token:
        st.session_state.dhan_verified = False
        st.session_state.dhan_connected = False
        st.error("Please enter both Dhan Client ID and Access Token.")
    else:
        req = urllib.request.Request(
            DHAN_API + "/profile",
            method="GET",
            headers={
                "Accept": "application/json",
                "access-token": token,
                "client-id": client_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            st.session_state.dhan_client_id = client_id
            st.session_state.dhan_access_token = token
            st.session_state.dhan_token = token
            st.session_state.dhan_connected = True
            st.session_state.dhan_verified = True
            st.success("✅ Dhan login verified. The same session is now available to the Vega Dashboard.")
            validity = None
            if isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, dict):
                    validity = data.get("tokenValidity")
                validity = validity or payload.get("tokenValidity")
            if validity:
                st.info(f"Token validity reported by Dhan: {validity}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            st.session_state.dhan_verified = False
            st.session_state.dhan_connected = False
            st.error(f"Dhan authentication failed (HTTP {exc.code}).")
            st.code(detail)
        except Exception as exc:
            st.session_state.dhan_verified = False
            st.session_state.dhan_connected = False
            st.error(f"Could not verify Dhan credentials: {exc}")

st.divider()

if st.session_state.dhan_verified and st.session_state.dhan_token:
    st.success("✅ Dhan credentials are active and shared across the app.")
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

st.caption("Security note: FRIDAY does not print the access token, put it in source code, or commit it to GitHub.")
