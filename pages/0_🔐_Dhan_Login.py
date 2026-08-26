import json
import urllib.error
import urllib.request

import streamlit as st

DHAN_API = "https://api.dhan.co/v2"
DEFAULT_CLIENT_ID = ""

st.set_page_config(page_title="FRIDAY — Dhan Login", layout="wide")

st.title("🔐 FRIDAY — Dhan Login")
st.caption("Enter your Dhan Client ID and Access Token. Credentials are kept only in this Streamlit session and are not written to the repository.")

if "dhan_client_id" not in st.session_state:
    st.session_state.dhan_client_id = DEFAULT_CLIENT_ID
if "dhan_access_token" not in st.session_state:
    st.session_state.dhan_access_token = ""
if "dhan_verified" not in st.session_state:
    st.session_state.dhan_verified = False

with st.form("dhan_login_form"):
    client_id = st.text_input(
        "Dhan Client ID",
        value=st.session_state.dhan_client_id,
        placeholder="Enter Dhan Client ID",
        help="Example format: 1113195747",
    ).strip()
    token = st.text_input(
        "Dhan Access Token",
        value=st.session_state.dhan_access_token,
        type="password",
        placeholder="Paste your Dhan access token",
        help="Your access token is masked on screen.",
    ).strip()
    submitted = st.form_submit_button("LOGIN / VERIFY", use_container_width=True, type="primary")

if submitted:
    if not client_id or not token:
        st.session_state.dhan_verified = False
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
            st.session_state.dhan_verified = True
            validity = None
            if isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, dict):
                    validity = data.get("tokenValidity")
                validity = validity or payload.get("tokenValidity")
            st.success("Dhan login verified for this session.")
            if validity:
                st.info(f"Token validity reported by Dhan: {validity}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            st.session_state.dhan_verified = False
            st.error(f"Dhan authentication failed (HTTP {exc.code}).")
            st.code(detail)
        except Exception as exc:
            st.session_state.dhan_verified = False
            st.error(f"Could not verify Dhan credentials: {exc}")

st.divider()

if st.session_state.dhan_verified:
    st.success("✅ Dhan credentials are active in this Streamlit session.")
    st.write(f"Client ID: `{st.session_state.dhan_client_id}`")
    if st.button("LOGOUT / CLEAR CREDENTIALS", use_container_width=True):
        st.session_state.dhan_client_id = ""
        st.session_state.dhan_access_token = ""
        st.session_state.dhan_verified = False
        st.rerun()
else:
    st.warning("Not logged in.")

st.caption("Security note: FRIDAY does not print the access token, put it in source code, or commit it to GitHub.")
