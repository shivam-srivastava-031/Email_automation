import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import ai, campaigns as svc, gmail
from .csv_import import import_csv
from .forms import AttachmentForm, CampaignForm, ContactForm, ImportForm, SettingsForm
from .models import (
    AppSettings, Attachment, Campaign, Contact, ContactList, GmailAccount, Message, Reply, sent_today_count,
)
from .render import BASE_FIELDS, build_email, fill, unknown_fields
from .replies import sync_replies

log = logging.getLogger(__name__)


# ── Overview ────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    app = AppSettings.load()
    account = GmailAccount.current()
    status_counts = dict(Message.objects.values_list("status").annotate(n=Count("id")))
    recent = list(Campaign.objects.all()[:5])
    for c in recent:
        c.stats = c.counts()
    steps = [
        ("Connect Gmail", bool(account), "settings"),
        ("Add your name and what you offer", bool(app.sender_name and app.product_description), "settings"),
        ("Import contacts", Contact.objects.exists(), "contacts"),
        ("Create a campaign", Campaign.objects.exists(), "campaign_new"),
    ]
    return render(request, "mailer/dashboard.html", {
        "tab": "overview",
        "account": account,
        "app": app,
        "sent_today": sent_today_count(),
        "to_review": status_counts.get(Message.STATUS_DRAFT, 0),
        "queued": status_counts.get(Message.STATUS_APPROVED, 0),
        "writing": status_counts.get(Message.STATUS_PENDING, 0),
        "failed": status_counts.get(Message.STATUS_FAILED, 0),
        "contacts_count": Contact.objects.count(),
        "recent": recent,
        "steps": steps,
        "setup_done": all(done for _, done, _ in steps),
        "running": Campaign.objects.filter(status=Campaign.STATUS_RUNNING).exists(),
    })


# ── Contacts ────────────────────────────────────────────────────────────────

@login_required
def contacts(request):
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Contact added.")
            return redirect("contacts")
    else:
        form = ContactForm()

    qs = Contact.objects.prefetch_related("lists")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(company__icontains=q))
    active_list = None
    if request.GET.get("list"):
        active_list = ContactList.objects.filter(pk=request.GET["list"]).first()
        if active_list:
            qs = qs.filter(lists=active_list)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "mailer/contacts.html", {
        "tab": "contacts",
        "form": form,
        "import_form": ImportForm(),
        "page": page,
        "q": q,
        "lists": ContactList.objects.annotate(n=Count("contacts")),
        "active_list": active_list,
        "total": Contact.objects.count(),
        "show_add": request.method == "POST",
    })


@login_required
@require_POST
def contacts_import(request):
    form = ImportForm(request.POST, request.FILES)
    if not form.is_valid():
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        return redirect("contacts")
    target = form.target_list()
    result = import_csv(form.cleaned_data["file"].read(), contact_list=target)
    if result.total:
        where = f" to “{target.name}”" if target else ""
        messages.success(request, f"Imported {result.total} contacts{where}: {result.added} new, {result.updated} updated."
                         + (f" Skipped {result.skipped} rows." if result.skipped else ""))
    for err in result.errors:
        messages.warning(request, err)
    if target:
        return redirect(f"{_url('contacts')}?list={target.pk}")
    return redirect("contacts")


@login_required
@require_POST
def contact_delete(request, pk):
    get_object_or_404(Contact, pk=pk).delete()
    messages.success(request, "Contact deleted.")
    return redirect(request.POST.get("next") or "contacts")


@login_required
@require_POST
def contact_unsubscribe(request, pk):
    c = get_object_or_404(Contact, pk=pk)
    c.unsubscribed = not c.unsubscribed
    c.save(update_fields=["unsubscribed", "updated_at"])
    messages.success(request, f"{c.email} will {'no longer' if c.unsubscribed else 'again'} get emails.")
    return redirect(request.POST.get("next") or "contacts")


@login_required
@require_POST
def list_delete(request, pk):
    lst = get_object_or_404(ContactList, pk=pk)
    lst.delete()
    messages.success(request, f"List “{lst.name}” deleted. Its contacts were kept.")
    return redirect("contacts")


# ── Campaigns ───────────────────────────────────────────────────────────────

@login_required
def campaign_list(request):
    items = list(Campaign.objects.select_related("contact_list"))
    for c in items:
        c.stats = c.counts()
    return render(request, "mailer/campaigns.html", {"tab": "campaigns", "items": items})


