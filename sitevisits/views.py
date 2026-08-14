"""Site visit endpoints.

One visit is open at a time. The server holds both stamps and computes the
duration from them, so the figure a supervisor reads is one nobody's phone
had a hand in.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import (
    GenericAPIView,
    ListAPIView,
    ListCreateAPIView,
    RetrieveAPIView,
)
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Site, SiteVisit, VisitStatus
from .permissions import HasBusinessPermission, IsVisitOwner
from .serializers import (
    AddImageSerializer,
    CancelVisitSerializer,
    CheckInSerializer,
    CheckOutSerializer,
    SiteCreateSerializer,
    SiteSerializer,
    SiteVisitImageSerializer,
    SiteVisitSerializer,
)

VISIT_QUERYSET = SiteVisit.objects.select_related('site', 'user').prefetch_related(
    'images'
)


class SiteListView(ListCreateAPIView):
    """The project sites available to call on, and the way to register one.

    **Query** `search` matches the site name, its code or the customer.
    `customer_id` narrows the list to one customer's sites.

    Reads stay open to any signed-in user — a site is somewhere to visit, not
    a private record. Registering one needs `onboard_customers`, the same
    permission that gates adding the customer it belongs to.

    **Responses**
    * `200` — a paginated list
    * `201` — the site, in the same shape a list entry has
    * `400` — a field failed validation, keyed by field name
    * `401` — missing or invalid access token
    * `403` — the role does not allow onboarding
    """

    permission_classes = [IsAuthenticated, HasBusinessPermission]
    search_fields = ['name', 'code', 'customer_name', 'city']

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return SiteCreateSerializer
        return SiteSerializer

    @property
    def required_permission(self):
        # The read is deliberately ungated; only the write is.
        return 'onboard_customers' if self.request.method == 'POST' else None

    def get_queryset(self):
        queryset = Site.objects.filter(is_active=True)

        customer_id = self.request.query_params.get('customer_id', '').strip()
        if customer_id:
            queryset = queryset.filter(customer_ref=customer_id)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        site = serializer.save()

        # Answered with the read serializer, so a site that was just created
        # and a site off the list are the same object to the client.
        return Response(
            SiteSerializer(site, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class SiteVisitListView(ListAPIView):
    """The signed-in user's own visits, newest first.

    Scoped by the queryset rather than by a parameter the client sends, so
    there is nothing to tamper with.

    **Query** `status` (`in_progress`, `completed`, `cancelled`),
    `follow_up_due=true` for the visits whose follow-up date has arrived.

    **Responses**
    * `200` — a paginated list
    * `401` — missing or invalid access token
    """

    serializer_class = SiteVisitSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = VISIT_QUERYSET.filter(user=self.request.user)

        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        if self.request.query_params.get('follow_up_due') == 'true':
            queryset = queryset.filter(
                follow_up_date__isnull=False,
                follow_up_date__lte=timezone.localdate(),
                status=VisitStatus.COMPLETED,
            )

        return queryset


class SiteVisitDetailView(RetrieveAPIView):
    """One visit, with its photos.

    **Responses**
    * `200` — the visit
    * `403` — it belongs to another user
    * `404` — no such visit
    """

    serializer_class = SiteVisitSerializer
    permission_classes = [IsAuthenticated, IsVisitOwner]
    queryset = VISIT_QUERYSET


@extend_schema(
    responses={200: OpenApiTypes.OBJECT},
    summary='The visit currently in progress',
)
class OpenVisitView(APIView):
    """The visit the user is in the middle of, or `null`.

    The client asks this on launch to decide whether to offer a check-in or a
    check-out.

    **Responses**
    * `200` — `{"visit": {...}}` or `{"visit": null}`
    * `401` — missing or invalid access token
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        visit = VISIT_QUERYSET.filter(
            user=request.user, status=VisitStatus.IN_PROGRESS
        ).first()
        return Response(
            {
                'visit': SiteVisitSerializer(
                    visit, context={'request': request}
                ).data
                if visit
                else None
            },
            status=status.HTTP_200_OK,
        )


