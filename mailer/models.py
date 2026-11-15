"""Data for the standalone email automation app.

Contacts come from CSV uploads (or are added by hand) into the local
database. A campaign sends one email per contact of a list, written from a
template or by the AI, through the connected Gmail account, within a daily
limit. Nothing here talks to a CRM.
"""
import os

from django.db import models
from django.utils import timezone


class AppSettings(models.Model):
    """One row (pk=1): who the emails come from and how fast they go out."""
    sender_name = models.CharField(max_length=120, blank=True, default="")
    company_name = models.CharField(max_length=120, blank=True, default="")
    product_description = models.TextField(
        blank=True, default="", help_text="What you offer, in a few sentences. The AI uses this.")
    signature = models.TextField(blank=True, default="", help_text="Added under every email.")
    daily_limit = models.PositiveIntegerField(default=50)

    class Meta:
        verbose_name = verbose_name_plural = "Settings"

    def __str__(self):
        return "Settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class GmailAccount(models.Model):
    """One row (pk=1): the connected Gmail mailbox and its OAuth tokens."""
    email = models.EmailField(blank=True, default="")
    refresh_token = models.TextField(blank=True, default="")
    access_token = models.TextField(blank=True, default="")
    token_expiry = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True, default="")
    connected_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.email or "Gmail (not connected)"

    @property
    def is_connected(self):
        return bool(self.email and self.refresh_token)

    @classmethod
    def current(cls):
        """The connected account, or None."""
        obj = cls.objects.filter(pk=1).first()
        return obj if obj is not None and obj.is_connected else None


class ContactList(models.Model):
    name = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Contact(models.Model):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=120, blank=True, default="")
    last_name = models.CharField(max_length=120, blank=True, default="")
    company = models.CharField(max_length=200, blank=True, default="")
    role = models.CharField(max_length=200, blank=True, default="")
    extra = models.JSONField(default=dict, blank=True, help_text="Other CSV columns, usable as {{column}} fields.")
    lists = models.ManyToManyField(ContactList, related_name="contacts", blank=True)
    unsubscribed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    def merge_values(self):
        """The {{field}} values for this contact: fixed fields win over CSV extras."""
        values = {str(k): str(v) for k, v in (self.extra or {}).items() if v not in (None, "")}
        values.update({
            "email": self.email,
            "first_name": self.first_name or "there",
            "last_name": self.last_name,
            "full_name": self.full_name or self.first_name or "there",
            "company": self.company,
            "role": self.role,
        })
        return values


class Campaign(models.Model):
    WRITER_AI = "ai"
    WRITER_TEMPLATE = "template"
    WRITER_CHOICES = [(WRITER_AI, "AI writes each email"), (WRITER_TEMPLATE, "My template")]

    STATUS_DRAFT = "draft"
    STATUS_RUNNING = "running"
    STATUS_PAUSED = "paused"
    STATUS_DONE = "done"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"), (STATUS_RUNNING, "Running"),
        (STATUS_PAUSED, "Paused"), (STATUS_DONE, "Done"),
    ]

    name = models.CharField(max_length=200)
    contact_list = models.ForeignKey(
        ContactList, null=True, blank=True, on_delete=models.SET_NULL, related_name="campaigns",
        help_text="Empty = all contacts.")
    writer = models.CharField(max_length=12, choices=WRITER_CHOICES, default=WRITER_TEMPLATE)
    ai_instructions = models.TextField(blank=True, default="")
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField(blank=True, default="")
    cta_text = models.CharField(max_length=80, blank=True, default="")
    cta_url = models.URLField(blank=True, default="")
    review_first = models.BooleanField(default=True, help_text="Hold every email until you approve it.")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def is_ai(self):
        return self.writer == self.WRITER_AI

    def recipients(self):
        qs = self.contact_list.contacts.all() if self.contact_list_id else Contact.objects.all()
        return qs.filter(unsubscribed=False)

    def counts(self):
        rows = self.messages.values("status").annotate(n=models.Count("id"))
        out = {s: 0 for s, _ in Message.STATUS_CHOICES}
        out.update({r["status"]: r["n"] for r in rows})
        out["total"] = sum(out[s] for s, _ in Message.STATUS_CHOICES)
        return out


def attachment_path(instance, filename):
    return os.path.join("attachments", str(instance.campaign_id or "new"), filename)


class Attachment(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path)
    name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Message(models.Model):
    STATUS_PENDING = "pending"    # AI has not written it yet
    STATUS_DRAFT = "draft"        # written, waiting for your approval
    STATUS_APPROVED = "approved"  # queued: the worker sends it
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Writing"), (STATUS_DRAFT, "To review"), (STATUS_APPROVED, "Queued"),
        (STATUS_SENT, "Sent"), (STATUS_FAILED, "Failed"), (STATUS_SKIPPED, "Skipped"),
    ]
    OPEN_STATUSES = (STATUS_PENDING, STATUS_DRAFT, STATUS_APPROVED)

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="messages")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="messages")
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField(blank=True, default="", help_text="Plain text; paragraphs separated by a blank line.")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    gmail_id = models.CharField(max_length=64, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["campaign", "contact"], name="one_message_per_contact")]

    def __str__(self):
        return f"{self.campaign} → {self.contact}"


def sent_today_count(now=None):
    """Emails sent since local midnight: what the daily limit counts."""
    now = timezone.localtime(now or timezone.now())
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return Message.objects.filter(status=Message.STATUS_SENT, sent_at__gte=midnight).count()
