from django.urls import path
from rest_framework import routers

from .views import AgentEmailSettingsViewSet, EmailAssetViewSet, EmailDecisionViewSet, TeamEmailSettingsView

router = routers.SimpleRouter()
router.register(r"email-orchestrator/settings", AgentEmailSettingsViewSet, basename="email-orchestrator-settings")
router.register(r"email-orchestrator/assets", EmailAssetViewSet, basename="email-orchestrator-assets")
router.register(r"email-orchestrator/decisions", EmailDecisionViewSet, basename="email-orchestrator-decisions")

urlpatterns = [
    path("email-orchestrator/team/", TeamEmailSettingsView.as_view(), name="email-orchestrator-team"),
] + router.urls
