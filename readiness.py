"""Can this agent send email at all, and as whom?

Answers the question the UI toggle and every send both need, in one place.
The mailbox is resolved in this order:
  1. an explicit outreach.SenderConfiguration chosen in the agent's settings
     (SMTP / Resend / Gmail-backed), if it is structurally usable;
  2. the business admin's connected Gmail, when the team shares it - the
     admin's switch AND this agent's switch are both on (sharing.py). If the
     admin's mailbox cannot send, the person's own is used instead;
  3. the agent owner's own connected Gmail (integration.GmailIntegration is
     one-per-User; the owner is the agent's partner user, else the business
     owner).
There is deliberately no fallback to the platform's system mailbox: a
prospect must receive mail from a person at the business, never from
noreply@.

`user` in the result is always the agent OWNER (the rep). It is who the email
is attributed to - the timeline note, EmailLog.sent_by. `mailbox_user` is
whose mailbox actually carries it, which differs only when `shared`.
"""
import logging

from .sharing import _personal, is_admin_agent, team_of, uses_team_content, uses_team_mailbox

logger = logging.getLogger(__name__)

CONNECT_URL = "/agency/integration"

REASON_NO_OWNER = "no_owner_user"
REASON_NO_MAILBOX = "no_mailbox"
REASON_SENDER_MISCONFIGURED = "sender_misconfigured"
REASON_TOKEN_MISSING = "gmail_refresh_token_missing"

REASON_ADMIN_NO_MAILBOX = "admin_no_mailbox"

REASON_TEXT = {
    REASON_NO_OWNER: "This agent has no owner user, so there is nobody to send as.",
    REASON_NO_MAILBOX: "The agent owner has not connected an email account.",
    REASON_SENDER_MISCONFIGURED: "The chosen sender is missing its host, password or linked Gmail.",
    REASON_TOKEN_MISSING: "The connected Gmail has no refresh token; reconnect it.",
    REASON_ADMIN_NO_MAILBOX: "The admin has not connected a usable email account.",
}


def _usable_gmail(user):
    """The user's GmailIntegration if it can send, else (None, reason)."""
    from integration.models import GmailIntegration
    if user is None:
        return None, REASON_NO_OWNER
    gi = GmailIntegration.objects.filter(user=user).first()
    if gi is None:
        return None, REASON_NO_MAILBOX
    if not (gi.refresh_token or "").strip():
        return None, REASON_TOKEN_MISSING
    return gi, None


def _full_name(user):
    return " ".join(
        p for p in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if p
    ).strip()


def admin_user(agent):
    """The business admin: the BusinessRecord's own user. None when unknown."""
    try:
        business = agent.business_record if agent.business_record_id else None
        if business is not None and business.user_id:
            return business.user
    except Exception:
        logger.exception("admin lookup failed for agent %s", agent.pk)
    return None


def owner_user(agent):
    """The human whose mailbox and name the email goes out under."""
    try:
        partner = agent.partner if agent.partner_id else None
        if partner is not None and partner.user_id:
            return partner.user
    except Exception:
        logger.exception("owner lookup via partner failed for agent %s", agent.pk)
    try:
        business = agent.business_record if agent.business_record_id else None
        if business is not None and business.user_id:
            return business.user
    except Exception:
        logger.exception("owner lookup via business failed for agent %s", agent.pk)
    return None


def sender_config_usable(sender_cfg):
    """Structural check copied from find_leads.v2_outreach._sender_usable so this
    module does not import that (heavy) one. Same rule: does the row have what
    its own transport needs."""
    if sender_cfg is None or not getattr(sender_cfg, "is_active", True):
        return False
    if sender_cfg.use_gmail:
        return bool(sender_cfg.gmail_integration_id)
    if sender_cfg.use_resend:
        return bool((sender_cfg.email or "").strip())
    return bool(
        (sender_cfg.smtp_host or "").strip() and sender_cfg.smtp_port
        and (sender_cfg.email or "").strip() and (sender_cfg.password or "").strip()
    )


