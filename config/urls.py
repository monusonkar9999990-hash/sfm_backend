"""Root URL configuration for the SFM backend.

Feature routes are mounted under `/api/v1/` as each app's endpoints are built.
The admin stays on its own prefix so it is never versioned with the API.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from reports.views import DashboardView

urlpatterns = [
    path('admin/', admin.site.urls),

    # The web portal: the same Flutter application, built for the browser.
    # Served from this project so it shares an origin with the API below it —
    # the portal's calls are then same-site and need no CORS grant.
    path('portal/', include('portal.urls')),

    # `<version>` rather than a literal `v1`: DRF's URLPathVersioning reads
    # this kwarg, checks it against ALLOWED_VERSIONS and sets request.version.
    # With the version hardcoded in the path, the versioning class is
    # configured but never actually consulted.
    path('api/<version>/auth/', include('accounts.urls')),
    path('api/<version>/attendance/', include('attendance.urls')),
    path('api/<version>/beats/', include('beats.urls')),
    path('api/<version>/site-visits/', include('sitevisits.urls')),
    path('api/<version>/customers/', include('customers.urls')),
    path('api/<version>/products/', include('products.urls')),
    path('api/<version>/orders/', include('orders.urls')),

    # The dashboard is the front page rather than a report of one module, so
    # it is routed directly instead of under /reports/.
    path('api/<version>/dashboard/', DashboardView.as_view(), name='dashboard'),
    path('api/<version>/reports/', include('reports.urls')),
    path('api/<version>/sync/', include('sync.urls')),
    # A ticket onto the live-updates socket. The socket itself is answered by
    # `socketio.WSGIApp` in config/wsgi.py, before Django's resolver runs.
    path('api/<version>/realtime/', include('realtime.urls')),
    path('api/<version>/admin/', include('administration.urls')),
    path('api/<version>/admin/', include('appinfo.admin_urls')),

    # Public: no authentication. Mounted last so nothing above it is shadowed.
    path('api/<version>/', include('appinfo.urls')),

    # The OpenAPI document and two readers for it. Outside the versioned
    # prefix because the schema describes every version rather than living in
    # one of them.
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path(
        'api/docs/',
        SpectacularSwaggerView.as_view(url_name='schema'),
        name='swagger-ui',
    ),
    path(
        'api/redoc/',
        SpectacularRedocView.as_view(url_name='schema'),
        name='redoc',
    ),
]

if settings.DEBUG:
    # Uploaded files are served by Django only in development. In production
    # nginx serves MEDIA_ROOT at MEDIA_URL directly.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
