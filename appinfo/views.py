"""Public configuration endpoints, and the admin CRUD behind them.

The public five take no authentication: the app calls them before anybody has
signed in, and two of them — the version check and the maintenance flag — are
what it needs precisely when it *cannot* sign in.
"""

import logging

from django.db import IntegrityError, connection, transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.generics import GenericAPIView, ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from administration.permissions import SplitPermission

from . import services
from .models import Announcement, AppRelease, DocumentKind, LegalDocument
from .serializers import (
    AnnouncementSerializer,
    AppReleaseSerializer,
    AppVersionQuerySerializer,
    LegalDocumentSerializer,
)

logger = logging.getLogger(__name__)


class Conflict(APIException):
    """409, for the race the serializer's own check cannot close."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = 'That record already exists.'
    default_code = 'conflict'


class PublicView(GenericAPIView):
    """Open, read-only, and cached.

    `authentication_classes` is emptied rather than left to default: with the
    JWT authenticator attached, an expired token on a public endpoint produces
    a 401 instead of the payload, and the one moment a client most needs
    `/app-config/` is when its token has just expired.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    http_method_names = ['get', 'head', 'options']


@extend_schema(
    responses={200: OpenApiTypes.OBJECT},
    summary='The privacy policy in force',
)
class PrivacyPolicyView(PublicView):
    """The privacy policy in force.

    **Responses**
    * `200` — the current version
    * `404` — nothing published yet
    """

    def get(self, request, *args, **kwargs):
        payload = services.cached(
            'privacy', lambda: services.legal_document(DocumentKind.PRIVACY)
        )
        if payload is None:
            return Response(
                {'detail': 'No privacy policy has been published yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(payload)


@extend_schema(
    responses={200: OpenApiTypes.OBJECT},
    summary='The terms and conditions in force',
)
class TermsView(PublicView):
    """The terms and conditions in force.

    **Responses** `200` · `404`
    """

    def get(self, request, *args, **kwargs):
        payload = services.cached(
            'terms', lambda: services.legal_document(DocumentKind.TERMS)
        )
        if payload is None:
            return Response(
                {'detail': 'No terms and conditions have been published yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(payload)


@extend_schema(
    responses={200: OpenApiTypes.OBJECT},
    summary='The release the client should be running',
    parameters=[
        OpenApiParameter('platform', str, description='android, ios or web'),
        OpenApiParameter(
            'current_version',
            str,
            description='Send it to receive an update_status verdict.',
        ),
    ],
)
class AppVersionView(PublicView):
    """What the client should be running, and whether this one must update.

    **Query** `platform` (`android`, `ios`, `web`; default android) ·
    `current_version` — send it to get a verdict rather than a description.

    With `current_version`, the response carries `update_status`, one of
    `up_to_date`, `update_available` or `update_required`. The rule that
    decides which is server-side on purpose: a client that computed it could
    not be updated to change it.

    **Responses** `200` · `400` · `404`
    """

    serializer_class = AppVersionQuerySerializer

    def get(self, request, *args, **kwargs):
        query = self.get_serializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        platform = query.validated_data['platform']
        client_version = query.validated_data.get('current_version') or ''

        payload = services.cached(
            f'version:{platform}', lambda: services.app_version(platform)
        )
        if payload is None:
            return Response(
                {'detail': f'No release has been published for {platform}.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        payload = dict(payload)
        if client_version:
            release = AppRelease.current(platform)
            payload['current_version'] = client_version
            payload['update_status'] = release.verdict_for(client_version)

        return Response(payload)


@extend_schema(
    responses={200: OpenApiTypes.OBJECT},
    summary='Application configuration for the client',
)
class AppConfigView(PublicView):
    """Everything the app needs to configure itself.

    Values come from the administration settings store, except the ones the
    server genuinely enforces — those are read from where they take effect, so
    this endpoint cannot describe behaviour the server does not have.
    `enforced_by_server` names which are which.

    **Responses** `200`
    """

    def get(self, request, *args, **kwargs):
        payload = services.cached('config', services.app_config)
        return Response({**payload, 'server_time': services.server_time()})


@extend_schema(
    responses={200: OpenApiTypes.OBJECT},
    summary='Active announcements',
)
class AnnouncementListView(PublicView):
    """Announcements that are active, started and not yet finished.

    Most urgent first. Not paginated: there are never many, and a start-up
    call should not need a second round trip.

    **Responses** `200`
    """

    def get(self, request, *args, **kwargs):
        payload = services.cached('announcements', services.announcements)
        return Response({'count': len(payload), 'results': payload})


# -------------------------------------------------------------- admin CRUD


class AdminPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 200


class AdminWriteView(GenericAPIView):
    """Authenticated, and gated on the permission that means "change how the
    application is configured"."""

    permission_classes = [IsAuthenticated, SplitPermission]
    read_permission = 'edit_configuration'
    write_permission = 'edit_configuration'
    pagination_class = AdminPagination

    def perform_write(self, serializer, **extra):
        try:
            # Its own savepoint, so a constraint violation does not leave the
            # surrounding transaction unusable for the response that reports
            # it — the same trap the customers module fell into.
            with transaction.atomic():
                instance = serializer.save(**extra)
        except IntegrityError as error:
            raise Conflict(
                'That version already exists. Give this one a different '
                'version number.'
            ) from error

        # Every public payload is retired at once — a policy that has just
        # been published has to be the one the next device downloads.
        services.invalidate()
        return instance


class LegalDocumentListCreateView(AdminWriteView, ListCreateAPIView):
    """Every version of both documents, and the way to publish a new one.

    **Query** `kind` (`privacy`, `terms`) · `is_published`

    **Responses** `200` · `201` · `400` · `401` · `403`
    """

    serializer_class = LegalDocumentSerializer

    def get_queryset(self):
        queryset = LegalDocument.objects.select_related('created_by')

        kind = self.request.query_params.get('kind', '').strip()
        if kind:
            queryset = queryset.filter(kind=kind)

        published = self.request.query_params.get('is_published', '').strip().lower()
        if published in ('true', '1'):
            queryset = queryset.filter(is_published=True)
        elif published in ('false', '0'):
            queryset = queryset.filter(is_published=False)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = self.perform_write(serializer, created_by=request.user)
        return Response(
            self.get_serializer(document).data, status=status.HTTP_201_CREATED
        )


class LegalDocumentDetailView(AdminWriteView, RetrieveUpdateDestroyAPIView):
    """One version of a document.

    A published version can be edited — a typo in a policy should be fixable —
    but deleting one is refused while it is the version in force, because
    somebody agreed to it and the record of what they agreed to has to survive.

    **Responses** `200` · `204` · `400` · `401` · `403` · `404` · `409`
    """

    serializer_class = LegalDocumentSerializer
    queryset = LegalDocument.objects.select_related('created_by')

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        document = self.get_object()

        serializer = self.get_serializer(document, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        document = self.perform_write(serializer)

        return Response(self.get_serializer(document).data)

    def destroy(self, request, *args, **kwargs):
        document = self.get_object()

        current = LegalDocument.current(document.kind)
        if current is not None and current.pk == document.pk:
            return Response(
                {
                    'detail': (
                        'This is the version currently in force. Publish a '
                        'newer one first, or unpublish this one.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            document.delete()
        services.invalidate()

        return Response(status=status.HTTP_204_NO_CONTENT)


class AppReleaseListCreateView(AdminWriteView, ListCreateAPIView):
    """Published releases.

    **Query** `platform` · `is_current`

    **Responses** `200` · `201` · `400` · `401` · `403`
    """

    serializer_class = AppReleaseSerializer

    def get_queryset(self):
        queryset = AppRelease.objects.all()

        platform = self.request.query_params.get('platform', '').strip()
        if platform:
            queryset = queryset.filter(platform=platform)

        current = self.request.query_params.get('is_current', '').strip().lower()
        if current in ('true', '1'):
            queryset = queryset.filter(is_current=True)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        release = self.perform_write(serializer)
        return Response(
            self.get_serializer(release).data, status=status.HTTP_201_CREATED
        )


class AppReleaseDetailView(AdminWriteView, RetrieveUpdateDestroyAPIView):
    """One release.

    **Responses** `200` · `204` · `400` · `401` · `403` · `404`
    """

    serializer_class = AppReleaseSerializer
    queryset = AppRelease.objects.all()

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        release = self.get_object()

        serializer = self.get_serializer(release, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        release = self.perform_write(serializer)

        return Response(self.get_serializer(release).data)

    def destroy(self, request, *args, **kwargs):
        release = self.get_object()

        with transaction.atomic():
            release.delete()
        services.invalidate()

        return Response(status=status.HTTP_204_NO_CONTENT)


class AnnouncementListCreateView(AdminWriteView, ListCreateAPIView):
    """Every announcement, live or not.

    **Query** `is_active` · `priority`

    **Responses** `200` · `201` · `400` · `401` · `403`
    """

    serializer_class = AnnouncementSerializer

    def get_queryset(self):
        queryset = Announcement.objects.select_related('created_by')

        active = self.request.query_params.get('is_active', '').strip().lower()
        if active in ('true', '1'):
            queryset = queryset.filter(is_active=True)
        elif active in ('false', '0'):
            queryset = queryset.filter(is_active=False)

        priority = self.request.query_params.get('priority', '').strip()
        if priority:
            queryset = queryset.filter(priority=priority)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        announcement = self.perform_write(serializer, created_by=request.user)
        return Response(
            self.get_serializer(announcement).data, status=status.HTTP_201_CREATED
        )


class AnnouncementDetailView(AdminWriteView, RetrieveUpdateDestroyAPIView):
    """One announcement.

    **Responses** `200` · `204` · `400` · `401` · `403` · `404`
    """

    serializer_class = AnnouncementSerializer
    queryset = Announcement.objects.select_related('created_by')

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        announcement = self.get_object()

        serializer = self.get_serializer(
            announcement, data=request.data, partial=partial
        )
        serializer.is_valid(raise_exception=True)
        announcement = self.perform_write(serializer)

        return Response(self.get_serializer(announcement).data)

    def destroy(self, request, *args, **kwargs):
        announcement = self.get_object()

        with transaction.atomic():
            announcement.delete()
        services.invalidate()

        return Response(status=status.HTTP_204_NO_CONTENT)


class HealthView(PublicView):
    """Is this instance able to serve requests?

    For load balancers, container orchestrators and uptime monitors, which
    need one cheap URL that answers without a token and means something.

    It checks the database, because that is what actually breaks: a Django
    process survives its database going away and keeps returning 200 on any
    endpoint that happens not to touch it, so a health check that only proves
    "the process is up" keeps a dead instance in the load balancer's rotation.

    `503` when the database is unreachable, so the orchestrator takes this
    instance out rather than sending it traffic it cannot serve.

    Deliberately says nothing about *why*: the host, the user, the driver
    error. An unauthenticated endpoint that reports its own connection string
    on failure is a reconnaissance tool.

    **Responses** `200` · `503`
    """

    serializer_class = None

    @extend_schema(
        responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
        description='Liveness and database readiness. No authentication.',
    )
    def get(self, request, *args, **kwargs):
        try:
            connection.ensure_connection()
            database = 'ok'
        except Exception:
            logger.exception('Health check failed: database unreachable')
            database = 'unavailable'

        healthy = database == 'ok'
        return Response(
            {
                'status': 'ok' if healthy else 'degraded',
                'database': database,
            },
            status=(
                status.HTTP_200_OK
                if healthy
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )
