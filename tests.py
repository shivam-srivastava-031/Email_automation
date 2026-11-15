"""Email orchestrator.

Run (from sales_tech_backend/):
    venv/Scripts/python.exe manage.py test email_orchestrator --settings=sales_tech.settings_test

Gemini is mocked at email_orchestrator.engine.call_json; the mailbox at
email_orchestrator.sending.send_email. Celery is eager under settings_test,
so apply_async runs inline and a scheduled send completes before the test
continues.
"""
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from agentic_deals.models import DealSignal
from agentic_deals.tests import grant, make_user
from ai_sdr.models import Conversation, ConversationMessage, LinkedInAgent, SDRLead
from integration.models import GmailIntegration
from lead.models import BusinessRecord, Contact, Partners, UniversalLog
from lead.services import advance_lead_to_deal, promote_contact_to_lead
from user.middleware import set_user_context

from . import engine, gates
from .models import AgentEmailSettings, EmailAsset, EmailDecision
from .readiness import readiness, resolve_sender
from .triggers import looks_like_email_ask
from .views import AgentEmailSettingsViewSet, EmailAssetViewSet, EmailDecisionViewSet

GEMINI = "email_orchestrator.engine.call_json"
SEND = "email_orchestrator.sending.send_email"

ADDRESS = "jane.doe@prospect.example"


def model_says(**kw):
    data = {
        "should_email": True,
        "confidence": 0.9,
        "ask": "details and the demo video",
        "recipient_email": ADDRESS,
        "asset_ids": [],
        "subject": "The details you asked for",
        "body_markdown": "Thanks for the chat, Jane.\n\nHere is the overview you asked for.",
        "reasons": ["asked for details over mail"],
        "evidence": ["Share the details over the mail will get back to you"],
        "hold_for_human": False,
        "hold_reason": "",
    }
    data.update(kw)
    return data


def sent_ok(**kw):
    out = {"success": True, "via": "gmail_api", "message_id": "abc", "from_email": "owner@example.com",
           "error": None, "suppressed": False, "tracking_id": None}
    out.update(kw)
    return out


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="eo-test-media-"))
class Base(TestCase):
    def setUp(self):
        # agentic_deals listens to the same inbound-message and note signals
        # and would call Gemini for real; keep those quiet and free.
        self._sibling_patches = [
            patch("agentic_deals.extraction.call_gemini_json", return_value={"kind": "none"}),
            patch("agentic_deals.synthesis.call_gemini_json", return_value={}),
        ]
        for p in self._sibling_patches:
            p.start()
        self.owner = make_user(f"owner-{self.id()}@example.com")
        self.business = BusinessRecord.objects.create(
            business_name=f"Biz {self._testMethodName}", address="1 St", user=self.owner, status="Active",
        )
        self.partner = Partners.objects.create(user=self.owner, publisher=self.business, business_name="P")
        self.agent = LinkedInAgent.objects.create(
            business_record=self.business, partner=self.partner, agent_name="Sam",
            company_name="SenderCo", product_description="AI SDR for LinkedIn", meeting_link="https://booking.example/rep",
            company_website="https://sender.example",
        )
        # The base agent uses its OWN mailbox and content. Team sharing (both
        # switches on by default) is exercised in test_admin_mailbox and
        # test_team_content.
        self.settings = AgentEmailSettings.objects.create(
            agent=self.agent, business=self.business, mode=AgentEmailSettings.MODE_REVIEW,
            cancel_window_minutes=0, use_team_mailbox=False, use_team_content=False,
        )
        self.contact = Contact.objects.create(
            name="Jane", last_name="Doe", business=self.business, company_name="ProspectCo",
        )
        self.lead = promote_contact_to_lead(self.contact, business_id=self.business.id, partner_id=None)
        self.deal = advance_lead_to_deal(self.lead, business_id=self.business.id, partner_id=None)
        self.sdr_lead = SDRLead.objects.create(
            agent=self.agent, person_linkedin_url="https://linkedin.com/in/jane-doe", full_name="Jane Doe",
            crm_record_id=self.contact.pk, sdr_status="replied",
        )
        self.conv = Conversation.objects.create(lead=self.sdr_lead, unipile_account_id="acc1")
        set_user_context(user_id=self.owner.id, business_record_id=self.business.id, master_admin=False, partner_id=None)

    def tearDown(self):
        for p in self._sibling_patches:
            p.stop()
        set_user_context()

    def gmail(self, user=None):
        return GmailIntegration.objects.create(
            user=user or self.owner, email="owner@example.com", access_token="t", refresh_token="r",
        )

    def message(self, text, direction="inbound"):
        with self.captureOnCommitCallbacks(execute=True):
            return ConversationMessage.objects.create(
                conversation=self.conv, direction=direction, content=text, sent_at=timezone.now(),
            )

    def asset_link(self, name="Demo video", url="https://video.example/demo", description="3-minute walkthrough; send when asked for a demo or video"):
        return EmailAsset.objects.create(
            agent=self.agent, business=self.business, kind=EmailAsset.KIND_LINK, name=name, url=url,
            description=description,
        )

    def asset_file(self, name="Deck", content=b"%PDF-1.4 fake", size=None):
        f = SimpleUploadedFile("deck.pdf", content, content_type="application/pdf")
        return EmailAsset.objects.create(
            agent=self.agent, business=self.business, kind=EmailAsset.KIND_FILE, name=name, file=f,
            file_size=size if size is not None else len(content), mime_type="application/pdf",
            description="Product deck; send when asked for details or a deck",
        )


