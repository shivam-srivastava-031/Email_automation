"""Team sharing: does this agent use the admin's mailbox and the admin's
email content, or its own?

Two switches on each side, and both must be on:
  * the admin's (TeamEmailSettings.share_mailbox / share_content) apply to
    the whole team at once - turning one off stops everyone, turning it back
    on restores everyone whose own switch is still on;
  * each agent's (AgentEmailSettings.use_team_mailbox / use_team_content)
    is that person's own choice, and is kept as they left it.

The admin's own agents are the source, not a consumer: they always use the
admin content (unless switched to their own), whatever the sharing switch
says, and their mailbox is the admin's anyway.

Everything that needs the answer - the prompt, the attachments, the button,
the From address, the settings page - asks here, so the page can never
describe a different email from the one that is sent.
"""
import logging

from .models import AgentEmailSettings, EmailAsset, TeamEmailSettings

logger = logging.getLogger(__name__)

SOURCE_TEAM = "team"
SOURCE_OWN = "own"

# Kinds that are sent. "snippet" is retired and never sent.
SENDABLE_KINDS = (EmailAsset.KIND_FILE, EmailAsset.KIND_LINK)


def business_id_of(agent):
    return getattr(agent, "business_record_id", None) if agent is not None else None


def team_for_business(business_id):
    """The business's TeamEmailSettings, or an UNSAVED one with the defaults
    (both switches on) when none was ever saved. None without a business."""
    if not business_id:
        return None
    team = TeamEmailSettings.objects.filter(business_id=business_id).first()
    return team if team is not None else TeamEmailSettings(business_id=business_id)


def team_of(agent):
    return team_for_business(business_id_of(agent))


def admin_user_id(agent):
    try:
        business = agent.business_record if business_id_of(agent) else None
        return business.user_id if business is not None else None
    except Exception:
        logger.exception("admin lookup failed for agent %s", getattr(agent, "pk", None))
        return None


def owner_user_id(agent):
    """The agent's person: the partner's user, else the business admin."""
    try:
        partner = agent.partner if agent.partner_id else None
        if partner is not None and partner.user_id:
            return partner.user_id
    except Exception:
        logger.exception("partner lookup failed for agent %s", getattr(agent, "pk", None))
    return admin_user_id(agent)


def is_admin_agent(agent):
    """True when the business admin is this agent's own person - the source of
    the team's email and content rather than a member using them."""
    admin = admin_user_id(agent)
    return admin is not None and owner_user_id(agent) == admin


def _personal(email_settings, field):
    # No settings row yet means the defaults: on.
    if email_settings is None:
        return True
    return bool(getattr(email_settings, field, True))


def uses_team_mailbox(agent, email_settings, team=None):
    team = team if team is not None else team_of(agent)
    if team is None or is_admin_agent(agent):
        return False
    return bool(team.share_mailbox) and _personal(email_settings, "use_team_mailbox")


def uses_team_content(agent, email_settings, team=None):
    team = team if team is not None else team_of(agent)
    if team is None or not _personal(email_settings, "use_team_content"):
        return False
    if is_admin_agent(agent):
        return True
    return bool(team.share_content)


def content_for(agent, email_settings, team=None):
    """-> (source, obj): obj carries body_mode, playbook, fixed_subject,
    fixed_body, include_booking_link, cta_text and cta_url - either the team's
    row or the agent's own settings."""
    team = team if team is not None else team_of(agent)
    if uses_team_content(agent, email_settings, team):
        return SOURCE_TEAM, team
    if email_settings is None:
        email_settings = AgentEmailSettings(agent=agent)
    return SOURCE_OWN, email_settings


def team_assets(business_id):
    if not business_id:
        return EmailAsset.objects.none()
    return EmailAsset.objects.filter(agent__isnull=True, business_id=business_id)


def own_assets(agent):
    if agent is None or agent.pk is None:
        return EmailAsset.objects.none()
    return EmailAsset.objects.filter(agent=agent)


def attachments_for(agent, email_settings, team=None):
    """What goes with every email this agent sends: the active files and
    links of whichever content it uses. Never a mix of the two."""
    source, _obj = content_for(agent, email_settings, team)
    qs = team_assets(business_id_of(agent)) if source == SOURCE_TEAM else own_assets(agent)
    return qs.filter(is_active=True, kind__in=SENDABLE_KINDS)


def can_manage_team(request, business):
    """Only the business admin (whose mailbox and content these are) or a
    master admin may change the team switches and the team content.
    Sub-Admins and sales see them read-only: they are members of the team,
    and a mailbox is shared only by the person who owns it."""
    from user.middleware import get_user_context

    if business is None:
        return False
    auth = getattr(request, "auth", None)
    if auth and hasattr(auth, "get") and auth.get("is_read_only", False):
        return False
    if get_user_context().get("master_admin"):
        return True
    user = getattr(request, "user", None)
    return bool(user is not None and getattr(user, "pk", None) and user.pk == business.user_id)
