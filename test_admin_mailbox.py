"""The admin's mailbox, shared with the team: who carries the email.

The base fixture makes one user both the business admin and the agent's
partner. Here the rep (the agent's partner user) and the admin (the
BusinessRecord's user) are different people, as they are for every real
sales team. Rule under test: the admin's email is used when the admin shares
it AND the rep's own switch is on; otherwise the rep's own.
"""
from unittest.mock import patch

from rest_framework.test import APIRequestFactory, force_authenticate

from agentic_deals.tests import make_user
from common.models import EMAIL_SOURCE_AI_SDR, EmailLog
from integration.models import GmailIntegration
from lead.models import Partners, UniversalLog
from outreach.models import SenderConfiguration
from user.middleware import set_user_context

from . import engine
from .models import AgentEmailSettings, EmailDecision, TeamEmailSettings
from .readiness import readiness, resolve_sender
from .sending import dispatch_send
from .tests import ADDRESS, GEMINI, SEND, Base, model_says, sent_ok
from .views import AgentEmailSettingsViewSet

ADMIN_EMAIL = "admin@example.com"
REP_EMAIL = "rep@example.com"


class SharedBase(Base):
    """self.owner is the business ADMIN; self.rep owns the agent."""

    def setUp(self):
        super().setUp()
        self.admin = self.owner
        self.admin.first_name, self.admin.last_name = "Ada", "Admin"
        self.admin.save()
        self.rep = make_user(f"rep-{self.id()}@example.com")
        self.rep.first_name, self.rep.last_name = "Ravi", "Rep"
        self.rep.save()
        self.rep_partner = Partners.objects.create(user=self.rep, publisher=self.business, business_name="Rep")
        self.agent.partner = self.rep_partner
        self.agent.save()
        self.agent.refresh_from_db()
        self.factory = APIRequestFactory()

    # -- mailboxes ---------------------------------------------------------
    def admin_gmail(self, refresh="r", sign=""):
        return GmailIntegration.objects.create(
            user=self.admin, email=ADMIN_EMAIL, access_token="t", refresh_token=refresh, gmail_sign=sign,
        )

    def rep_gmail(self, refresh="r"):
        return GmailIntegration.objects.create(
            user=self.rep, email=REP_EMAIL, access_token="t", refresh_token=refresh,
        )

    def toggle(self, **kw):
        for k, v in kw.items():
            setattr(self.settings, k, v)
        self.settings.save()
        self.agent.refresh_from_db()

    def team(self, **kw):
        team, _ = TeamEmailSettings.objects.get_or_create(business=self.business)
        for k, v in kw.items():
            setattr(team, k, v)
        team.save()
        return team

    # -- tenancy contexts --------------------------------------------------
    def as_admin(self):
        set_user_context(user_id=self.admin.id, business_record_id=self.business.id,
                         master_admin=False, partner_id=None)
        return self.admin

    def as_rep(self):
        set_user_context(user_id=self.rep.id, business_record_id=self.business.id,
                         master_admin=False, partner_id=self.rep_partner.id)
        return self.rep

    def call(self, view, method, path, data=None, user=None, fmt="json", **kwargs):
        # A real request always carries the JWT header. Without one, the
        # project's custom_exception_handler (user.permissions) rewrites every
        # raised 403 into a 401, so a correct "forbidden" would look like
        # "not logged in". force_authenticate still decides who the user is.
        req = getattr(self.factory, method)(path, data, format=fmt, HTTP_AUTHORIZATION="Bearer test")
        force_authenticate(req, user=user)
        resp = view(req, **kwargs)
        if hasattr(resp, "render"):
            resp.render()
        return resp

    @staticmethod
    def rows(resp):
        data = resp.data
        return data["results"] if isinstance(data, dict) and "results" in data else data

    # -- material ----------------------------------------------------------
    def draft(self, asset_ids=None):
        return EmailDecision.objects.create(
            agent=self.agent, business=self.business, contact=self.contact, deal=self.deal,
            sdr_lead_id=self.sdr_lead.pk, conversation_id=self.conv.pk,
            trigger_source=EmailDecision.TRIGGER_MANUAL,
            trigger_ref=f"shared:{EmailDecision.objects.count() + 1}",
            status=EmailDecision.STATUS_AWAITING_REVIEW, should_email=True, recipient_email=ADDRESS,
            asset_ids=asset_ids or [], subject="Details", body_html="<p>Here you go.</p>",
            evidence=["Share the details over the mail"],
        )

    def asked(self):
        with patch("email_orchestrator.tasks.evaluate_decision"):
            self.message(ADDRESS)
            self.message("Share the details over the mail will get back to you")
        return EmailDecision.objects.get(contact=self.contact)


