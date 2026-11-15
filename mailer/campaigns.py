"""Campaign actions and the fully automatic pipeline the worker runs.

Per pass (python manage.py run_worker):
  1. daily_adjust       warm-up / recovery of the daily limit (safety.py)
  2. write_pending      the AI writes first emails and follow-ups
  3. schedule_followups no reply after N days -> queue the next follow-up
  4. send_next          one email, inside business hours, within the limit
  5. replies            now and then: fetch replies, sort them with AI, react
Emails that pass the quality check (quality.py) go out on their own unless
the campaign asks to review everything; the rest wait under "To review".
"""
import logging
import re
import time as time_mod
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import ai, gmail, quality, safety
from .models import AppSettings, Campaign, GmailAccount, Message, sent_today_count
from .render import build_email, fill

log = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 3
FOLLOWUP_CHECK_SECONDS = 300
DEFAULT_FOLLOWUP = ("Hi {{first_name}},\n\nJust following up on my email below in case it got buried. "
                    "I'd be glad to share more details or have a quick call whenever it suits you.\n\nThank you for your time.")
_last_followup_check = 0.0


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
        problems.append("The chosen list has no contacts (unsubscribed and bounced contacts are left out).")
    return problems


def gate(campaign, contact, subject, body, followup=False):
    """(status, note) for a written email: queued if it may go out on its own, else a draft."""
    if campaign.review_first:
        return Message.STATUS_DRAFT, ""
    problems = quality.check(subject, body, contact, followup=followup)
    if problems:
        return Message.STATUS_DRAFT, "Held for review: " + "; ".join(problems) + "."
    return Message.STATUS_APPROVED, ""


def reply_subject(subject):
    return "Re: " + re.sub(r"^(re:\s*)+", "", subject or "", flags=re.I).strip()


@transaction.atomic
def start(campaign):
    """Create a first email for every recipient not yet in the campaign and set it running.

    Template emails are filled in straight away; AI emails are written by the
    worker. Returns how many emails were added.
    """
    problems = check_ready_to_start(campaign)
    if problems:
        raise CampaignError(" ".join(problems))
    already = set(campaign.messages.filter(step=0).values_list("contact_id", flat=True))
    new = []
    for contact in campaign.recipients().exclude(pk__in=already):
        if campaign.is_ai:
            new.append(Message(campaign=campaign, contact=contact, status=Message.STATUS_PENDING))
        else:
            values = contact.merge_values()
            subject, body = fill(campaign.subject, values)[:300], fill(campaign.body, values)
            status, note = gate(campaign, contact, subject, body)
            new.append(Message(campaign=campaign, contact=contact, status=status, subject=subject, body=body, error=note))
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
        .update(status=Message.STATUS_APPROVED, error="", updated_at=timezone.now())


# ── follow-ups ──────────────────────────────────────────────────────────────

def followup_candidates(campaign, now=None, due_only=True):
    """Sent emails that should get a follow-up (now, or later if due_only=False)."""
    if not campaign.followups or campaign.max_followups < 1:
        return []
    qs = Message.objects.filter(
        campaign=campaign, status=Message.STATUS_SENT, step__lt=campaign.max_followups, replied_at__isnull=True,
        contact__unsubscribed=False, contact__bounced=False,
    ).exclude(gmail_thread_id="")
    if due_only:
        qs = qs.filter(sent_at__lte=(now or timezone.now()) - timedelta(days=campaign.followup_days))
    taken = set(campaign.messages.values_list("contact_id", "step"))
    return [m for m in qs.select_related("contact", "parent") if (m.contact_id, m.step + 1) not in taken]


