"""The admin's email content, shared with the team.

Rule under test: an agent uses the team content (instructions or fixed
template, attachments, button) when the admin shares it AND the agent's own
switch is on - never a mix with its own. The admin's own agents always use
it. Only the business admin may change it.
"""
import importlib
from datetime import timedelta
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from agentic_deals.tests import make_user
from ai_sdr.models import LinkedInAgent
from lead.models import BusinessRecord, Partners
from user.middleware import set_user_context

from . import engine
from .models import AgentEmailSettings, EmailAsset, EmailDecision, TeamEmailSettings
from .sending import build_payload
from .sharing import content_for, uses_team_content
from .test_admin_mailbox import SharedBase
from .tests import GEMINI, model_says
from .views import AgentEmailSettingsViewSet, EmailAssetViewSet, TeamEmailSettingsView

MIGRATION = importlib.import_module("email_orchestrator.migrations.0005_team_sharing")
BOOKING = "https://cal.com/hodos"           # the rep agent's own booking link (fixture)
TEAM_LINK = "https://acme.example/meet"


class TeamBase(SharedBase):
    def setUp(self):
        super().setUp()
        self.rep_gmail()
        self.toggle(use_team_content=True)

    def item(self, name, agent="mine", business=None, active=True):
        f = SimpleUploadedFile(f"{name}.pdf", b"%PDF-1.4", content_type="application/pdf")
        return EmailAsset.objects.create(
            agent=self.agent if agent == "mine" else agent, business=business or self.business,
            kind=EmailAsset.KIND_FILE, name=name, file=f, file_size=8, mime_type="application/pdf",
            is_active=active,
        )

    def team_item(self, name, business=None, active=True):
        return self.item(name, agent=None, business=business, active=active)

    def decided(self, **model):
        row = self.asked()
        with patch(GEMINI, return_value=model_says(**model)) as g:
            engine.decide(row.pk)
        row.refresh_from_db()
        return row, g.call_args[0][0]

    def view_team(self, method, data=None, user=None):
        view = TeamEmailSettingsView.as_view()
        path = f"/api/v1/email-orchestrator/team/?agent={self.agent.pk}"
        return self.call(view, method, path, data, user=user)

    def as_sub_admin(self):
        sub = make_user(f"sub-{self.id()}@example.com")
        Partners.objects.create(user=sub, publisher=self.business, business_name="Sub")
        # A Sub-Admin's token carries the business but no partner_id.
        set_user_context(user_id=sub.id, business_record_id=self.business.id,
                         master_admin=False, partner_id=None)
        return sub


# ===========================================================================
# 1. What the email is made of
# ===========================================================================
class TeamContentEngineTests(TeamBase):
    def test_a_rep_on_team_content_sends_the_admins_email_not_their_own(self):
        self.team(playbook="Mention our 14-day trial.", cta_text="Talk to us", cta_url=TEAM_LINK)
        team_deck = self.team_item("Team deck")
        self.item("My own deck")
        self.toggle(playbook="MY OWN INSTRUCTIONS")
        row, prompt = self.decided()
        self.assertEqual(row.status, EmailDecision.STATUS_AWAITING_REVIEW, row.hold_reason)
        self.assertIn("Mention our 14-day trial.", prompt)
        self.assertNotIn("MY OWN INSTRUCTIONS", prompt)
        self.assertEqual(row.asset_ids, [team_deck.pk])                   # never a mix
        self.assertIn(f'href="{TEAM_LINK}"', row.body_html)
        self.assertIn(">Talk to us<", row.body_html)
        self.assertEqual(row.raw["content_source"], "team")

    def test_a_team_button_without_a_link_uses_each_reps_own_booking_link(self):
        self.team(cta_text="Book with me", cta_url="")
        row, _prompt = self.decided()
        self.assertIn(f'href="{BOOKING}"', row.body_html)

    def test_a_team_fixed_template_is_filled_for_this_prospect(self):
        self.team(body_mode="fixed", fixed_subject="For {{company_name}}", fixed_body="Hi {{first_name}}, as promised.")
        row, _prompt = self.decided(body_markdown="AI PROSE")
        self.assertEqual(row.subject, "For RebelCorp")
        self.assertIn("Hi Pratapraj, as promised.", row.body_html)
        self.assertNotIn("AI PROSE", row.body_html)

    def test_the_reps_switch_off_means_their_own_content(self):
        self.team(playbook="TEAM INSTRUCTIONS")
        self.team_item("Team deck")
        mine = self.item("My deck")
        self.toggle(use_team_content=False, playbook="Mine.")
        row, prompt = self.decided()
        self.assertIn("Mine.", prompt)
        self.assertNotIn("TEAM INSTRUCTIONS", prompt)
        self.assertEqual(row.asset_ids, [mine.pk])
        self.assertEqual(row.raw["content_source"], "own")

    def test_the_admin_stopping_sharing_moves_reps_to_their_own_but_not_the_admins_agent(self):
        self.team(share_content=False, playbook="TEAM INSTRUCTIONS")
        self.assertFalse(uses_team_content(self.agent, self.settings))
        self.assertEqual(content_for(self.agent, self.settings)[0], "own")
        admins = LinkedInAgent.objects.create(business_record=self.business, agent_name="Admin's own")
        own_settings = AgentEmailSettings.objects.create(agent=admins, business=self.business)
        self.assertTrue(uses_team_content(admins, own_settings))

    def test_inactive_team_attachments_are_not_sent(self):
        self.team_item("Old team deck", active=False)
        live = self.team_item("Team deck")
        row, _prompt = self.decided()
        self.assertEqual(row.asset_ids, [live.pk])


