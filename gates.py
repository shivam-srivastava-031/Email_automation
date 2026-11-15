"""Deterministic checks that stand between a drafted email and a mailbox.

The agentic_deals incidents taught one thing: never let a model's output be
the last word before an outward action. Every gate here is code, is cheap,
and produces a named reason that lands on the decision row.

A gate returns None to pass, or (status, reason) to stop:
  * "blocked"  - a hard rule (unsubscribed, cap, duplicate, off)
  * "held"     - a human should look (human owns the thread, no mailbox)
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from common.models import EMAIL_SOURCE_AI_SDR

from .models import AgentEmailSettings, EmailDecision
from .readiness import resolve_sender

SOURCE = EMAIL_SOURCE_AI_SDR

HELD = EmailDecision.STATUS_HELD
BLOCKED = EmailDecision.STATUS_BLOCKED


def enabled():
    return bool(getattr(settings, "EMAIL_ORCHESTRATOR_ENABLED", True))


def dedup_hours():
    return int(getattr(settings, "EMAIL_ORCHESTRATOR_DEDUP_HOURS", 24))


def gate_enabled(email_settings):
    if not enabled():
        return (BLOCKED, "orchestrator disabled by setting")
    if email_settings is None or email_settings.mode == AgentEmailSettings.MODE_OFF:
        return (BLOCKED, "auto email is off for this agent")
    return None


def gate_mailbox(agent, email_settings):
    resolved = resolve_sender(agent, email_settings)
    if resolved["kind"] is None:
        return (HELD, "no connected mailbox: " + ", ".join(resolved["reasons"] or ["unknown"]))
    return None


def gate_unsubscribed(recipient_email):
    try:
        from common.email_service import _is_unsubscribed
        if recipient_email and _is_unsubscribed(recipient_email):
            return (BLOCKED, "recipient is unsubscribed")
    except Exception:
        return None
    return None


def gate_recent_email(contact, recipient_email, exclude_pk=None):
    """Nobody should get two orchestrator emails in a day, and if a human
    already emailed this person recently the AI stays out of the way."""
    since = timezone.now() - timedelta(hours=dedup_hours())
    qs = EmailDecision.objects.filter(status=EmailDecision.STATUS_SENT, sent_at__gte=since)
    if contact is not None:
        qs = qs.filter(contact=contact)
    elif recipient_email:
        qs = qs.filter(recipient_email__iexact=recipient_email)
    else:
        return None
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        return (BLOCKED, f"already emailed in the last {dedup_hours()}h")
    try:
        from common.models import EmailLog
        human = EmailLog.objects.filter(status=EmailLog.STATUS_SENT, sent_at__gte=since)
        if contact is not None:
            human = human.filter(contact=contact)
        else:
            human = human.filter(to_email__iexact=recipient_email)
        if human.exclude(source=SOURCE).exists():
            return (HELD, "a person already emailed them recently")
    except Exception:
        pass
    return None


def gate_open_decision(contact, exclude_pk=None):
    if contact is None:
        return None
    qs = EmailDecision.objects.filter(contact=contact, status__in=EmailDecision.OPEN_STATUSES)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        return (BLOCKED, "another email for this person is already in progress")
    return None


def gate_daily_cap(agent, email_settings):
    cap = int(getattr(email_settings, "daily_cap", 20) or 20)
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    n = EmailDecision.objects.filter(
        agent=agent, status__in=(EmailDecision.STATUS_SENT, EmailDecision.STATUS_SENDING,
                                 EmailDecision.STATUS_SCHEDULED),
        created_at__gte=start,
    ).count()
    if n >= cap:
        return (BLOCKED, f"daily cap of {cap} reached")
    return None


def gate_human_owns(state):
    """`state` is the parsed Newton ConversationState (dict) or None."""
    if not isinstance(state, dict):
        return None
    if state.get("human_owns_conversation"):
        return (HELD, "a human owns this LinkedIn thread")
    return None


def gate_lead_paused(sdr_lead):
    if sdr_lead is not None and getattr(sdr_lead, "agent_paused", False):
        return (HELD, "the agent is paused for this lead")
    return None


def run_pre_draft_gates(agent, email_settings, contact, sdr_lead, state, exclude_pk=None):
    """Gates that need no model output. Order: cheapest and most final first."""
    for gate in (
        lambda: gate_enabled(email_settings),
        lambda: gate_open_decision(contact, exclude_pk=exclude_pk),
        lambda: gate_lead_paused(sdr_lead),
        lambda: gate_human_owns(state),
        lambda: gate_daily_cap(agent, email_settings),
        lambda: gate_mailbox(agent, email_settings),
    ):
        hit = gate()
        if hit:
            return hit
    return None


def run_pre_send_gates(agent, email_settings, contact, recipient_email, exclude_pk=None):
    """Re-run at send time: the world may have moved during review or the cancel window."""
    for gate in (
        lambda: gate_enabled(email_settings),
        lambda: gate_unsubscribed(recipient_email),
        lambda: gate_recent_email(contact, recipient_email, exclude_pk=exclude_pk),
        lambda: gate_mailbox(agent, email_settings),
    ):
        hit = gate()
        if hit:
            return hit
    return None
