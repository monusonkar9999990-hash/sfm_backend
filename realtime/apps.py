from django.apps import AppConfig


class RealtimeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'realtime'
    verbose_name = 'Live updates'

    def ready(self):
        # Importing is what connects the receivers. Kept here rather than at
        # module scope so the app registry is fully loaded first.
        from . import signals  # noqa: F401
