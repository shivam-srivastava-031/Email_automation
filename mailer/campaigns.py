"""Campaign actions: start, approve, and the send pipeline the worker runs."""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import ai, gmail
from .models import AppSettings, Campaign, GmailAccount, Message, sent_today_count
from .render import build_email, fill

log = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 3


class CampaignError(Exception):
    pass


def check_ready_to_start(campaign):
    """Problems that stop a campaign from starting, in plain words (empty = ready)."""
    problems = []
    if campaign.is_ai:
        if not settings.GEMINI_API_KEY:
            problems.append("The AI is not set up: add GEMINI_API_KEY to the .env file.")
    else:
        if not campaign.subject.strip():
            problems.append("Add a subject.")
        if not campaign.body.strip():
            problems.append("Write the email.")
    if bool(campaign.cta_text.strip()) != bool(campaign.cta_url.strip()):
        problems.append("The button needs both a text and a link (or neither).")
    if not campaign.recipients().exists():
        problems.append("The chosen list has no contacts (unsubscribed contacts are left out).")
    return problems


@transaction.atomic
def start(campaign):
    """Create a message for every recipient not yet in the campaign and set it running.

    Template emails are filled in straight away; AI emails are written by the
    worker. With review_first they wait as drafts, otherwise they are queued.
    Returns how many messages were added.
    """
    problems = check_ready_to_start(campaign)
    if problems:
        raise CampaignError(" ".join(problems))
    already = set(campaign.messages.values_list("contact_id", flat=True))
    ready_status = Message.STATUS_DRAFT if campaign.review_first else Message.STATUS_APPROVED
    new = []
    for contact in campaign.recipients().exclude(pk__in=already):
        if campaign.is_ai:
            new.append(Message(campaign=campaign, contact=contact, status=Message.STATUS_PENDING))
        else:
            values = contact.merge_values()
            new.append(Message(
                campaign=campaign, contact=contact, status=ready_status,
                subject=fill(campaign.subject, values)[:300], body=fill(campaign.body, values),
            ))
    Message.objects.bulk_create(new)
    campaign.status = Campaign.STATUS_RUNNING
    campaign.save(update_fields=["status", "updated_at"])
    return len(new)


def approve(message, subject=None, body=None):
    if message.status not in (Message.STATUS_DRAFT, Message.STATUS_FAILED):
        raise CampaignError("Only drafts and failed emails can be approved.")
    if subject is not None:
        message.subject = subject.strip()[:300]
    if body is not None:
        message.body = body.strip()
    if not message.subject or not message.body:
        raise CampaignError("The email needs a subject and a message.")
    message.status = Message.STATUS_APPROVED
    message.error = ""
    message.attempts = 0
    message.save()


def approve_all_drafts(campaign):
    return campaign.messages.filter(status=Message.STATUS_DRAFT).exclude(subject="").exclude(body="") \
        .update(status=Message.STATUS_APPROVED, updated_at=timezone.now())


def refresh_done(campaign):
    """A running campaign with nothing left to write, review or send is done."""
    if campaign.status == Campaign.STATUS_RUNNING and not campaign.messages.filter(status__in=Message.OPEN_STATUSES).exists():
        campaign.status = Campaign.STATUS_DONE
        campaign.save(update_fields=["status", "updated_at"])


# ── worker steps ────────────────────────────────────────────────────────────

def write_pending(limit=5, writer=ai.write_email):
    """Let the AI write up to `limit` pending emails of running campaigns."""
    app = AppSettings.load()
    written = 0
    pending = (Message.objects.select_related("campaign", "contact")
               .filter(status=Message.STATUS_PENDING, campaign__status=Campaign.STATUS_RUNNING)[:limit])
    for msg in pending:
        try:
            out = writer(msg.campaign, msg.contact, app)
        except ai.AIError as exc:
            msg.attempts += 1
            msg.error = str(exc)
            if msg.attempts >= MAX_SEND_ATTEMPTS:
                msg.status = Message.STATUS_FAILED
            msg.save(update_fields=["attempts", "error", "status", "updated_at"])
            log.warning("AI could not write message %s: %s", msg.pk, exc)
            continue
        msg.subject, msg.body = out["subject"], out["body"]
        msg.status = Message.STATUS_DRAFT if msg.campaign.review_first else Message.STATUS_APPROVED
        msg.error, msg.attempts = "", 0
        msg.save()
        written += 1
    return written


