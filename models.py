"""Email orchestrator: per-agent email knowledge base, the decision/queue row,
and the settings that gate whether the AI may email a prospect at all.

Why this exists: the LinkedIn reply prompt tells the agent to "ask for their
email and commit to send" whenever a lead asks for material. Nothing stood
behind that commitment, so the agent promised and never delivered (deal 3011).
This app is the thing that delivers.

Every email has the same simple shape: a reply that continues the LinkedIn
conversation (written by the AI, or a fixed template), the attachments, and a
call-to-action button. That is the whole product.

Team sharing: the business admin's connected email and email content are
shared with the whole team by default (TeamEmailSettings). The admin's two
switches apply to everyone at once; each agent's two switches are that
person's own choice. An agent uses the team's mailbox or content only when
BOTH are on - see sharing.py for the one place that decides it.

Rules the models encode:
  * mode is off / review / auto, never a bare boolean;
  * every outward email is preceded by an EmailDecision row that stores WHY
    (evidence quotes, gates that fired), so a rep can audit or undo it from
    the deal page;
  * an agent's active attachments go with EVERY email it sends - there is no
    per-email selection to second-guess.
"""
from django.conf import settings as dj_settings
from django.db import models

from common.models import BaseModel


class AgentEmailSettings(models.Model):
    MODE_OFF = "off"
    MODE_REVIEW = "review"
    MODE_AUTO = "auto"
    MODE_CHOICES = [
        (MODE_OFF, "Off"),
        (MODE_REVIEW, "Review before send"),
        (MODE_AUTO, "Automatic"),
    ]
    MODES = {m for m, _ in MODE_CHOICES}

    agent = models.OneToOneField(
        "ai_sdr.LinkedInAgent", on_delete=models.CASCADE, related_name="email_settings",
    )
    business = models.ForeignKey(
        "lead.BusinessRecord", on_delete=models.CASCADE, null=True, blank=True,
        related_name="agent_email_settings",
    )
    mode = models.CharField(max_length=8, choices=MODE_CHOICES, default=MODE_OFF, db_index=True)
    # Null means "send as the agent owner's connected Gmail". Set to use one of
    # the business's SMTP / Resend / Gmail sender rows instead.
    sender_config = models.ForeignKey(
        "outreach.SenderConfiguration", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agent_email_settings",
    )
    # Free-text instructions to the email writer ("always attach the deck when
    # asked for details; never quote prices; sign off as Rayees").
    playbook = models.TextField(blank=True, default="")
    daily_cap = models.PositiveIntegerField(default=20)
    cc_owner = models.BooleanField(default=False)
    # The call to action: a button appended by code to every email, so it is
    # never left to the model. On/off reuses this column (it already meant
    # "booking link in the email"). Link = cta_url, else the agent's booking
    # link from the Identity tab.
    include_booking_link = models.BooleanField(default=True)
    cta_text = models.CharField(
        max_length=80, blank=True, default="Book a 15-minute call", db_default="Book a 15-minute call",
    )
    cta_url = models.URLField(max_length=500, blank=True, default="", db_default="")
    # Auto mode holds the send this many minutes so a rep can cancel. Zero by
    # owner's instruction: somebody who just asked for something by email
    # should get it now, not on a schedule. There is deliberately no
    # time-of-day window - a request at 22:00 is answered at 22:00.
    cancel_window_minutes = models.PositiveIntegerField(default=0, db_default=0)

    # How the body is produced.
    #   ai    - the model writes it from the playbook, persona and the thread.
    #   fixed - the template below is used verbatim, merge tags filled in.
    # The model still decides WHETHER to email, to WHOM, and WHICH material,
    # because those are judgements about the conversation; only the prose is
    # taken out of its hands.
    BODY_AI = "ai"
    BODY_FIXED = "fixed"
    BODY_CHOICES = [(BODY_AI, "AI writes it"), (BODY_FIXED, "Fixed template")]
    BODY_MODES = {m for m, _ in BODY_CHOICES}
    body_mode = models.CharField(
        max_length=8, choices=BODY_CHOICES, default=BODY_AI, db_default=BODY_AI,
    )
    fixed_subject = models.CharField(max_length=300, blank=True, default="", db_default="")
    fixed_body = models.TextField(blank=True, default="", db_default="")

    # This person's half of team sharing. On by default (owner's
    # instruction: the admin's email and content apply to the whole team);
    # switching one off means "my own mailbox" / "my own content below".
    # Neither has any effect while the admin has stopped sharing - see
    # sharing.py.
    use_team_mailbox = models.BooleanField(default=True, db_default=True)
    use_team_content = models.BooleanField(default=True, db_default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Agent email settings"
        verbose_name_plural = "Agent email settings"

    def __str__(self):
        return f"email settings for agent {self.agent_id} ({self.mode})"

    @property
    def enabled(self):
        return self.mode in (self.MODE_REVIEW, self.MODE_AUTO)


class TeamEmailSettings(models.Model):
    """The business admin's side of team sharing: the two switches, and the
    email content the team uses (the same fields an agent has for its own).

    A business with no row behaves as if it had one with the defaults -
    both switches on, AI-written, the standard button - so nothing has to be
    created before a team can start.
    """
    business = models.OneToOneField(
        "lead.BusinessRecord", on_delete=models.CASCADE, related_name="team_email_settings",
    )
    # "My team sends from my email" / "My team uses my email content".
    share_mailbox = models.BooleanField(default=True)
    share_content = models.BooleanField(default=True)

    body_mode = models.CharField(
        max_length=8, choices=AgentEmailSettings.BODY_CHOICES, default=AgentEmailSettings.BODY_AI,
    )
    playbook = models.TextField(blank=True, default="")
    fixed_subject = models.CharField(max_length=300, blank=True, default="")
    fixed_body = models.TextField(blank=True, default="")
    # Same names as on AgentEmailSettings so the engine reads either one the
    # same way. An empty cta_url means each agent's own booking link.
    include_booking_link = models.BooleanField(default=True)
    cta_text = models.CharField(max_length=80, blank=True, default="Book a 15-minute call")
    cta_url = models.URLField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Team email settings"
        verbose_name_plural = "Team email settings"

    def __str__(self):
        return f"team email settings for business {self.business_id}"


class EmailAsset(BaseModel):
    """An attachment sent with EVERY email: a file (attached) or a link
    (listed). With an agent it is that agent's own; with agent NULL it is
    part of the team content of `business`. The `snippet` kind is retired and
    never sent.
    """
    KIND_FILE = "file"
    KIND_LINK = "link"
    KIND_SNIPPET = "snippet"
    KIND_CHOICES = [
        (KIND_FILE, "File attachment"),
        (KIND_LINK, "Link"),
        (KIND_SNIPPET, "Snippet / template"),
    ]
    KINDS = {k for k, _ in KIND_CHOICES}

    # NULL = a team attachment (the admin's content), owned by `business`.
    agent = models.ForeignKey(
        "ai_sdr.LinkedInAgent", on_delete=models.CASCADE, related_name="email_assets",
        null=True, blank=True,
    )
    business = models.ForeignKey(
        "lead.BusinessRecord", on_delete=models.CASCADE, null=True, blank=True,
        related_name="email_assets",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    tags = models.JSONField(default=list, blank=True)

    # kind == file
    file = models.FileField(upload_to="email_assets/%Y/%m/", null=True, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True, default="")
    # kind == link
    url = models.URLField(max_length=1000, blank=True, default="")
    # kind == snippet (subject optional; body may use {{first_name}} style tokens)
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.IntegerField(default=0)

    class Meta(BaseModel.Meta):
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["agent", "is_active"]),
            models.Index(fields=["business", "is_active"]),
        ]
        constraints = [
            # An asset nobody owns could never be reached, and one with no
            # business could never be tenant-scoped. Every row needs one.
            models.CheckConstraint(
                check=models.Q(agent__isnull=False) | models.Q(business__isnull=False),
                name="email_asset_has_owner",
            ),
        ]

    def __str__(self):
        owner = f"agent {self.agent_id}" if self.agent_id else f"team {self.business_id}"
        return f"{self.kind}:{self.name} ({owner})"

    @property
    def filename(self):
        return self.file.name.rsplit("/", 1)[-1] if self.file else ""


