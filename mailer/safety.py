"""Automatic protection for the Gmail account: warm-up, flag detection, limit cuts.

Gmail watches new senders closely. Signs that it is (or soon will be) flagging
the account are: a high bounce rate, rate-limit / quota errors, and revoked
access. This module reacts to them without waiting for you:

  warm-up      start at warmup_start emails/day, +warmup_step each healthy day,
               never above daily_limit (the most you ever want)
  bounces      >= 5% of the last 3 days' emails bounced: halve today's limit
               >= 10%: halve it and pause all sending until tomorrow morning
  rate limits  Gmail says "too many / quota": halve the limit, pause until tomorrow
  revoked      access lost: pause until you reconnect and press Resume
  list hygiene bounced addresses are never emailed again; anyone who replies
               "unsubscribe", "remove me", "not interested"... is unsubscribed

Everything it does is written to the safety log (SafetyEvent) shown in Settings.
Opt-outs and bounces are always honoured, even with auto_protect switched off.
"""
import logging
import re
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .models import AppSettings, Message, Reply, SafetyEvent

log = logging.getLogger(__name__)

MIN_LIMIT = 5                 # never cut below this
BOUNCE_WINDOW_DAYS = 3
BOUNCE_MIN_SAMPLE = 20        # too few sends to judge below this
BOUNCE_WARN = 0.05
BOUNCE_PAUSE = 0.10
HEALTHY_BOUNCE = 0.02         # a day at or under this lets the limit grow
RESUME_HOUR = 9               # pauses lift at 9:00 local time the next day
FOREVER = timedelta(days=3650)

OPT_OUT = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt[ -]?out|stop (e-?mailing|sending|contacting)|"
    r"(do not|don'?t) (e-?mail|contact|send)|not interested|no longer interested)\b", re.I)


def event(level, text):
    SafetyEvent.objects.create(level=level, text=text[:500])
    getattr(log, "warning" if level != SafetyEvent.LEVEL_INFO else "info")("Safety: %s", text)


def effective_limit(app=None):
    """Today's limit: the protection's current limit, never above daily_limit."""
    app = app or AppSettings.load()
    if not app.auto_protect:
        return app.daily_limit
    current = app.current_limit or (app.warmup_start if app.warmup else app.daily_limit)
    return max(1, min(app.daily_limit, current))


def is_paused(app=None, now=None):
    app = app or AppSettings.load()
    return bool(app.paused_until and app.paused_until > (now or timezone.now()))


def next_morning(now=None):
    local = timezone.localtime(now or timezone.now())
    tomorrow = (local + timedelta(days=1)).date()
    return timezone.make_aware(datetime.combine(tomorrow, time(RESUME_HOUR)), local.tzinfo)


def pause(app, until, reason):
    app.paused_until, app.pause_reason = until, reason[:500]
    app.save(update_fields=["paused_until", "pause_reason"])
    event(SafetyEvent.LEVEL_CRITICAL, f"Sending paused: {reason}")


def resume(app=None):
    app = app or AppSettings.load()
    app.paused_until, app.pause_reason = None, ""
    app.save(update_fields=["paused_until", "pause_reason"])
    event(SafetyEvent.LEVEL_INFO, "Sending resumed.")


