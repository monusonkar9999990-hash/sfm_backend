"""Admin CRUD for the public payloads, mounted under /api/<version>/admin/.

Application settings are not here: they are managed by
`/admin/settings/`, which already stores, validates and audits them.
`/app-config/` is a public projection of that store rather than a second one.
"""

from django.urls import path

from .views import (
    AnnouncementDetailView,
    AnnouncementListCreateView,
    AppReleaseDetailView,
    AppReleaseListCreateView,
    LegalDocumentDetailView,
    LegalDocumentListCreateView,
)

app_name = 'appinfo-admin'

urlpatterns = [
    path(
        'legal-documents/',
        LegalDocumentListCreateView.as_view(),
        name='legal-list',
    ),
    path(
        'legal-documents/<uuid:pk>/',
        LegalDocumentDetailView.as_view(),
        name='legal-detail',
    ),
    path('app-releases/', AppReleaseListCreateView.as_view(), name='release-list'),
    path(
        'app-releases/<uuid:pk>/',
        AppReleaseDetailView.as_view(),
        name='release-detail',
    ),
    path(
        'announcements/',
        AnnouncementListCreateView.as_view(),
        name='announcement-list',
    ),
    path(
        'announcements/<uuid:pk>/',
        AnnouncementDetailView.as_view(),
        name='announcement-detail',
    ),
]
