from django import forms
from django.conf import settings

from .models import AppSettings, Campaign, Contact, ContactList


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["email", "first_name", "last_name", "company", "role", "lists"]
        widgets = {"lists": forms.CheckboxSelectMultiple}

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        qs = Contact.objects.filter(email=email)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A contact with this email already exists.")
        return email


class ImportForm(forms.Form):
    file = forms.FileField(label="CSV file")
    contact_list = forms.ModelChoiceField(
        queryset=ContactList.objects.all(), required=False, empty_label="(no list)", label="Add to list")
    new_list = forms.CharField(max_length=120, required=False, label="Or a new list")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".csv", ".txt")):
            raise forms.ValidationError("Upload a .csv file (in Excel or Google Sheets: File > Download > CSV).")
        if f.size > 10 * 1024 * 1024:
            raise forms.ValidationError("The file is larger than 10 MB.")
        return f

    def target_list(self):
        name = (self.cleaned_data.get("new_list") or "").strip()
        if name:
            return ContactList.objects.get_or_create(name=name)[0]
        return self.cleaned_data.get("contact_list")


class CampaignForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ["name", "contact_list", "writer", "ai_instructions", "subject", "body",
                  "cta_text", "cta_url", "review_first",
                  "followups", "followup_days", "max_followups", "followup_instructions", "followup_body"]
        widgets = {
            "writer": forms.RadioSelect,
            "ai_instructions": forms.Textarea(attrs={"rows": 4, "placeholder": "e.g. Mention we help sales teams book more meetings. Keep it friendly. Ask for a 15-minute call."}),
            "subject": forms.TextInput(attrs={"placeholder": "Quick question, {{first_name}}"}),
            "body": forms.Textarea(attrs={"rows": 9, "placeholder": "Hi {{first_name}},\n\nI noticed {{company}} ..."}),
            "cta_text": forms.TextInput(attrs={"placeholder": "Book a 15-minute call"}),
            "cta_url": forms.URLInput(attrs={"placeholder": "https://"}),
            "followup_instructions": forms.Textarea(attrs={"rows": 3, "placeholder": "e.g. Mention I can start within 15 days. Keep it very short."}),
            "followup_body": forms.Textarea(attrs={"rows": 5, "placeholder": "Hi {{first_name}},\n\nJust following up on my email below..."}),
            "followup_days": forms.NumberInput(attrs={"min": 1, "max": 30}),
            "max_followups": forms.NumberInput(attrs={"min": 0, "max": 3}),
        }
        labels = {
            "contact_list": "Send to",
            "writer": "Who writes the email?",
            "ai_instructions": "Tips for the AI",
            "body": "Message",
            "cta_text": "Button text",
            "cta_url": "Button link",
            "review_first": "Let me review every email before it is sent",
            "followups": "Follow up automatically when there is no reply",
            "followup_days": "Wait this many days for a reply",
            "max_followups": "Follow-ups at most",
            "followup_instructions": "Tips for the follow-ups",
            "followup_body": "Follow-up text",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact_list"].empty_label = "All contacts"
        for name in ("followup_days", "max_followups"):
            self.fields[name].required = False

    def _number(self, name, low, high, message):
        n = self.cleaned_data.get(name)
        if n is None:  # left empty: keep what it was (or the default)
            return getattr(self.instance, name) if self.instance.pk else Campaign._meta.get_field(name).default
        if not low <= n <= high:
            raise forms.ValidationError(message)
        return n

    def clean_followup_days(self):
        return self._number("followup_days", 1, 30, "Choose between 1 and 30 days.")

    def clean_max_followups(self):
        return self._number("max_followups", 0, 3, "Choose between 0 and 3.")


WEEKDAYS = [("0", "Mon"), ("1", "Tue"), ("2", "Wed"), ("3", "Thu"), ("4", "Fri"), ("5", "Sat"), ("6", "Sun")]


class SettingsForm(forms.ModelForm):
    send_day_choices = forms.MultipleChoiceField(choices=WEEKDAYS, required=False, widget=forms.CheckboxSelectMultiple,
                                                 label="Sending days")

    class Meta:
        model = AppSettings
        fields = ["sender_name", "company_name", "product_description", "signature", "daily_limit",
                  "auto_protect", "warmup", "warmup_start", "warmup_step",
                  "send_window", "send_start", "send_end", "notify_interested"]
        widgets = {
            "send_start": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "send_end": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "product_description": forms.Textarea(attrs={"rows": 4, "placeholder": "e.g. Full stack developer (Django, React) with 9+ months of internship experience... or: We build an AI assistant that books sales meetings."}),
            "signature": forms.Textarea(attrs={"rows": 3, "placeholder": "Your Name\nYour Title, Your Company"}),
        }
        labels = {"sender_name": "Your name", "company_name": "Company (optional)", "product_description": "About you / what you offer",
                  "daily_limit": "Maximum per day", "auto_protect": "Protect my Gmail account automatically",
                  "warmup": "Warm up slowly", "warmup_start": "Start at (per day)", "warmup_step": "Add each healthy day",
                  "send_window": "Send only during business hours", "send_start": "From", "send_end": "Until",
                  "notify_interested": "Email me when someone replies with interest"}
        help_texts = {"daily_limit": "The most emails per day, ever. The protection may keep today's limit lower."}

    def clean_daily_limit(self):
        n = self.cleaned_data["daily_limit"]
        if not 1 <= n <= 500:  # Gmail's own cap is ~500/day for free accounts
            raise forms.ValidationError("Choose between 1 and 500.")
        return n

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["send_day_choices"].initial = [str(d) for d in self.instance.send_day_list]
        for name in ("send_start", "send_end"):
            self.fields[name].required = False

    def clean(self):
        data = super().clean()
        for name in ("send_start", "send_end"):  # left empty: keep the saved time
            if not data.get(name) and name not in self.errors:
                data[name] = getattr(self.instance, name)
        if data.get("send_window"):
            if not data.get("send_day_choices"):
                self.add_error("send_day_choices", "Pick at least one day.")
            start, end = data.get("send_start"), data.get("send_end")
            if start and end and start >= end:
                self.add_error("send_end", "Must be later than the start time.")
        return data

    def clean_warmup_start(self):
        n = self.cleaned_data["warmup_start"]
        if not 1 <= n <= 200:
            raise forms.ValidationError("Choose between 1 and 200.")
        return n

    def clean_warmup_step(self):
        n = self.cleaned_data["warmup_step"]
        if not 1 <= n <= 50:
            raise forms.ValidationError("Choose between 1 and 50.")
        return n

    def save(self, commit=True):
        obj = super().save(commit=False)
        if "send_day_choices" in self.cleaned_data and (self.cleaned_data["send_day_choices"] or not obj.send_window):
            obj.send_days = ",".join(sorted(self.cleaned_data["send_day_choices"])) or obj.send_days
        # A lower maximum takes effect at once; protection never exceeds it.
        if obj.current_limit and obj.current_limit > obj.daily_limit:
            obj.current_limit = obj.daily_limit
        # Turning the warm-up on restarts it from its starting point.
        if "warmup" in self.changed_data and obj.warmup:
            obj.current_limit = min(obj.daily_limit, obj.warmup_start)
        if commit:
            obj.save()
        return obj


class AttachmentForm(forms.Form):
    file = forms.FileField()

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > settings.MAX_ATTACHMENT_MB * 1024 * 1024:
            raise forms.ValidationError(f"Files must be under {settings.MAX_ATTACHMENT_MB} MB.")
        return f
