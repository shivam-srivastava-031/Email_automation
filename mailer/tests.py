from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import ai, campaigns as svc, gmail
from .csv_import import import_csv
from .models import AppSettings, Campaign, Contact, ContactList, GmailAccount, Message
from .render import build_email, fill, unknown_fields


def connect_gmail():
    return GmailAccount.objects.create(pk=1, email="sender@example.com", refresh_token="rt")


class CsvImportTests(TestCase):
    def test_maps_headers_and_keeps_extra_columns(self):
        raw = b"Email,First Name,Last Name,Company Name,Job Title,City\nJane.Doe@Example.com,Jane,Doe,ProspectCo,VP Sales,Pune\n"
        lst = ContactList.objects.create(name="Leads")
        result = import_csv(raw, contact_list=lst)
        self.assertEqual((result.added, result.updated, result.skipped), (1, 0, 0))
        c = Contact.objects.get()
        self.assertEqual(c.email, "jane.doe@example.com")
        self.assertEqual((c.first_name, c.last_name, c.company, c.role), ("Jane", "Doe", "ProspectCo", "VP Sales"))
        self.assertEqual(c.extra, {"city": "Pune"})
        self.assertEqual(list(c.lists.all()), [lst])

    def test_updates_existing_and_skips_invalid(self):
        Contact.objects.create(email="a@example.com", first_name="Old")
        raw = b"email,name\na@example.com,Alex Kim\nnot-an-email,Bad Row\nb@example.com,\n"
        result = import_csv(raw)
        self.assertEqual((result.added, result.updated, result.skipped), (1, 1, 1))
        a = Contact.objects.get(email="a@example.com")
        self.assertEqual((a.first_name, a.last_name), ("Alex", "Kim"))
        self.assertIn("Row 3", result.errors[0])

    def test_semicolon_csv_with_bom(self):
        raw = "﻿E-mail;First name\nx@example.com;Xi\n".encode("utf-8")
        result = import_csv(raw)
        self.assertEqual(result.added, 1)
        self.assertEqual(Contact.objects.get().first_name, "Xi")

    def test_requires_email_column(self):
        result = import_csv(b"name,company\nJane,Acme\n")
        self.assertEqual(result.total, 0)
        self.assertIn("No email column", result.errors[0])


class RenderTests(TestCase):
    def test_fill_and_unknown_fields(self):
        self.assertEqual(fill("Hi {{ first_name }} at {{company}}{{nope}}", {"first_name": "Jane", "company": "Co"}), "Hi Jane at Co")
        self.assertEqual(unknown_fields("{{first_name}} {{city}}", ["first_name"]), ["city"])

    def test_html_is_escaped_and_has_button_and_signature(self):
        html, text = build_email("Hi <b>Jane</b>\n\nSee https://example.com/a?x=1&y=2", "Book a call", "https://cal.example/me", "Sam")
        self.assertIn("&lt;b&gt;Jane&lt;/b&gt;", html)
        self.assertIn('href="https://example.com/a?x=1&amp;y=2"', html)
        self.assertIn('href="https://cal.example/me"', html)
        self.assertIn("Book a call: https://cal.example/me", text)
        self.assertTrue(text.endswith("Sam"))


class CampaignStartTests(TestCase):
    def setUp(self):
        self.lst = ContactList.objects.create(name="L")
        self.jane = Contact.objects.create(email="jane@example.com", first_name="Jane", company="ProspectCo")
        self.gone = Contact.objects.create(email="gone@example.com", unsubscribed=True)
        self.jane.lists.add(self.lst)
        self.gone.lists.add(self.lst)

    def test_template_campaign_fills_drafts_and_skips_unsubscribed(self):
        c = Campaign.objects.create(name="T", contact_list=self.lst, subject="For {{company}}", body="Hi {{first_name}},\n\nHello.")
        self.assertEqual(svc.start(c), 1)
        m = Message.objects.get()
        self.assertEqual((m.contact, m.status, m.subject), (self.jane, Message.STATUS_DRAFT, "For ProspectCo"))
        self.assertTrue(m.body.startswith("Hi Jane,"))
        self.assertEqual(svc.start(c), 0)  # starting again adds only new contacts

    def test_no_review_queues_directly(self):
        c = Campaign.objects.create(name="T", contact_list=self.lst, subject="s", body="b", review_first=False)
        svc.start(c)
        self.assertEqual(Message.objects.get().status, Message.STATUS_APPROVED)

    def test_template_needs_subject_and_body(self):
        c = Campaign.objects.create(name="T", contact_list=self.lst)
        with self.assertRaises(svc.CampaignError):
            svc.start(c)

    @override_settings(GEMINI_API_KEY="")
    def test_ai_campaign_needs_key(self):
        c = Campaign.objects.create(name="A", writer=Campaign.WRITER_AI, contact_list=self.lst)
        with self.assertRaisesMessage(svc.CampaignError, "GEMINI_API_KEY"):
            svc.start(c)


