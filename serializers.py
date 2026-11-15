from types import SimpleNamespace

from django.utils import timezone
from rest_framework import serializers

from .models import AgentEmailSettings, EmailAsset, EmailDecision, TeamEmailSettings
from .readiness import readiness
from .sharing import uses_team_content


def _cta_text(value):
    return " ".join((value or "").split())[:80]


class AgentEmailSettingsSerializer(serializers.ModelSerializer):
    readiness = serializers.SerializerMethodField(read_only=True)
    merge_tags = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = AgentEmailSettings
        fields = [
            "id", "agent", "business", "mode", "sender_config", "playbook", "daily_cap",
            "cc_owner", "include_booking_link", "cancel_window_minutes",
            "body_mode", "fixed_subject", "fixed_body",
            "use_team_mailbox", "use_team_content", "cta_text", "cta_url",
            "readiness", "merge_tags", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "agent", "business", "readiness", "merge_tags",
                            "created_at", "updated_at"]

    def get_readiness(self, obj):
        return readiness(obj.agent, obj)

    def get_merge_tags(self, obj):
        """The tags a fixed template may use, so the UI never has to hardcode
        a list that can drift from what the engine actually fills."""
        from .engine import MERGE_TAGS
        return MERGE_TAGS

    def validate_mode(self, value):
        if value not in AgentEmailSettings.MODES:
            raise serializers.ValidationError("mode must be off, review or auto")
        return value

    def validate_daily_cap(self, value):
        if value < 1 or value > 200:
            raise serializers.ValidationError("daily_cap must be between 1 and 200")
        return value

    def validate_body_mode(self, value):
        if value not in AgentEmailSettings.BODY_MODES:
            raise serializers.ValidationError("body_mode must be ai or fixed")
        return value

    def validate(self, attrs):
        inst = self.instance
        mode = attrs.get("mode", getattr(inst, "mode", AgentEmailSettings.MODE_OFF))
        body_mode = attrs.get("body_mode", getattr(inst, "body_mode", AgentEmailSettings.BODY_AI))
        body = attrs.get("fixed_body", getattr(inst, "fixed_body", ""))
        # Only the content actually in use must be complete: an agent on the
        # team's content may leave a half-written template of its own.
        use_team = attrs.get("use_team_content", getattr(inst, "use_team_content", True))
        on_team = inst is not None and uses_team_content(inst.agent, SimpleNamespace(use_team_content=use_team))
        # A fixed template with nothing in it would hold every draft; refuse it
        # at save time where the person can still see why.
        if (mode != AgentEmailSettings.MODE_OFF and not on_team
                and body_mode == AgentEmailSettings.BODY_FIXED and not (body or "").strip()):
            raise serializers.ValidationError({"fixed_body": "write the email body, or switch to AI"})
        return attrs

    def validate_cta_text(self, value):
        return _cta_text(value)

    def validate_cancel_window_minutes(self, value):
        if value > 60:
            raise serializers.ValidationError("cancel_window_minutes must be 60 or less")
        return value

    def validate_sender_config(self, value):
        if value is None:
            return value
        agent = self.instance.agent if self.instance is not None else None
        biz_id = getattr(agent, "business_record_id", None)
        if biz_id and value.business_id and value.business_id != biz_id:
            raise serializers.ValidationError("sender belongs to a different business")
        return value


class TeamEmailSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TeamEmailSettings
        fields = [
            "share_mailbox", "share_content",
            "body_mode", "playbook", "fixed_subject", "fixed_body",
            "include_booking_link", "cta_text", "cta_url", "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def validate_body_mode(self, value):
        if value not in AgentEmailSettings.BODY_MODES:
            raise serializers.ValidationError("body_mode must be ai or fixed")
        return value

    def validate_cta_text(self, value):
        return _cta_text(value)

    def validate(self, attrs):
        inst = self.instance
        body_mode = attrs.get("body_mode", getattr(inst, "body_mode", AgentEmailSettings.BODY_AI))
        body = attrs.get("fixed_body", getattr(inst, "fixed_body", ""))
        # The whole team would get empty drafts; refuse it here instead.
        if body_mode == AgentEmailSettings.BODY_FIXED and not (body or "").strip():
            raise serializers.ValidationError({"fixed_body": "write the email body, or switch to AI"})
        return attrs


class EmailAssetSerializer(serializers.ModelSerializer):
    filename = serializers.SerializerMethodField(read_only=True)
    is_team = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = EmailAsset
        fields = [
            "id", "agent", "business", "kind", "name", "description", "tags", "file",
            "filename", "file_size", "mime_type", "url", "subject", "body", "is_active",
            "sort_order", "is_team", "created_on", "updated_at",
        ]
        # `business` is always derived server-side from the agent.
        read_only_fields = ["id", "business", "file_size", "mime_type", "filename", "is_team",
                            "created_on", "updated_at"]

    def get_filename(self, obj):
        return obj.filename

    def get_is_team(self, obj):
        return obj.agent_id is None


    def validate(self, attrs):
        kind = attrs.get("kind") or getattr(self.instance, "kind", None)
        if kind not in EmailAsset.KINDS:
            raise serializers.ValidationError({"kind": "kind must be file, link or snippet"})
        creating = self.instance is None
        if kind == EmailAsset.KIND_FILE and creating and not attrs.get("file"):
            raise serializers.ValidationError({"file": "a file is required for kind=file"})
        if kind == EmailAsset.KIND_LINK and creating and not (attrs.get("url") or "").strip():
            raise serializers.ValidationError({"url": "a url is required for kind=link"})
        if kind == EmailAsset.KIND_SNIPPET and creating and not (attrs.get("body") or "").strip():
            raise serializers.ValidationError({"body": "a body is required for kind=snippet"})
        tags = attrs.get("tags")
        if tags is not None and not isinstance(tags, list):
            raise serializers.ValidationError({"tags": "tags must be a list of strings"})
        return attrs


class EmailDecisionSerializer(serializers.ModelSerializer):
    contact_name = serializers.SerializerMethodField(read_only=True)
    agent_name = serializers.SerializerMethodField(read_only=True)
    assets = serializers.SerializerMethodField(read_only=True)
    opened = serializers.SerializerMethodField(read_only=True)
    can_edit = serializers.SerializerMethodField(read_only=True)
    seconds_to_send = serializers.SerializerMethodField(read_only=True)
    from_email = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = EmailDecision
        fields = [
            "id", "status", "trigger_source", "trigger_ref", "trigger_text", "should_email",
            "ask_summary", "recipient_email", "from_email", "subject", "body_html", "asset_ids", "assets",
            "reasons", "evidence", "hold_reason", "confidence", "model",
            "agent", "agent_name", "business", "contact", "contact_name", "deal", "sdr_lead_id",
            "scheduled_at", "cancel_deadline_at", "sent_at", "email_log", "opened",
            "reviewed_by", "reviewed_at", "error", "can_edit", "seconds_to_send",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_contact_name(self, obj):
        c = obj.contact
        if c is None:
            return None
        return " ".join(p for p in (c.name, c.last_name) if p).strip() or None

    def get_agent_name(self, obj):
        return obj.agent.agent_name if obj.agent_id else None

    def get_assets(self, obj):
        ids = [i for i in (obj.asset_ids or []) if isinstance(i, int)]
        if not ids:
            return []
        rows = EmailAsset.objects.all_with_deleted().filter(pk__in=ids)
        by_id = {a.pk: a for a in rows}
        out = []
        for i in ids:
            a = by_id.get(i)
            if a is None:
                continue
            out.append({
                "id": a.pk, "kind": a.kind, "name": a.name,
                "filename": a.filename, "url": a.url, "file_size": a.file_size,
            })
        return out

    def get_opened(self, obj):
        log = obj.email_log
        if log is None:
            return None
        return {
            "is_opened": bool(log.is_opened),
            "opened_at": log.opened_at,
            "open_count": log.open_count,
        }

    def get_can_edit(self, obj):
        return obj.status in EmailDecision.EDITABLE_STATUSES

    def get_seconds_to_send(self, obj):
        if obj.status != EmailDecision.STATUS_SCHEDULED or not obj.cancel_deadline_at:
            return None
        return max(0, int((obj.cancel_deadline_at - timezone.now()).total_seconds()))

    def get_from_email(self, obj):
        log = obj.email_log
        if log is not None and log.from_email:
            return log.from_email
        raw = obj.raw if isinstance(obj.raw, dict) else {}
        return raw.get("from_email") or None


class DraftUpdateSerializer(serializers.Serializer):
    subject = serializers.CharField(max_length=300, required=False, allow_blank=True)
    body_html = serializers.CharField(required=False, allow_blank=True)
    asset_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    recipient_email = serializers.EmailField(required=False)


class ApproveSerializer(DraftUpdateSerializer):
    pass


class SkipSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True)
