"""REST surface for the email orchestrator.

  GET/PATCH email-orchestrator/settings/{agent_id}/          per-agent mode, sender, playbook
  GET       email-orchestrator/settings/{agent_id}/readiness/
  POST      email-orchestrator/settings/{agent_id}/preview/   render the email (template + button)
  POST      email-orchestrator/settings/{agent_id}/copy_team_content/   start own content from the team's
  GET/PATCH email-orchestrator/team/?agent=                   the admin's switches + team content
  CRUD      email-orchestrator/assets/?agent=[&team=1]        the agent's (or its team's) attachments
  GET       email-orchestrator/decisions/?contact_id=|deal_id=|agent=&status=
  POST      email-orchestrator/decisions/{id}/approve|skip|cancel|update_draft/
  GET       email-orchestrator/decisions/queue_counts/?agent=

Tenant scoping mirrors LinkedInAgentViewSet: master sees all, a partner
sees its own agents, a business user sees the business's agents. The team is
always the business of an agent the caller can see - never a business id
from the request. Changing the team (switches, content, team attachments) is
for the business admin only (sharing.can_manage_team).

"""
import logging
import mimetypes
import os

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from ai_sdr.models import LinkedInAgent
from lead.models import DealsAndOpportunity
from user.middleware import get_user_context

from .models import AgentEmailSettings, EmailAsset, EmailDecision, TeamEmailSettings
from .permissions import DecisionPermission, KnowledgeBasePermission
from .readiness import _full_name, _usable_gmail, owner_user, readiness
from .serializers import (
    AgentEmailSettingsSerializer,
    ApproveSerializer,
    DraftUpdateSerializer,
    EmailAssetSerializer,
    EmailDecisionSerializer,
    SkipSerializer,
    TeamEmailSettingsSerializer,
)
from .sharing import (
    SENDABLE_KINDS, _personal, can_manage_team, is_admin_agent, team_assets, team_for_business,
)

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".txt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


CONTENT_FIELDS = (
    "body_mode", "playbook", "fixed_subject", "fixed_body",
    "include_booking_link", "cta_text", "cta_url",
)


def copy_asset(item, **owner):
    """A copy of an attachment for another owner, sharing the same stored
    file (no bytes are duplicated)."""
    return EmailAsset.objects.create(
        kind=item.kind, name=item.name, description=item.description, tags=item.tags,
        file=item.file.name if item.file else None, file_size=item.file_size,
        mime_type=item.mime_type, url=item.url, is_active=item.is_active,
        sort_order=item.sort_order, **owner,
    )


def scoped_agents():
    """LinkedInAgent rows the caller may touch. Same branches as LinkedInAgentViewSet."""
    ctx = get_user_context()
    qs = LinkedInAgent.objects.all()
    if ctx.get("master_admin"):
        return qs
    partner_id = ctx.get("partner_id")
    if partner_id is not None:
        return qs.filter(partner_id=partner_id)
    business_id = ctx.get("business_record_id")
    if not business_id:
        return qs.none()
    return qs.filter(business_record_id=business_id)


def scoped_agent_from(request):
    """The ?agent= (or body agent) the caller may see, or None."""
    agent_id = _int(request.query_params.get("agent"))
    if agent_id is None:
        agent_id = _int(getattr(request, "data", {}).get("agent"))
    if not agent_id:
        return None
    return scoped_agents().filter(pk=agent_id).first()


def settings_for(agent):
    obj, _ = AgentEmailSettings.objects.get_or_create(
        agent=agent, defaults={"business_id": agent.business_record_id},
    )
    if obj.business_id is None and agent.business_record_id:
        obj.business_id = agent.business_record_id
        obj.save(update_fields=["business"])
    return obj


