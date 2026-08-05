"""Site visit routes, mounted under /api/<version>/site-visits/."""

from django.urls import path

from .views import (
    AddImageView,
    CancelVisitView,
    CheckInView,
    CheckOutView,
    OpenVisitView,
    RemoveImageView,
    SiteListView,
    SiteVisitDetailView,
    SiteVisitListView,
)

app_name = 'sitevisits'

urlpatterns = [
    path('sites/', SiteListView.as_view(), name='site-list'),
    path('', SiteVisitListView.as_view(), name='list'),
    path('open/', OpenVisitView.as_view(), name='open'),
    path('check-in/', CheckInView.as_view(), name='check-in'),
    path('<uuid:pk>/', SiteVisitDetailView.as_view(), name='detail'),
    path('<uuid:pk>/check-out/', CheckOutView.as_view(), name='check-out'),
    path('<uuid:pk>/cancel/', CancelVisitView.as_view(), name='cancel'),
    path('<uuid:pk>/images/', AddImageView.as_view(), name='add-image'),
    path(
        '<uuid:pk>/images/<uuid:image_pk>/',
        RemoveImageView.as_view(),
        name='remove-image',
    ),
]
