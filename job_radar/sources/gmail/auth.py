"""One-time Gmail consent and the stored token, read-only scope only.

``authorize`` opens the browser consent flow (``job-radar gmail auth``) and saves the
token with owner-only permissions; ``load_credentials`` loads it and refreshes it when
needed, never opening a browser, and raises ``GmailAuthError`` when the user has to run
the consent again. See docs/gmail.md.

Usage:
    import job_radar.sources.gmail.auth as gmail_auth
"""

import pathlib

import google.auth.transport.requests as transport_requests
import google.oauth2.credentials as oauth2_credentials
import google_auth_oauthlib.flow as google_auth_oauthlib_flow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailAuthError(RuntimeError):
    """No usable token: the user has to run ``job-radar gmail auth`` again."""

    pass


def _save(credentials: oauth2_credentials.Credentials, token_path: pathlib.Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.touch(mode=0o600, exist_ok=True)
    token_path.chmod(0o600)
    token_path.write_text(credentials.to_json())


def authorize(
    client_secret: pathlib.Path, token_path: pathlib.Path
) -> oauth2_credentials.Credentials:
    """Run the browser consent flow and store the token."""
    if not client_secret.exists():
        raise GmailAuthError(f"OAuth client file not found: {client_secret}. See docs/gmail.md.")
    flow = google_auth_oauthlib_flow.InstalledAppFlow.from_client_secrets_file(
        str(client_secret), SCOPES
    )
    credentials = flow.run_local_server(port=0)
    _save(credentials, token_path)
    return credentials


def load_credentials(token_path: pathlib.Path) -> oauth2_credentials.Credentials:
    """Load the stored token, refreshing it if needed. Never opens a browser."""
    if not token_path.exists():
        raise GmailAuthError("No Gmail token. Run `job-radar gmail auth` first.")
    credentials = oauth2_credentials.Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(transport_requests.Request())
        _save(credentials, token_path)
        return credentials
    raise GmailAuthError("Gmail token is invalid. Run `job-radar gmail auth` again.")
