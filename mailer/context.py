from .models import GmailAccount, Message, Reply


def nav(request):
    """Values every page's top bar needs."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {
        "nav_gmail": GmailAccount.current(),
        "nav_to_review": Message.objects.filter(status=Message.STATUS_DRAFT).count(),
        "nav_unread": Reply.objects.filter(is_read=False).count(),
    }
