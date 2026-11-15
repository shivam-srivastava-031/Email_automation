"""Who may see and change the email orchestrator.

Settings and assets sit on the Knowledge Base page, which the agent endpoints
already expose to any approved, confirmed user of the tenant (tenant scoping
does the real work). Decisions are shown on the Deal page and reuse the deal
permissions, exactly like agentic_deals.DealPermission - a freshly generated
codename would be missing from every role that exists today.
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

DECISION_WRITE_ACTIONS = {"approve", "skip", "cancel", "update_draft"}


def _approved(request):
    user = request.user
    return bool(
        user
        and str(user) != "AnonymousUser"
        and getattr(user, "status", None) == "Approved"
        and getattr(user, "is_confirmed", False)
    )


def _impersonating(request):
    auth = getattr(request, "auth", None)
    return bool(auth.get("is_read_only", False)) if auth else False


class KnowledgeBasePermission(BasePermission):
    def has_permission(self, request, view):
        impersonating = _impersonating(request)
        if not _approved(request) and not impersonating:
            return False
        if impersonating:
            return request.method in SAFE_METHODS
        return True


class DecisionPermission(BasePermission):
    def has_permission(self, request, view):
        impersonating = _impersonating(request)
        if not _approved(request) and not impersonating:
            return False
        if impersonating:
            return request.method in SAFE_METHODS
        user = request.user
        if getattr(view, "action", None) in DECISION_WRITE_ACTIONS:
            return user.has_perm("lead.change_dealsandopportunity")
        return user.has_perm("lead.view_dealsandopportunity")
