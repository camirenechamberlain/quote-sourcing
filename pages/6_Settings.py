from __future__ import annotations

import streamlit as st

from branding import inject_branding, brand_header
from db.models import AppSetting
from db.session import get_session
from services import google_oauth

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")
inject_branding()
brand_header("Settings")

session = get_session()


def get_setting(key: str, default: str = "") -> str:
    row = session.get(AppSetting, key)
    return row.value if row else default


def set_setting(key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row:
        row.value = value
    else:
        session.add(AppSetting(key=key, value=value))
    session.commit()


st.subheader("Your details")
with st.form("sender_form"):
    sender_name = st.text_input("Your name (used to sign RFQ emails)", value=get_setting("sender_name"))
    if st.form_submit_button("Save"):
        set_setting("sender_name", sender_name)
        st.success("Saved.")

st.divider()
st.subheader("Gmail connection")

connected = False
account_email = None
try:
    creds = google_oauth.get_credentials()
    if creds and creds.valid:
        connected = True
        account_email = google_oauth.get_account_email(creds)
except Exception as e:  # noqa: BLE001
    st.warning(f"Could not verify existing connection: {e}")

if connected:
    st.success(f"Connected as **{account_email}**")
    if st.button("Disconnect Gmail"):
        google_oauth.disconnect()
        st.rerun()
else:
    st.info(
        "Not connected. Before connecting, make sure you've created a Google Cloud OAuth "
        "**Desktop app** client and placed the downloaded `client_secret.json` at the path set by "
        "`GOOGLE_OAUTH_CLIENT_SECRET_FILE` in your `.env` (default: `data/client_secret.json`)."
    )
    if st.button("Connect Gmail", type="primary"):
        try:
            with st.spinner("Opening your browser to complete Google sign-in..."):
                google_oauth.connect()
            st.success("Connected!")
            st.rerun()
        except google_oauth.OAuthNotConfigured as e:
            st.error(str(e))
        except Exception as e:  # noqa: BLE001
            st.error(f"Connection failed: {e}")

session.close()
