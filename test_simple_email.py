"""The simple email: a reply that continues the chat, every attachment, and a
call-to-action button - plus the migration that retired the company library.
"""
import importlib
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile

from agentic_deals.tests import make_user
from ai_sdr.models import LinkedInAgent
from lead.models import BusinessRecord

from . import engine
from .models import AgentEmailSettings, EmailAsset, EmailDecision
from .serializers import AgentEmailSettingsSerializer
from .test_admin_mailbox import SharedBase
from .tests import GEMINI, model_says
from .views import AgentEmailSettingsViewSet, EmailAssetViewSet

MIGRATION = importlib.import_module(
    "email_orchestrator.migrations.0004_simple_email_cta_retire_company_library"
)
BOOKING = "https://cal.com/hodos"


class SimpleBase(SharedBase):
    def setUp(self):
        super().setUp()
        self.rep_gmail()

    def item(self, name="Deck", kind=EmailAsset.KIND_FILE, agent="mine", active=True,
             business=None, url="https://example.com/x"):
        extra = {}
        if kind == EmailAsset.KIND_FILE:
            extra = {"file": SimpleUploadedFile(f"{name}.pdf", b"%PDF-1.4", content_type="application/pdf"),
                     "file_size": 8, "mime_type": "application/pdf"}
        elif kind == EmailAsset.KIND_LINK:
            extra = {"url": url}
        else:
            extra = {"body": "snippet text"}
        return EmailAsset.objects.create(
            agent=self.agent if agent == "mine" else agent, business=business or self.business,
            kind=kind, name=name, is_active=active, **extra,
        )

    def decided(self, **model):
        row = self.asked()
        with patch(GEMINI, return_value=model_says(**model)) as g:
            engine.decide(row.pk)
        row.refresh_from_db()
        return row, g.call_args[0][0]


class AttachmentTests(SimpleBase):
    def test_every_active_file_and_link_goes_with_every_email(self):
        deck = self.item("Deck")
        video = self.item("Video", kind=EmailAsset.KIND_LINK)
        self.item("Old deck", active=False)
        self.item("hi", kind=EmailAsset.KIND_SNIPPET)                     # retired kind
        colleague = LinkedInAgent.objects.create(business_record=self.business, agent_name="Colleague")
        self.item("Theirs", agent=colleague)                             # another agent's
        self.item("Company deck", agent=None)                            # retired company item
        self.assertEqual({a["id"] for a in engine.asset_catalogue(self.agent)}, {deck.pk, video.pk})

        row, _prompt = self.decided()
        self.assertEqual(row.status, EmailDecision.STATUS_AWAITING_REVIEW, row.hold_reason)
        self.assertEqual(sorted(row.asset_ids), sorted([deck.pk, video.pk]))

    def test_the_writer_sees_the_attachments_by_name_and_writes_no_links(self):
        self.item("Product Deck")
        _row, prompt = self.decided()
        self.assertIn("- Product Deck (", prompt)
        self.assertIn("Do not write any URL yourself", prompt)
        self.assertNotIn("asset_ids", prompt)

    def test_with_nothing_attached_the_writer_is_told_not_to_mention_attachments(self):
        _row, prompt = self.decided()
        self.assertIn("nothing is attached", prompt)


class CallToActionTests(SimpleBase):
    def test_an_ai_email_ends_with_the_booking_button(self):
        row, prompt = self.decided(body_markdown="Here is the overview you asked for.")
        body = row.body_html
        self.assertIn(f'href="{BOOKING}"', body)
        self.assertIn("Book a 15-minute call", body)
        self.assertLess(body.index("overview you asked for"), body.index(BOOKING))
        self.assertEqual(row.raw["cta"], {"label": "Book a 15-minute call", "url": BOOKING})
        self.assertIn('"Book a 15-minute call" button', prompt)

    def test_a_fixed_template_gets_the_same_button(self):
        self.toggle(body_mode=AgentEmailSettings.BODY_FIXED, fixed_subject="For {{company_name}}",
                    fixed_body="Hi {{first_name}},\n\nAs promised.")
        row, _prompt = self.decided(body_markdown="AI PROSE")
        self.assertNotIn("AI PROSE", row.body_html)
        self.assertIn("Hi Pratapraj", row.body_html)
        self.assertLess(row.body_html.index("As promised"), row.body_html.index(BOOKING))

    def test_custom_text_and_link_are_used_and_escaped(self):
        self.toggle(cta_text='Grab <b>time</b>', cta_url='https://acme.example/book?a=1&b="2"')
        row, _prompt = self.decided()
        self.assertIn("Grab &lt;b&gt;time&lt;/b&gt;", row.body_html)
        self.assertIn('href="https://acme.example/book?a=1&amp;b=&quot;2&quot;"', row.body_html)
        self.assertNotIn(BOOKING, row.body_html)

    def test_button_off_means_no_button_and_the_writer_is_told(self):
        self.toggle(include_booking_link=False)
        row, prompt = self.decided()
        self.assertNotIn(BOOKING, row.body_html)
        self.assertIn("There is no booking link", prompt)
        self.assertEqual(row.raw["cta"]["url"], "")

    def test_a_reply_that_would_only_be_a_button_is_held_as_empty(self):
        row, _prompt = self.decided(body_markdown="")
        self.assertEqual(row.status, EmailDecision.STATUS_HELD)
        self.assertEqual(row.hold_reason, "empty draft")

    def test_no_booking_link_anywhere_means_no_button(self):
        self.agent.meeting_link = ""
        self.agent.conversion_events = []
        self.agent.save()
        row, _prompt = self.decided()
        self.assertNotIn("<a href", row.body_html)


