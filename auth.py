import streamlit as st

CLIENT_KEY = "shared_client_id"
TOKEN_KEY = "shared_access_token"
AUTH_KEY = "shared_authenticated"


def login_form() -> bool:
    st.title("Market Access")
    st.caption("Sign in once to use all monitoring modules.")

    with st.form("shared_login_form"):
        client_id = st.text_input("Client ID", key="login_client_id")
        access_token = st.text_input("Access Token", type="password", key="login_access_token")
        submitted = st.form_submit_button("LOGIN", type="primary", use_container_width=True)

    if submitted:
        if not client_id.strip() or not access_token.strip():
            st.error("Enter both Client ID and Access Token.")
            return False
        st.session_state[CLIENT_KEY] = client_id.strip()
        st.session_state[TOKEN_KEY] = access_token.strip()
        st.session_state[AUTH_KEY] = True
        st.rerun()
    return bool(st.session_state.get(AUTH_KEY))


def is_authenticated() -> bool:
    return bool(
        st.session_state.get(AUTH_KEY)
        and st.session_state.get(CLIENT_KEY)
        and st.session_state.get(TOKEN_KEY)
    )


def credentials() -> tuple[str, str]:
    if not is_authenticated():
        raise RuntimeError("Not authenticated")
    return st.session_state[CLIENT_KEY], st.session_state[TOKEN_KEY]


def require_login() -> tuple[str, str]:
    if not is_authenticated():
        st.warning("Please log in from the Market Access page.")
        if st.button("OPEN LOGIN", type="primary"):
            st.switch_page("app.py")
        st.stop()
    return credentials()


def logout_button() -> None:
    if st.sidebar.button("LOGOUT"):
        for key in (CLIENT_KEY, TOKEN_KEY, AUTH_KEY):
            st.session_state.pop(key, None)
        st.rerun()
