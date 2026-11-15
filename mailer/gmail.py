"""Gmail: connect with OAuth, send messages, and read replies to them, via the Gmail API."""
import base64
import mimetypes
import os
import re
from datetime import datetime, timezone as dt_timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr, parsedate_to_datetime

import requests
from django.conf import settings
from django.utils import timezone

from .models import GmailAccount

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.send",
    # Read-only: used only to find replies in the conversations of emails this app sent.
    "https://www.googleapis.com/auth/gmail.readonly",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Google returns scopes in its own order/form; don't fail on that.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
# OAuth over plain http is only allowed for a localhost redirect (local use).
if settings.GOOGLE_REDIRECT_URI.startswith(("http://localhost", "http://127.0.0.1")):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


class GmailError(Exception):
    """A Gmail API call failed.

    permanent:    retrying this email will not help (bad address, no permission)
    rate_limited: Gmail is limiting the account (quota / too many sends): slow down
    auth:         access was revoked or expired: reconnect Gmail
    """

    def __init__(self, message, permanent=False, rate_limited=False, auth=False):
        super().__init__(message)
        self.permanent = permanent
        self.rate_limited = rate_limited
        self.auth = auth


RATE_LIMIT_SIGNS = re.compile(
    r"rateLimitExceeded|userRateLimitExceeded|quotaExceeded|dailyLimitExceeded|"
    r"sending limit|sending quota|limit exceeded|too many", re.I)


def classify_http_error(status, text):
    """{permanent, rate_limited, auth} for a Gmail HTTP error status and body."""
    if status == 429 or (status == 403 and RATE_LIMIT_SIGNS.search(text or "")):
        return {"permanent": False, "rate_limited": True, "auth": False}
    if status == 401:
        return {"permanent": True, "rate_limited": False, "auth": True}
    return {"permanent": status in (400, 403), "rate_limited": False, "auth": False}


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
            raise GmailError(f"Gmail access was revoked or expired. Reconnect Gmail in Settings. ({exc})",
                             permanent=True, auth=True) from exc
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


def _service(account):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credentials(account), cache_discovery=False)


def _call(request, what):
    """Run a Gmail API request, turning failures into GmailError."""
    from googleapiclient.errors import HttpError

    try:
        return request.execute()
    except HttpError as exc:
        status = getattr(exc.resp, "status", 0)
        body = (exc.content or b"").decode("utf-8", "replace") if isinstance(exc.content, bytes) else str(exc.content or "")
        kind = classify_http_error(status, f"{exc.reason} {body}")
        raise GmailError(f"Gmail refused to {what} ({status}): {exc.reason}", **kind) from exc
    except GmailError:
        raise
    except Exception as exc:  # network trouble: worth another try later
        raise GmailError(f"Could not reach Gmail: {exc}") from exc


def send_message(account, mime):
    """Send through the Gmail API. Returns {"id", "threadId"}."""
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    sent = _call(_service(account).users().messages().send(userId="me", body={"raw": raw}), "send the email")
    return {"id": sent.get("id", ""), "threadId": sent.get("threadId", "")}


def list_message_ids(account, query, max_pages=5):
    """(message id, thread id) pairs matching a Gmail search, newest first."""
    service = _service(account)
    out, token = [], None
    for _ in range(max_pages):
        resp = _call(service.users().messages().list(userId="me", q=query, maxResults=500, pageToken=token),
                     "list messages")
        out += [(m["id"], m["threadId"]) for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def _plain_text(payload):
    """The text/plain body of a Gmail message payload (falls back to stripped HTML)."""
    def walk(part):
        yield part
        for sub in part.get("parts", []) or []:
            yield from walk(sub)

    def decode(part):
        data = (part.get("body") or {}).get("data", "")
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace") if data else ""

    parts = list(walk(payload))
    for p in parts:
        if p.get("mimeType") == "text/plain" and not p.get("filename"):
            text = decode(p)
            if text.strip():
                return text.strip()
    for p in parts:
        if p.get("mimeType") == "text/html" and not p.get("filename"):
            html = re.sub(r"(?is)<(script|style).*?</\1>", "", decode(p))
            html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
            return re.sub(r"\n{3,}", "\n\n", re.sub(r"<[^>]+>", "", html)).strip()
    return ""


def get_message(account, message_id):
    """One message as {"id", "threadId", "from_email", "from_name", "subject", "snippet", "body", "received_at"}."""
    import html as html_lib

    msg = _call(_service(account).users().messages().get(userId="me", id=message_id, format="full"), "read a message")
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    try:
        received = parsedate_to_datetime(headers["date"])
        if received.tzinfo is None:
            received = received.replace(tzinfo=dt_timezone.utc)
    except (KeyError, TypeError, ValueError):
        received = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=dt_timezone.utc)
    return {
        "id": msg["id"],
        "threadId": msg.get("threadId", ""),
        "from_email": addr.lower(),
        "from_name": name,
        "subject": headers.get("subject", ""),
        "snippet": html_lib.unescape(msg.get("snippet", "")),
        "body": _plain_text(msg.get("payload", {})),
        "received_at": received,
    }
