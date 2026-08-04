"""Attendance endpoints.

Every write is idempotent on `sync_id`, because the client that calls them is
often offline and will retry. Replaying a punch returns the record that was
already stored, with 200 instead of 201, rather than a duplicate or an error a
phone cannot recover from.
"""

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import GenericAPIView, ListAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Attendance
from .permissions import HasBusinessPermission
from .serializers import AttendanceSerializer, CheckInSerializer, CheckOutSerializer


def local_day(moment):
    """The calendar day a punch belongs to, in the project's timezone."""
    return timezone.localtime(moment).date()


class CheckInView(GenericAPIView):
    """Start the working day.

    Send the GPS fix and a selfie as `multipart/form-data`. The punch is
    stamped with the nearest geofence and flagged if it falls outside it;
    whether that also refuses the punch is a server policy
    (`ATTENDANCE_ENFORCE_GEOFENCE`), not something the client decides.

    Pass `sync_id` — a UUID the device generates — so a retry after a dropped
    connection returns the original record instead of creating a second one.

    **Request** (`multipart/form-data`)
    `latitude`, `longitude`, `accuracy`, `address`, `captured_at`, `note`,
    `sync_id`, `selfie`

    **Responses**
    * `201` — the day's record
    * `200` — this `sync_id` was already recorded; the original is returned
    * `400` — bad GPS fix, missing selfie, stale punch, or outside the fence
    * `401` — missing or invalid access token
    * `403` — the role lacks `mark_attendance`
    * `409` — already checked in today
    """

    serializer_class = CheckInSerializer
    permission_classes = [IsAuthenticated, HasBusinessPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    required_permission = 'mark_attendance'

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        punched_at = data.get('captured_at') or timezone.now()
        day = local_day(punched_at)

        # Idempotent replay: same device, same punch, second delivery.
        sync_id = data.get('sync_id')
        if sync_id:
            existing = Attendance.objects.filter(
                user=request.user, sync_id=sync_id
            ).first()
            if existing:
                return Response(
                    AttendanceSerializer(existing, context={'request': request}).data,
                    status=status.HTTP_200_OK,
                )

        record = Attendance(
            user=request.user,
            day=day,
            punch_in_at=punched_at,
            punch_in_latitude=data['latitude'],
            punch_in_longitude=data['longitude'],
            punch_in_accuracy_meters=data.get('accuracy'),
            punch_in_address=data.get('address', ''),
            punch_in_selfie=data.get('selfie'),
            punch_in_geofence=data['_geofence'],
            punch_in_distance_meters=data['_distance'],
            punch_in_within_fence=data['_within_fence'],
            is_late=timezone.localtime(punched_at).hour >= settings.ATTENDANCE_LATE_HOUR,
            note=data.get('note', ''),
        )
        if sync_id:
            record.sync_id = sync_id

        try:
            with transaction.atomic():
                record.save()
        except IntegrityError:
            # The unique index did the refusing, so two requests racing each
            # other cannot both create today's record.
            return Response(
                {
                    'detail': 'You have already checked in today.',
                    'attendance': AttendanceSerializer(
                        Attendance.objects.get(user=request.user, day=day),
                        context={'request': request},
                    ).data,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            AttendanceSerializer(record, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class CheckOutView(GenericAPIView):
    """Close the working day.

    Applies to the open record. `worked_minutes` is computed on the server
    from the two stamps, so a device with a skewed clock cannot inflate its
    own hours.

    **Responses**
    * `200` — the closed record, or the same record again on a replay
    * `400` — bad GPS fix, missing selfie, or a punch-out before the punch-in
    * `401` — missing or invalid access token
    * `403` — the role lacks `mark_attendance`
    * `409` — no open check-in to close
    """

    serializer_class = CheckOutSerializer
    permission_classes = [IsAuthenticated, HasBusinessPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    required_permission = 'mark_attendance'

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        sync_id = data.get('sync_id')
        if sync_id:
            replayed = Attendance.objects.filter(
                user=request.user, punch_out_sync_id=sync_id
            ).first()
            if replayed:
                return Response(
                    AttendanceSerializer(replayed, context={'request': request}).data,
                    status=status.HTTP_200_OK,
                )

        record = (
            Attendance.objects.filter(user=request.user, punch_out_at__isnull=True)
            .order_by('-punch_in_at')
            .first()
        )
        if record is None:
            return Response(
                {'detail': 'There is no open check-in to close.'},
                status=status.HTTP_409_CONFLICT,
            )

        punched_at = data.get('captured_at') or timezone.now()
        if punched_at < record.punch_in_at:
            return Response(
                {'captured_at': ['Check-out cannot be earlier than check-in.']},
                status=status.HTTP_400_BAD_REQUEST,
            )

        record.close(
            punched_at,
            punch_out_latitude=data['latitude'],
            punch_out_longitude=data['longitude'],
            punch_out_accuracy_meters=data.get('accuracy'),
            punch_out_address=data.get('address', ''),
            punch_out_selfie=data.get('selfie'),
            punch_out_geofence=data['_geofence'],
            punch_out_distance_meters=data['_distance'],
            punch_out_within_fence=data['_within_fence'],
            punch_out_sync_id=sync_id,
        )
        if data.get('note'):
            record.note = data['note']
        record.save()

        return Response(
            AttendanceSerializer(record, context={'request': request}).data,
            status=status.HTTP_200_OK,
        )


class TodayAttendanceView(APIView):
    """Today's record for the signed-in user, or `null` before the first punch.

    The client needs this to decide whether to offer a check-in or a check-out
    button when it launches.

    **Responses**
    * `200` — `{"attendance": {...}}` or `{"attendance": null}`
    * `401` — missing or invalid access token
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        record = Attendance.objects.filter(
            user=request.user, day=local_day(timezone.now())
        ).first()
        return Response(
            {
                'attendance': (
                    AttendanceSerializer(record, context={'request': request}).data
                    if record
                    else None
                )
            },
            status=status.HTTP_200_OK,
        )


class AttendanceHistoryView(ListAPIView):
    """The signed-in user's own attendance, newest first.

    Scoped to the caller by the queryset itself rather than by a filter the
    client sends, so there is no parameter to tamper with.

    **Responses**
    * `200` — a paginated list
    * `401` — missing or invalid access token
    """

    serializer_class = AttendanceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Attendance.objects.filter(user=self.request.user).select_related('user')
