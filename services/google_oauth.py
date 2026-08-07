from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

TOKEN_PATH = Path(__file__).resolve().parent.parent / "data" / "token.json"


class OAuthNotConfigured(RuntimeError):
    pass


def _client_secret_path() -> Path:
    raw = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET_FILE", "data/client_secret.json")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    if not path.exists():
        raise OAuthNotConfigured(
            f"Google OAuth client secret file not found at {path}. "
            "Download it from Google Cloud Console (OAuth Client ID, Desktop app type) and place it there."
        )
    return path


def get_credentials() -> Credentials | None:
    if not TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def is_connected() -> bool:
    creds = get_credentials()
    return bool(creds and creds.valid)


def connect() -> Credentials:
    """Runs the local-server OAuth flow. Blocks until the user completes consent in their browser."""
    flow = InstalledAppFlow.from_client_secrets_file(str(_client_secret_path()), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.parent.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    return creds


def disconnect() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def get_account_email(creds: Credentials) -> str | None:
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress")