class PreviewAndSettingsTests(SimpleBase):
    def preview(self, data):
        view = AgentEmailSettingsViewSet.as_view({"post": "preview"})
        return self.call(view, "post", f"/api/v1/email-orchestrator/settings/{self.agent.pk}/preview/",
                         data, user=self.as_admin(), pk=self.agent.pk)

    def test_preview_shows_the_template_and_the_same_button(self):
        r = self.preview({"subject": "Hi {{first_name}}", "body": "As promised.", "include_cta": True})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["subject"], "Hi Pratapraj")
        self.assertIn("As promised.", r.data["body_html"])
        self.assertIn(engine.cta_html("Book a 15-minute call", BOOKING), r.data["body_html"])
        r = self.preview({"body": "x", "include_cta": False})
        self.assertNotIn("<a href", r.data["body_html"])
        self.assertEqual(r.data["cta_url"], "")
        r = self.preview({"body": "x", "include_cta": True, "cta_text": "Demo", "cta_url": "https://acme.example/d"})
        self.assertIn('href="https://acme.example/d"', r.data["body_html"])
        self.assertIn(">Demo<", r.data["body_html"])

    def test_settings_save_the_button_text_and_link(self):
        view = AgentEmailSettingsViewSet.as_view({"patch": "partial_update"})
        r = self.call(view, "patch", f"/api/v1/email-orchestrator/settings/{self.agent.pk}/",
                      {"cta_text": "  Book   a demo ", "cta_url": "https://acme.example/demo"},
                      user=self.as_admin(), pk=self.agent.pk)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["cta_text"], "Book a demo")
        self.assertEqual(r.data["cta_url"], "https://acme.example/demo")
        self.assertNotIn("use_company_material", r.data)

    def test_snippets_cannot_be_created_and_are_not_listed(self):
        self.item("old text", kind=EmailAsset.KIND_SNIPPET)
        deck = self.item("Deck")
        user = self.as_admin()
        create = EmailAssetViewSet.as_view({"post": "create"})
        r = self.call(create, "post", "/api/v1/email-orchestrator/assets/",
                      {"agent": self.agent.pk, "kind": "snippet", "name": "x", "body": "y"}, user=user)
        self.assertEqual(r.status_code, 400, r.data)
        listing = EmailAssetViewSet.as_view({"get": "list"})
        r = self.call(listing, "get", f"/api/v1/email-orchestrator/assets/?agent={self.agent.pk}", user=user)
        self.assertEqual([a["id"] for a in self.rows(r)], [deck.pk])

    def test_fixed_mode_needs_a_template_of_its_own(self):
        ser = AgentEmailSettingsSerializer(self.settings, data={"body_mode": "fixed", "fixed_body": ""}, partial=True)
        self.assertFalse(ser.is_valid())
        self.assertIn("fixed_body", ser.errors)


class RetireCompanyLibraryMigrationTests(SimpleBase):
    def test_company_items_become_attachments_of_every_set_up_agent(self):
        set_up = LinkedInAgent.objects.create(business_record=self.business, agent_name="Set up")
        AgentEmailSettings.objects.create(agent=set_up, business=self.business)
        has_own = LinkedInAgent.objects.create(business_record=self.business, agent_name="Has own")
        AgentEmailSettings.objects.create(agent=has_own, business=self.business)
        own = self.item("COMPANY deck", agent=has_own)
        no_settings = LinkedInAgent.objects.create(business_record=self.business, agent_name="Not set up")
        other_biz = BusinessRecord.objects.create(business_name="Other", address="2 St",
                                                  user=make_user("o-mig@example.com"), status="Active")
        outsider = LinkedInAgent.objects.create(business_record=other_biz, agent_name="Outsider")
        AgentEmailSettings.objects.create(agent=outsider, business=other_biz)
        company = self.item("Company deck", agent=None)

        MIGRATION.company_items_to_agents(django_apps, None)
        MIGRATION.company_items_to_agents(django_apps, None)  # a re-run must not duplicate

        for agent in (self.agent, set_up):
            copies = EmailAsset.objects.filter(agent=agent, name="Company deck")
            self.assertEqual(copies.count(), 1, agent.agent_name)
            self.assertEqual(copies.get().file.name, company.file.name)  # same stored file
        self.assertEqual(list(EmailAsset.objects.filter(agent=has_own).values_list("pk", flat=True)), [own.pk])
        self.assertFalse(EmailAsset.objects.filter(agent=no_settings).exists())
        self.assertFalse(EmailAsset.objects.filter(agent=outsider).exists())
        company.refresh_from_db()
        self.assertTrue(company.deleted)