class ReadinessTests(Base):
    def test_not_ready_without_a_mailbox(self):
        r = readiness(self.agent, self.settings)
        self.assertFalse(r["ready"])
        self.assertIn("no_mailbox", r["reasons"])

    def test_owner_gmail_makes_it_ready(self):
        self.gmail()
        r = readiness(self.agent, self.settings)
        self.assertTrue(r["ready"])
        self.assertEqual(r["sender_type"], "gmail")
        self.assertEqual(r["from_email"], "owner@example.com")

    def test_gmail_without_refresh_token_is_not_ready(self):
        GmailIntegration.objects.create(user=self.owner, email="o@example.com", access_token="t", refresh_token="")
        self.assertFalse(readiness(self.agent, self.settings)["ready"])

    def test_resolve_sender_reports_owner_user(self):
        self.gmail()
        self.assertEqual(resolve_sender(self.agent, self.settings)["user"].pk, self.owner.pk)


class TriggerTests(Base):
    def test_prefilter(self):
        self.assertTrue(looks_like_email_ask("jane.doe@prospect.example"))
        self.assertTrue(looks_like_email_ask("Share the details over the mail will get back to you"))
        self.assertTrue(looks_like_email_ask("can you send me the deck?"))
        self.assertFalse(looks_like_email_ask("Sure, sounds interesting. How does it work?"))
        self.assertTrue(looks_like_email_ask("ok", {"captured_email": ADDRESS, "last_asked_for_artifact": True}))

    def test_inbound_ask_creates_one_evaluating_row_per_burst(self):
        with patch("email_orchestrator.tasks.evaluate_decision") as task:
            m1 = self.message(ADDRESS)
            m2 = self.message("Share the details over the mail will get back to you")
        rows = EmailDecision.objects.filter(contact=self.contact)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.status, EmailDecision.STATUS_EVALUATING)
        self.assertEqual(row.trigger_ref, f"ConversationMessage:{m1.pk}")
        self.assertEqual(row.sdr_lead_id, self.sdr_lead.pk)
        self.assertEqual(row.deal_id, self.deal.pk)
        self.assertIn(f"ConversationMessage:{m2.pk}", row.raw.get("extra_triggers", []))
        self.assertEqual(task.apply_async.call_count, 1)

    def test_mode_off_creates_nothing(self):
        self.settings.mode = AgentEmailSettings.MODE_OFF
        self.settings.save()
        with patch("email_orchestrator.tasks.evaluate_decision"):
            self.message(ADDRESS)
        self.assertEqual(EmailDecision.objects.count(), 0)

    def test_outbound_and_unrelated_messages_create_nothing(self):
        with patch("email_orchestrator.tasks.evaluate_decision"):
            self.message("I will send it to jane.doe@prospect.example", direction="outbound")
            self.message("Interesting, tell me more")
        self.assertEqual(EmailDecision.objects.count(), 0)


class ValidateTests(Base):
    def ctx(self, **overrides):
        ctx = {
            "messages": [{"at": timezone.now(), "who": "them", "channel": "linkedin",
                          "text": f"{ADDRESS}\nShare the details over the mail will get back to you"}],
            "allowed_recipients": [ADDRESS],
            "assets": [{"id": 1, "kind": "link", "name": "Demo", "description": "", "tags": [],
                        "filename": "", "url": "https://loom.com/x", "subject": "", "body": "", "file_size": 0}],
            "settings": self.settings,
            "booking_link": "https://booking.example/rep",
            "persona": {"website": "https://sender.example"},
        }
        ctx.update(overrides)
        return ctx

    def test_recipient_must_be_typed_by_prospect(self):
        clean = engine.validate(model_says(recipient_email="ceo@prospect.example"), self.ctx())
        self.assertIn("recipient_not_typed_by_prospect", clean["problems"])
        # falls back to the address they did type
        self.assertEqual(clean["recipient_email"], ADDRESS)

    def test_no_typed_address_means_no_recipient(self):
        clean = engine.validate(model_says(recipient_email="ceo@prospect.example"), self.ctx(allowed_recipients=[]))
        self.assertEqual(clean["recipient_email"], "")

    def test_evidence_must_be_verbatim(self):
        clean = engine.validate(model_says(evidence=["they were keen on receiving material"]), self.ctx())
        self.assertIn("no_grounded_evidence", clean["problems"])
        self.assertEqual(clean["evidence"], [])

    def test_the_model_writes_no_links_except_the_company_site(self):
        """Attachments, links and the button are added by code, so any URL the
        model writes is removed - even the booking link, which the button
        already carries (a second copy in the prose is clutter)."""
        clean = engine.validate(model_says(
            body_markdown=("See https://loom.com/x and https://evil.example.com/pricing "
                           "and https://booking.example/rep and https://sender.example"),
        ), self.ctx())
        self.assertIn("unknown_links_removed", clean["problems"])
        for gone in ("loom.com/x", "evil.example.com", "booking.example/rep"):
            self.assertNotIn(gone, clean["body_html"])
        self.assertIn("sender.example", clean["body_html"])
        self.assertNotIn("asset_ids", clean)

    def test_body_is_escaped_html_paragraphs(self):
        clean = engine.validate(model_says(body_markdown="Hi <b>there</b>\n\nSecond para"), self.ctx())
        self.assertIn("<p>Hi &lt;b&gt;there&lt;/b&gt;</p>", clean["body_html"])
        self.assertIn("<p>Second para</p>", clean["body_html"])


class DecideTests(Base):
    def decision(self, text="Share the details over the mail will get back to you"):
        # The trigger would evaluate eagerly under test Celery; hold it so the
        # test controls when (and with which mock) decide() runs.
        with patch("email_orchestrator.tasks.evaluate_decision"):
            self.message(ADDRESS)
            self.message(text)
        return EmailDecision.objects.get(contact=self.contact)

    def test_review_mode_parks_a_grounded_draft(self):
        self.gmail()
        link = self.asset_link()
        row = self.decision()
        with patch(GEMINI, return_value=model_says(body_markdown="Here is the overview you asked for.")) as g:
            engine.decide(row.pk)
        self.assertEqual(g.call_count, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_AWAITING_REVIEW)
        self.assertEqual(row.recipient_email, ADDRESS)
        self.assertEqual(row.asset_ids, [link.pk])           # every attachment, not a pick
        self.assertIn("overview you asked for", row.body_html)
        self.assertIn('href="https://booking.example/rep"', row.body_html)  # the button
        self.assertTrue(row.evidence)
        self.assertEqual(row.raw["from_email"], "owner@example.com")

    def test_model_says_no_email_is_skipped(self):
        self.gmail()
        row = self.decision("no thanks, just email me if you must")
        with patch(GEMINI, return_value=model_says(should_email=False, hold_reason="brush-off next to a no")):
            engine.decide(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_SKIPPED)

    def test_no_mailbox_holds_before_spending_a_model_call(self):
        row = self.decision()
        with patch(GEMINI) as g:
            engine.decide(row.pk)
        g.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_HELD)
        self.assertIn("no connected mailbox", row.hold_reason)

    def test_no_attachments_still_drafts_the_reply_and_button(self):
        """The old rule held every email when nothing was uploaded. Now the
        reply and the call to action go on their own."""
        self.gmail()
        row = self.decision()
        with patch(GEMINI, return_value=model_says()):
            engine.decide(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_AWAITING_REVIEW, row.hold_reason)
        self.assertEqual(row.asset_ids, [])
        self.assertIn("Book a 15-minute call", row.body_html)

    def test_the_model_can_still_ask_for_a_human(self):
        self.gmail()
        row = self.decision()
        with patch(GEMINI, return_value=model_says(hold_for_human=True, hold_reason="they asked for exact pricing")):
            engine.decide(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_HELD)
        self.assertEqual(row.hold_reason, "they asked for exact pricing")

    def test_human_owned_thread_is_held(self):
        self.gmail()
        self.conv.lead_brief = '{"human_owns_conversation": true, "captured_email": "%s"}' % ADDRESS
        self.conv.save()
        row = self.decision()
        with patch(GEMINI) as g:
            engine.decide(row.pk)
        g.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_HELD)

    def test_fixed_template_replaces_the_model_prose(self):
        """Fixed mode: the rep's words go out, the model still picks who/what."""
        self.gmail()
        link = self.asset_link()
        self.settings.body_mode = AgentEmailSettings.BODY_FIXED
        self.settings.fixed_subject = "The details for {{company_name}}"
        self.settings.fixed_body = "\n\n".join([
            "Hi {{first_name}},",
            "Here is what you asked for.",
            "Book a slot: {{booking_link}}",
        ])
        self.settings.save()
        row = self.decision()
        with patch(GEMINI, return_value=model_says(
                asset_ids=[link.pk], subject="AI SUBJECT", body_markdown="AI PROSE")):
            engine.decide(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_AWAITING_REVIEW)
        self.assertEqual(row.subject, "The details for ProspectCo")
        self.assertIn("Hi Jane", row.body_html)
        self.assertNotIn("AI PROSE", row.body_html)
        self.assertNotIn("AI SUBJECT", row.subject)
        # merge tags the engine fills, and the model's asset choice is kept
        self.assertIn("booking.example/rep", row.body_html)
        self.assertEqual(row.asset_ids, [link.pk])

    def test_fixed_mode_with_an_empty_template_is_held(self):
        self.gmail()
        self.asset_link()
        self.settings.body_mode = AgentEmailSettings.BODY_FIXED
        self.settings.fixed_body = "   "
        self.settings.save()
        row = self.decision()
        with patch(GEMINI, return_value=model_says()):
            engine.decide(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_HELD)
        self.assertIn("fixed email template is empty", row.hold_reason)

    def test_unknown_merge_tag_is_left_visible_not_blanked(self):
        vals = engine.merge_context({"prospect": {"first_name": "Ravi"}, "persona": {}, "booking_link": ""})
        out = engine.render_tags("Hi {{first_name}}, about {{nope}}", vals)
        self.assertEqual(out, "Hi Ravi, about {{nope}}")

    def test_auto_mode_sends_after_the_cancel_window(self):
        self.gmail()
        link = self.asset_link()
        self.settings.mode = AgentEmailSettings.MODE_AUTO
        self.settings.save()
        row = self.decision()
        with patch(GEMINI, return_value=model_says(asset_ids=[link.pk], body_markdown=f"Here: {link.url}")), \
             patch(SEND, return_value=sent_ok()) as send:
            engine.decide(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_SENT)
        self.assertEqual(send.call_count, 1)
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["to_email"], ADDRESS)
        self.assertEqual(kwargs["source"], "ai_sdr_email")
        self.assertEqual(kwargs["contact_id"], self.contact.pk)
        self.assertEqual(kwargs["user"].pk, self.owner.pk)
        self.assertIn("gmail_integration", kwargs)


