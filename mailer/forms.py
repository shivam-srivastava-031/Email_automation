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
                  "cta_text", "cta_url", "review_first"]
        widgets = {
            "writer": forms.RadioSelect,
            "ai_instructions": forms.Textarea(attrs={"rows": 4, "placeholder": "e.g. Mention we help sales teams book more meetings. Keep it friendly. Ask for a 15-minute call."}),
            "subject": forms.TextInput(attrs={"placeholder": "Quick question, {{first_name}}"}),
            "body": forms.Textarea(attrs={"rows": 9, "placeholder": "Hi {{first_name}},\n\nI noticed {{company}} ..."}),
            "cta_text": forms.TextInput(attrs={"placeholder": "Book a 15-minute call"}),
            "cta_url": forms.URLInput(attrs={"placeholder": "https://"}),
        }
        labels = {
            "contact_list": "Send to",
            "writer": "Who writes the email?",
            "ai_instructions": "Tips for the AI",
            "body": "Message",
            "cta_text": "Button text",
            "cta_url": "Button link",
            "review_first": "Let me review each email before it is sent",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact_list"].empty_label = "All contacts"


class SettingsForm(forms.ModelForm):
    class Meta:
        model = AppSettings
        fields = ["sender_name", "company_name", "product_description", "signature", "daily_limit",
                  "auto_protect", "warmup", "warmup_start", "warmup_step"]
        widgets = {
            "product_description": forms.Textarea(attrs={"rows": 4, "placeholder": "e.g. Full stack developer (Django, React) with 9+ months of internship experience... or: We build an AI assistant that books sales meetings."}),
            "signature": forms.Textarea(attrs={"rows": 3, "placeholder": "Your Name\nYour Title, Your Company"}),
        }
        labels = {"sender_name": "Your name", "company_name": "Company (optional)", "product_description": "About you / what you offer",
                  "daily_limit": "Maximum per day", "auto_protect": "Protect my Gmail account automatically",
                  "warmup": "Warm up slowly", "warmup_start": "Start at (per day)", "warmup_step": "Add each healthy day"}
        help_texts = {"daily_limit": "The most emails per day, ever. The protection may keep today's limit lower."}

    def clean_daily_limit(self):
        n = self.cleaned_data["daily_limit"]
        if not 1 <= n <= 500:  # Gmail's own cap is ~500/day for free accounts
            raise forms.ValidationError("Choose between 1 and 500.")
        return n

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
