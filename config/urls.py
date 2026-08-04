"""Root URL configuration for the SFM backend.

Feature routes are mounted under `/api/v1/` as each app's endpoints are built.
The admin stays on its own prefix so it is never versioned with the API.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),

    # `<version>` rather than a literal `v1`: DRF's URLPathVersioning reads
    # this kwarg, checks it against ALLOWED_VERSIONS and sets request.version.
    # With the version hardcoded in the path, the versioning class is
    # configured but never actually consulted.
    path('api/<version>/auth/', include('accounts.urls')),
]

if settings.DEBUG:
    # Uploaded files are served by Django only in development. In production
    # nginx serves MEDIA_ROOT at MEDIA_URL directly.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
