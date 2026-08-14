"""Beat routes, mounted under /api/<version>/beats/."""

from django.urls import path

from .views import (
    BulkPlanView,
    BulkStartView,
    BeatListView,
    BeatPlanDetailView,
    BeatPlanListCreateView,
    CompleteBeatView,
    MarkVisitedView,
    SkipVisitView,
    StartBeatView,
)

app_name = 'beats'

urlpatterns = [
    path('', BeatListView.as_view(), name='list'),
    path('plans/', BeatPlanListCreateView.as_view(), name='plans'),
    # Multi-select on the planning screen. One row per beat comes back,
    # so a half-applied selection is legible instead of guesswork.
    path('plans/bulk/', BulkPlanView.as_view(), name='plans-bulk'),
    path('plans/bulk-start/', BulkStartView.as_view(), name='plans-bulk-start'),
    path('plans/<uuid:pk>/', BeatPlanDetailView.as_view(), name='plan-detail'),
    path('plans/<uuid:pk>/start/', StartBeatView.as_view(), name='plan-start'),
    path('plans/<uuid:pk>/complete/', CompleteBeatView.as_view(), name='plan-complete'),
    path(
        'plans/<uuid:pk>/visits/<uuid:visit_pk>/visit/',
        MarkVisitedView.as_view(),
        name='visit-mark',
    ),
    path(
        'plans/<uuid:pk>/visits/<uuid:visit_pk>/skip/',
        SkipVisitView.as_view(),
        name='visit-skip',
    ),
]
