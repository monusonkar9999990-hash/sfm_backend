"""Root URL configuration for the SFM backend.

Feature routes are mounted under `/api/v1/` as each app's endpoints are built.
The admin stays on its own prefix so it is never versioned with the API.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path('admin/', admin.site.urls),

    # Mounted as each app's API lands:
    # path('api/v1/auth/', include('accounts.urls')),
]

if settings.DEBUG:
    # Uploaded files are served by Django only in development. In production
    # nginx serves MEDIA_ROOT at MEDIA_URL directly.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
