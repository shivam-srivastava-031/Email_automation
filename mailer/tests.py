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


GOOD_BODY = (
    "Hi {{first_name}},\n\nI am a full stack developer with nine months of internship experience building Django and "
    "React applications for a SaaS platform, and I would love to be considered for openings in your team. My resume "
    "is attached and my portfolio is linked below for a quick look.\n\nWould you be open to a short call this week?"
)


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
        c = Campaign.objects.create(name="T", contact_list=self.lst, subject="Hello", body=GOOD_BODY, review_first=False)
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
        AppSettings.objects.create(pk=1, sender_name="Sam", daily_limit=2, send_window=False)
        self.contacts = [Contact.objects.create(email=f"p{i}@example.com", first_name=f"P{i}") for i in range(3)]
        self.campaign = Campaign.objects.create(name="C", subject="Hi {{first_name}}", body=GOOD_BODY, review_first=False)
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


READ_SCOPES = " ".join(gmail.SCOPES)


def reply_data(mid, tid, sender="hr@prospect.example", subject="Re: Hello", when=None):
    return {"id": mid, "threadId": tid, "from_email": sender, "from_name": "Priya HR", "subject": subject,
            "snippet": "Thanks, please share", "body": "Thanks, please share your notice period.",
            "received_at": when or timezone.now()}


class ReplySyncTests(TestCase):
    def setUp(self):
        self.account = GmailAccount.objects.create(pk=1, email="sender@example.com", refresh_token="rt", scopes=READ_SCOPES)
        contact = Contact.objects.create(email="hr@prospect.example", first_name="Priya")
        self.campaign = Campaign.objects.create(name="Jobs", subject="s", body="b")
        self.msg = Message.objects.create(campaign=self.campaign, contact=contact, status=Message.STATUS_SENT,
                                          subject="Hello", body="b", gmail_id="sent1", gmail_thread_id="t1", sent_at=timezone.now())

    def run_sync(self, listed, data):
        from .replies import sync_replies
        return sync_replies(list_ids=lambda account, q: listed, fetch=lambda account, mid: data[mid],
                            classifier=lambda subj, text: {"category": "other", "summary": ""},
                            sender=lambda *a, **k: {"id": "n"})

    def test_stores_only_new_replies_in_our_threads(self):
        from .models import Reply
        listed = [("r1", "t1"), ("x1", "other-thread"), ("sent1", "t1"), ("own1", "t1")]
        data = {"r1": reply_data("r1", "t1"), "own1": reply_data("own1", "t1", sender="sender@example.com")}
        self.assertEqual(self.run_sync(listed, data), 1)
        reply = Reply.objects.get()
        self.assertEqual((reply.message, reply.kind, reply.is_read), (self.msg, Reply.KIND_REPLY, False))
        self.msg.refresh_from_db()
        self.assertIsNotNone(self.msg.replied_at)
        self.assertEqual(self.run_sync(listed, data), 0)  # no duplicates on the next check
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.last_reply_sync)

    def test_bounce_is_marked_and_not_counted_as_reply(self):
        from .models import Reply
        data = {"b1": reply_data("b1", "t1", sender="mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)")}
        self.run_sync([("b1", "t1")], data)
        self.assertEqual(Reply.objects.get().kind, Reply.KIND_BOUNCE)
        self.msg.refresh_from_db()
        self.assertIsNone(self.msg.replied_at)

    def test_needs_read_scope(self):
        GmailAccount.objects.filter(pk=1).update(scopes="https://www.googleapis.com/auth/gmail.send")
        listed = mock.Mock()
        from .replies import sync_replies
        self.assertEqual(sync_replies(list_ids=listed, fetch=mock.Mock()), 0)
        listed.assert_not_called()

    @override_settings(REPLY_SYNC_SECONDS=120)
    def test_worker_checks_replies_on_schedule(self):
        syncer = mock.Mock(return_value=2)
        t = timezone.now()
        self.assertEqual(svc.tick(now=t, syncer=syncer)["replies"], 2)
        GmailAccount.objects.filter(pk=1).update(last_reply_sync=t)
        svc.tick(now=t + timedelta(seconds=30), syncer=syncer)
        self.assertEqual(syncer.call_count, 1)  # not due yet
        svc.tick(now=t + timedelta(seconds=130), syncer=syncer)
        self.assertEqual(syncer.call_count, 2)

    def test_worker_survives_reply_check_errors(self):
        def boom(now=None):
            raise gmail.GmailError("network down")
        self.assertEqual(svc.tick(syncer=boom)["replies"], 0)