# ===========================================================================
# 1. Who sends
# ===========================================================================
class TeamMailboxReadinessTests(SharedBase):
    def test_a_new_agent_follows_the_admin_by_default(self):
        fresh = AgentEmailSettings(agent=self.agent)
        self.assertTrue(fresh.use_team_mailbox)
        self.assertTrue(fresh.use_team_content)
        self.admin_gmail()
        self.toggle(use_team_mailbox=True)
        r = readiness(self.agent, self.settings)
        self.assertTrue(r["ready"])
        self.assertTrue(r["shared"])
        self.assertTrue(r["uses_team_mailbox"])
        self.assertEqual(r["from_email"], ADMIN_EMAIL)
        self.assertEqual(r["owner_user_id"], self.rep.id)           # attributed to the rep
        self.assertEqual(r["mailbox_owner_user_id"], self.admin.id)  # carried by the admin
        self.assertEqual(resolve_sender(self.agent, self.settings)["user"].pk, self.rep.pk)

    def test_on_means_the_admins_email_even_when_the_rep_has_one(self):
        self.admin_gmail()
        self.rep_gmail()
        self.toggle(use_team_mailbox=True)
        r = readiness(self.agent, self.settings)
        self.assertEqual(r["from_email"], ADMIN_EMAIL)
        self.assertTrue(r["own_mailbox"]["ready"])

    def test_the_reps_own_switch_off_means_their_own_mailbox(self):
        self.admin_gmail()
        self.rep_gmail()
        self.toggle(use_team_mailbox=False)
        r = readiness(self.agent, self.settings)
        self.assertEqual(r["from_email"], REP_EMAIL)
        self.assertFalse(r["shared"])
        self.assertFalse(r["uses_team_mailbox"])

    def test_switched_off_with_no_mailbox_of_their_own_is_not_ready(self):
        self.admin_gmail()
        self.toggle(use_team_mailbox=False)
        r = readiness(self.agent, self.settings)
        self.assertFalse(r["ready"])
        self.assertIn("no_mailbox", r["reasons"])

    def test_when_the_admins_mailbox_cannot_send_the_reps_own_is_used(self):
        self.admin_gmail(refresh="")
        self.rep_gmail()
        self.toggle(use_team_mailbox=True)
        r = readiness(self.agent, self.settings)
        self.assertTrue(r["ready"])
        self.assertEqual(r["from_email"], REP_EMAIL)
        self.assertIn("admin_no_mailbox", r["reasons"])

    def test_neither_mailbox_is_not_ready_and_says_both(self):
        self.toggle(use_team_mailbox=True)
        r = readiness(self.agent, self.settings)
        self.assertFalse(r["ready"])
        self.assertIn("no_mailbox", r["reasons"])
        self.assertIn("admin_no_mailbox", r["reasons"])

    def test_the_admin_stopping_sharing_applies_to_everyone_and_comes_back(self):
        self.admin_gmail()
        self.rep_gmail()
        self.toggle(use_team_mailbox=True)
        self.team(share_mailbox=False)
        r = readiness(self.agent, self.settings)
        self.assertEqual(r["from_email"], REP_EMAIL)
        self.assertFalse(r["uses_team_mailbox"])
        self.assertTrue(r["use_team_mailbox"])          # the rep's own choice is kept
        self.assertFalse(r["team"]["share_mailbox"])
        self.team(share_mailbox=True)
        self.assertEqual(readiness(self.agent, self.settings)["from_email"], ADMIN_EMAIL)

    def test_the_admins_own_agent_sends_from_the_admins_mailbox_unshared(self):
        self.agent.partner = None
        self.agent.save()
        self.agent.refresh_from_db()
        self.admin_gmail()
        self.toggle(use_team_mailbox=True)
        r = readiness(self.agent, self.settings)
        self.assertTrue(r["is_admin_agent"])
        self.assertFalse(r["shared"])
        self.assertFalse(r["uses_team_mailbox"])
        self.assertEqual(r["from_email"], ADMIN_EMAIL)

    def test_an_explicit_sender_wins_over_the_team_mailbox(self):
        self.admin_gmail()
        sender = SenderConfiguration.objects.create(
            business=self.business, use_smtp=True, use_gmail=False, email="sales@acme.example",
            password="pw", smtp_host="smtp.acme.example", smtp_port=587,
        )
        self.toggle(use_team_mailbox=True, sender_config=sender)
        r = readiness(self.agent, self.settings)
        self.assertEqual(r["sender_type"], "sender_config")
        self.assertFalse(r["shared"])


