"""Finding replies: new messages in the Gmail conversations of emails we sent.

Cheap by design: one search lists recent messages that are not from you
(ids only), and only those in a conversation this app started are fetched.
"""
import logging
import re

from django.utils import timezone

from . import gmail, safety
from .models import GmailAccount, Message, Reply

log = logging.getLogger(__name__)

SEARCH = "-from:me newer_than:60d"
BOUNCE_SENDER = re.compile(r"^(mailer-daemon|postmaster)@", re.I)


def is_bounce(from_email, subject):
    return bool(BOUNCE_SENDER.match(from_email or "")) or bool(
        re.search(r"(delivery status notification|undeliverable|delivery has failed|address not found)", subject or "", re.I))


def sync_replies(now=None, list_ids=gmail.list_message_ids, fetch=gmail.get_message):
    """Store new replies; returns how many were added. Needs the read-only scope."""
    account = GmailAccount.current()
    if account is None or not account.can_read_replies:
        return 0
    threads = dict(Message.objects.filter(status=Message.STATUS_SENT).exclude(gmail_thread_id="")
                   .values_list("gmail_thread_id", "id"))
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
            reply = Reply.objects.create(
                message_id=threads[thread_id], gmail_id=message_id, kind=kind,
                from_email=data["from_email"][:320], from_name=data["from_name"][:200],
                subject=data["subject"][:500], snippet=data["snippet"], body=data["body"],
                received_at=data["received_at"],
            )
            safety.handle_reply(reply)  # bounce → never email again; "unsubscribe" → unsubscribed
            if kind == Reply.KIND_REPLY:
                Message.objects.filter(pk=threads[thread_id], replied_at__isnull=True).update(replied_at=data["received_at"])
            added += 1
            known.add(message_id)
    account.last_reply_sync = now or timezone.now()
    account.save(update_fields=["last_reply_sync"])
    if added:
        log.info("Found %s new repl%s", added, "y" if added == 1 else "ies")
    return added