class CheckInView(GenericAPIView):
    """Arrive at a site.

    Only one visit may be open at a time — two at once would make every
    duration a guess. The distance from where the site is plotted is recorded
    but never used to refuse the visit: a site's pin is often approximate
    while the person is standing on it.

    **Request** `{"site": "<uuid>", "purpose": "follow_up", "latitude": …,
    "longitude": …, "accuracy": …, "address": "…", "captured_at": "…",
    "sync_id": "<uuid>"}`

    **Responses**
    * `201` — the open visit
    * `200` — this `sync_id` was already recorded; the original is returned
    * `400` — bad GPS fix, stale timestamp, or an inactive site
    * `401` — missing or invalid access token
    * `403` — the role lacks `log_site_visits`
    * `409` — a visit is already open
    """

    serializer_class = CheckInSerializer
    permission_classes = [IsAuthenticated, HasBusinessPermission]
    required_permission = 'log_site_visits'

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        sync_id = data.get('sync_id')
        if sync_id:
            existing = SiteVisit.objects.filter(
                user=request.user, sync_id=sync_id
            ).first()
            if existing:
                return Response(
                    SiteVisitSerializer(existing, context={'request': request}).data,
                    status=status.HTTP_200_OK,
                )

        open_visit = SiteVisit.objects.filter(
            user=request.user, status=VisitStatus.IN_PROGRESS
        ).first()
        if open_visit:
            return Response(
                {
                    'detail': (
                        f'You are already checked in at {open_visit.site.name}. '
                        'Check out before starting another visit.'
                    ),
                    'visit': SiteVisitSerializer(
                        open_visit, context={'request': request}
                    ).data,
                },
                status=status.HTTP_409_CONFLICT,
            )

        site = data['site']
        at = data.get('captured_at') or timezone.now()

        visit = SiteVisit(
            user=request.user,
            site=site,
            purpose=data['purpose'],
            check_in_at=at,
            check_in_latitude=data['latitude'],
            check_in_longitude=data['longitude'],
            check_in_accuracy_meters=data.get('accuracy'),
            check_in_address=data.get('address', ''),
            check_in_distance_meters=site.distance_from(
                data['latitude'], data['longitude']
            ),
            remarks=data.get('remarks', ''),
        )
        if sync_id:
            visit.sync_id = sync_id
        visit.save()

        return Response(
            SiteVisitSerializer(visit, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class CheckOutView(GenericAPIView):
    """Leave the site, and record what the visit found.

    `duration_minutes` is computed from the two server-held stamps.

    **Responses**
    * `200` — the closed visit
    * `400` — bad GPS fix, or a check-out before the check-in
    * `403` — the role lacks `log_site_visits`, or it is another user's visit
    * `404` — no such visit
    * `409` — the visit is already closed
    """

    serializer_class = CheckOutSerializer
    permission_classes = [IsAuthenticated, HasBusinessPermission, IsVisitOwner]
    required_permission = 'log_site_visits'

    def post(self, request, pk, *args, **kwargs):
        visit = get_object_or_404(VISIT_QUERYSET, pk=pk)
        self.check_object_permissions(request, visit)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if not visit.is_open:
            return Response(
                {'detail': 'This visit has already been closed.'},
                status=status.HTTP_409_CONFLICT,
            )

        at = data.get('captured_at') or timezone.now()
        if at < visit.check_in_at:
            return Response(
                {'captured_at': ['Check-out cannot be earlier than check-in.']},
                status=status.HTTP_400_BAD_REQUEST,
            )

        visit.close(
            at,
            check_out_latitude=data['latitude'],
            check_out_longitude=data['longitude'],
            check_out_accuracy_meters=data.get('accuracy'),
            check_out_address=data.get('address', ''),
            stage_observed=data.get('stage_observed', ''),
            competitor_brands=data.get('competitor_brands', []),
            expected_order_value=data.get('expected_order_value'),
            follow_up_date=data.get('follow_up_date'),
        )
        if data.get('remarks'):
            visit.remarks = data['remarks']
        visit.save()

        return Response(
            SiteVisitSerializer(visit, context={'request': request}).data,
            status=status.HTTP_200_OK,
        )


class AddImageView(GenericAPIView):
    """Attach a photo to an open visit.

    Photos are only accepted while the visit is open — a picture added after
    the fact is not evidence of what was there at the time.

    **Request** `multipart/form-data`: `image`, `tag`, `caption`, `latitude`,
    `longitude`, `captured_at`

    **Responses**
    * `201` — the visit, with the new photo on it
    * `400` — not an image, or a field is malformed
    * `403` — the role lacks `log_site_visits`, or it is another user's visit
    * `409` — the visit is closed
    """

    serializer_class = AddImageSerializer
    permission_classes = [IsAuthenticated, HasBusinessPermission, IsVisitOwner]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    required_permission = 'log_site_visits'

    def post(self, request, pk, *args, **kwargs):
        visit = get_object_or_404(VISIT_QUERYSET, pk=pk)
        self.check_object_permissions(request, visit)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if not visit.is_open:
            return Response(
                {'detail': 'This visit is closed, so no more photos can be added.'},
                status=status.HTTP_409_CONFLICT,
            )

        visit.images.create(
            image=data['image'],
            tag=data['tag'],
            caption=data.get('caption', ''),
            latitude=data.get('latitude'),
            longitude=data.get('longitude'),
            captured_at=data.get('captured_at') or timezone.now(),
        )
        visit.refresh_from_db()

        return Response(
            SiteVisitSerializer(visit, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    responses={200: OpenApiTypes.OBJECT},
    summary='Remove a visit photo',
)
class RemoveImageView(APIView):
    """Remove a photo from an open visit.

    **Responses**
    * `200` — the visit, without it
    * `403` — another user's visit
    * `404` — no such visit or photo
    * `409` — the visit is closed
    """

    permission_classes = [IsAuthenticated, HasBusinessPermission, IsVisitOwner]
    required_permission = 'log_site_visits'

    def delete(self, request, pk, image_pk, *args, **kwargs):
        visit = get_object_or_404(VISIT_QUERYSET, pk=pk)
        self.check_object_permissions(request, visit)

        if not visit.is_open:
            return Response(
                {'detail': 'This visit is closed, so its photos are fixed.'},
                status=status.HTTP_409_CONFLICT,
            )

        image = get_object_or_404(visit.images, pk=image_pk)
        with transaction.atomic():
            # Delete the file as well as the row; an orphaned upload is
            # somebody's storage bill and somebody's privacy problem.
            image.image.delete(save=False)
            image.delete()

        visit.refresh_from_db()
        return Response(
            SiteVisitSerializer(visit, context={'request': request}).data,
            status=status.HTTP_200_OK,
        )


class CancelVisitView(GenericAPIView):
    """Abandon an open visit, with a reason.

    Cancelled rather than deleted: a visit that was started and then vanished
    is a hole in the record.

    **Responses**
    * `200` — the cancelled visit
    * `400` — no reason given
    * `409` — the visit is already closed
    """

    serializer_class = CancelVisitSerializer
    permission_classes = [IsAuthenticated, HasBusinessPermission, IsVisitOwner]
    required_permission = 'log_site_visits'

    def post(self, request, pk, *args, **kwargs):
        visit = get_object_or_404(VISIT_QUERYSET, pk=pk)
        self.check_object_permissions(request, visit)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not visit.is_open:
            return Response(
                {'detail': 'This visit has already been closed.'},
                status=status.HTTP_409_CONFLICT,
            )

        visit.status = VisitStatus.CANCELLED
        visit.check_out_at = timezone.now()
        visit.duration_minutes = visit.worked_minutes()
        visit.remarks = serializer.validated_data['reason']
        visit.save()

        return Response(
            SiteVisitSerializer(visit, context={'request': request}).data,
            status=status.HTTP_200_OK,
        )
