from django.apps import AppConfig


class AdministrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'administration'

    # Not `admin`: that label belongs to `django.contrib.admin`, and Django
    # refuses to start with two apps sharing one. The URLs are still mounted
    # at /api/<version>/admin/, which is what a client sees.
    label = 'administration'
    verbose_name = 'Administration'

    def ready(self):
        # Connecting the audit receivers is the whole reason this method
        # exists — importing for the side effect, which is the documented way
        # to register signals.
        from . import signals  # noqa: F401
