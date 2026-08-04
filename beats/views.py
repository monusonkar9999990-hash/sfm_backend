"""Beat endpoints.

A day's run moves planned -> in_progress -> completed (or missed). The status
is derived from what actually happened at the stops rather than set by the
client, so a plan cannot be reported complete with every outlet still pending.
"""

from calendar import monthrange
from datetime import date

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import GenericAPIView, ListAPIView, RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Beat, BeatPlan, BeatPlanStatus, BeatPlanVisit, VisitStatus
from .permissions import HasBusinessPermission, IsPlanOwner
from .serializers import (
    BeatPlanCreateSerializer,
    BeatPlanSerializer,
    BeatSerializer,
    CompleteBeatSerializer,
    EmptySerializer,
    MarkVisitedSerializer,
    SkipVisitSerializer,
)

PLAN_PREFETCH = ('visits',)
PLAN_SELECT = ('beat', 'user')


def _as_date(raw, fallback):
    """Parses a `YYYY-MM-DD` query parameter, or falls back.

    Raises ValueError on anything else, so a typo answers 400 rather than
    reaching the ORM and surfacing as a 500.
    """
    return date.fromisoformat(raw) if raw else fallback


class BeatListView(ListAPIView):
    """The routes assigned to the signed-in user, with their stops in order.

    **Responses**
    * `200` — a paginated list of beats
    * `401` — missing or invalid access token
    """

    serializer_class = BeatSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            Beat.objects.filter(assigned_user=self.request.user)
            .prefetch_related('outlets')
            .order_by('name')
        )