class TeamAttachmentOwnershipTests(TeamBase):
    def test_the_payload_sends_the_teams_attachment_but_never_another_businesss(self):
        mine = self.team_item("Team deck")
        other_biz = BusinessRecord.objects.create(business_name="Other", address="2 St",
                                                  user=make_user(f"o-{self.id()}@example.com"), status="Active")
        theirs = self.team_item("Their deck", business=other_biz)
        row = self.draft(asset_ids=[mine.pk, theirs.pk])
        attachments, _links = build_payload(row)
        self.assertEqual([name for name, _b, _m in attachments], [mine.filename])
        self.assertEqual(engine.allowed_asset_ids(self.agent, [mine.pk, theirs.pk]), {mine.pk})


# ===========================================================================
# 2. Who may change it
# ===========================================================================
class TeamApiTests(TeamBase):
    def test_the_admin_can_change_the_switches_and_the_content(self):
        r = self.view_team("patch", {"share_mailbox": False, "playbook": "Be brief."}, user=self.as_admin())
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(r.data["share_mailbox"])
        self.assertTrue(r.data["can_manage"])
        team = TeamEmailSettings.objects.get(business=self.business)
        self.assertEqual(team.playbook, "Be brief.")

    def test_a_rep_sees_the_team_but_cannot_change_it(self):
        self.team(playbook="Be brief.")
        user = self.as_rep()
        r = self.view_team("get", user=user)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(r.data["can_manage"])
        self.assertEqual(r.data["playbook"], "Be brief.")
        self.assertIsNone(r.data["usage"])
        r = self.view_team("patch", {"share_content": False}, user=user)
        self.assertEqual(r.status_code, 403, r.data)
        self.assertTrue(TeamEmailSettings.objects.get(business=self.business).share_content)

    def test_a_sub_admin_cannot_share_the_admins_mailbox(self):
        r = self.view_team("patch", {"share_mailbox": True}, user=self.as_sub_admin())
        self.assertEqual(r.status_code, 403, r.data)
        self.assertIn("only the business admin", str(r.data))

    def test_defaults_without_a_saved_row_are_both_on(self):
        r = self.view_team("get", user=self.as_admin())
        self.assertTrue(r.data["share_mailbox"])
        self.assertTrue(r.data["share_content"])
        self.assertFalse(TeamEmailSettings.objects.filter(business=self.business).exists())

    def test_an_empty_team_fixed_template_is_refused(self):
        r = self.view_team("patch", {"body_mode": "fixed", "fixed_body": "  "}, user=self.as_admin())
        self.assertEqual(r.status_code, 400, r.data)

    def test_usage_names_who_would_be_left_without_a_mailbox(self):
        stuck = LinkedInAgent.objects.create(
            business_record=self.business, agent_name="No Mailbox Rep",
            partner=Partners.objects.create(user=make_user(f"nm-{self.id()}@example.com"),
                                            publisher=self.business, business_name="NM"),
        )
        AgentEmailSettings.objects.create(agent=stuck, business=self.business, mode="auto")
        self.toggle(use_team_mailbox=True)
        r = self.view_team("get", user=self.as_admin())
        usage = r.data["usage"]
        self.assertEqual(usage["members"], 2)
        self.assertEqual(usage["on_mailbox"], 2)
        self.assertEqual(usage["need_own_mailbox"], ["No Mailbox Rep"])   # self.rep has Gmail

    def test_a_team_request_for_an_agent_outside_your_scope_is_not_found(self):
        other_biz = BusinessRecord.objects.create(business_name="Other", address="2 St",
                                                  user=make_user(f"o2-{self.id()}@example.com"), status="Active")
        outsider = LinkedInAgent.objects.create(business_record=other_biz, agent_name="Outsider")
        view = TeamEmailSettingsView.as_view()
        r = self.call(view, "patch", f"/api/v1/email-orchestrator/team/?agent={outsider.pk}",
                      {"share_mailbox": False}, user=self.as_admin())
        self.assertEqual(r.status_code, 404, r.data)