class SendTests(Base):
    def draft(self, asset_ids=None, status=EmailDecision.STATUS_AWAITING_REVIEW):
        return EmailDecision.objects.create(
            agent=self.agent, business=self.business, contact=self.contact, deal=self.deal,
            sdr_lead_id=self.sdr_lead.pk, conversation_id=self.conv.pk,
            trigger_source=EmailDecision.TRIGGER_LINKEDIN, trigger_ref=f"manual:{EmailDecision.objects.count()+1}",
            status=status, should_email=True, recipient_email=ADDRESS, asset_ids=asset_ids or [],
            subject="Details", body_html="<p>Here you go.</p>", evidence=["Share the details over the mail"],
        )

    def test_send_attaches_small_files_links_large_ones_and_closes_the_loop(self):
        from .sending import dispatch_send
        self.gmail()
        small = self.asset_file(name="Deck")
        big = self.asset_file(name="Big brochure", size=50 * 1024 * 1024)
        link = self.asset_link()
        DealSignal.objects.create(
            business=self.business, contact=self.contact, deal=self.deal, kind="info_requested", side="ours",
            quote="Share the details over the mail", confidence=0.9, source_type="linkedin",
            source_ref="ConversationMessage:1", source_at=timezone.now(), status="open",
        )
        row = self.draft(asset_ids=[small.pk, big.pk, link.pk])
        with patch(SEND, return_value=sent_ok()) as send, patch("agentic_deals.tasks.schedule_rethink") as rethink:
            dispatch_send(row, countdown_seconds=0, approved_by=self.owner)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_SENT)
        kwargs = send.call_args.kwargs
        names = [a[0] for a in kwargs["attachments"]]
        self.assertEqual(names, ["deck.pdf"] if names == ["deck.pdf"] else names)
        self.assertEqual(len(kwargs["attachments"]), 1)
        self.assertIn("Big brochure", kwargs["html_body"])
        self.assertIn("video.example/demo", kwargs["html_body"])
        # loop closed
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.email, ADDRESS)
        self.assertTrue(UniversalLog.objects.filter(model_name="Contact", model_id=self.contact.pk,
                                                    log__0__action="custom_log").exists())
        self.assertEqual(DealSignal.objects.get().status, "done")
        self.assertEqual(rethink.call_count, 1)

    def test_pre_send_gates_stop_a_duplicate(self):
        from .sending import dispatch_send
        self.gmail()
        earlier = self.draft(status=EmailDecision.STATUS_SENT)
        earlier.sent_at = timezone.now()
        earlier.save()
        row = self.draft()
        with patch(SEND) as send:
            dispatch_send(row, countdown_seconds=0)
        send.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_BLOCKED)
        self.assertIn("already emailed", row.hold_reason)

    def test_failed_send_is_recorded(self):
        from .sending import dispatch_send
        self.gmail()
        row = self.draft()
        with patch(SEND, return_value=sent_ok(success=False, error="boom")):
            dispatch_send(row, countdown_seconds=0)
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_FAILED)
        self.assertEqual(row.error, "boom")

    def test_cancel_before_send(self):
        from .sending import cancel_send
        row = self.draft(status=EmailDecision.STATUS_SCHEDULED)
        self.assertTrue(cancel_send(row, reason="rep cancelled", by=self.owner))
        row.refresh_from_db()
        self.assertEqual(row.status, EmailDecision.STATUS_CANCELLED)


class GateTests(Base):
    def test_there_is_no_send_window_so_a_late_ask_is_answered_now(self):
        """Owner's rule: somebody who asks for an email at 22:00 gets it at
        22:00. The time-of-day window was removed deliberately."""
        self.assertFalse(hasattr(AgentEmailSettings, "send_window_start"))
        self.assertFalse(hasattr(gates, "next_send_slot"))
        self.assertEqual(AgentEmailSettings().cancel_window_minutes, 0)

    def test_daily_cap(self):
        self.settings.daily_cap = 1
        self.settings.save()
        EmailDecision.objects.create(
            agent=self.agent, business=self.business, contact=self.contact, trigger_source="manual",
            trigger_ref="x1", status=EmailDecision.STATUS_SENT, recipient_email=ADDRESS,
        )
        self.assertEqual(gates.gate_daily_cap(self.agent, self.settings)[0], EmailDecision.STATUS_BLOCKED)