class BeatPlanListCreateView(GenericAPIView):
    """List the signed-in user's beat plans, or schedule a beat onto a day.

    `GET` accepts `from` and `to` (`YYYY-MM-DD`) to bound the window; without
    them it returns the current month, which is what a calendar screen needs.

    `POST` snapshots the route's stops onto the new plan, so a stop added to
    the route tomorrow does not appear in today's coverage.

    **Responses**
    * `200` — the list, or the existing plan when `sync_id` is replayed
    * `201` — the plan was created
    * `400` — inactive beat, empty route, a date in the past, or someone
      else's beat
    * `401` — missing or invalid access token
    * `403` — the role lacks `plan_beats`
    * `409` — this beat is already planned for that day
    """

    permission_classes = [IsAuthenticated, HasBusinessPermission]
    required_permission = 'plan_beats'

    def get_serializer_class(self):
        return BeatPlanCreateSerializer if self.request.method == 'POST' else BeatPlanSerializer

    def get(self, request, *args, **kwargs):
        today = timezone.localdate()
        try:
            start = _as_date(request.query_params.get('from'), today.replace(day=1))
            end = _as_date(
                request.query_params.get('to'),
                today.replace(day=monthrange(today.year, today.month)[1]),
            )
        except ValueError:
            return Response(
                {'detail': 'Use YYYY-MM-DD for `from` and `to`.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        plans = (
            BeatPlan.objects.filter(user=request.user, date__gte=start, date__lte=end)
            .select_related(*PLAN_SELECT)
            .prefetch_related(*PLAN_PREFETCH)
        )
        page = self.paginate_queryset(plans)
        serializer = BeatPlanSerializer(page, many=True, context={'request': request})
        return self.get_paginated_response(serializer.data)

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        sync_id = data.get('sync_id')
        if sync_id:
            existing = BeatPlan.objects.filter(
                user=request.user, sync_id=sync_id
            ).first()
            if existing:
                return Response(
                    BeatPlanSerializer(existing, context={'request': request}).data,
                    status=status.HTTP_200_OK,
                )

        plan = BeatPlan(
            beat=data['beat'],
            user=request.user,
            date=data['date'],
            remarks=data.get('remarks', ''),
        )
        if sync_id:
            plan.sync_id = sync_id

        try:
            with transaction.atomic():
                plan.save()
                plan.snapshot_outlets()
                plan.save(update_fields=['planned_outlet_count'])
        except IntegrityError:
            return Response(
                {'detail': 'This beat is already planned for that day.'},
                status=status.HTTP_409_CONFLICT,
            )

        payload = BeatPlanSerializer(plan, context={'request': request}).data
        payload['off_schedule'] = data['_off_schedule']
        return Response(payload, status=status.HTTP_201_CREATED)


class BeatPlanDetailView(RetrieveAPIView):
    """One plan, with every stop and its status.

    **Responses**
    * `200` — the plan
    * `403` — the plan belongs to another user
    * `404` — no such plan
    """

    serializer_class = BeatPlanSerializer
    permission_classes = [IsAuthenticated, IsPlanOwner]
    queryset = BeatPlan.objects.select_related(*PLAN_SELECT).prefetch_related(
        *PLAN_PREFETCH
    )


class PlanActionView(GenericAPIView):
    """Shared plumbing for the actions that operate on one plan."""

    permission_classes = [IsAuthenticated, HasBusinessPermission, IsPlanOwner]
    required_permission = 'plan_beats'

    def get_plan(self, pk):
        plan = get_object_or_404(
            BeatPlan.objects.select_related(*PLAN_SELECT).prefetch_related(
                *PLAN_PREFETCH
            ),
            pk=pk,
        )
        self.check_object_permissions(self.request, plan)
        return plan

    def plan_response(self, plan, http_status=status.HTTP_200_OK):
        plan.refresh_from_db()
        return Response(
            BeatPlanSerializer(plan, context={'request': self.request}).data,
            status=http_status,
        )


class StartBeatView(PlanActionView):
    """Start the day's run.

    Moves a `planned` plan to `in_progress` and stamps `started_at`. Starting
    an already-started plan returns it unchanged rather than failing, because
    a phone that retried cannot tell the difference.

    **Responses**
    * `200` — the plan, now in progress
    * `403` — not this user's plan, or the role lacks `plan_beats`
    * `404` — no such plan
    * `409` — the plan is already closed
    """

    serializer_class = EmptySerializer

    def post(self, request, pk, *args, **kwargs):
        plan = self.get_plan(pk)

        if plan.status in {BeatPlanStatus.COMPLETED, BeatPlanStatus.MISSED}:
            return Response(
                {'detail': 'This beat plan is already closed.'},
                status=status.HTTP_409_CONFLICT,
            )

        if plan.status == BeatPlanStatus.PLANNED:
            plan.status = BeatPlanStatus.IN_PROGRESS
            plan.started_at = timezone.now()
            plan.save(update_fields=['status', 'started_at'])

        return self.plan_response(plan)


class CompleteBeatView(PlanActionView):
    """Close the day's run.

    A plan with nothing visited closes as `missed`, not `completed` — that is
    the whole difference between a beat that was run badly and one that was
    never run. Any stop still pending is left pending; the plan is closed
    around it.

    **Responses**
    * `200` — the closed plan
    * `403` — not this user's plan, or the role lacks `plan_beats`
    * `404` — no such plan
    * `409` — the plan is already closed
    """

    serializer_class = CompleteBeatSerializer

    def post(self, request, pk, *args, **kwargs):
        plan = self.get_plan(pk)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not plan.is_open:
            return Response(
                {'detail': 'This beat plan is already closed.'},
                status=status.HTTP_409_CONFLICT,
            )

        plan.status = (
            BeatPlanStatus.COMPLETED if plan.covered_count else BeatPlanStatus.MISSED
        )
        plan.closed_at = timezone.now()
        if serializer.validated_data.get('remarks'):
            plan.remarks = serializer.validated_data['remarks']
        plan.save(update_fields=['status', 'closed_at', 'remarks'])

        return self.plan_response(plan)


class VisitActionView(PlanActionView):
    """Shared plumbing for the two per-stop actions."""

    def get_visit(self, plan, visit_pk):
        return get_object_or_404(BeatPlanVisit, pk=visit_pk, plan=plan)

    def advance_plan(self, plan):
        """Keep the plan's status honest after a stop changes.

        Starting work on any stop implies the run has begun; visiting the last
        outstanding stop finishes it. Neither is something the client should
        have to tell us.
        """
        fields = []
        if plan.status == BeatPlanStatus.PLANNED:
            plan.status = BeatPlanStatus.IN_PROGRESS
            plan.started_at = plan.started_at or timezone.now()
            fields += ['status', 'started_at']

        if plan.is_fully_covered and plan.status == BeatPlanStatus.IN_PROGRESS:
            plan.status = BeatPlanStatus.COMPLETED
            plan.closed_at = timezone.now()
            fields += ['status', 'closed_at']

        if fields:
            plan.save(update_fields=set(fields))


class MarkVisitedView(VisitActionView):
    """Record that a stop was called on.

    When the last pending stop is visited the plan closes itself as
    `completed`, so the client does not have to make a second call.

    **Responses**
    * `200` — the plan, with the stop now `visited`
    * `400` — a visit stamped in the future
    * `403` — not this user's plan, or the role lacks `plan_beats`
    * `404` — no such plan or stop
    * `409` — the plan is closed, or the stop was already resolved
    """

    serializer_class = MarkVisitedSerializer

    def post(self, request, pk, visit_pk, *args, **kwargs):
        plan = self.get_plan(pk)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not plan.is_open:
            return Response(
                {'detail': 'This beat plan is already closed.'},
                status=status.HTTP_409_CONFLICT,
            )

        visit = self.get_visit(plan, visit_pk)
        if not visit.is_pending:
            return Response(
                {'detail': f'This outlet is already marked {visit.status}.'},
                status=status.HTTP_409_CONFLICT,
            )

        visit.status = VisitStatus.VISITED
        visit.visited_at = serializer.validated_data.get('visited_at') or timezone.now()
        visit.save(update_fields=['status', 'visited_at'])

        self.advance_plan(plan)
        return self.plan_response(plan)


class SkipVisitView(VisitActionView):
    """Skip a stop, with a reason.

    A skipped stop never counts as covered — coverage would stop meaning
    anything if it did. The reason is required because an unexplained gap in a
    route is not information anyone can act on.

    **Responses**
    * `200` — the plan, with the stop now `skipped`
    * `400` — no reason given
    * `403` — not this user's plan, or the role lacks `plan_beats`
    * `404` — no such plan or stop
    * `409` — the plan is closed, or the stop was already resolved
    """

    serializer_class = SkipVisitSerializer

    def post(self, request, pk, visit_pk, *args, **kwargs):
        plan = self.get_plan(pk)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not plan.is_open:
            return Response(
                {'detail': 'This beat plan is already closed.'},
                status=status.HTTP_409_CONFLICT,
            )

        visit = self.get_visit(plan, visit_pk)
        if not visit.is_pending:
            return Response(
                {'detail': f'This outlet is already marked {visit.status}.'},
                status=status.HTTP_409_CONFLICT,
            )

        visit.status = VisitStatus.SKIPPED
        visit.skip_reason = serializer.validated_data['reason']
        visit.save(update_fields=['status', 'skip_reason'])

        self.advance_plan(plan)
        return self.plan_response(plan)
