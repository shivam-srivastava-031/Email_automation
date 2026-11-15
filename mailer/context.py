from . import safety
from .models import AppSettings, GmailAccount, Message, Reply


def nav(request):
    """Values every page's top bar needs."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    app = AppSettings.load()
    return {
        "nav_gmail": GmailAccount.current(),
        "nav_to_review": Message.objects.filter(status=Message.STATUS_DRAFT).count(),
        "nav_unread": Reply.objects.filter(is_read=False).count(),
        "nav_paused": safety.is_paused(app),
        "nav_pause_reason": app.pause_reason,
        "nav_paused_until": app.paused_until,
    }
