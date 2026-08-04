"""Serializers for the attendance endpoints.

The read serializer speaks the shape the mobile client already parses:
`punch_in_at`, `punch_in_location {lat, lng, address, accuracy}`,
`punch_in_selfie`. Writes take flat fields, because a multipart upload cannot
carry a nested JSON object alongside a file.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from .models import Attendance, GeoFence


class GeoStampSerializer(serializers.Serializer):
    """`{lat, lng, address, accuracy}` — the client's own shape."""

    lat = serializers.DecimalField(max_digits=9, decimal_places=6)
    lng = serializers.DecimalField(max_digits=9, decimal_places=6)
    address = serializers.CharField(allow_blank=True, required=False)
    accuracy = serializers.FloatField(required=False, allow_null=True)


class AttendanceSerializer(serializers.ModelSerializer):
    """A day's attendance, read-only."""

    user_id = serializers.CharField(source='user.id', read_only=True)
    employee_code = serializers.CharField(source='user.employee_code', read_only=True)
    punch_in_location = serializers.SerializerMethodField()
    punch_out_location = serializers.SerializerMethodField()
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = Attendance
        fields = (
            'id',
            'user_id',
            'employee_code',
            'day',
            'punch_in_at',
            'punch_out_at',
            'punch_in_location',
            'punch_out_location',
            'punch_in_selfie',
            'punch_out_selfie',
            'punch_in_within_fence',
            'punch_out_within_fence',
            'punch_in_distance_meters',
            'punch_out_distance_meters',
            'is_late',
            'is_open',
            'worked_minutes',
            'note',
            'source',
            'sync_id',
            'created_at',
            'updated_at',
        )
        read_only_fields = fields

    def _location(self, latitude, longitude, address, accuracy):
        if latitude is None or longitude is None:
            return None
        return {
            'lat': float(latitude),
            'lng': float(longitude),
            'address': address or None,
            'accuracy': accuracy,
        }

    def get_punch_in_location(self, obj) -> dict:
        return self._location(
            obj.punch_in_latitude,
            obj.punch_in_longitude,
            obj.punch_in_address,
            obj.punch_in_accuracy_meters,
        )

    def get_punch_out_location(self, obj) -> dict:
        return self._location(
            obj.punch_out_latitude,
            obj.punch_out_longitude,
            obj.punch_out_address,
            obj.punch_out_accuracy_meters,
        )


class PunchSerializer(serializers.Serializer):
    """Fields shared by check-in and check-out, and the GPS rules over them."""

    latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    accuracy = serializers.FloatField(required=False, allow_null=True)
    address = serializers.CharField(
        required=False, allow_blank=True, max_length=255, default=''
    )
    # The moment the punch happened on the device, which is not the moment it
    # reached the server when it was captured offline.
    captured_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, default='')
    sync_id = serializers.UUIDField(required=False)
    selfie = serializers.ImageField(required=False, allow_null=True)

    def validate_latitude(self, value):
        if not -90 <= value <= 90:
            raise serializers.ValidationError('Latitude must be between -90 and 90.')
        return value

    def validate_longitude(self, value):
        if not -180 <= value <= 180:
            raise serializers.ValidationError('Longitude must be between -180 and 180.')
        return value

    def validate_accuracy(self, value):
        if value is None:
            return value
        if value < 0:
            raise serializers.ValidationError('Accuracy cannot be negative.')
        limit = settings.ATTENDANCE_MAX_ACCURACY_METERS
        if value > limit:
            raise serializers.ValidationError(
                f'The location is only accurate to {value:.0f} m. '
                f'Move into the open and try again (limit {limit} m).'
            )
        return value

    def validate_captured_at(self, value):
        now = timezone.now()
        skew = timedelta(minutes=settings.ATTENDANCE_CLOCK_SKEW_MINUTES)
        if value > now + skew:
            raise serializers.ValidationError(
                'This punch is in the future. Check the device clock.'
            )
        if value < now - timedelta(days=settings.ATTENDANCE_MAX_BACKDATE_DAYS):
            raise serializers.ValidationError(
                f'A punch older than {settings.ATTENDANCE_MAX_BACKDATE_DAYS} days '
                'can no longer be synced. Ask your manager to enter it.'
            )
        return value

    def validate(self, attrs):
        latitude, longitude = attrs['latitude'], attrs['longitude']

        # Exactly (0, 0) is in the Gulf of Guinea. In practice it means the
        # device returned an empty fix, not that anyone is standing there.
        if latitude == 0 and longitude == 0:
            raise serializers.ValidationError(
                {'latitude': 'No usable GPS fix was received. Try again outdoors.'}
            )

        if settings.ATTENDANCE_SELFIE_REQUIRED and not attrs.get('selfie'):
            raise serializers.ValidationError(
                {'selfie': 'A selfie is required to record attendance.'}
            )

        fence, distance = GeoFence.nearest(latitude, longitude)
        within = None if fence is None else distance <= fence.radius_meters

        if settings.ATTENDANCE_ENFORCE_GEOFENCE and within is False:
            raise serializers.ValidationError(
                {
                    'latitude': (
                        f'You are {distance:.0f} m from {fence.name}, which allows '
                        f'{fence.radius_meters} m. Move closer and try again.'
                    )
                }
            )

        # Handed to the view rather than re-computed there.
        attrs['_geofence'] = fence
        attrs['_distance'] = distance
        attrs['_within_fence'] = within
        return attrs


class CheckInSerializer(PunchSerializer):
    """Starts the working day."""


class CheckOutSerializer(PunchSerializer):
    """Closes the working day."""