class GmailParsingTests(TestCase):
    def test_send_next_stores_thread_id(self):
        connect_gmail()
        AppSettings.objects.create(pk=1, daily_limit=5, send_window=False)
        Contact.objects.create(email="p@example.com")
        c = Campaign.objects.create(name="C", subject="Hello", body=GOOD_BODY, review_first=False)
        svc.start(c)
        msg = svc.send_next(sender=lambda account, mime: {"id": "m1", "threadId": "t9"})
        self.assertEqual((msg.gmail_id, msg.gmail_thread_id), ("m1", "t9"))
        self.assertIn("#all/t9", msg.gmail_url)

    def test_plain_text_prefers_text_part_and_falls_back_to_html(self):
        import base64 as b64
        enc = lambda s: b64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": enc("<p>Hi <b>there</b></p>")}},
            {"mimeType": "text/plain", "body": {"data": enc("Hi there")}},
        ]}
        self.assertEqual(gmail._plain_text(payload), "Hi there")
        html_only = {"mimeType": "text/html", "body": {"data": enc("<div>Line one</div><div>Line two</div>")}}
        self.assertEqual(gmail._plain_text(html_only), "Line one\nLine two")


class SafetyTests(TestCase):
    def setUp(self):
        from . import safety
        self.safety = safety
        connect_gmail()
        self.app = AppSettings.objects.create(pk=1, daily_limit=40, current_limit=20, send_window=False)
        self.contacts = [Contact.objects.create(email=f"p{i}@example.com") for i in range(3)]
        self.campaign = Campaign.objects.create(name="C", subject="Hello", body=GOOD_BODY, review_first=False)
        svc.start(self.campaign)

    def make_sent(self, n, when=None, bounced=0):
        from .models import Reply
        when = when or timezone.now() - timedelta(hours=1)
        msgs = []
        for i in range(n):
            c = Contact.objects.create(email=f"s{i}-{when.timestamp()}@example.com")
            m = Message.objects.create(campaign=self.campaign, contact=c, status=Message.STATUS_SENT, subject="s", body="b",
                                       sent_at=when, gmail_thread_id=f"t{i}-{when.timestamp()}")
            msgs.append(m)
        for m in msgs[:bounced]:
            Reply.objects.create(message=m, gmail_id=f"b-{m.pk}", kind=Reply.KIND_BOUNCE, received_at=when)
        return msgs

    def test_classify_http_error(self):
        self.assertTrue(gmail.classify_http_error(429, "")["rate_limited"])
        self.assertTrue(gmail.classify_http_error(403, "User-rate limit exceeded: userRateLimitExceeded")["rate_limited"])
        self.assertFalse(gmail.classify_http_error(403, "insufficient permissions")["rate_limited"])
        self.assertTrue(gmail.classify_http_error(401, "")["auth"])
        self.assertTrue(gmail.classify_http_error(400, "invalid to")["permanent"])

    def test_rate_limit_halves_limit_and_pauses_until_morning(self):
        def limited(account, mime):
            raise gmail.GmailError("quota", rate_limited=True)
        now = timezone.now()
        msg = svc.send_next(now=now, sender=limited)
        self.assertEqual((msg.status, msg.attempts), (Message.STATUS_APPROVED, 0))  # not the email's fault
        app = AppSettings.load()
        self.assertEqual(app.current_limit, 10)
        self.assertEqual(app.paused_until, self.safety.next_morning(now))
        self.assertIsNone(svc.send_next(now=now + timedelta(hours=1), sender=lambda a, m: "x"))  # paused
        self.safety.resume()
        self.assertEqual(svc.send_next(now=now + timedelta(hours=1), sender=lambda a, m: "x").status, Message.STATUS_SENT)

    def test_lost_access_pauses_until_resumed(self):
        def revoked(account, mime):
            raise gmail.GmailError("revoked", permanent=True, auth=True)
        svc.send_next(sender=revoked)
        self.assertTrue(self.safety.is_paused(now=timezone.now() + timedelta(days=30)))
        self.assertIn("Reconnect Gmail", AppSettings.load().pause_reason)

    def test_bounces_cut_then_pause(self):
        self.make_sent(20, bounced=1)                       # 5%
        self.assertEqual(self.safety.check_bounces(), "cut")
        self.assertEqual(AppSettings.load().current_limit, 10)
        self.assertIsNone(self.safety.check_bounces())       # only one cut a day
        self.make_sent(20, bounced=3)                        # 4 of 40 = 10%
        self.assertEqual(self.safety.check_bounces(), "paused")
        self.assertTrue(self.safety.is_paused())
        self.assertEqual(AppSettings.load().current_limit, 10)  # not cut twice in a day

    def test_small_samples_are_ignored(self):
        self.make_sent(10, bounced=5)
        self.assertIsNone(self.safety.check_bounces())

    def test_bounce_and_opt_out_replies_stop_future_emails(self):
        from .models import Reply
        sent = self.make_sent(2)
        jane = sent[0].contact
        other = Campaign.objects.create(name="Next", subject="s", body="b")
        queued = Message.objects.create(campaign=other, contact=jane, status=Message.STATUS_APPROVED, subject="s", body="b")
        bounce = Reply.objects.create(message=sent[0], gmail_id="b1", kind=Reply.KIND_BOUNCE, received_at=timezone.now())
        self.assertEqual(self.safety.handle_reply(bounce), "bounced")
        jane.refresh_from_db(); queued.refresh_from_db()
        self.assertTrue(jane.bounced)
        self.assertEqual(queued.status, Message.STATUS_SKIPPED)
        self.assertNotIn(jane, other.recipients())

        no = Reply.objects.create(message=sent[1], gmail_id="r1", body="Thanks, but please remove me from your list.",
                                  received_at=timezone.now())
        self.assertEqual(self.safety.handle_reply(no), "unsubscribed")
        self.assertTrue(Contact.objects.get(pk=sent[1].contact_id).unsubscribed)
        yes = Reply.objects.create(message=sent[1], gmail_id="r2", body="Sounds interesting, call me tomorrow?",
                                   received_at=timezone.now())
        self.assertIsNone(self.safety.handle_reply(yes))

    def test_warmup_starts_low_and_grows_after_healthy_days(self):
        from .models import SafetyEvent
        AppSettings.objects.filter(pk=1).update(current_limit=None, daily_limit=40, warmup_start=15, warmup_step=5)
        tz = timezone.get_current_timezone()
        today_10 = timezone.make_aware(timezone.datetime.combine(timezone.localdate(), timezone.datetime.min.time()), tz) + timedelta(hours=10)
        self.assertEqual(self.safety.daily_adjust(today_10), "started")
        self.assertEqual(self.safety.effective_limit(), 15)
        self.assertIsNone(self.safety.daily_adjust(today_10 + timedelta(hours=1)))  # once a day

        tomorrow = today_10 + timedelta(days=1)
        self.make_sent(10, when=today_10)                   # a clean day
        SafetyEvent.objects.all().delete()
        self.assertEqual(self.safety.daily_adjust(tomorrow), "raised")
        self.assertEqual(self.safety.effective_limit(), 20)

        self.make_sent(10, when=tomorrow)
        self.safety.event(SafetyEvent.LEVEL_WARNING, "something went wrong")
        SafetyEvent.objects.update(created_at=tomorrow + timedelta(hours=8))  # within the day being judged
        self.assertIsNone(self.safety.daily_adjust(tomorrow + timedelta(days=1)))  # not after trouble
        self.assertEqual(self.safety.effective_limit(), 20)

    def test_never_above_maximum_and_off_switch(self):
        AppSettings.objects.filter(pk=1).update(current_limit=100)
        self.assertEqual(self.safety.effective_limit(), 40)
        AppSettings.objects.filter(pk=1).update(auto_protect=False, current_limit=5)
        self.assertEqual(self.safety.effective_limit(), 40)
        self.safety.on_send_error(gmail.GmailError("quota", rate_limited=True))
        self.assertFalse(self.safety.is_paused())

    def test_lowering_maximum_in_settings_lowers_todays_limit(self):
        from .forms import SettingsForm
        form = SettingsForm({"sender_name": "", "company_name": "", "product_description": "", "signature": "",
                             "daily_limit": 8, "auto_protect": "on", "warmup": "on", "warmup_start": 15, "warmup_step": 5},
                            instance=AppSettings.load())
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(AppSettings.load().current_limit, 8)


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

    def test_inbox_lists_replies_and_opens_thread(self):
        from .models import Reply
        self.client.force_login(self.user)
        GmailAccount.objects.create(pk=1, email="sender@example.com", refresh_token="rt", scopes=READ_SCOPES)
        contact = Contact.objects.create(email="hr@prospect.example", first_name="Priya", company="ProspectCo")
        campaign = Campaign.objects.create(name="Jobs", subject="s", body="b")
        msg = Message.objects.create(campaign=campaign, contact=contact, status=Message.STATUS_SENT, subject="My resume",
                                     body="Hello Priya", gmail_thread_id="t1", sent_at=timezone.now())
        Reply.objects.create(message=msg, gmail_id="r1", from_email="hr@prospect.example", from_name="Priya",
                             subject="Re: My resume", snippet="Please share", body="Please share <b>notice</b>",
                             received_at=timezone.now())
        r = self.client.get(reverse("inbox"))
        self.assertContains(r, "Please share")      # the reply in the list (company + summary/snippet)
        self.assertContains(r, "ProspectCo")
        self.assertContains(r, 'class="badge badge-blue"')  # unread count in the tab
        r = self.client.get(reverse("inbox"), {"f": "replies", "open": msg.pk})
        self.assertContains(r, "Please share &lt;b&gt;notice&lt;/b&gt;")  # reply body escaped
        self.assertContains(r, "Hello Priya")                            # the email we sent
        self.assertFalse(Reply.objects.filter(is_read=False).exists())  # opening marks it read
        r = self.client.get(reverse("inbox"), {"f": "sent", "q": "prospectco"})
        self.assertContains(r, "Replied")

    def test_inbox_asks_to_reconnect_without_read_scope(self):
        self.client.force_login(self.user)
        GmailAccount.objects.create(pk=1, email="sender@example.com", refresh_token="rt",
                                    scopes="https://www.googleapis.com/auth/gmail.send")
        self.assertContains(self.client.get(reverse("inbox")), "Reconnect Gmail")

    def test_pause_banner_and_resume(self):
        from . import safety
        self.client.force_login(self.user)
        safety.pause(AppSettings.load(), timezone.now() + timedelta(hours=5), "Gmail rate limit reached.")
        r = self.client.get(reverse("settings"))
        self.assertContains(r, "Sending is paused by the automatic protection")
        self.assertContains(r, "Gmail rate limit reached.")
        self.client.post(reverse("safety_resume"))
        self.assertFalse(safety.is_paused())
        self.assertNotContains(self.client.get(reverse("dashboard")), "Sending is paused")

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