def cut_limit(app, reason, now=None):
    new = max(MIN_LIMIT, effective_limit(app) // 2)
    app.current_limit, app.last_cut_at = new, now or timezone.now()
    app.save(update_fields=["current_limit", "last_cut_at"])
    event(SafetyEvent.LEVEL_WARNING, f"Daily limit lowered to {new}: {reason}")
    return new


# ── reactions ───────────────────────────────────────────────────────────────

def on_send_error(exc, now=None):
    """Called when Gmail refuses an email."""
    app = AppSettings.load()
    if not app.auto_protect:
        return
    now = now or timezone.now()
    if getattr(exc, "rate_limited", False):
        cut_limit(app, "Gmail is limiting how much this account can send.", now)
        pause(app, next_morning(now), f"Gmail rate limit or quota reached ({exc}). Sending restarts tomorrow at {RESUME_HOUR}:00.")
    elif getattr(exc, "auth", False):
        pause(app, now + FOREVER, "Gmail access was revoked or expired. Reconnect Gmail in Settings, then press Resume.")


def bounce_rate(now=None, days=BOUNCE_WINDOW_DAYS):
    """(bounced, sent) for emails sent in the last `days`."""
    since = (now or timezone.now()) - timedelta(days=days)
    sent = Message.objects.filter(status=Message.STATUS_SENT, sent_at__gte=since)
    bounced = sent.filter(replies__kind=Reply.KIND_BOUNCE).distinct().count()
    return bounced, sent.count()


def check_bounces(now=None):
    """Cut or pause when too many recent emails bounce."""
    app = AppSettings.load()
    if not app.auto_protect:
        return None
    now = now or timezone.now()
    bounced, sent = bounce_rate(now)
    if sent < BOUNCE_MIN_SAMPLE:
        return None
    rate = bounced / sent
    cut_today = app.last_cut_at and timezone.localdate(app.last_cut_at) == timezone.localdate(now)
    if rate >= BOUNCE_PAUSE and not is_paused(app, now):
        if not cut_today:
            cut_limit(app, f"{rate:.0%} of recent emails bounced.", now)
        pause(app, next_morning(now),
              f"{bounced} of the last {sent} emails bounced ({rate:.0%}). Clean the list (bounced addresses are already "
              f"excluded) before sending more. Restarts tomorrow at {RESUME_HOUR}:00.")
        return "paused"
    if rate >= BOUNCE_WARN and not cut_today:
        cut_limit(app, f"{bounced} of the last {sent} emails bounced ({rate:.0%}).", now)
        return "cut"
    return None


def handle_reply(reply):
    """Bounces and opt-outs: stop emailing that address (always, even with protection off)."""
    contact = reply.message.contact
    open_msgs = Message.objects.filter(contact=contact, status__in=Message.OPEN_STATUSES)
    if reply.kind == Reply.KIND_BOUNCE:
        if not contact.bounced:
            contact.bounced = True
            contact.save(update_fields=["bounced", "updated_at"])
            open_msgs.update(status=Message.STATUS_SKIPPED, error="Address bounced earlier.")
            event(SafetyEvent.LEVEL_INFO, f"{contact.email} bounced; it will not be emailed again.")
        return "bounced"
    if OPT_OUT.search(f"{reply.subject}\n{reply.body or reply.snippet}") and not contact.unsubscribed:
        contact.unsubscribed = True
        contact.save(update_fields=["unsubscribed", "updated_at"])
        open_msgs.update(status=Message.STATUS_SKIPPED, error="Contact asked not to be emailed.")
        event(SafetyEvent.LEVEL_INFO, f"{contact.email} asked not to be emailed; unsubscribed.")
        return "unsubscribed"
    return None


def daily_adjust(now=None):
    """Once a day: start the warm-up, or raise the limit after a healthy day."""
    app = AppSettings.load()
    now = now or timezone.now()
    today = timezone.localdate(now)
    if app.last_adjust_date == today:
        return None
    app.last_adjust_date = today
    app.save(update_fields=["last_adjust_date"])
    if not app.auto_protect:
        return None
    if app.current_limit is None:
        app.current_limit = min(app.daily_limit, app.warmup_start if app.warmup else app.daily_limit)
        app.save(update_fields=["current_limit"])
        if app.warmup and app.current_limit < app.daily_limit:
            event(SafetyEvent.LEVEL_INFO, f"Warm-up started: {app.current_limit} emails a day, rising by "
                                          f"{app.warmup_step} each healthy day up to {app.daily_limit}.")
        return "started"
    if app.current_limit >= app.daily_limit:
        return None
    # A healthy yesterday: something was sent, few bounces, no warnings since.
    start = timezone.make_aware(datetime.combine(today - timedelta(days=1), time.min), timezone.get_current_timezone())
    end = start + timedelta(days=1)
    sent = Message.objects.filter(status=Message.STATUS_SENT, sent_at__gte=start, sent_at__lt=end)
    if not sent.exists():
        return None
    bounced = sent.filter(replies__kind=Reply.KIND_BOUNCE).distinct().count()
    troubled = SafetyEvent.objects.filter(created_at__gte=now - timedelta(hours=24)).filter(
        Q(level=SafetyEvent.LEVEL_WARNING) | Q(level=SafetyEvent.LEVEL_CRITICAL)).exists()
    if troubled or is_paused(app, now) or bounced / sent.count() > HEALTHY_BOUNCE:
        return None
    new = min(app.daily_limit, app.current_limit + max(1, app.warmup_step))
    app.current_limit = new
    app.save(update_fields=["current_limit"])
    event(SafetyEvent.LEVEL_INFO, f"Yesterday looked healthy; daily limit raised to {new}.")
    return "raised"
