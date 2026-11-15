from django.contrib import admin

from .models import AppSettings, Attachment, Campaign, Contact, ContactList, GmailAccount, Message, Reply


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("email", "first_name", "last_name", "company", "unsubscribed", "created_at")
    list_filter = ("unsubscribed", "lists")
    search_fields = ("email", "first_name", "last_name", "company")


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "writer", "status", "contact_list", "created_at")
    list_filter = ("status", "writer")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("contact", "campaign", "status", "sent_at", "attempts")
    list_filter = ("status", "campaign")
    search_fields = ("contact__email", "subject")


@admin.register(GmailAccount)
class GmailAccountAdmin(admin.ModelAdmin):
    list_display = ("email", "connected_at")
    exclude = ("refresh_token", "access_token")  # never show tokens


@admin.register(Reply)
class ReplyAdmin(admin.ModelAdmin):
    list_display = ("from_email", "subject", "kind", "received_at", "is_read")
    list_filter = ("kind", "is_read")
    search_fields = ("from_email", "subject", "snippet")


admin.site.register(ContactList)
admin.site.register(Attachment)
admin.site.register(AppSettings)