@override_settings(SEND_INTERVAL_SECONDS=0, GEMINI_API_KEY="k")
class AutomationTests(TestCase):
    def setUp(self):
        import tempfile
        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        connect_gmail()
        GmailAccount.objects.filter(pk=1).update(scopes=READ_SCOPES)
        AppSettings.objects.create(pk=1, daily_limit=50, send_window=False, warmup=False, sender_name="Shivam")
        self.jane = Contact.objects.create(email="jane@prospect.example", first_name="Jane", company="ProspectCo")
        self.campaign = Campaign.objects.create(name="Jobs", subject="Application - Shivam", body=GOOD_BODY,
                                                review_first=False, followups=True, followup_days=4, max_followups=2)
        self.sent = []
        self.alerts = []

    def tearDown(self):
        self.media_override.disable()
        self.media.cleanup()

    def send(self, account, mime, thread_id=None):
        self.sent.append((mime, thread_id))
        return {"id": f"g{len(self.sent)}", "threadId": thread_id or "t-root"}

    def sync(self, body, classifier=None, subject="Re: Application - Shivam"):
        from .replies import sync_replies
        data = {"r1": {**reply_data("r1", "t-root", subject=subject), "body": body, "snippet": body[:50]}}
        return sync_replies(list_ids=lambda a, q: [("r1", "t-root")], fetch=lambda a, mid: data[mid],
                            classifier=classifier or (lambda s, t: {"category": "interested", "summary": "Wants a call"}),
                            sender=lambda account, mime, **k: self.alerts.append(mime) or {"id": "a"})

    def test_quality_check(self):
        from .quality import check
        good = fill(GOOD_BODY, self.jane.merge_values())
        self.assertEqual(check("Hello", good, self.jane), [])
        self.assertTrue(any("placeholder" in p for p in check("Hello", good + " at [Company Name]", self.jane)))
        self.assertTrue(any("link" in p for p in check("Hello", good + " https://x.example", self.jane)))
        self.assertTrue(any("greet" in p for p in check("Hello", good.replace("Jane", "there"), self.jane)))
        self.assertTrue(any("short" in p for p in check("Hello", "Hi Jane, thanks.", self.jane)))
        self.assertTrue(any("AI" in p for p in check("Hello", good + " As an AI I wrote this.", self.jane)))

    def test_auto_send_when_check_passes_else_hold(self):
        svc.start(self.campaign)
        self.assertEqual(Message.objects.get().status, Message.STATUS_APPROVED)
        bad = Campaign.objects.create(name="Bad", subject="Hi", body="Hello there.", review_first=False)
        svc.start(bad)
        held = bad.messages.get()
        self.assertEqual(held.status, Message.STATUS_DRAFT)
        self.assertIn("Held for review", held.error)

    def test_ai_emails_are_gated(self):
        ai_campaign = Campaign.objects.create(name="AI", writer=Campaign.WRITER_AI, review_first=False)
        svc.start(ai_campaign)
        good = fill(GOOD_BODY, self.jane.merge_values())
        svc.write_pending(writer=lambda c, ct, app: {"subject": "Full Stack Developer - Shivam", "body": good})
        self.assertEqual(ai_campaign.messages.get().status, Message.STATUS_APPROVED)
        Message.objects.filter(campaign=ai_campaign).update(status=Message.STATUS_PENDING)
        svc.write_pending(writer=lambda c, ct, app: {"subject": "Hi", "body": good + " Regards to [Company]."})
        self.assertEqual(ai_campaign.messages.get().status, Message.STATUS_DRAFT)

    def test_writes_only_a_couple_of_days_ahead_and_waits_when_ai_is_out_of_quota(self):
        from . import ai
        AppSettings.objects.filter(pk=1).update(daily_limit=5, current_limit=5)
        for i in range(30):
            Contact.objects.create(email=f"p{i}@co{i}.example", first_name="Jane")
        ai_campaign = Campaign.objects.create(name="AI", writer=Campaign.WRITER_AI, review_first=False)
        svc.start(ai_campaign)
        good = fill(GOOD_BODY, self.jane.merge_values())
        writer = lambda c, ct, app: {"subject": "Application", "body": good.replace("Jane", ct.first_name)}
        for _ in range(10):
            svc.write_pending(writer=writer)
        approved = ai_campaign.messages.filter(status=Message.STATUS_APPROVED).count()
        self.assertEqual(approved, svc.write_ahead(AppSettings.load()))  # 2 x today's limit, not all 31
        self.assertLess(approved, ai_campaign.messages.count())

        Message.objects.filter(campaign=ai_campaign).update(status=Message.STATUS_PENDING)
        def out_of_quota(c, ct, app):
            raise ai.AIError("Gemini error 429: quota", busy=True)
        try:
            self.assertEqual(svc.write_pending(writer=out_of_quota), 0)
            self.assertFalse(ai_campaign.messages.filter(attempts__gt=0).exists())  # not counted against the emails
            self.assertEqual(svc.write_pending(writer=writer), 0)  # waits instead of retrying at once
        finally:
            svc._ai_paused_until = 0.0
        self.assertEqual(svc.write_pending(writer=writer), 5)

    def test_never_say_list_is_told_to_the_ai_checked_and_rewritten_once(self):
        from . import ai
        from .quality import check
        ai_campaign = Campaign.objects.create(name="AI", writer=Campaign.WRITER_AI, review_first=False,
                                              avoid_phrases="student\nfinal year")
        self.assertEqual(ai_campaign.avoid_list, ["student", "final year"])
        self.assertIn('"student"; "final year"', ai.build_prompt(ai_campaign, self.jane, AppSettings.load()))
        good = fill(GOOD_BODY, self.jane.merge_values())
        self.assertTrue(any("student" in p for p in check("Hi", good + " I am a final-year Student.", self.jane,
                                                          avoid=ai_campaign.avoid_list)))
        self.assertEqual(check("Hi", good + " I mentor students.", self.jane, avoid=["final year"]), [])

        svc.start(ai_campaign)
        drafts = iter([good + " I am a B.Tech student.", good])  # first try fails the check, the rewrite passes
        calls = []
        svc.write_pending(writer=lambda c, ct, app: calls.append(1) or {"subject": "Application", "body": next(drafts)})
        msg = ai_campaign.messages.get()
        self.assertEqual((len(calls), msg.status, msg.body), (2, Message.STATUS_APPROVED, good))

        Message.objects.filter(pk=msg.pk).update(status=Message.STATUS_PENDING)
        calls.clear()
        svc.write_pending(writer=lambda c, ct, app: calls.append(1) or {"subject": "Application",
                                                                       "body": good + " A final year student."})
        msg.refresh_from_db()
        self.assertEqual((len(calls), msg.status), (2, Message.STATUS_DRAFT))
        self.assertIn("never-say", msg.error)

    def test_followups_go_in_the_same_thread_until_the_last_one(self):
        from .models import Attachment
        Attachment.objects.create(campaign=self.campaign, name="resume.pdf", size=4,
                                  file=SimpleUploadedFile("resume.pdf", b"%PDF"))
        t0 = timezone.now()
        svc.start(self.campaign)
        first = svc.send_next(now=t0, sender=self.send)
        self.assertEqual((first.status, first.gmail_thread_id), (Message.STATUS_SENT, "t-root"))
        root_mime = self.sent[0][0]
        self.assertEqual([a.get_filename() for a in root_mime.iter_attachments()], ["resume.pdf"])
        self.assertEqual(svc.schedule_followups(t0 + timedelta(days=1)), 0)        # not yet due

        self.assertEqual(svc.schedule_followups(t0 + timedelta(days=5)), 1)
        fu1 = svc.send_next(now=t0 + timedelta(days=5), sender=self.send)
        mime, thread = self.sent[-1]
        self.assertEqual((fu1.step, thread, fu1.gmail_thread_id), (1, "t-root", "t-root"))
        self.assertEqual(mime["Subject"], "Re: Application - Shivam")
        self.assertEqual(mime["In-Reply-To"], first.rfc_message_id)
        self.assertEqual(list(mime.iter_attachments()), [])                       # resume only once
        self.assertTrue(fu1.body.startswith("Hi Jane,"))

        self.assertEqual(svc.schedule_followups(t0 + timedelta(days=10)), 1)
        svc.send_next(now=t0 + timedelta(days=10), sender=self.send)
        self.assertIn(first.rfc_message_id, self.sent[-1][0]["References"])
        self.assertEqual(svc.schedule_followups(t0 + timedelta(days=20)), 0)      # max 2 reached
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.STATUS_DONE)

    def test_no_followups_without_permission_to_read_replies(self):
        t0 = timezone.now()
        svc.start(self.campaign)
        svc.send_next(now=t0, sender=self.send)
        GmailAccount.objects.filter(pk=1).update(scopes="https://www.googleapis.com/auth/gmail.send")
        self.assertEqual(svc.schedule_followups(t0 + timedelta(days=5)), 0)

    def test_campaign_not_done_while_a_followup_may_come(self):
        svc.start(self.campaign)
        svc.send_next(sender=self.send)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.STATUS_RUNNING)

    def test_reply_stops_followups_and_alerts_me(self):
        t0 = timezone.now()
        svc.start(self.campaign)
        svc.send_next(now=t0, sender=self.send)
        self.assertEqual(self.sync("Yes, happy to talk tomorrow at 11."), 1)
        from .models import Reply
        self.assertEqual(Reply.objects.get().category, Reply.CAT_INTERESTED)
        self.assertEqual(svc.schedule_followups(t0 + timedelta(days=5)), 0)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0]["To"], "sender@example.com")
        self.assertIn("[Interested]", self.alerts[0]["Subject"])

    def test_out_of_office_does_not_stop_followups(self):
        t0 = timezone.now()
        svc.start(self.campaign)
        svc.send_next(now=t0, sender=self.send)
        classifier = mock.Mock()
        self.sync("I am out of office until Monday with limited access to email.", classifier=classifier)
        classifier.assert_not_called()
        from .models import Reply
        self.assertEqual(Reply.objects.get().category, Reply.CAT_OOO)
        self.assertEqual(svc.schedule_followups(t0 + timedelta(days=5)), 1)
        self.assertEqual(self.alerts, [])

    def test_not_interested_unsubscribes(self):
        svc.start(self.campaign)
        svc.send_next(sender=self.send)
        self.sync("We are not hiring.", classifier=lambda s, t: {"category": "not_interested", "summary": "Not hiring"})
        self.jane.refresh_from_db()
        self.assertTrue(self.jane.unsubscribed)

    def test_classifier_failure_falls_back_to_rules(self):
        svc.start(self.campaign)
        svc.send_next(sender=self.send)

        def broken(s, t):
            raise ai.AIError("quota")
        self.sync("Please remove me from your list.", classifier=broken)
        from .models import Reply
        self.assertEqual(Reply.objects.get().category, Reply.CAT_NOT_INTERESTED)

    def test_business_hours(self):
        from . import safety
        from datetime import datetime as dt, time as t
        AppSettings.objects.filter(pk=1).update(send_window=True, send_days="0,1,2,3,4", send_start=t(10), send_end=t(18))
        tz = timezone.get_current_timezone()
        saturday = timezone.make_aware(dt(2026, 11, 14, 12, 0), tz)   # a Saturday
        monday_10 = timezone.make_aware(dt(2026, 11, 16, 10, 0), tz)
        self.assertFalse(safety.in_send_window(now=saturday))
        self.assertEqual(safety.next_window_start(now=saturday), monday_10)
        self.assertFalse(safety.in_send_window(now=monday_10 - timedelta(minutes=1)))
        self.assertTrue(safety.in_send_window(now=monday_10 + timedelta(hours=1)))
        self.assertFalse(safety.in_send_window(now=monday_10 + timedelta(hours=8)))  # 18:00 is closed
        svc.start(self.campaign)
        self.assertIsNone(svc.send_next(now=saturday, sender=self.send))
        self.assertEqual(svc.send_next(now=monday_10 + timedelta(hours=1), sender=self.send).status, Message.STATUS_SENT)

    def test_ai_followup_gets_the_history(self):
        ai_campaign = Campaign.objects.create(name="AI", writer=Campaign.WRITER_AI, review_first=False, followup_days=3)
        root = Message.objects.create(campaign=ai_campaign, contact=self.jane, status=Message.STATUS_SENT, subject="My application",
                                      body="Hi Jane, first email.", gmail_thread_id="t1", sent_at=timezone.now() - timedelta(days=4))
        ai_campaign.status = Campaign.STATUS_RUNNING
        ai_campaign.save()
        self.assertEqual(svc.schedule_followups(), 1)
        writer = mock.Mock(return_value={"body": "Hi Jane,\n\nJust checking in on my note from last week about full stack roles at ProspectCo. Happy to share anything else that helps. Would a quick call work?"})
        svc.write_pending(followup_writer=writer)
        fu = ai_campaign.messages.get(step=1)
        self.assertEqual((fu.status, fu.subject, fu.parent), (Message.STATUS_APPROVED, "Re: My application", root))
        args = writer.call_args.args
        self.assertEqual((args[3], args[4]), ([("My application", "Hi Jane, first email.")], 1))

    def test_strip_signoff(self):
        self.assertEqual(ai.strip_signoff("Hi Jane,\n\nThanks for reading.\n\nBest regards,\nShivam Kumar"),
                         "Hi Jane,\n\nThanks for reading.")
        self.assertEqual(ai.strip_signoff("Hi Jane,\n\nThanks for reading."), "Hi Jane,\n\nThanks for reading.")

    def test_followup_writer_and_classifier_call_gemini(self):
        def resp(text):
            r = mock.Mock(status_code=200)
            r.json.return_value = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
            return r
        with mock.patch("mailer.ai.requests.post", return_value=resp('{"body": "Hi Jane,\\n\\nJust a nudge.\\n\\nRegards,\\nShivam"}')) as post:
            out = ai.write_followup(self.campaign, self.jane, AppSettings.load(), [("S", "B")], 2)
        self.assertEqual(out, {"body": "Hi Jane,\n\nJust a nudge."})
        self.assertIn("last follow-up", post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"])
        with mock.patch("mailer.ai.requests.post", return_value=resp('{"category": "Referral", "summary": "Contact Ravi"}')):
            self.assertEqual(ai.classify_reply("S", "Please write to Ravi"), {"category": "referral", "summary": "Contact Ravi"})
        with mock.patch("mailer.ai.requests.post", return_value=resp('{"category": "maybe"}')):
            self.assertEqual(ai.classify_reply("S", "?")["category"], "other")

    def test_inbox_filters_and_timeline(self):
        user = get_user_model().objects.create_user("me", password="pw-for-tests")
        self.client.force_login(user)
        t0 = timezone.now()
        svc.start(self.campaign)
        svc.send_next(now=t0, sender=self.send)
        svc.schedule_followups(t0 + timedelta(days=5))
        fu = svc.send_next(now=t0 + timedelta(days=5), sender=self.send)
        self.sync("Sounds great, are you free Friday?")
        r = self.client.get(reverse("inbox"), {"f": "replies", "cat": "positive"})
        self.assertContains(r, "Interested")
        r = self.client.get(reverse("inbox"), {"f": "sent", "open": fu.pk})   # opening a follow-up shows the whole thread
        self.assertContains(r, "You · follow-up 1")
        self.assertContains(r, "Wants a call")

    def test_settings_business_hours_form(self):
        user = get_user_model().objects.create_user("me2", password="pw-for-tests")
        self.client.force_login(user)
        r = self.client.post(reverse("settings"), {
            "sender_name": "Shivam", "daily_limit": 40, "auto_protect": "on", "warmup": "on", "warmup_start": 15,
            "warmup_step": 5, "send_window": "on", "send_day_choices": ["0", "2", "4"], "send_start": "09:30",
            "send_end": "17:00", "notify_interested": "on"})
        self.assertRedirects(r, reverse("settings"))
        app = AppSettings.load()
        self.assertEqual((app.send_days, str(app.send_start)), ("0,2,4", "09:30:00"))
        r = self.client.post(reverse("settings"), {"daily_limit": 40, "warmup_start": 15, "warmup_step": 5, "send_window": "on",
                                                   "send_day_choices": ["1"], "send_start": "18:00", "send_end": "10:00"})
        self.assertContains(r, "Must be later than the start time")

    def test_single_worker_lock(self):
        from io import StringIO
        from django.core.management import call_command
        from mailer.management.commands.run_worker import Command
        call_command("run_worker", "--once", stdout=StringIO())   # gets the lock and runs one pass
        self.assertTrue(Command()._lock())                         # same session may take it again