class TeamAttachmentApiTests(TeamBase):
    def upload(self, user, team):
        view = EmailAssetViewSet.as_view({"post": "create"})
        f = SimpleUploadedFile("deck.pdf", b"%PDF-1.4", content_type="application/pdf")
        data = {"agent": self.agent.pk, "kind": "file", "name": "Deck", "file": f}
        if team:
            data["team"] = "1"
        return self.call(view, "post", "/api/v1/email-orchestrator/assets/", data, user=user, fmt="multipart")

    def test_the_admin_uploads_team_attachments_and_reps_only_their_own(self):
        r = self.upload(self.as_admin(), team=True)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["is_team"])
        team_asset = EmailAsset.objects.get(pk=r.data["id"])
        self.assertIsNone(team_asset.agent_id)
        self.assertEqual(team_asset.business_id, self.business.pk)

        rep = self.as_rep()
        self.assertEqual(self.upload(rep, team=True).status_code, 403)
        self.assertEqual(self.upload(rep, team=False).status_code, 201)

        listing = EmailAssetViewSet.as_view({"get": "list"})
        r = self.call(listing, "get", f"/api/v1/email-orchestrator/assets/?agent={self.agent.pk}&team=1", user=rep)
        self.assertEqual([a["id"] for a in self.rows(r)], [team_asset.pk])

        destroy = EmailAssetViewSet.as_view({"delete": "destroy"})
        r = self.call(destroy, "delete", f"/api/v1/email-orchestrator/assets/{team_asset.pk}/", user=rep, pk=team_asset.pk)
        self.assertEqual(r.status_code, 403)
        r = self.call(destroy, "delete", f"/api/v1/email-orchestrator/assets/{team_asset.pk}/",
                      user=self.as_admin(), pk=team_asset.pk)
        self.assertEqual(r.status_code, 204)


class AgentSwitchApiTests(TeamBase):
    def patch_settings(self, data, user):
        view = AgentEmailSettingsViewSet.as_view({"patch": "partial_update"})
        return self.call(view, "patch", f"/api/v1/email-orchestrator/settings/{self.agent.pk}/",
                         data, user=user, pk=self.agent.pk)

    def test_a_half_written_own_template_does_not_block_while_on_team_content(self):
        user = self.as_rep()
        r = self.patch_settings({"mode": "review", "body_mode": "fixed", "fixed_body": ""}, user)
        self.assertEqual(r.status_code, 200, r.data)
        r = self.patch_settings({"use_team_content": False}, user)
        self.assertEqual(r.status_code, 400, r.data)     # now it would be what is sent
        self.assertIn("fixed_body", r.data)

    def test_readiness_reports_the_switches_and_what_is_in_effect(self):
        self.team(share_content=False)
        view = AgentEmailSettingsViewSet.as_view({"get": "retrieve"})
        r = self.call(view, "get", f"/api/v1/email-orchestrator/settings/{self.agent.pk}/",
                      user=self.as_rep(), pk=self.agent.pk)
        rd = r.data["readiness"]
        self.assertTrue(rd["use_team_content"])
        self.assertFalse(rd["uses_team_content"])
        self.assertFalse(rd["team"]["share_content"])
        self.assertEqual(rd["team"]["admin_name"], "Ada Admin")

    def test_copy_team_content_starts_the_reps_own_from_the_admins(self):
        self.team(playbook="Be brief.", cta_text="Talk to us", cta_url=TEAM_LINK)
        deck = self.team_item("Team deck")
        self.item("team deck")                       # same name, different case: kept, not duplicated
        case = self.team_item("Case study")
        view = AgentEmailSettingsViewSet.as_view({"post": "copy_team_content"})
        r = self.call(view, "post", f"/api/v1/email-orchestrator/settings/{self.agent.pk}/copy_team_content/",
                      {}, user=self.as_rep(), pk=self.agent.pk)
        self.assertEqual(r.status_code, 200, r.data)
        self.settings.refresh_from_db()
        self.assertFalse(self.settings.use_team_content)
        self.assertEqual(self.settings.playbook, "Be brief.")
        self.assertEqual(self.settings.cta_url, TEAM_LINK)
        own = sorted(EmailAsset.objects.filter(agent=self.agent).values_list("name", flat=True))
        self.assertEqual(own, ["Case study", "team deck"])
        copy = EmailAsset.objects.get(agent=self.agent, name="Case study")
        self.assertEqual(copy.file.name, case.file.name)                   # same stored file
        self.assertTrue(EmailAsset.objects.filter(pk=deck.pk, agent__isnull=True).exists())  # team untouched