def _form_context(form, campaign=None):
    sample = Contact.objects.filter(unsubscribed=False).first()
    extra_fields = sorted({k for c in Contact.objects.exclude(extra={}).values_list("extra", flat=True)[:200] for k in c})
    return {
        "tab": "campaigns",
        "form": form,
        "campaign": campaign,
        "merge_fields": BASE_FIELDS + [f for f in extra_fields if f not in BASE_FIELDS],
        "sample": sample,
        "ai_ready": bool(settings.GEMINI_API_KEY),
        "attachment_form": AttachmentForm(),
    }


@login_required
def campaign_new(request):
    form = CampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        campaign = form.save()
        messages.success(request, "Campaign saved. Add files if you like, then start it.")
        return redirect("campaign_edit", pk=campaign.pk)
    return render(request, "mailer/campaign_form.html", _form_context(form))


@login_required
def campaign_edit(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    form = CampaignForm(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid():
        form.save()
        note = " Emails already written keep their text." if campaign.messages.exists() else ""
        messages.success(request, "Campaign saved." + note)
        if request.POST.get("then") == "start":
            return campaign_start(request, pk)
        return redirect("campaign_edit", pk=pk)
    return render(request, "mailer/campaign_form.html", _form_context(form, campaign))


@login_required
@require_POST
def campaign_preview(request):
    """The email for the sample contact, from the (unsaved) form values."""
    app = AppSettings.load()
    contact = Contact.objects.filter(unsubscribed=False).first()
    data = request.POST
    cta_text, cta_url = data.get("cta_text", ""), data.get("cta_url", "")
    if data.get("writer") == Campaign.WRITER_AI:
        if not data.get("ai_sample"):
            return JsonResponse({"ai": True})
        if contact is None:
            return JsonResponse({"error": "Import a contact first: the AI needs someone to write to."}, status=400)
        draft = Campaign(name="preview", writer=Campaign.WRITER_AI, ai_instructions=data.get("ai_instructions", ""),
                         cta_text=cta_text, cta_url=cta_url)
        try:
            out = ai.write_email(draft, contact, app)
        except ai.AIError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        subject, body = out["subject"], out["body"]
    else:
        values = contact.merge_values() if contact else {f: f"[{f}]" for f in BASE_FIELDS}
        subject, body = fill(data.get("subject", ""), values), fill(data.get("body", ""), values)
    html_body, _ = build_email(body, cta_text, cta_url, app.signature)
    known = set(BASE_FIELDS) | (set(contact.merge_values()) if contact else set())
    return JsonResponse({
        "subject": subject,
        "html": html_body,
        "to": contact.email if contact else "",
        "unknown": unknown_fields(data.get("subject", "") + data.get("body", ""), known),
    })


@login_required
@require_POST
def campaign_start(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    try:
        added = svc.start(campaign)
    except svc.CampaignError as exc:
        messages.error(request, str(exc))
        return redirect("campaign_edit", pk=pk)
    if campaign.is_ai:
        what = f"The AI is writing {added} emails."
    elif campaign.review_first:
        what = f"{added} emails are ready for you to review."
    else:
        what = f"{added} emails are queued to send."
    if not GmailAccount.current():
        what += " Connect Gmail in Settings so they can be sent."
    messages.success(request, f"Campaign started. {what} Keep the worker running (python manage.py run_worker).")
    return redirect("campaign_detail", pk=pk)


@login_required
@require_POST
def campaign_pause(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    if campaign.status == Campaign.STATUS_RUNNING:
        campaign.status = Campaign.STATUS_PAUSED
        campaign.save(update_fields=["status", "updated_at"])
        messages.success(request, "Paused. Nothing more is written or sent until you resume.")
    return redirect("campaign_detail", pk=pk)


@login_required
@require_POST
def campaign_resume(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    if campaign.status in (Campaign.STATUS_PAUSED, Campaign.STATUS_DONE):
        campaign.status = Campaign.STATUS_RUNNING
        campaign.save(update_fields=["status", "updated_at"])
        svc.refresh_done(campaign)
        messages.success(request, "Resumed.")
    return redirect("campaign_detail", pk=pk)


@login_required
@require_POST
def campaign_delete(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    campaign.delete()
    messages.success(request, f"Campaign “{campaign.name}” deleted.")
    return redirect("campaign_list")


@login_required
@require_POST
def campaign_approve_all(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    n = svc.approve_all_drafts(campaign)
    messages.success(request, f"Approved {n} emails. They go out one by one within your daily limit.")
    return redirect("campaign_detail", pk=pk)


FILTERS = [
    ("review", "To review", [Message.STATUS_DRAFT]),
    ("queued", "Queued", [Message.STATUS_APPROVED, Message.STATUS_PENDING]),
    ("sent", "Sent", [Message.STATUS_SENT]),
    ("problems", "Failed & skipped", [Message.STATUS_FAILED, Message.STATUS_SKIPPED]),
]


@login_required
def campaign_detail(request, pk):
    campaign = get_object_or_404(Campaign.objects.select_related("contact_list"), pk=pk)
    stats = campaign.counts()
    default = "review" if stats[Message.STATUS_DRAFT] else "queued" if (stats[Message.STATUS_APPROVED] or stats[Message.STATUS_PENDING]) else "sent"
    current = request.GET.get("f") or default
    statuses = next((s for k, _, s in FILTERS if k == current), FILTERS[0][2])
    rows = campaign.messages.select_related("contact").filter(status__in=statuses).order_by("-updated_at")
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    app = AppSettings.load()
    for m in page:
        m.preview_html = build_email(m.body, campaign.cta_text, campaign.cta_url, app.signature)[0]
    filters = []
    for key, label, st in FILTERS:
        filters.append({"key": key, "label": label, "count": sum(stats[s] for s in st), "active": key == current})
    done = stats[Message.STATUS_SENT]
    return render(request, "mailer/campaign_detail.html", {
        "tab": "campaigns",
        "campaign": campaign,
        "stats": stats,
        "filters": filters,
        "current": current,
        "page": page,
        "progress": int(100 * done / stats["total"]) if stats["total"] else 0,
        "account": GmailAccount.current(),
        "app": app,
        "sent_today": sent_today_count(),
    })


@login_required
@require_POST
def message_action(request, pk):
    msg = get_object_or_404(Message.objects.select_related("campaign"), pk=pk)
    action = request.POST.get("action")
    back = request.POST.get("next") or _url("campaign_detail", msg.campaign_id)
    try:
        if action == "approve":
            svc.approve(msg, request.POST.get("subject"), request.POST.get("body"))
            messages.success(request, f"Approved. Queued for {msg.contact.email}.")
        elif action == "save":
            msg.subject = request.POST.get("subject", msg.subject).strip()[:300]
            msg.body = request.POST.get("body", msg.body).strip()
            msg.save(update_fields=["subject", "body", "updated_at"])
            messages.success(request, "Saved.")
        elif action == "skip" and msg.status in Message.OPEN_STATUSES + (Message.STATUS_FAILED,):
            msg.status = Message.STATUS_SKIPPED
            msg.save(update_fields=["status", "updated_at"])
            svc.refresh_done(msg.campaign)
            messages.success(request, f"Skipped. {msg.contact.email} will not get this email.")
        elif action == "retry" and msg.status == Message.STATUS_FAILED:
            msg.status = Message.STATUS_PENDING if (msg.campaign.is_ai and not msg.body) else Message.STATUS_APPROVED
            msg.attempts, msg.error = 0, ""
            msg.save(update_fields=["status", "attempts", "error", "updated_at"])
            if msg.campaign.status == Campaign.STATUS_DONE:
                msg.campaign.status = Campaign.STATUS_RUNNING
                msg.campaign.save(update_fields=["status", "updated_at"])
            messages.success(request, "Queued again.")
    except svc.CampaignError as exc:
        messages.error(request, str(exc))
    return redirect(back)


@login_required
@require_POST
def attachment_add(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        f = form.cleaned_data["file"]
        total = sum(a.size for a in campaign.attachments.all()) + f.size
        if total > settings.MAX_ATTACHMENT_MB * 1024 * 1024:
            messages.error(request, f"All files together must stay under {settings.MAX_ATTACHMENT_MB} MB.")
        else:
            Attachment.objects.create(campaign=campaign, file=f, name=f.name, size=f.size)
            messages.success(request, f"Added {f.name}.")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect("campaign_edit", pk=pk)


@login_required
@require_POST
def attachment_delete(request, pk):
    att = get_object_or_404(Attachment, pk=pk)
    campaign_id = att.campaign_id
    att.file.delete(save=False)
    att.delete()
    messages.success(request, "File removed.")
    return redirect("campaign_edit", pk=campaign_id)


# ── Inbox: sent emails and the replies to them ──────────────────────────────

@login_required
def inbox(request):
    view = request.GET.get("f") if request.GET.get("f") in ("replies", "sent") else "replies"
    q = request.GET.get("q", "").strip()
    account = GmailAccount.current()

    if view == "replies":
        rows = Reply.objects.select_related("message__contact", "message__campaign")
        if q:
            rows = rows.filter(Q(from_email__icontains=q) | Q(from_name__icontains=q) | Q(subject__icontains=q)
                               | Q(snippet__icontains=q) | Q(message__contact__company__icontains=q))
    else:
        rows = (Message.objects.select_related("contact", "campaign").filter(status=Message.STATUS_SENT)
                .annotate(reply_count=Count("replies")).order_by("-sent_at"))
        if q:
            rows = rows.filter(Q(contact__email__icontains=q) | Q(contact__first_name__icontains=q)
                               | Q(contact__last_name__icontains=q) | Q(contact__company__icontains=q) | Q(subject__icontains=q))
    page = Paginator(rows, 30).get_page(request.GET.get("page"))

    # The open conversation: the email we sent and every reply to it.
    thread = None
    open_id = request.GET.get("open")
    if open_id and open_id.isdigit():
        thread = Message.objects.select_related("contact", "campaign").filter(pk=open_id, status=Message.STATUS_SENT).first()
    if thread is not None:
        app = AppSettings.load()
        thread.preview_html = build_email(thread.body, thread.campaign.cta_text, thread.campaign.cta_url, app.signature)[0]
        thread.thread_replies = list(thread.replies.order_by("received_at"))
        thread.replies.filter(is_read=False).update(is_read=True)

    return render(request, "mailer/inbox.html", {
        "tab": "inbox",
        "view": view,
        "q": q,
        "page": page,
        "thread": thread,
        "account": account,
        "can_read": bool(account and account.can_read_replies),
        "reply_count": Reply.objects.filter(kind=Reply.KIND_REPLY).count(),
        "unread_count": Reply.objects.filter(is_read=False).count(),
        "sent_count": Message.objects.filter(status=Message.STATUS_SENT).count(),
        "replied_count": Message.objects.filter(replied_at__isnull=False).count(),
        "keep": urlencode({k: v for k, v in (("f", view), ("q", q), ("page", request.GET.get("page", ""))) if v}),
    })


@login_required
@require_POST
def inbox_sync(request):
    account = GmailAccount.current()
    if account is None or not account.can_read_replies:
        messages.error(request, "Reconnect Gmail in Settings to allow reading replies.")
        return redirect("inbox")
    try:
        added = sync_replies()
    except gmail.GmailError as exc:
        messages.error(request, f"Could not check for replies: {exc}")
    else:
        messages.success(request, f"{added} new repl{'y' if added == 1 else 'ies'}." if added else "No new replies.")
    return redirect(request.POST.get("next") or "inbox")


# ── Settings & Gmail ────────────────────────────────────────────────────────

@login_required
def settings_view(request):
    app = AppSettings.load()
    form = SettingsForm(request.POST or None, instance=app)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("settings")
    return render(request, "mailer/settings.html", {
        "tab": "settings",
        "form": form,
        "account": GmailAccount.current(),
        "gmail_configured": gmail.is_configured(),
        "ai_ready": bool(settings.GEMINI_API_KEY),
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "interval": settings.SEND_INTERVAL_SECONDS,
    })


@login_required
def gmail_connect(request):
    if not gmail.is_configured():
        messages.error(request, "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to the .env file first.")
        return redirect("settings")
    url, state, verifier = gmail.authorization_url()
    request.session["gmail_oauth"] = {"state": state, "verifier": verifier}
    return redirect(url)


@login_required
def gmail_callback(request):
    saved = request.session.pop("gmail_oauth", None)
    if request.GET.get("error"):
        messages.error(request, f"Gmail was not connected: {request.GET['error']}.")
        return redirect("settings")
    if not saved or saved.get("state") != request.GET.get("state"):
        messages.error(request, "The Gmail sign-in expired or did not match. Please try again.")
        return redirect("settings")
    try:
        account = gmail.finish_connect(request.build_absolute_uri(), saved["state"], saved.get("verifier"))
    except Exception as exc:
        log.exception("Gmail connect failed")
        messages.error(request, f"Could not connect Gmail: {exc}")
        return redirect("settings")
    messages.success(request, f"Gmail connected: {account.email}. Emails will be sent from this address.")
    return redirect("settings")


@login_required
@require_POST
def gmail_disconnect(request):
    gmail.disconnect()
    messages.success(request, "Gmail disconnected. Nothing will be sent until you connect it again.")
    return redirect("settings")


def _url(name, *args):
    return reverse(name, args=args)