@transaction.atomic
def schedule_followups(now=None):
    """Queue the next follow-up for every email that got no reply in time. Returns how many."""
    created = 0
    for campaign in Campaign.objects.filter(status=Campaign.STATUS_RUNNING, followups=True):
        for prev in followup_candidates(campaign, now):
            root = prev.parent or prev
            msg = Message(campaign=campaign, contact=prev.contact, step=prev.step + 1, parent=root,
                          subject=reply_subject(root.subject), status=Message.STATUS_PENDING)
            if not campaign.is_ai:
                msg.body = fill(campaign.followup_body.strip() or DEFAULT_FOLLOWUP, prev.contact.merge_values())
                msg.status, msg.error = gate(campaign, prev.contact, msg.subject, msg.body, followup=True)
            msg.save()
            created += 1
    if created:
        log.info("Queued %s follow-up(s)", created)
    return created


def chain(message):
    """The emails already sent in this conversation, oldest first."""
    root = message.parent or message
    return list(Message.objects.filter(campaign=message.campaign, contact=message.contact, status=Message.STATUS_SENT,
                                       step__lt=message.step).order_by("step")) if message.step else [root]


# ── keeping campaign status right ───────────────────────────────────────────

def refresh_done(campaign):
    """A running campaign with nothing left to write, review, send or follow up is done."""
    if campaign.status != Campaign.STATUS_RUNNING:
        return
    if campaign.messages.filter(status__in=Message.OPEN_STATUSES).exists():
        return
    if followup_candidates(campaign, due_only=False):
        return  # still waiting to see if a follow-up is needed
    campaign.status = Campaign.STATUS_DONE
    campaign.save(update_fields=["status", "updated_at"])


# ── worker steps ────────────────────────────────────────────────────────────

def write_pending(limit=5, writer=ai.write_email, followup_writer=ai.write_followup):
    """Let the AI write up to `limit` pending emails (first emails and follow-ups)."""
    app = AppSettings.load()
    written = 0
    pending = (Message.objects.select_related("campaign", "contact", "parent")
               .filter(status=Message.STATUS_PENDING, campaign__status=Campaign.STATUS_RUNNING)
               .order_by("id")[:limit])
    for msg in pending:
        try:
            if msg.step:
                history = [(m.subject, m.body) for m in chain(msg)]
                out = followup_writer(msg.campaign, msg.contact, app, history, msg.step)
                msg.body = out["body"]
                msg.subject = msg.subject or reply_subject((msg.parent or msg).subject)
            else:
                out = writer(msg.campaign, msg.contact, app)
                msg.subject, msg.body = out["subject"], out["body"]
        except ai.AIError as exc:
            msg.attempts += 1
            msg.error = str(exc)
            if msg.attempts >= MAX_SEND_ATTEMPTS:
                msg.status = Message.STATUS_FAILED
            msg.save(update_fields=["attempts", "error", "status", "updated_at"])
            log.warning("AI could not write message %s: %s", msg.pk, exc)
            continue
        msg.status, msg.error = gate(msg.campaign, msg.contact, msg.subject, msg.body, followup=bool(msg.step))
        msg.attempts = 0
        msg.save()
        written += 1
    return written


def compose(message, account, app=None):
    """The MIME email for a message, exactly as it will be sent."""
    app = app or AppSettings.load()
    campaign = message.campaign
    html_body, text_body = build_email(message.body, campaign.cta_text, campaign.cta_url, app.signature)
    files = []
    if not message.step:  # the resume/files go with the first email only
        for att in campaign.attachments.all():
            with att.file.open("rb") as fh:
                files.append((att.name, fh.read()))
    if not message.rfc_message_id:
        message.rfc_message_id = gmail.new_message_id(account.email)
    in_reply_to = references = None
    if message.step:
        earlier = [m.rfc_message_id for m in chain(message) if m.rfc_message_id]
        if earlier:
            in_reply_to, references = earlier[-1], " ".join(earlier)
    return gmail.build_mime(app.sender_name, account.email, message.contact.email, message.subject, html_body,
                            text_body, files, message_id=message.rfc_message_id,
                            in_reply_to=in_reply_to, references=references)


