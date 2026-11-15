from django.apps import AppConfig


class EmailOrchestratorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "email_orchestrator"

    def ready(self):
        import email_orchestrator.triggers  # noqa: F401
