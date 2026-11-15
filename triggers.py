"""An inbound LinkedIn message becomes an EmailDecision row when it looks like
the prospect wants something by email.

Cheap by design: this runs in the request that persisted the message, so it
does regex and two lookups, never a model call. Most tenants exit on the
first check (mode off). The row is judged later by the evaluate task, after
a short delay so "here is my address" and "send me the details" a few
minutes apart become one email, not two.
"""
import logging
import re

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .engine import EMAIL_RE, parse_state
from .models import AgentEmailSettings, EmailDecision

logger = logging.getLogger(__name__)

ASK_RE = re.compile(
    r"\b(e-?mail|mail|inbox|send (?:it|me|us|over|across|the|those|these|details|deck|a|an|your)|"
    r"share (?:it|the|those|these|details|over|your|a|an)|drop (?:it|me)|forward|"
    r"details over|over the mail|on mail|by mail|via mail)\b",
    re.IGNORECASE,
)


def enabled():
    return bool(getattr(settings, "EMAIL_ORCHESTRATOR_ENABLED", True))


def debounce_seconds():
    return int(getattr(settings, "EMAIL_ORCHESTRATOR_DEBOUNCE_SECONDS", 150))


def looks_like_email_ask(text, state=None):
    text = str(text or "")
    if EMAIL_RE.search(text):
        return True
    if ASK_RE.search(text):
        return True
    if isinstance(state, dict) and state.get("captured_email") and state.get("last_asked_for_artifact"):
        return True
    return False


@receiver(post_save, sender="ai_sdr.ConversationMessage")
def on_linkedin_message(sender, instance, created, **kwargs):
    if not created or instance.direction != "inbound" or not enabled():
        return
    try:
        transaction.on_commit(lambda: consider_message(instance.pk), robust=True)
    except Exception:
        logger.exception("email orchestrator trigger dispatch failed for message %s", instance.pk)


def consider_message(message_id):
    from ai_sdr.models import ConversationMessage
    from lead.models import Contact

    msg = (
        ConversationMessage.objects.select_related("conversation__lead__agent")
        .filter(pk=message_id).first()
    )
    if msg is None:
        return None
    conv = msg.conversation
    lead = getattr(conv, "lead", None)
    agent = getattr(lead, "agent", None)
    if lead is None or agent is None:
        return None

    email_settings = AgentEmailSettings.objects.filter(agent=agent).only("id", "mode").first()
    if email_settings is None or email_settings.mode == AgentEmailSettings.MODE_OFF:
        return None

    state = parse_state(conv)
    if not looks_like_email_ask(msg.content, state):
        return None

    contact = Contact.objects.filter(pk=lead.crm_record_id).first() if lead.crm_record_id else None
    business_id = agent.business_record_id or (contact.business_id if contact is not None else None)
    if not business_id:
        return None

    open_qs = EmailDecision.objects.filter(status__in=EmailDecision.OPEN_STATUSES)
    if contact is not None:
        open_qs = open_qs.filter(contact=contact)
    else:
        open_qs = open_qs.filter(sdr_lead_id=lead.pk)
    existing = open_qs.order_by("-id").first()
    if existing is not None:
        # One email per burst. The evaluator re-reads the whole thread, so the
        # new message is seen; we only record that it arrived.
        raw = dict(existing.raw or {})
        raw.setdefault("extra_triggers", []).append(f"ConversationMessage:{msg.pk}")
        EmailDecision.objects.filter(pk=existing.pk).update(raw=raw)
        return existing

    deal = None
    if contact is not None:
        try:
            from agentic_deals.extraction import resolve_links
            deal = resolve_links(contact)[0]
        except Exception:
            deal = None

    decision, created_now = EmailDecision.objects.get_or_create(
        trigger_source=EmailDecision.TRIGGER_LINKEDIN,
        trigger_ref=f"ConversationMessage:{msg.pk}",
        defaults={
            "agent": agent, "business_id": business_id, "contact": contact, "deal": deal,
            "sdr_lead_id": lead.pk, "conversation_id": conv.pk,
            "trigger_text": (msg.content or "")[:2000],
            "status": EmailDecision.STATUS_EVALUATING,
        },
    )
    if not created_now:
        return decision
    from .tasks import evaluate_decision
    try:
        evaluate_decision.apply_async(args=[decision.pk], countdown=debounce_seconds())
    except Exception:
        logger.exception("could not enqueue evaluation for decision %s", decision.pk)
    return decision
