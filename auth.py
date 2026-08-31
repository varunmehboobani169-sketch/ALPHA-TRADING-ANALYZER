import streamlit as st

# Fixed Dhan client ID requested for this dashboard.
FIXED_CLIENT_ID = "1113195747"
CLIENT_KEY = "shared_client_id"
TOKEN_KEY = "shared_access_token"
AUTH_KEY = "shared_authenticated"


def login_form() -> bool:
    st.title("Market Access")
    st.caption("Client ID is fixed for this dashboard. Enter only the Access Token.")
    st.info(f"Client ID: {FIXED_CLIENT_ID}")

    with st.form("shared_login_form"):
        access_token = st.text_input("Access Token", type="password", key="login_access_token")
        submitted = st.form_submit_button("LOGIN", type="primary", use_container_width=True)

    if submitted:
        if not access_token.strip():
            st.error("Enter Access Token.")
            return False
        st.session_state[CLIENT_KEY] = FIXED_CLIENT_ID
        st.session_state[TOKEN_KEY] = access_token.strip()
        st.session_state[AUTH_KEY] = True
        st.rerun()
    return bool(st.session_state.get(AUTH_KEY))


def is_authenticated() -> bool:
    return bool(
        st.session_state.get(AUTH_KEY)
        and st.session_state.get(CLIENT_KEY) == FIXED_CLIENT_ID
        and st.session_state.get(TOKEN_KEY)
    )


def credentials() -> tuple[str, str]:
    if not is_authenticated():
        raise RuntimeError("Not authenticated")
    return FIXED_CLIENT_ID, st.session_state[TOKEN_KEY]


def require_login() -> tuple[str, str]:
    """Return fixed client ID + session token, showing the login form when needed."""
    if not is_authenticated():
        logged_in = login_form()
        if not logged_in:
            st.stop()
    return credentials()


def logout_button() -> None:
    if st.sidebar.button("LOGOUT"):
        for key in (CLIENT_KEY, TOKEN_KEY, AUTH_KEY):
            st.session_state.pop(key, None)
        st.rerun()
