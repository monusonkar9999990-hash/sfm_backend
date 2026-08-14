"""Public routes, mounted directly under /api/<version>/.

The admin CRUD behind them is mounted under /api/<version>/admin/ by
`admin_urls.py`, so the public surface and the managed surface do not share a
prefix — a reader can tell which is which from the path alone.
"""

from django.urls import path

from .views import (
    AnnouncementListView,
    AppConfigView,
    AppVersionView,
    HealthView,
    PrivacyPolicyView,
    TermsView,
)

app_name = 'appinfo'

urlpatterns = [
    path('health/', HealthView.as_view(), name='health'),
    path('privacy/', PrivacyPolicyView.as_view(), name='privacy'),
    path('terms/', TermsView.as_view(), name='terms'),
    path('app-version/', AppVersionView.as_view(), name='app-version'),
    path('app-config/', AppConfigView.as_view(), name='app-config'),
    path('announcements/', AnnouncementListView.as_view(), name='announcements'),
]
