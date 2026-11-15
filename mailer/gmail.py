"""Gmail: connect with OAuth (send-only) and send messages through the Gmail API."""
import base64
import mimetypes
import os
from datetime import timezone as dt_timezone
from email.message import EmailMessage
from email.utils import formataddr

import requests
from django.conf import settings
from django.utils import timezone

from .models import GmailAccount

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.send",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Google returns scopes in its own order/form; don't fail on that.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
# OAuth over plain http is only allowed for a localhost redirect (local use).
if settings.GOOGLE_REDIRECT_URI.startswith(("http://localhost", "http://127.0.0.1")):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


class GmailError(Exception):
    """Sending failed. `permanent` means retrying will not help (e.g. access revoked)."""

    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def is_configured():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def _client_config():
    return {"web": {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": TOKEN_URI,
        "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
    }}


def make_flow(state=None, code_verifier=None):
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, state=state, code_verifier=code_verifier)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    return flow


def authorization_url():
    """(url, state, code_verifier): send the user to url; keep the other two for the callback."""
    flow = make_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return url, state, flow.code_verifier


def finish_connect(authorization_response, state, code_verifier):
    """Exchange the callback for tokens and store the account. Returns the GmailAccount."""
    flow = make_flow(state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials
    resp = requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
                        headers={"Authorization": f"Bearer {creds.token}"}, timeout=20)
    resp.raise_for_status()
    email = resp.json().get("email", "")
    account, _ = GmailAccount.objects.get_or_create(pk=1)
    # Google sends a refresh token only on first consent; keep the old one otherwise.
    account.refresh_token = creds.refresh_token or account.refresh_token
    account.email = email
    account.access_token = creds.token or ""
    account.token_expiry = creds.expiry.replace(tzinfo=dt_timezone.utc) if creds.expiry else None
    account.scopes = " ".join(creds.scopes or SCOPES)
    account.connected_at = timezone.now()
    account.save()
    return account


def disconnect():
    account = GmailAccount.objects.filter(pk=1).first()
    if account is None:
        return
    if account.refresh_token:
        try:  # best effort: also revoke on Google's side
            requests.post("https://oauth2.googleapis.com/revoke", params={"token": account.refresh_token}, timeout=10)
        except requests.RequestException:
            pass
    account.delete()


def _credentials(account):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=account.access_token or None,
        refresh_token=account.refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
        expiry=account.token_expiry.astimezone(dt_timezone.utc).replace(tzinfo=None) if account.token_expiry else None,
    )
    if not creds.valid:
        from google.auth.exceptions import RefreshError
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise GmailError(f"Gmail access was revoked or expired. Reconnect Gmail in Settings. ({exc})", permanent=True) from exc
        account.access_token = creds.token
        account.token_expiry = creds.expiry.replace(tzinfo=dt_timezone.utc) if creds.expiry else None
        account.save(update_fields=["access_token", "token_expiry"])
    return creds


def build_mime(from_name, from_email, to, subject, html_body, text_body, attachments=()):
    """The RFC 822 message. `attachments` are (filename, bytes) pairs."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    for filename, data in attachments:
        ctype, _ = mimetypes.guess_type(filename)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def send_message(account, mime):
    """Send through the Gmail API. Returns the Gmail message id."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = _credentials(account)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except HttpError as exc:
        status = getattr(exc.resp, "status", 0)
        # 400 bad recipient / 403 no permission: retrying will not help.
        raise GmailError(f"Gmail refused the email ({status}): {exc.reason}", permanent=status in (400, 401, 403)) from exc
    except Exception as exc:  # network trouble: worth another try later
        raise GmailError(f"Could not reach Gmail: {exc}") from exc
    return sent.get("id", "")