@override_settings(SEND_INTERVAL_SECONDS=45, GEMINI_API_KEY="k")
class WorkerTests(TestCase):
    def setUp(self):
        connect_gmail()
        AppSettings.objects.create(pk=1, sender_name="Sam", daily_limit=2)
        self.contacts = [Contact.objects.create(email=f"p{i}@example.com", first_name=f"P{i}") for i in range(3)]
        self.campaign = Campaign.objects.create(name="C", subject="Hi {{first_name}}", body="Hello", review_first=False)
        svc.start(self.campaign)
        self.sent = []

    def fake_send(self, account, mime):
        self.sent.append(mime["To"])
        return f"id{len(self.sent)}"

    def test_paces_sends_and_respects_daily_limit(self):
        t0 = timezone.now()
        self.assertEqual(svc.send_next(now=t0, sender=self.fake_send).status, Message.STATUS_SENT)
        self.assertIsNone(svc.send_next(now=t0 + timedelta(seconds=10), sender=self.fake_send))  # too soon
        svc.send_next(now=t0 + timedelta(seconds=50), sender=self.fake_send)
        self.assertIsNone(svc.send_next(now=t0 + timedelta(seconds=120), sender=self.fake_send))  # limit of 2
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(Message.objects.filter(status=Message.STATUS_APPROVED).count(), 1)

    def test_nothing_sent_without_gmail(self):
        GmailAccount.objects.all().delete()
        self.assertIsNone(svc.send_next(sender=self.fake_send))

    def test_paused_campaign_is_not_sent(self):
        self.campaign.status = Campaign.STATUS_PAUSED
        self.campaign.save()
        self.assertIsNone(svc.send_next(sender=self.fake_send))

    def test_permanent_failure_marks_failed(self):
        def boom(account, mime):
            raise gmail.GmailError("revoked", permanent=True)
        msg = svc.send_next(sender=boom)
        self.assertEqual((msg.status, msg.error), (Message.STATUS_FAILED, "revoked"))

    def test_transient_failure_retries_then_fails(self):
        def flaky(account, mime):
            raise gmail.GmailError("timeout")
        msg = svc.send_next(sender=flaky)
        self.assertEqual((msg.status, msg.attempts), (Message.STATUS_APPROVED, 1))  # will retry
        for _ in range(len(self.contacts) * svc.MAX_SEND_ATTEMPTS):
            svc.send_next(sender=flaky)
        self.assertEqual(Message.objects.filter(status=Message.STATUS_FAILED).count(), 3)

    def test_sends_in_list_order(self):
        AppSettings.objects.filter(pk=1).update(daily_limit=10)
        t = timezone.now()
        for i in range(3):
            svc.send_next(now=t + timedelta(minutes=i), sender=self.fake_send)
        self.assertEqual(self.sent, sorted(self.sent, key=lambda e: Contact.objects.get(email=e).messages.get().pk))

    def test_campaign_done_when_all_sent(self):
        AppSettings.objects.filter(pk=1).update(daily_limit=10)
        t = timezone.now()
        for i in range(3):
            svc.send_next(now=t + timedelta(minutes=i), sender=self.fake_send)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.STATUS_DONE)

    def test_ai_pending_messages_get_written(self):
        c = Campaign.objects.create(name="AI", writer=Campaign.WRITER_AI)
        svc.start(c)
        writer = mock.Mock(return_value={"subject": "Hello P", "body": "Hi P,\n\nText."})
        written = svc.write_pending(limit=10, writer=writer)
        self.assertEqual(written, 3)
        self.assertEqual(c.messages.filter(status=Message.STATUS_DRAFT).count(), 3)