class EmailDecision(models.Model):
    """One considered email: why it was (or was not) written, and what became of it.

    Also the review queue. `status` walks
      evaluating -> skipped | held | blocked | awaiting_review | scheduled
      awaiting_review -> scheduled (approved) | cancelled (skipped by rep)
      scheduled -> sending -> sent | failed ; scheduled -> cancelled
    """
    TRIGGER_LINKEDIN = "linkedin_message"
    TRIGGER_NOTE = "note"
    TRIGGER_VERDICT = "verdict"
    TRIGGER_MANUAL = "manual"
    TRIGGER_CHOICES = [
        (TRIGGER_LINKEDIN, "LinkedIn message"),
        (TRIGGER_NOTE, "Rep note"),
        (TRIGGER_VERDICT, "Deal verdict"),
        (TRIGGER_MANUAL, "Manual"),
    ]

    STATUS_EVALUATING = "evaluating"
    STATUS_SKIPPED = "skipped"            # model: no email is wanted here
    STATUS_HELD = "held"                  # needs a human: no material, no mailbox, human owns thread
    STATUS_BLOCKED = "blocked"            # a hard gate: unsubscribed, cap, duplicate
    STATUS_AWAITING_REVIEW = "awaiting_review"
    STATUS_SCHEDULED = "scheduled"
    STATUS_SENDING = "sending"
    STATUS_SENT = "sent"
    STATUS_CANCELLED = "cancelled"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_EVALUATING, "Evaluating"),
        (STATUS_SKIPPED, "Skipped"),
        (STATUS_HELD, "Held for a human"),
        (STATUS_BLOCKED, "Blocked"),
        (STATUS_AWAITING_REVIEW, "Awaiting review"),
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_SENDING, "Sending"),
        (STATUS_SENT, "Sent"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_FAILED, "Failed"),
    ]
    STATUSES = {s for s, _ in STATUS_CHOICES}
    # Rows that still occupy the "one open email per contact" slot.
    OPEN_STATUSES = (STATUS_EVALUATING, STATUS_AWAITING_REVIEW, STATUS_SCHEDULED, STATUS_SENDING)
    # Rows a rep can still change.
    EDITABLE_STATUSES = (STATUS_AWAITING_REVIEW, STATUS_SCHEDULED, STATUS_HELD)

    agent = models.ForeignKey(
        "ai_sdr.LinkedInAgent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="email_decisions",
    )
    business = models.ForeignKey(
        "lead.BusinessRecord", on_delete=models.CASCADE, related_name="email_decisions",
    )
    contact = models.ForeignKey(
        "lead.Contact", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="email_decisions",
    )
    deal = models.ForeignKey(
        "lead.DealsAndOpportunity", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="email_decisions",
    )
    sdr_lead_id = models.IntegerField(null=True, blank=True, db_index=True)
    conversation_id = models.IntegerField(null=True, blank=True)

    trigger_source = models.CharField(max_length=24, choices=TRIGGER_CHOICES, db_index=True)
    trigger_ref = models.CharField(max_length=120)
    # The prospect's words that started this, kept verbatim for the UI.
    trigger_text = models.TextField(blank=True, default="")

    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_EVALUATING, db_index=True)
    should_email = models.BooleanField(default=False)
    ask_summary = models.CharField(max_length=300, blank=True, default="")
    recipient_email = models.EmailField(blank=True, default="")
    asset_ids = models.JSONField(default=list, blank=True)
    subject = models.CharField(max_length=300, blank=True, default="")
    body_html = models.TextField(blank=True, default="")
    # Model's stated reasons and the verbatim quotes that justify them; plus
    # the names of gates that fired, appended by code.
    reasons = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=list, blank=True)
    hold_reason = models.CharField(max_length=300, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    model = models.CharField(max_length=64, blank=True, default="")
    raw = models.JSONField(default=dict, blank=True)

    scheduled_at = models.DateTimeField(null=True, blank=True)
    cancel_deadline_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    email_log = models.ForeignKey(
        "common.EmailLog", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="email_decisions",
    )
    reviewed_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_email_decisions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trigger_source", "trigger_ref"], name="email_decision_unique_trigger",
            ),
        ]
        indexes = [
            models.Index(fields=["business", "status"]),
            models.Index(fields=["contact", "-created_at"]),
            models.Index(fields=["agent", "status"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"email decision {self.pk} [{self.status}] -> {self.recipient_email or '?'}"

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES
