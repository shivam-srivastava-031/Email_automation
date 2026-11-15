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
        return sync_replies(list_ids=lambda account, q: listed, fetch=lambda account, mid: data[mid])

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
        AppSettings.objects.create(pk=1, daily_limit=5)
        Contact.objects.create(email="p@example.com")
        c = Campaign.objects.create(name="C", subject="s", body="b", review_first=False)
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
        self.app = AppSettings.objects.create(pk=1, daily_limit=40, current_limit=20)
        self.contacts = [Contact.objects.create(email=f"p{i}@example.com") for i in range(3)]
        self.campaign = Campaign.objects.create(name="C", subject="s", body="b", review_first=False)
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
        self.assertContains(r, "Re: My resume")
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