class AgentEmailSettingsViewSet(GenericViewSet):
    """Keyed by AGENT id, not by the settings row id: the KB page only knows the agent."""
    serializer_class = AgentEmailSettingsSerializer
    permission_classes = [IsAuthenticated, KnowledgeBasePermission]

    def _agent(self, pk):
        return scoped_agents().filter(pk=_int(pk)).select_related("partner", "business_record").first()

    def retrieve(self, request, pk=None):
        agent = self._agent(pk)
        if agent is None:
            return Response({"error": "agent not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(self.serializer_class(settings_for(agent)).data)

    def partial_update(self, request, pk=None):
        agent = self._agent(pk)
        if agent is None:
            return Response({"error": "agent not found"}, status=status.HTTP_404_NOT_FOUND)
        obj = settings_for(agent)
        ser = self.serializer_class(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        new_mode = ser.validated_data.get("mode", obj.mode)
        if new_mode != AgentEmailSettings.MODE_OFF:
            # Server-side rule, not just a disabled toggle: a mode other than
            # off is refused unless a mailbox can actually send right now.
            # The probe carries every field readiness depends on, including
            # the admin-mailbox toggle, so turning the toggle on and choosing
            # a mode in ONE save works.
            probe = AgentEmailSettings(
                agent=agent, mode=new_mode,
                sender_config=ser.validated_data.get("sender_config", obj.sender_config),
                use_team_mailbox=ser.validated_data.get("use_team_mailbox", obj.use_team_mailbox),
                use_team_content=ser.validated_data.get("use_team_content", obj.use_team_content),
            )
            r = readiness(agent, probe)
            if not r["ready"]:
                return Response(
                    {"error": "email is not connected for this agent", "readiness": r},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        ser.save()
        return Response(self.serializer_class(obj).data)

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        """Render a fixed template the way the engine will, with sample values.

        The UI must never fill merge tags itself: a second implementation
        would drift from the one that actually sends, and the rep would
        approve a preview that is not the email.
        """
        agent = self._agent(pk)
        if agent is None:
            return Response({"error": "agent not found"}, status=status.HTTP_404_NOT_FOUND)
        from .engine import (
            DEFAULT_CTA_TEXT, MERGE_TAGS, booking_link, cta_html, markdown_to_html, merge_context,
            persona_of, render_tags,
        )

        sample = {
            "prospect": {
                "name": request.data.get("sample_name") or "Jane Doe",
                "first_name": (request.data.get("sample_name") or "Jane").split(" ")[0],
                "title": request.data.get("sample_role") or "Director",
                "company": request.data.get("sample_company") or "ProspectCo",
            },
            "persona": persona_of(agent),
            "booking_link": (getattr(agent, "meeting_link", "") or ""),
        }
        values = merge_context(sample)
        subject = render_tags(request.data.get("subject") or "", values)
        body = render_tags(request.data.get("body") or "", values)
        body_html = markdown_to_html(body)
        # The same button the engine appends, from the same function, so the
        # preview can never show a different call to action than the email.
        include_cta = str(request.data.get("include_cta", "true")).lower() not in ("false", "0", "")
        cta_url = (request.data.get("cta_url") or "").strip() or booking_link(agent)
        cta_text = (request.data.get("cta_text") or "").strip() or DEFAULT_CTA_TEXT
        if include_cta and cta_url:
            body_html += cta_html(cta_text, cta_url)
        return Response({
            "subject": subject.strip(),
            "body_html": body_html,
            "cta_url": cta_url if include_cta else "",
            "merge_tags": MERGE_TAGS,
            "values": values,
        })

    @action(detail=True, methods=["post"])
    def copy_team_content(self, request, pk=None):
        """Start this agent's own content from the team's: the text, the
        button and the attachments (same stored files), then switch the agent
        to its own content. For a rep who wants to tweak the admin's email
        rather than write one from nothing. Own attachments with the same
        name are kept, not duplicated."""
        agent = self._agent(pk)
        if agent is None:
            return Response({"error": "agent not found"}, status=status.HTTP_404_NOT_FOUND)
        team = team_for_business(agent.business_record_id)
        if team is None:
            return Response({"error": "this agent has no team"}, status=status.HTTP_400_BAD_REQUEST)
        obj = settings_for(agent)
        with transaction.atomic():
            for field in CONTENT_FIELDS:
                setattr(obj, field, getattr(team, field))
            obj.use_team_content = False
            obj.save()
            mine = {n.lower() for n in EmailAsset.objects.filter(agent=agent).values_list("name", flat=True)}
            for item in team_assets(agent.business_record_id).filter(kind__in=SENDABLE_KINDS):
                if item.name.lower() in mine:
                    continue
                copy_asset(item, agent=agent, business_id=agent.business_record_id)
        return Response(self.serializer_class(obj).data)

    @action(detail=True, methods=["get"])
    def readiness(self, request, pk=None):
        agent = self._agent(pk)
        if agent is None:
            return Response({"error": "agent not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(readiness(agent, settings_for(agent)))


def team_usage(business):
    """How the team uses the admin's email and content, for the admin's
    switches: who is on each, and who would be left with no mailbox at all
    if the admin stopped sharing (email switched on, no mailbox of their
    own). Active agents only: a deactivated one sends nothing."""
    agents = list(
        LinkedInAgent.objects.filter(business_record_id=business.pk, is_active=True)
        .select_related("partner", "business_record")
    )
    by_agent = {s.agent_id: s for s in AgentEmailSettings.objects.filter(agent__in=agents)}
    members = [a for a in agents if not is_admin_agent(a)]
    on_mailbox, on_content, need_own = 0, 0, []
    for a in members:
        es = by_agent.get(a.pk)
        if _personal(es, "use_team_mailbox"):
            on_mailbox += 1
            if es is not None and es.enabled and es.sender_config_id is None:
                if _usable_gmail(owner_user(a))[0] is None:
                    need_own.append(a.agent_name or f"agent {a.pk}")
        if _personal(es, "use_team_content"):
            on_content += 1
    return {
        "members": len(members), "on_mailbox": on_mailbox, "on_content": on_content,
        "need_own_mailbox": need_own,
    }


class TeamEmailSettingsView(APIView):
    """The admin's side of team sharing, for the business of ?agent=."""
    permission_classes = [IsAuthenticated, KnowledgeBasePermission]

    def _business(self, request):
        agent = scoped_agent_from(request)
        if agent is None or not agent.business_record_id:
            return None
        return agent.business_record

    def _payload(self, request, business, team):
        data = TeamEmailSettingsSerializer(team).data
        admin = business.user if business.user_id else None
        admin_gi = _usable_gmail(admin)[0] if admin is not None else None
        manage = can_manage_team(request, business)
        data.update({
            "can_manage": manage,
            "admin": {
                "name": (_full_name(admin) or admin.email) if admin is not None else "",
                "email": getattr(admin, "email", "") or "",
                "mailbox_ready": admin_gi is not None,
                "mailbox_email": admin_gi.email if admin_gi is not None else "",
            },
            "usage": team_usage(business) if manage else None,
        })
        return data

    def get(self, request):
        business = self._business(request)
        if business is None:
            return Response({"error": "agent not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._payload(request, business, team_for_business(business.pk)))

    def patch(self, request):
        business = self._business(request)
        if business is None:
            return Response({"error": "agent not found"}, status=status.HTTP_404_NOT_FOUND)
        if not can_manage_team(request, business):
            raise PermissionDenied("only the business admin can change what the team shares")
        team, _ = TeamEmailSettings.objects.get_or_create(business=business)
        ser = TeamEmailSettingsSerializer(team, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(self._payload(request, business, team))


class EmailAssetViewSet(ModelViewSet):
    serializer_class = EmailAssetSerializer
    permission_classes = [IsAuthenticated, KnowledgeBasePermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        if self.action == "list":
            agent = scoped_agent_from(self.request)
            if agent is None:
                return EmailAsset.objects.none()
            qs = (team_assets(agent.business_record_id) if _truthy(self.request.query_params.get("team"))
                  else EmailAsset.objects.filter(agent=agent))
            # Only what can be sent: retired snippets stay out of sight.
            return qs.exclude(kind=EmailAsset.KIND_SNIPPET).select_related("agent")
        scoped = scoped_agents()
        return EmailAsset.objects.filter(
            Q(agent__in=scoped) | Q(agent__isnull=True, business_id__in=scoped.values("business_record_id")),
        ).select_related("agent", "business")

    def _require_team_admin(self, business):
        if not can_manage_team(self.request, business):
            raise PermissionDenied("only the business admin can change the team's attachments")

    def _check_file(self, upload):
        name = getattr(upload, "name", "") or ""
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return f"file type {ext or '(none)'} is not allowed"
        # Stored regardless of size; the sender decides attach-vs-link using
        # EMAIL_ORCHESTRATOR_MAX_ATTACHMENT_MB. Refuse only what no mailbox
        # could ever carry.
        if upload.size > 25 * 1024 * 1024:
            return "file is larger than 25 MB"
        return None

    def perform_create(self, serializer):
        agent = serializer.validated_data.get("agent")
        if agent is None:
            raise ValidationError({"agent": "required: the agent this attachment is for"})
        if not scoped_agents().filter(pk=agent.pk).exists():
            raise PermissionDenied("agent is outside your scope")
        if serializer.validated_data.get("kind") == EmailAsset.KIND_SNIPPET:
            raise ValidationError({"kind": "text snippets are no longer used; write it into the "
                                           "instructions or the fixed template instead"})
        extra = {"business_id": agent.business_record_id}
        if _truthy(self.request.data.get("team")):
            # A team attachment: `agent` only names the team (its business).
            if not agent.business_record_id:
                raise ValidationError({"agent": "this agent has no team"})
            self._require_team_admin(agent.business_record)
            extra["agent"] = None
        upload = serializer.validated_data.get("file")
        if upload is not None:
            err = self._check_file(upload)
            if err:
                raise ValidationError({"file": err})
            extra["file_size"] = upload.size
            extra["mime_type"] = (
                getattr(upload, "content_type", "") or mimetypes.guess_type(upload.name)[0] or ""
            )[:120]
        serializer.save(**extra)

    def perform_update(self, serializer):
        if serializer.instance.agent_id is None:
            self._require_team_admin(serializer.instance.business)
        upload = serializer.validated_data.get("file")
        extra = {}
        if upload is not None:
            err = self._check_file(upload)
            if err:
                raise ValidationError({"file": err})
            extra["file_size"] = upload.size
            extra["mime_type"] = (
                getattr(upload, "content_type", "") or mimetypes.guess_type(upload.name)[0] or ""
            )[:120]
        # Ownership is fixed at creation; ignore attempts to move an item.
        serializer.validated_data.pop("agent", None)
        serializer.save(**extra)

    def perform_destroy(self, instance):
        if instance.agent_id is None:
            self._require_team_admin(instance.business)
        instance.delete()  # BaseModel soft delete


class EmailDecisionViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    serializer_class = EmailDecisionSerializer
    permission_classes = [IsAuthenticated, DecisionPermission]

    def get_queryset(self):
        qs = EmailDecision.objects.select_related("contact", "agent", "email_log", "deal")
        ctx = get_user_context()
        if not ctx.get("master_admin"):
            business_id = ctx.get("business_record_id")
            if not business_id:
                return qs.none()
            qs = qs.filter(business_id=business_id)
            partner_id = ctx.get("partner_id")
            if partner_id is not None:
                qs = qs.filter(agent__partner_id=partner_id)
        p = self.request.query_params
        contact_id = _int(p.get("contact_id"))
        deal_id = _int(p.get("deal_id"))
        if deal_id and not contact_id:
            deal = DealsAndOpportunity.objects.all_with_deleted().filter(pk=deal_id).first()
            contact_id = deal.contact_id if deal is not None else None
            if deal is not None and not contact_id:
                qs = qs.filter(deal_id=deal_id)
            elif not contact_id:
                return qs.none()
        if contact_id:
            qs = qs.filter(contact_id=contact_id)
        agent_id = _int(p.get("agent"))
        if agent_id:
            qs = qs.filter(agent_id=agent_id)
        statuses = [s for s in (p.get("status") or "").split(",") if s in EmailDecision.STATUSES]
        if statuses:
            qs = qs.filter(status__in=statuses)
        if p.get("open") in ("1", "true"):
            qs = qs.filter(status__in=EmailDecision.OPEN_STATUSES)
        return qs

    def _apply_draft(self, decision, data):
        if "subject" in data:
            decision.subject = data["subject"][:300]
        if "body_html" in data:
            decision.body_html = data["body_html"]
        if "recipient_email" in data:
            decision.recipient_email = data["recipient_email"]
        if "asset_ids" in data:
            from .engine import allowed_asset_ids
            allowed = allowed_asset_ids(decision.agent, data["asset_ids"])
            decision.asset_ids = [i for i in data["asset_ids"] if i in allowed]

    @action(detail=True, methods=["post"])
    def update_draft(self, request, pk=None):
        decision = self.get_object()
        if decision.status not in EmailDecision.EDITABLE_STATUSES:
            return Response({"error": f"cannot edit a decision in status {decision.status}"},
                            status=status.HTTP_409_CONFLICT)
        ser = DraftUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        self._apply_draft(decision, ser.validated_data)
        decision.reviewed_by = request.user
        decision.reviewed_at = timezone.now()
        decision.save()
        return Response(self.serializer_class(decision).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Rep says send. Applies any edits, then queues the send immediately
        (no cancel window: the human just chose)."""
        from .sending import dispatch_send

        decision = self.get_object()
        if decision.status not in (EmailDecision.STATUS_AWAITING_REVIEW, EmailDecision.STATUS_HELD,
                                   EmailDecision.STATUS_SCHEDULED):
            return Response({"error": f"cannot approve a decision in status {decision.status}"},
                            status=status.HTTP_409_CONFLICT)
        ser = ApproveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        self._apply_draft(decision, ser.validated_data)
        if not decision.recipient_email:
            return Response({"error": "recipient_email is required before approving"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not (decision.subject or "").strip() or not (decision.body_html or "").strip():
            return Response({"error": "subject and body are required before approving"},
                            status=status.HTTP_400_BAD_REQUEST)
        decision.reviewed_by = request.user
        decision.reviewed_at = timezone.now()
        decision.hold_reason = ""
        dispatch_send(decision, countdown_seconds=0, approved_by=request.user)
        decision.refresh_from_db()
        return Response(self.serializer_class(decision).data)

    def _cancel(self, request, reason, ok_statuses):
        from .sending import cancel_send

        decision = self.get_object()
        if decision.status not in ok_statuses:
            return Response({"error": f"cannot cancel a decision in status {decision.status}"},
                            status=status.HTTP_409_CONFLICT)
        cancel_send(decision, reason=reason, by=request.user)
        decision.refresh_from_db()
        return Response(self.serializer_class(decision).data)

    @action(detail=True, methods=["post"])
    def skip(self, request, pk=None):
        ser = SkipSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return self._cancel(
            request, ser.validated_data.get("reason") or "skipped by rep",
            (EmailDecision.STATUS_AWAITING_REVIEW, EmailDecision.STATUS_HELD, EmailDecision.STATUS_SCHEDULED),
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        ser = SkipSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return self._cancel(
            request, ser.validated_data.get("reason") or "cancelled by rep",
            (EmailDecision.STATUS_SCHEDULED, EmailDecision.STATUS_AWAITING_REVIEW),
        )

    @action(detail=False, methods=["get"])
    def queue_counts(self, request):
        qs = self.get_queryset()
        rows = qs.values("status").annotate(n=Count("id"))
        counts = {r["status"]: r["n"] for r in rows}
        return Response({
            "awaiting_review": counts.get(EmailDecision.STATUS_AWAITING_REVIEW, 0),
            "scheduled": counts.get(EmailDecision.STATUS_SCHEDULED, 0),
            "held": counts.get(EmailDecision.STATUS_HELD, 0),
            "sent": counts.get(EmailDecision.STATUS_SENT, 0),
            "failed": counts.get(EmailDecision.STATUS_FAILED, 0),
        })
