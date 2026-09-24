from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailAuthError(RuntimeError):
    pass


def _save(credentials: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.touch(mode=0o600, exist_ok=True)
    token_path.chmod(0o600)
    token_path.write_text(credentials.to_json())


def authorize(client_secret: Path, token_path: Path) -> Credentials:
    """Run the browser consent flow and store the token."""
    if not client_secret.exists():
        raise GmailAuthError(f"OAuth client file not found: {client_secret}. See docs/gmail.md.")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    credentials = flow.run_local_server(port=0)
    _save(credentials, token_path)
    return credentials


def load_credentials(token_path: Path) -> Credentials:
    """Load the stored token, refreshing it if needed. Never opens a browser."""
    if not token_path.exists():
        raise GmailAuthError("No Gmail token. Run `job-radar gmail auth` first.")
    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _save(credentials, token_path)
        return credentials
    raise GmailAuthError("Gmail token is invalid. Run `job-radar gmail auth` again.")