class AiParseTests(TestCase):
    def test_strips_links_and_markdown(self):
        out = ai.parse_output('```json\n{"subject": "  Quick   idea ", "body": "Hi Jane,\\n\\n**Big** win at https://x.example/y.\\n\\n\\n\\nThanks"}\n```')
        self.assertEqual(out["subject"], "Quick idea")
        self.assertNotIn("https://", out["body"])
        self.assertNotIn("**", out["body"])
        self.assertNotIn("\n\n\n", out["body"])

    def test_rejects_bad_output(self):
        with self.assertRaises(ai.AIError):
            ai.parse_output("not json")
        with self.assertRaises(ai.AIError):
            ai.parse_output('{"subject": "", "body": "x"}')

    @override_settings(GEMINI_API_KEY="k", GEMINI_MODEL="m")
    def test_write_email_calls_gemini(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": '{"subject": "S", "body": "Hi"}'}]}}]}
        with mock.patch("mailer.ai.requests.post", return_value=resp) as post:
            out = ai.write_email(Campaign(name="x"), Contact(email="a@example.com", first_name="A"), AppSettings())
        self.assertEqual(out, {"subject": "S", "body": "Hi"})
        self.assertIn("/models/m:generateContent", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "k")


class MimeTests(TestCase):
    def test_build_mime_has_alternatives_and_attachment(self):
        mime = gmail.build_mime("Sam", "s@example.com", "p@example.com", "Hi", "<p>Hi</p>", "Hi", [("deck.pdf", b"%PDF")])
        self.assertEqual(mime["From"], "Sam <s@example.com>")
        names = [part.get_filename() for part in mime.iter_attachments()]
        self.assertEqual(names, ["deck.pdf"])


class ViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("me", password="pw-for-tests")

    def test_login_required(self):
        self.assertRedirects(self.client.get(reverse("dashboard")), f"{reverse('login')}?next=/")

    def test_pages_render(self):
        self.client.force_login(self.user)
        c = Campaign.objects.create(name="C", subject="s", body="b")
        for name, args in [("dashboard", []), ("contacts", []), ("campaign_list", []), ("campaign_new", []),
                           ("campaign_edit", [c.pk]), ("campaign_detail", [c.pk]), ("settings", [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200, name)

    def test_csv_upload_and_campaign_flow(self):
        self.client.force_login(self.user)
        f = SimpleUploadedFile("leads.csv", b"email,first name\njane@example.com,Jane\n", content_type="text/csv")
        self.client.post(reverse("contacts_import"), {"file": f, "new_list": "Leads"})
        self.assertEqual(Contact.objects.get().lists.get().name, "Leads")

        r = self.client.post(reverse("campaign_new"), {
            "name": "Hello", "contact_list": ContactList.objects.get().pk, "writer": "template",
            "subject": "Hi {{first_name}}", "body": "Hello {{first_name}}", "review_first": "on",
        })
        campaign = Campaign.objects.get()
        self.assertRedirects(r, reverse("campaign_edit", args=[campaign.pk]))

        preview = self.client.post(reverse("campaign_preview"), {"writer": "template", "subject": "Hi {{first_name}}", "body": "Yo {{city}}"}).json()
        self.assertEqual(preview["subject"], "Hi Jane")
        self.assertEqual(preview["unknown"], ["city"])

        self.client.post(reverse("campaign_start", args=[campaign.pk]))
        msg = Message.objects.get()
        self.assertEqual(msg.status, Message.STATUS_DRAFT)
        self.client.post(reverse("message_action", args=[msg.pk]), {"action": "approve", "subject": "Edited", "body": "New body"})
        msg.refresh_from_db()
        self.assertEqual((msg.status, msg.subject), (Message.STATUS_APPROVED, "Edited"))

    def test_gmail_connect_needs_config(self):
        self.client.force_login(self.user)
        with override_settings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET=""):
            self.assertRedirects(self.client.get(reverse("gmail_connect")), reverse("settings"))

    def test_gmail_callback_rejects_bad_state(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["gmail_oauth"] = {"state": "good", "verifier": "v"}
        session.save()
        r = self.client.get(reverse("gmail_callback"), {"state": "evil", "code": "x"})
        self.assertRedirects(r, reverse("settings"))
        self.assertFalse(GmailAccount.objects.exists())