# ===========================================================================
# 3. The migration that turns sharing on for existing teams
# ===========================================================================
class SeedTeamContentMigrationTests(TeamBase):
    def seed(self):
        MIGRATION.seed_team_content(django_apps, None)

    def test_the_admins_agent_content_becomes_the_team_content(self):
        admins = LinkedInAgent.objects.create(business_record=self.business, agent_name="Admin's own")
        AgentEmailSettings.objects.create(agent=admins, business=self.business, playbook="Admin playbook",
                                          cta_url=TEAM_LINK)
        deck = self.item("Brochure", agent=admins)
        self.toggle(playbook="rep playbook")

        self.seed()
        self.seed()  # a re-run changes nothing

        team = TeamEmailSettings.objects.get(business=self.business)
        self.assertEqual(team.playbook, "Admin playbook")
        self.assertEqual(team.cta_url, TEAM_LINK)
        copies = EmailAsset.objects.filter(agent__isnull=True, business=self.business, deleted=False)
        self.assertEqual(copies.count(), 1)
        self.assertEqual(copies.get().file.name, deck.file.name)     # same stored file
        self.assertTrue(EmailAsset.objects.filter(pk=deck.pk, agent=admins).exists())
        # Everyone follows the admin; the rep's own content is kept for later.
        self.settings.refresh_from_db()
        self.assertTrue(self.settings.use_team_content)
        self.assertEqual(self.settings.playbook, "rep playbook")
        self.assertTrue(AgentEmailSettings.objects.get(agent=admins).use_team_content)

    def test_a_second_admin_agent_with_its_own_content_keeps_it(self):
        older = LinkedInAgent.objects.create(business_record=self.business, agent_name="Older")
        s_old = AgentEmailSettings.objects.create(agent=older, business=self.business, playbook="older")
        newer = LinkedInAgent.objects.create(business_record=self.business, agent_name="Newer")
        AgentEmailSettings.objects.create(agent=newer, business=self.business, playbook="newer")
        AgentEmailSettings.objects.filter(pk=s_old.pk).update(updated_at=timezone.now() - timedelta(days=1))
        self.seed()
        self.assertEqual(TeamEmailSettings.objects.get(business=self.business).playbook, "newer")
        s_old.refresh_from_db()
        self.assertFalse(s_old.use_team_content)

    def test_a_team_with_no_admin_content_starts_empty(self):
        self.seed()
        team = TeamEmailSettings.objects.get(business=self.business)
        self.assertEqual(team.playbook, "")
        self.assertTrue(team.share_mailbox and team.share_content)

    def test_an_admin_already_set_team_is_never_overwritten(self):
        self.team(playbook="set by the admin")
        admins = LinkedInAgent.objects.create(business_record=self.business, agent_name="Admin's own")
        AgentEmailSettings.objects.create(agent=admins, business=self.business, playbook="from the agent")
        self.seed()
        self.assertEqual(TeamEmailSettings.objects.get(business=self.business).playbook, "set by the admin")
