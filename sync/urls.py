"""Sync routes, mounted under /api/<version>/sync/."""

from django.urls import path

from .views import SyncDownloadView, SyncStatusView, SyncUploadView

app_name = 'sync'

urlpatterns = [
    path('upload/', SyncUploadView.as_view(), name='upload'),
    path('download/', SyncDownloadView.as_view(), name='download'),
    path('status/', SyncStatusView.as_view(), name='status'),
]