def send_next(now=None, sender=gmail.send_message):
    """Send at most one queued email, if business hours, the daily limit and the pacing allow.

    Returns the Message sent (or failed), or None when nothing was sent.
    """
    now = now or timezone.now()
    account = GmailAccount.current()
    if account is None:
        return None
    app = AppSettings.load()
    if (safety.is_paused(app, now) or not safety.in_send_window(app, now)
            or sent_today_count(now) >= safety.effective_limit(app)):
        return None
    last = Message.objects.filter(status=Message.STATUS_SENT).order_by("-sent_at").values_list("sent_at", flat=True).first()
    if last and (now - last).total_seconds() < settings.SEND_INTERVAL_SECONDS:
        return None

    msg = (Message.objects.select_related("campaign", "contact", "parent")
           .filter(status=Message.STATUS_APPROVED, campaign__status=Campaign.STATUS_RUNNING)
           .order_by("attempts", "id").first())  # list order; retries go to the back
    if msg is None:
        return None
    stop = ("Contact unsubscribed." if msg.contact.unsubscribed else "Address bounced earlier." if msg.contact.bounced
            else "They already replied." if msg.step and Message.objects.filter(
                campaign=msg.campaign, contact=msg.contact, replied_at__isnull=False).exists() else "")
    if stop:
        msg.status, msg.error = Message.STATUS_SKIPPED, stop
        msg.save(update_fields=["status", "error", "updated_at"])
        refresh_done(msg.campaign)
        return msg

    thread_id = (msg.parent.gmail_thread_id if msg.step and msg.parent_id else "") or ""
    try:
        mime = compose(msg, account, app)
        result = sender(account, mime, thread_id=thread_id) if thread_id else sender(account, mime)
    except gmail.GmailError as exc:
        msg.error = str(exc)
        if not exc.rate_limited:  # a Gmail limit is not this email's fault: keep it queued as is
            msg.attempts += 1
            if exc.permanent or msg.attempts >= MAX_SEND_ATTEMPTS:
                msg.status = Message.STATUS_FAILED
        msg.save(update_fields=["attempts", "error", "status", "updated_at"])
        log.warning("Sending message %s failed: %s", msg.pk, exc)
        safety.on_send_error(exc, now)
    else:
        if not isinstance(result, dict):  # a bare id
            result = {"id": result}
        msg.status, msg.sent_at, msg.error = Message.STATUS_SENT, now, ""
        msg.gmail_id = result.get("id", "")
        msg.gmail_thread_id = result.get("threadId", "") or thread_id
        msg.save(update_fields=["status", "gmail_id", "gmail_thread_id", "rfc_message_id", "sent_at", "error", "updated_at"])
        log.info("Sent %s %s to %s", f"follow-up {msg.step}" if msg.step else "message", msg.pk, msg.contact.email)
    refresh_done(msg.campaign)
    return msg


def replies_due(now=None):
    account = GmailAccount.current()
    if account is None or not account.can_read_replies:
        return False
    last = account.last_reply_sync
    return last is None or ((now or timezone.now()) - last).total_seconds() >= settings.REPLY_SYNC_SECONDS


def tick(now=None, writer=ai.write_email, sender=gmail.send_message, syncer=None, followup_writer=ai.write_followup):
    """One worker pass: see the module docstring."""
    global _last_followup_check
    from .replies import sync_replies

    safety.daily_adjust(now)
    written = write_pending(writer=writer, followup_writer=followup_writer)
    followups = 0
    if time_mod.monotonic() - _last_followup_check >= FOLLOWUP_CHECK_SECONDS or now is not None:
        _last_followup_check = time_mod.monotonic()
        followups = schedule_followups(now)
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
            if exc.auth:
                safety.on_send_error(exc, now)
        else:
            safety.check_bounces(now)
    return {"written": written, "followups": followups, "sent": sent, "replies": new_replies}