def compose(message, account, app=None):
    """The MIME email for a message, exactly as it will be sent."""
    app = app or AppSettings.load()
    campaign = message.campaign
    html_body, text_body = build_email(message.body, campaign.cta_text, campaign.cta_url, app.signature)
    files = []
    for att in campaign.attachments.all():
        with att.file.open("rb") as fh:
            files.append((att.name, fh.read()))
    return gmail.build_mime(app.sender_name, account.email, message.contact.email,
                            message.subject, html_body, text_body, files)


def send_next(now=None, sender=gmail.send_message):
    """Send at most one queued email, if the daily limit and the pacing allow.

    Returns the Message sent (or failed), or None when nothing was sent.
    """
    now = now or timezone.now()
    account = GmailAccount.current()
    if account is None:
        return None
    app = AppSettings.load()
    if sent_today_count(now) >= app.daily_limit:
        return None
    last = Message.objects.filter(status=Message.STATUS_SENT).order_by("-sent_at").values_list("sent_at", flat=True).first()
    if last and (now - last).total_seconds() < settings.SEND_INTERVAL_SECONDS:
        return None

    msg = (Message.objects.select_related("campaign", "contact")
           .filter(status=Message.STATUS_APPROVED, campaign__status=Campaign.STATUS_RUNNING)
           .order_by("attempts", "id").first())  # list order; retries go to the back
    if msg is None:
        return None
    if msg.contact.unsubscribed:
        msg.status, msg.error = Message.STATUS_SKIPPED, "Contact unsubscribed."
        msg.save(update_fields=["status", "error", "updated_at"])
        refresh_done(msg.campaign)
        return msg

    try:
        result = sender(account, compose(msg, account, app))
    except gmail.GmailError as exc:
        msg.attempts += 1
        msg.error = str(exc)
        if exc.permanent or msg.attempts >= MAX_SEND_ATTEMPTS:
            msg.status = Message.STATUS_FAILED
        msg.save(update_fields=["attempts", "error", "status", "updated_at"])
        log.warning("Sending message %s failed: %s", msg.pk, exc)
    else:
        if not isinstance(result, dict):  # a bare id
            result = {"id": result}
        msg.status, msg.sent_at, msg.error = Message.STATUS_SENT, now, ""
        msg.gmail_id, msg.gmail_thread_id = result.get("id", ""), result.get("threadId", "")
        msg.save(update_fields=["status", "gmail_id", "gmail_thread_id", "sent_at", "error", "updated_at"])
        log.info("Sent message %s to %s", msg.pk, msg.contact.email)
    refresh_done(msg.campaign)
    return msg


def replies_due(now=None):
    account = GmailAccount.current()
    if account is None or not account.can_read_replies:
        return False
    last = account.last_reply_sync
    return last is None or ((now or timezone.now()) - last).total_seconds() >= settings.REPLY_SYNC_SECONDS


def tick(now=None, writer=ai.write_email, sender=gmail.send_message, syncer=None):
    """One worker pass: write some AI drafts, send at most one email, and now and then look for replies."""
    from .replies import sync_replies

    written = write_pending(writer=writer)
    sent = send_next(now=now, sender=sender)
    for campaign in Campaign.objects.filter(status=Campaign.STATUS_RUNNING):
        refresh_done(campaign)
    new_replies = 0
    if replies_due(now):
        try:
            new_replies = (syncer or sync_replies)(now=now)
        except gmail.GmailError as exc:  # never let reply checks stop sending
            log.warning("Checking replies failed: %s", exc)
            GmailAccount.objects.filter(pk=1).update(last_reply_sync=now or timezone.now())
    return {"written": written, "sent": sent, "replies": new_replies}