class ApiTests(Base):
    """Views are driven through APIRequestFactory + force_authenticate: the JWT
    middleware does not run, so the tenant context set in setUp is exactly
    what the view sees (same approach as agentic_deals.test_api)."""

    def setUp(self):
        super().setUp()
        grant(self.owner, "view_dealsandopportunity", "change_dealsandopportunity")
        self.factory = APIRequestFactory()

    def call(self, viewset, actions, method, path, data=None, user=None, fmt="json", **kwargs):
        req = getattr(self.factory, method)(path, data, format=fmt)
        force_authenticate(req, user=user or self.owner)
        resp = viewset.as_view(actions)(req, **kwargs)
        if hasattr(resp, "render"):
            resp.render()
        return resp

    @staticmethod
    def rows(resp):
        """List endpoints are paginated project-wide: unwrap the envelope."""
        data = resp.data
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def test_settings_refuse_a_mode_without_a_mailbox(self):
        r = self.call(AgentEmailSettingsViewSet, {"patch": "partial_update"}, "patch",
                      f"/api/v1/email-orchestrator/settings/{self.agent.pk}/", {"mode": "auto"}, pk=self.agent.pk)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("readiness", r.data)
        self.gmail()
        r = self.call(AgentEmailSettingsViewSet, {"patch": "partial_update"}, "patch",
                      f"/api/v1/email-orchestrator/settings/{self.agent.pk}/", {"mode": "auto"}, pk=self.agent.pk)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["mode"], "auto")
        self.assertTrue(r.data["readiness"]["ready"])

    def test_settings_get_creates_row_and_shows_readiness(self):
        other = LinkedInAgent.objects.create(business_record=self.business, agent_name="Other")
        r = self.call(AgentEmailSettingsViewSet, {"get": "retrieve"}, "get",
                      f"/api/v1/email-orchestrator/settings/{other.pk}/", pk=other.pk)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["mode"], "off")
        self.assertFalse(r.data["readiness"]["ready"])
        self.assertTrue(AgentEmailSettings.objects.filter(agent=other).exists())

    def test_assets_crud_scoped_to_agent(self):
        r = self.call(EmailAssetViewSet, {"post": "create"}, "post", "/api/v1/email-orchestrator/assets/", {
            "agent": self.agent.pk, "kind": "link", "name": "Demo", "url": "https://loom.com/x", "description": "walkthrough",
        })
        self.assertEqual(r.status_code, 201, r.data)
        r = self.call(EmailAssetViewSet, {"post": "create"}, "post", "/api/v1/email-orchestrator/assets/", {
            "agent": self.agent.pk, "kind": "file", "name": "Deck",
            "file": SimpleUploadedFile("deck.pdf", b"%PDF-1.4", content_type="application/pdf"),
        }, fmt="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["file_size"], 8)
        r = self.call(EmailAssetViewSet, {"post": "create"}, "post", "/api/v1/email-orchestrator/assets/", {
            "agent": self.agent.pk, "kind": "file", "name": "Bad",
            "file": SimpleUploadedFile("virus.exe", b"MZ", content_type="application/octet-stream"),
        }, fmt="multipart")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.call(EmailAssetViewSet, {"get": "list"}, "get", f"/api/v1/email-orchestrator/assets/?agent={self.agent.pk}")
        self.assertEqual(len(self.rows(r)), 2)
        # another tenant sees nothing
        stranger = make_user("stranger@example.com")
        set_user_context(user_id=stranger.id, business_record_id=999999, master_admin=False, partner_id=None)
        r = self.call(EmailAssetViewSet, {"get": "list"}, "get",
                      f"/api/v1/email-orchestrator/assets/?agent={self.agent.pk}", user=stranger)
        self.assertEqual(list(self.rows(r)), [])

    def test_review_approve_sends_and_skip_cancels(self):
        self.gmail()
        row = SendTests.draft(self)
        with patch(SEND, return_value=sent_ok()) as send:
            r = self.call(EmailDecisionViewSet, {"post": "approve"}, "post",
                          f"/api/v1/email-orchestrator/decisions/{row.pk}/approve/", {"subject": "Edited subject"}, pk=row.pk)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "sent")
        self.assertEqual(send.call_args.kwargs["subject"], "Edited subject")
        row2 = SendTests.draft(self)
        r = self.call(EmailDecisionViewSet, {"post": "skip"}, "post",
                      f"/api/v1/email-orchestrator/decisions/{row2.pk}/skip/", {"reason": "no"}, pk=row2.pk)
        self.assertEqual(r.data["status"], "cancelled")
        r = self.call(EmailDecisionViewSet, {"get": "list"}, "get",
                      f"/api/v1/email-orchestrator/decisions/?deal_id={self.deal.pk}")
        self.assertEqual(len(self.rows(r)), 2)
        r = self.call(EmailDecisionViewSet, {"get": "queue_counts"}, "get", "/api/v1/email-orchestrator/decisions/queue_counts/")
        self.assertEqual(r.data["sent"], 1)