def resolve_sender(agent, email_settings=None, team=None):
    """-> dict(kind, sender_config, gmail_integration, from_email, from_name,
               user, mailbox_user, shared, fallback, reasons)

    kind is 'sender_config', 'gmail', or None when nothing usable exists.
    `user` is always the agent owner (for attribution); `mailbox_user` is
    whose mailbox sends; `shared` is True only when that is the admin's
    mailbox carrying a team member's email. `fallback` is the same value
    under its previous name, for callers that still read it.
    """
    reasons = []
    owner = owner_user(agent)
    if owner is None:
        reasons.append(REASON_NO_OWNER)

    if email_settings is None:
        email_settings = getattr(agent, "email_settings", None)

    def _result(kind, gi=None, sender_cfg=None, mailbox_user=None, from_email="", from_name="",
                shared=False, why=()):
        return {
            "kind": kind,
            "sender_config": sender_cfg,
            "gmail_integration": gi,
            "from_email": from_email or "",
            "from_name": from_name or "",
            "user": owner,
            "mailbox_user": mailbox_user,
            "shared": shared,
            "fallback": shared,
            "reasons": list(why),
        }

    sender_cfg = getattr(email_settings, "sender_config", None) if email_settings else None
    if sender_cfg is not None:
        if sender_config_usable(sender_cfg):
            gi = sender_cfg.gmail_integration if sender_cfg.use_gmail else None
            return _result(
                "sender_config", gi=gi, sender_cfg=sender_cfg,
                mailbox_user=getattr(gi, "user", None) if gi is not None else None,
                from_email=(gi.email if gi is not None else sender_cfg.email),
                from_name=sender_cfg.sender_name or "",
            )
        reasons.append(REASON_SENDER_MISCONFIGURED)

    # The team's mailbox: only when the admin shares it and this person has
    # not chosen their own. Never for the admin's own agents - theirs IS it.
    if uses_team_mailbox(agent, email_settings, team):
        admin = admin_user(agent)
        if admin is not None and (owner is None or admin.pk != owner.pk):
            agi, _why = _usable_gmail(admin)
            if agi is not None:
                return _result("gmail", gi=agi, mailbox_user=admin, from_email=agi.email,
                               from_name=_full_name(admin), shared=True, why=reasons)
            # The admin's mailbox cannot send right now; a mailbox of the
            # person's own still beats holding every draft.
            reasons.append(REASON_ADMIN_NO_MAILBOX)

    if owner is not None:
        gi, why = _usable_gmail(owner)
        if gi is not None:
            # `reasons` says what was skipped on the way here (the admin's
            # mailbox, a broken sender), so the page can explain the choice.
            return _result("gmail", gi=gi, mailbox_user=owner, from_email=gi.email,
                           from_name=_full_name(owner), why=reasons)
        reasons.append(why)

    return _result(None, why=reasons)


def readiness(agent, email_settings=None):
    """The JSON the settings tab shows and the PATCH validator checks.
    Newton reads it too (via the agent retrieve), on every reply turn, so
    each lookup is made once."""
    team = team_of(agent)
    resolved = resolve_sender(agent, email_settings, team=team)
    mode = getattr(email_settings, "mode", None) or "off"
    owner = resolved["user"]
    admin = admin_user(agent)
    admin_agent = is_admin_agent(agent)
    admin_gi = _usable_gmail(admin)[0] if admin is not None else None
    if admin_agent:
        own_gi = admin_gi
    else:
        own_gi = _usable_gmail(owner)[0] if owner is not None else None
    return {
        "ready": resolved["kind"] is not None,
        "mode": mode,
        "sender_type": resolved["kind"],
        "from_email": resolved["from_email"],
        "from_name": resolved["from_name"],
        "owner_user_id": getattr(owner, "id", None),
        "owner_name": _full_name(owner) if owner is not None else "",
        "mailbox_owner_user_id": getattr(resolved.get("mailbox_user"), "id", None),
        "shared": bool(resolved.get("shared")),
        "fallback": bool(resolved.get("shared")),
        "is_admin_agent": admin_agent,
        # This person's switches as saved, and what is actually in effect
        # once the admin's switches are applied.
        "use_team_mailbox": _personal(email_settings, "use_team_mailbox"),
        "use_team_content": _personal(email_settings, "use_team_content"),
        "uses_team_mailbox": uses_team_mailbox(agent, email_settings, team),
        "uses_team_content": uses_team_content(agent, email_settings, team),
        "team": None if team is None else {
            "share_mailbox": bool(team.share_mailbox),
            "share_content": bool(team.share_content),
            "admin_name": (_full_name(admin) or getattr(admin, "email", "")) if admin is not None else "",
            "admin_mailbox_ready": admin_gi is not None,
            "admin_mailbox_email": admin_gi.email if admin_gi is not None else "",
        },
        "own_mailbox": {
            "ready": own_gi is not None,
            "email": own_gi.email if own_gi is not None else "",
        },
        # The previous release's page reads this; kept until it is gone.
        "admin_mailbox": {
            "available": not admin_agent and admin_gi is not None,
            "is_owner": admin_agent or admin is None,
            "email": admin_gi.email if (admin_gi is not None and not admin_agent) else "",
            "name": _full_name(admin) if (admin is not None and not admin_agent) else "",
        },
        "reasons": resolved["reasons"],
        "reason_text": [REASON_TEXT.get(r, r) for r in resolved["reasons"]],
        "connect_url": CONNECT_URL,
    }
