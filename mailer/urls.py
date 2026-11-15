from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),

    path("contacts/", views.contacts, name="contacts"),
    path("contacts/import/", views.contacts_import, name="contacts_import"),
    path("contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
    path("contacts/<int:pk>/unsubscribe/", views.contact_unsubscribe, name="contact_unsubscribe"),
    path("lists/<int:pk>/delete/", views.list_delete, name="list_delete"),

    path("campaigns/", views.campaign_list, name="campaign_list"),
    path("campaigns/new/", views.campaign_new, name="campaign_new"),
    path("campaigns/preview/", views.campaign_preview, name="campaign_preview"),
    path("campaigns/<int:pk>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<int:pk>/edit/", views.campaign_edit, name="campaign_edit"),
    path("campaigns/<int:pk>/start/", views.campaign_start, name="campaign_start"),
    path("campaigns/<int:pk>/pause/", views.campaign_pause, name="campaign_pause"),
    path("campaigns/<int:pk>/resume/", views.campaign_resume, name="campaign_resume"),
    path("campaigns/<int:pk>/delete/", views.campaign_delete, name="campaign_delete"),
    path("campaigns/<int:pk>/approve-all/", views.campaign_approve_all, name="campaign_approve_all"),
    path("campaigns/<int:pk>/files/", views.attachment_add, name="attachment_add"),
    path("files/<int:pk>/delete/", views.attachment_delete, name="attachment_delete"),
    path("messages/<int:pk>/", views.message_action, name="message_action"),

    path("inbox/", views.inbox, name="inbox"),
    path("inbox/check/", views.inbox_sync, name="inbox_sync"),

    path("settings/", views.settings_view, name="settings"),
    path("settings/resume/", views.safety_resume, name="safety_resume"),
    path("gmail/connect/", views.gmail_connect, name="gmail_connect"),
    path("gmail/callback/", views.gmail_callback, name="gmail_callback"),
    path("gmail/disconnect/", views.gmail_disconnect, name="gmail_disconnect"),
]
