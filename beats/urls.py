"""Beat routes, mounted under /api/<version>/beats/."""

from django.urls import path

from .views import (
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