class TeamMailboxApiTests(SharedBase):
    def test_one_save_can_switch_to_the_admins_email_and_choose_auto(self):
        self.admin_gmail()
        user = self.as_rep()
        view = AgentEmailSettingsViewSet.as_view({"patch": "partial_update"})
        path = f"/api/v1/email-orchestrator/settings/{self.agent.pk}/"
        r = self.call(view, "patch", path, {"mode": "auto", "use_team_mailbox": False}, user=user, pk=self.agent.pk)
        self.assertEqual(r.status_code, 400, r.data)  # the rep has no mailbox of their own
        r = self.call(view, "patch", path, {"mode": "auto", "use_team_mailbox": True}, user=user, pk=self.agent.pk)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["readiness"]["shared"])
        self.assertEqual(r.data["readiness"]["from_email"], ADMIN_EMAIL)
        self.assertTrue(r.data["use_team_mailbox"])


class TeamMailboxSendTests(SharedBase):
    def test_a_shared_send_goes_through_admin_gmail_attributed_and_signed_as_rep(self):
        admin_gi = self.admin_gmail(sign="<p>ADMIN SIGNATURE +1 555</p>")
        self.toggle(use_team_mailbox=True)
        row = self.draft()
        with patch(SEND, return_value=sent_ok(from_email=ADMIN_EMAIL)) as send:
            dispatch_send(row, countdown_seconds=0)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_SENT)
        kw = send.call_args.kwargs
        self.assertEqual(kw["gmail_integration"].pk, admin_gi.pk)
        self.assertEqual(kw["user"].pk, self.rep.pk)
        self.assertNotIn("ADMIN SIGNATURE", kw["html_body"])
        self.assertIn("Ravi Rep", kw["html_body"])
        self.assertTrue(row.raw["mailbox_fallback"])
        note = UniversalLog.objects.filter(model_name="Contact", model_id=self.contact.pk,
                                           log__0__action="custom_log").latest("id")
        self.assertEqual(note.log[0]["performed_by"]["id"], self.rep.id)

    def test_the_admin_stopping_sharing_before_the_send_holds_it(self):
        self.admin_gmail()
        self.toggle(use_team_mailbox=True)
        row = self.draft()
        self.team(share_mailbox=False)
        with patch(SEND) as send:
            dispatch_send(row, countdown_seconds=0)
        row.refresh_from_db()
        send.assert_not_called()
        self.assertEqual(row.status, EmailDecision.STATUS_HELD)
        self.assertIn("no connected mailbox", row.hold_reason)

    def test_an_smtp_sender_is_not_hijacked_by_the_owners_gmail(self):
        """Regression: send_email resolves a Gmail account from `user` and
        prefers it over smtp_config, so an SMTP sender used to go out via
        the owner's Gmail."""
        self.rep_gmail()
        sender = SenderConfiguration.objects.create(
            business=self.business, use_smtp=True, use_gmail=False, email="sales@acme.example",
            password="pw", smtp_host="smtp.acme.example", smtp_port=587,
        )
        self.toggle(sender_config=sender)
        row = self.draft()

        def fake_send(**kw):
            EmailLog.objects.create(to_email=kw["to_email"], subject=kw["subject"], source=EMAIL_SOURCE_AI_SDR,
                                    source_id=kw["source_id"], status=EmailLog.STATUS_SENT,
                                    from_email="sales@acme.example")
            return sent_ok(via="smtp", from_email="sales@acme.example")

        with patch(SEND, side_effect=fake_send) as send:
            dispatch_send(row, countdown_seconds=0)
        kw = send.call_args.kwargs
        self.assertIsNone(kw["user"])
        self.assertNotIn("gmail_integration", kw)
        self.assertEqual(kw["smtp_config"]["host"], "smtp.acme.example")
        # sent_by is still the rep, set after the send
        log = EmailLog.objects.get(source=EMAIL_SOURCE_AI_SDR, source_id=str(row.pk))
        self.assertEqual(log.sent_by_id, self.rep.id)

    def test_the_writer_is_told_whose_mailbox_carries_the_email(self):
        self.admin_gmail()
        self.toggle(use_team_mailbox=True)
        row = self.asked()
        with patch(GEMINI, return_value=model_says()) as g:
            engine.decide(row.pk)
        prompt = g.call_args[0][0]
        self.assertIn("SENDING FROM", prompt)
        self.assertIn(ADMIN_EMAIL, prompt)

    def test_no_mailbox_line_when_the_rep_sends_from_their_own(self):
        self.rep_gmail()
        row = self.asked()
        with patch(GEMINI, return_value=model_says()) as g:
            engine.decide(row.pk)
        self.assertNotIn("SENDING FROM", g.call_args[0][0])
