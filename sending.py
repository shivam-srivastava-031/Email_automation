"""Scheduling, cancelling and actually sending one decision, then closing the
loop: recipient onto the Contact, a note on the timeline, the deal's open
"they asked for info" signal resolved, and the verdict re-thought.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from common.email_service import send_email
from common.models import EMAIL_SOURCE_AI_SDR, EmailLog

from . import gates
from .models import AgentEmailSettings, EmailAsset, EmailDecision
from .readiness import resolve_sender

logger = logging.getLogger(__name__)


def dispatch_send(decision, countdown_seconds=0, approved_by=None, eta=None):
    from .tasks import send_decision_task

    when = eta or (timezone.now() + timedelta(seconds=max(0, int(countdown_seconds))))
    decision.status = EmailDecision.STATUS_SCHEDULED
    decision.scheduled_at = when
    decision.cancel_deadline_at = when
    if approved_by is not None:
        decision.reviewed_by = approved_by
        decision.reviewed_at = timezone.now()
    decision.error = ""
    decision.save()
    try:
        res = send_decision_task.apply_async(args=[decision.pk], eta=when)
        task_id = getattr(res, "id", "") or ""
    except Exception as exc:  # broker down: leave it scheduled, the sweep can pick it up later
        logger.exception("could not enqueue send for decision %s", decision.pk)
        EmailDecision.objects.filter(pk=decision.pk).update(error=f"enqueue failed: {exc}"[:2000])
        return decision
    # Only this column: under eager Celery the task has already run and moved
    # the status on; a full save() here would put 'scheduled' back.
    EmailDecision.objects.filter(pk=decision.pk).update(celery_task_id=task_id)
    return decision


def cancel_send(decision, reason="", by=None):
    if decision.celery_task_id:
        try:
            from celery import current_app
            current_app.control.revoke(decision.celery_task_id)
        except Exception:
            logger.warning("revoke failed for decision %s", decision.pk, exc_info=True)
    updated = EmailDecision.objects.filter(
        pk=decision.pk, status__in=(EmailDecision.STATUS_SCHEDULED, EmailDecision.STATUS_AWAITING_REVIEW,
                                     EmailDecision.STATUS_HELD),
    ).update(
        status=EmailDecision.STATUS_CANCELLED, hold_reason=(reason or "cancelled")[:300],
        reviewed_by=by, reviewed_at=timezone.now(),
    )
    return bool(updated)


# --------------------------------------------------------------------------

def _absolute_media_url(path_or_url):
    if not path_or_url:
        return ""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    host = (getattr(settings, "SERVER_HOST", "") or "").rstrip("/")
    return f"{host}{path_or_url}" if path_or_url.startswith("/") else f"{host}/{path_or_url}"


def build_payload(decision):
    """-> (attachments [(name, bytes, mime)], extra_links [(name, url)])"""
    cap = int(getattr(settings, "EMAIL_ORCHESTRATOR_MAX_ATTACHMENT_MB", 10)) * 1024 * 1024
    attachments, links = [], []
    ids = [i for i in (decision.asset_ids or []) if isinstance(i, int)]
    # Ownership, not just existence: only this agent's own items and its
    # team's (agent NULL, same business). An id that slipped into asset_ids
    # from anywhere else - another agent, another tenant - is dropped here,
    # at the last moment before bytes leave the building. Deactivating an
    # item after approval does not pull it: the rep approved this exact
    # email. Deleting it does.
    if not decision.agent_id:
        return [], []
    mine = Q(agent_id=decision.agent_id) | Q(agent__isnull=True, business_id=decision.business_id)
    rows = {a.pk: a for a in EmailAsset.objects.filter(mine, pk__in=ids)}
    for i in ids:
        a = rows.get(i)
        if a is None:
            continue
        if a.kind == EmailAsset.KIND_FILE and a.file:
            if a.file_size and a.file_size > cap:
                links.append((a.name, _absolute_media_url(a.file.url)))
                continue
            try:
                a.file.open("rb")
                content = a.file.read()
                a.file.close()
            except Exception:
                logger.exception("could not read asset %s", a.pk)
                links.append((a.name, _absolute_media_url(a.file.url)))
                continue
            attachments.append((a.filename or a.name, content, a.mime_type or "application/octet-stream"))
        elif a.kind == EmailAsset.KIND_LINK and a.url:
            if a.url.rstrip("/").lower() not in (decision.body_html or "").lower():
                links.append((a.name, a.url))
    return attachments, links


def _signature_html(resolved):
    if resolved.get("shared"):
        # The admin's mailbox is only the carrier. The prospect talked to the
        # rep and the body is in the rep's voice, so the admin's own Gmail
        # signature (their title, their phone) would contradict it.
        owner = resolved.get("user")
        name = " ".join(
            p for p in (getattr(owner, "first_name", ""), getattr(owner, "last_name", "")) if p
        ).strip()
        return f"<p>{name}</p>" if name else ""
    gi = resolved.get("gmail_integration")
    sign = (getattr(gi, "gmail_sign", "") or "").strip() if gi is not None else ""
    if sign:
        host = (getattr(settings, "SERVER_HOST", "") or "").rstrip("/")
        return sign.replace('src="/media/', f'src="{host}/media/')
    name = resolved.get("from_name") or ""
    return f"<p>{name}</p>" if name else ""


def compose_html(decision, resolved, links):
    body = decision.body_html or ""
    if links:
        items = "".join(f'<li><a href="{url}">{name}</a></li>' for name, url in links)
        body += f"<p>Links:</p><ul>{items}</ul>"
    sig = _signature_html(resolved)
    if sig:
        body += "<br>" + sig
    return body


def transport_kwargs(resolved):
    kind = resolved.get("kind")
    if kind == "gmail":
        return {"gmail_integration": resolved["gmail_integration"]}
    sc = resolved.get("sender_config")
    if sc is None:
        return {}
    if sc.use_gmail and sc.gmail_integration_id:
        return {"gmail_integration": sc.gmail_integration}
    if sc.use_resend:
        return {"use_resend": True, "resend_from_email": sc.email, "resend_api_key": sc.resend_api_key or None}
    return {"smtp_config": {
        "host": sc.smtp_host, "port": sc.smtp_port, "username": sc.email, "password": sc.password,
        "security": sc.smtp_security, "from_email": sc.email,
    }}


def perform_send(decision_id):
    # Claim the row: exactly one worker may move scheduled -> sending.
    claimed = EmailDecision.objects.filter(
        pk=decision_id, status=EmailDecision.STATUS_SCHEDULED,
    ).update(status=EmailDecision.STATUS_SENDING)
    if not claimed:
        logger.info("decision %s not scheduled any more; nothing to send", decision_id)
        return None
    decision = EmailDecision.objects.select_related("agent", "contact", "business").get(pk=decision_id)
    agent = decision.agent
    email_settings = AgentEmailSettings.objects.filter(agent=agent).select_related("sender_config").first() if agent else None
    if agent is None or email_settings is None:
        return _fail(decision, "agent or settings missing", EmailDecision.STATUS_BLOCKED)

    hit = gates.run_pre_send_gates(agent, email_settings, decision.contact, decision.recipient_email, exclude_pk=decision.pk)
    if hit:
        decision.status, decision.hold_reason = hit
        decision.reasons = list(decision.reasons or []) + [f"gate at send: {hit[1]}"]
        decision.save()
        return decision

    resolved = resolve_sender(agent, email_settings)
    if resolved["kind"] is None:
        return _fail(decision, "no mailbox at send time", EmailDecision.STATUS_HELD)

    attachments, links = build_payload(decision)
    html = compose_html(decision, resolved, links)
    transport = transport_kwargs(resolved)
    # send_email looks up a Gmail account from `user` whenever no
    # gmail_integration is passed - and then prefers Gmail over the SMTP
    # config. So an agent set to send through an SMTP or Resend sender used
    # to go out through the owner's Gmail instead, silently. Pass no user
    # unless a Gmail account is explicitly chosen; sent_by is set below.
    owner = resolved.get("user")
    send_user = owner if transport.get("gmail_integration") is not None else None
    try:
        result = send_email(
            to_email=decision.recipient_email,
            subject=decision.subject,
            html_body=html,
            source=EMAIL_SOURCE_AI_SDR,
            source_id=str(decision.pk),
            contact_id=decision.contact_id,
            business_id=decision.business_id,
            user=send_user,
            attachments=attachments or None,
            **transport,
        )
    except Exception as exc:
        logger.exception("send_email raised for decision %s", decision.pk)
        return _fail(decision, f"send raised: {exc}")

    log = EmailLog.objects.filter(source=EMAIL_SOURCE_AI_SDR, source_id=str(decision.pk)).order_by("-id").first()
    if log is not None and send_user is None and owner is not None and log.sent_by_id is None:
        EmailLog.objects.filter(pk=log.pk).update(sent_by=owner)
    decision.email_log = log
    if not result.get("success"):
        err = result.get("error") or "send failed"
        if result.get("suppressed"):
            return _fail(decision, err, EmailDecision.STATUS_BLOCKED)
        return _fail(decision, err)

    decision.status = EmailDecision.STATUS_SENT
    decision.sent_at = timezone.now()
    decision.error = ""
    raw = dict(decision.raw or {})
    raw["from_email"] = result.get("from_email") or resolved.get("from_email") or ""
    raw["via"] = result.get("via")
    raw["mailbox_fallback"] = bool(resolved.get("shared"))
    raw["attachments"] = [name for name, _b, _m in attachments]
    raw["links"] = [url for _n, url in links]
    decision.raw = raw
    decision.save()
    try:
        after_send(decision, resolved, attachments, links)
    except Exception:
        logger.exception("after_send failed for decision %s", decision.pk)
    return decision


def _fail(decision, error, status=EmailDecision.STATUS_FAILED):
    decision.status = status
    decision.error = str(error)[:2000]
    if status != EmailDecision.STATUS_FAILED:
        decision.hold_reason = str(error)[:300]
    decision.save()
    return decision


# --------------------------------------------------------------------------
# Closing the loop

def write_back_contact_email(contact, address):
    if contact is None or not address:
        return False
    address = address.strip()
    known = {(contact.email or "").lower(), (getattr(contact, "secondary_email", "") or "").lower(),
             (getattr(contact, "tertiary_email", "") or "").lower()}
    if address.lower() in known:
        return False
    fields = []
    if not contact.email:
        contact.email = address
        fields.append("email")
        if hasattr(contact, "primary_email_source"):
            contact.primary_email_source = "conversation"
            fields.append("primary_email_source")
    elif hasattr(contact, "secondary_email") and not contact.secondary_email:
        contact.secondary_email = address
        fields.append("secondary_email")
    elif hasattr(contact, "tertiary_email") and not contact.tertiary_email:
        contact.tertiary_email = address
        fields.append("tertiary_email")
    if not fields:
        return False
    try:
        contact.save(update_fields=fields)
    except Exception:
        # primary_email_source may have choices this value is not in; retry without it.
        logger.warning("contact email write-back retry without source for %s", contact.pk, exc_info=True)
        contact.save(update_fields=[f for f in fields if f != "primary_email_source"])
    return True


def timeline_note_html(decision, attachments, links):
    import html as _h
    parts = [f"<p><strong>AI SDR sent an email</strong> to {_h.escape(decision.recipient_email)}</p>",
             f"<p>Subject: {_h.escape(decision.subject)}</p>"]
    if attachments:
        parts.append("<p>Attached: " + ", ".join(_h.escape(n) for n, _b, _m in attachments) + "</p>")
    if links:
        parts.append("<p>Links: " + ", ".join(_h.escape(n) for n, _u in links) + "</p>")
    if decision.ask_summary:
        parts.append(f"<p><em>They asked for: {_h.escape(decision.ask_summary)}</em></p>")
    return "".join(parts)


def after_send(decision, resolved, attachments, links):
    contact = decision.contact
    owner = resolved.get("user")
    write_back_contact_email(contact, decision.recipient_email)

    if contact is not None:
        try:
            from common.helpers import UniversalLogger
            UniversalLogger(contact, user=owner).log("custom_log", extra_info={
                "custom_log_message": timeline_note_html(decision, attachments, links),
                "direction": "outbound",
                "source": EMAIL_SOURCE_AI_SDR,
                "email_decision_id": decision.pk,
                "email_log_id": decision.email_log_id,
            })
        except Exception:
            logger.exception("timeline note failed for decision %s", decision.pk)

        try:
            from agentic_deals.models import DealSignal
            DealSignal.objects.filter(
                contact=contact, status__in=DealSignal.LIVE_STATUSES,
                kind__in=(DealSignal.KIND_INFO_REQUESTED, DealSignal.KIND_OUR_PROMISE),
            ).update(status=DealSignal.STATUS_DONE, resolved_at=timezone.now(), resolution_reason="email_sent")
        except Exception:
            logger.exception("signal resolution failed for decision %s", decision.pk)

        try:
            from agentic_deals.extraction import resolve_links
            from agentic_deals.tasks import schedule_rethink
            deal = decision.deal or resolve_links(contact)[0]
            if deal is not None:
                if decision.deal_id is None:
                    EmailDecision.objects.filter(pk=decision.pk).update(deal=deal)
                schedule_rethink(deal.pk)
        except Exception:
            logger.exception("rethink scheduling failed for decision %s", decision.pk)
