"""Finding and sorting replies: new messages in the Gmail conversations of emails we sent.

Cheap by design: one search lists recent messages that are not from you
(ids only), and only those in a conversation this app started are fetched.
Each reply is then sorted (AI, with a rules fallback) and acted on:
  - out of office      does not count as a reply; follow-ups continue
  - not interested     the contact is unsubscribed
  - interested / info / referral   highlighted, and you get an alert email
  - any real reply     stops the follow-ups for that person
"""
import logging
import re

from django.utils import timezone

from . import ai, gmail, safety
from .models import AppSettings, GmailAccount, Message, Reply
from .render import build_email

log = logging.getLogger(__name__)

SEARCH = "-from:me newer_than:60d"
BOUNCE_SENDER = re.compile(r"^(mailer-daemon|postmaster)@", re.I)
OOO = re.compile(r"(out of (the )?office|on (annual |sick )?leave|on vacation|automatic reply|auto[- ]?reply|"
                 r"away from (my )?(desk|office)|limited access to (my )?email)", re.I)


def is_bounce(from_email, subject):
    return bool(BOUNCE_SENDER.match(from_email or "")) or bool(
        re.search(r"(delivery status notification|undeliverable|delivery has failed|address not found)", subject or "", re.I))


def classify(reply, sent_subject, classifier=ai.classify_reply):
    """Set reply.category and summary: AI when available, simple rules otherwise."""
    text = reply.body or reply.snippet
    if OOO.search(f"{reply.subject}\n{text[:500]}"):
        reply.category, reply.summary = Reply.CAT_OOO, "Automatic out-of-office reply."
        return
    try:
        out = classifier(sent_subject, text)
        reply.category, reply.summary = out["category"], out["summary"]
    except ai.AIError as exc:
        log.info("Reply %s not sorted by AI (%s); using rules", reply.gmail_id, exc)
        reply.category = Reply.CAT_NOT_INTERESTED if safety.OPT_OUT.search(text) else Reply.CAT_OTHER
        reply.summary = ""


def notify(account, reply, sender=gmail.send_message):
    """Email yourself about a promising reply. Never raises."""
    contact = reply.message.contact
    label = reply.get_category_display()
    body = (f"{reply.from_name or reply.from_email} ({contact.company or reply.from_email}) replied: {label}.\n\n"
            f"{reply.summary}\n\n---\n{(reply.body or reply.snippet)[:1500]}\n\n"
            f"Open the conversation in Gmail: {reply.message.gmail_url}")
    html, text = build_email(body)
    try:
        sender(account, gmail.build_mime("Email automation", account.email, account.email,
                                         f"[{label}] Reply from {reply.from_name or reply.from_email}", html, text))
    except gmail.GmailError as exc:
        log.warning("Could not send the reply alert: %s", exc)


def sync_replies(now=None, list_ids=gmail.list_message_ids, fetch=gmail.get_message,
                 classifier=ai.classify_reply, sender=gmail.send_message):
    """Store, sort and act on new replies; returns how many were added. Needs the read-only scope."""
    account = GmailAccount.current()
    if account is None or not account.can_read_replies:
        return 0
    # thread id -> the first email of that conversation (follow-ups share the thread)
    threads = {}
    for thread_id, message_id in (Message.objects.filter(status=Message.STATUS_SENT).exclude(gmail_thread_id="")
                                  .order_by("-step", "-id").values_list("gmail_thread_id", "id")):
        threads[thread_id] = message_id
    app = AppSettings.load()
    added = 0
    if threads:
        known = set(Reply.objects.values_list("gmail_id", flat=True))
        ours = set(Message.objects.exclude(gmail_id="").values_list("gmail_id", flat=True))
        for message_id, thread_id in list_ids(account, SEARCH):
            if thread_id not in threads or message_id in known or message_id in ours:
                continue
            data = fetch(account, message_id)
            if data["from_email"] == account.email.lower():
                continue  # your own reply from Gmail: not an answer from them
            kind = Reply.KIND_BOUNCE if is_bounce(data["from_email"], data["subject"]) else Reply.KIND_REPLY
            reply = Reply(
                message_id=threads[thread_id], gmail_id=message_id, kind=kind,
                from_email=data["from_email"][:320], from_name=data["from_name"][:200],
                subject=data["subject"][:500], snippet=data["snippet"], body=data["body"],
                received_at=data["received_at"],
            )
            if kind == Reply.KIND_REPLY:
                classify(reply, Message.objects.get(pk=threads[thread_id]).subject, classifier)
                if reply.category == Reply.CAT_OOO:
                    reply.is_read = True  # nothing to do; follow-ups carry on
            reply.save()
            if kind == Reply.KIND_REPLY and reply.category != Reply.CAT_OOO:
                # a real answer: stop follow-ups for the whole conversation
                Message.objects.filter(gmail_thread_id=thread_id, replied_at__isnull=True).update(replied_at=data["received_at"])
                Message.objects.filter(campaign=reply.message.campaign, contact=reply.message.contact,
                                       status__in=Message.OPEN_STATUSES).update(status=Message.STATUS_SKIPPED,
                                                                                error="They replied.")
            safety.handle_reply(reply)  # bounce → never email again; opt-out → unsubscribed
            if kind == Reply.KIND_REPLY and reply.category in Reply.POSITIVE and app.notify_interested:
                notify(account, reply, sender)
            added += 1
            known.add(message_id)
    account.last_reply_sync = now or timezone.now()
    account.save(update_fields=["last_reply_sync"])
    if added:
        log.info("Found %s new repl%s", added, "y" if added == 1 else "ies")
    return added
