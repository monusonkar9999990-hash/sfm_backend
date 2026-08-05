"""Serializers for the site visit endpoints.

The read payloads follow the Flutter client's own models — `check_in_at`,
`check_in_location {lat, lng, address, accuracy}`, `images[{path, tag, ...}]`
— so nothing has to be translated on the way in.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Site,
    SiteImageTag,
    SiteStage,
    SiteVisit,
    SiteVisitImage,
    VisitPurpose,
)

# The same limits attendance uses. Repeated rather than imported: that module
# is finished and closed, and a shared constants file is a refactor for the
# day a third module needs them.
MAX_ACCURACY_METERS = getattr(settings, 'ATTENDANCE_MAX_ACCURACY_METERS', 100)
CLOCK_SKEW_MINUTES = getattr(settings, 'ATTENDANCE_CLOCK_SKEW_MINUTES', 5)
MAX_BACKDATE_DAYS = getattr(settings, 'ATTENDANCE_MAX_BACKDATE_DAYS', 7)


class SiteSerializer(serializers.ModelSerializer):
    """A project site, in the shape `SiteModel.fromJson` reads."""

    customer_id = serializers.CharField(source='customer_ref', read_only=True)
    location = serializers.SerializerMethodField()

    class Meta:
        model = Site
        fields = (
            'id',
            'name',
            'code',
            'customer_id',
            'customer_name',
            'address',
            'city',
            'pincode',
            'contact_person',
            'contact_phone',
            'stage',
            'estimated_value',
            'expected_closure',
            'location',
            'remarks',
            'is_active',
            'created_at',
        )
        read_only_fields = fields

    def get_location(self, obj) -> dict:
        if not obj.has_coordinates:
            return None
        return {
            'lat': float(obj.latitude),
            'lng': float(obj.longitude),
            'address': obj.address or None,
            'accuracy': None,
        }


class SiteVisitImageSerializer(serializers.ModelSerializer):
    """One photo. `path` is the key the client reads — on the device it was a
    local file path, and over the wire it is the URL that replaced it."""

    path = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()

    class Meta:
        model = SiteVisitImage
        fields = ('id', 'path', 'tag', 'caption', 'location', 'captured_at')
        read_only_fields = fields

    def get_path(self, obj) -> str:
        request = self.context.get('request')
        url = obj.image.url if obj.image else ''
        return request.build_absolute_uri(url) if request and url else url

    def get_location(self, obj) -> dict:
        if obj.latitude is None or obj.longitude is None:
            return None
        return {
            'lat': float(obj.latitude),
            'lng': float(obj.longitude),
            'address': None,
            'accuracy': None,
        }


class SiteVisitSerializer(serializers.ModelSerializer):
    """A visit, read-only."""

    user_id = serializers.CharField(source='user.id', read_only=True)
    site_id = serializers.CharField(source='site.id', read_only=True)
    site_name = serializers.CharField(source='site.name', read_only=True)
    customer_name = serializers.CharField(
        source='site.customer_name', read_only=True
    )
    check_in_location = serializers.SerializerMethodField()
    check_out_location = serializers.SerializerMethodField()
    images = SiteVisitImageSerializer(many=True, read_only=True)
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = SiteVisit
        fields = (
            'id',
            'user_id',
            'site_id',
            'site_name',
            'customer_name',
            'purpose',
            'status',
            'is_open',
            'check_in_at',
            'check_out_at',
            'check_in_location',
            'check_out_location',
            'check_in_distance_meters',
            'stage_observed',
            'competitor_brands',
            'expected_order_value',
            'follow_up_date',
            'remarks',
            'duration_minutes',
            'images',
            'sync_id',
            'created_at',
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

    def get_check_in_location(self, obj) -> dict:
        return self._location(
            obj.check_in_latitude,
            obj.check_in_longitude,
            obj.check_in_address,
            obj.check_in_accuracy_meters,
        )

    def get_check_out_location(self, obj) -> dict:
        return self._location(
            obj.check_out_latitude,
            obj.check_out_longitude,
            obj.check_out_address,
            obj.check_out_accuracy_meters,
        )


class GpsFieldsSerializer(serializers.Serializer):
    """The GPS rules, shared by check-in, check-out and a photo."""

    latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    accuracy = serializers.FloatField(required=False, allow_null=True)
    address = serializers.CharField(
        required=False, allow_blank=True, max_length=255, default=''
    )
    captured_at = serializers.DateTimeField(required=False)

    def validate_latitude(self, value):
        if not -90 <= value <= 90:
            raise serializers.ValidationError('Latitude must be between -90 and 90.')
        return value

    def validate_longitude(self, value):
        if not -180 <= value <= 180:
            raise serializers.ValidationError(
                'Longitude must be between -180 and 180.'
            )
        return value

    def validate_accuracy(self, value):
        if value is None:
            return value
        if value < 0:
            raise serializers.ValidationError('Accuracy cannot be negative.')
        if value > MAX_ACCURACY_METERS:
            raise serializers.ValidationError(
                f'The location is only accurate to {value:.0f} m. Move into the '
                f'open and try again (limit {MAX_ACCURACY_METERS} m).'
            )
        return value

    def validate_captured_at(self, value):
        now = timezone.now()
        if value > now + timedelta(minutes=CLOCK_SKEW_MINUTES):
            raise serializers.ValidationError(
                'That moment is in the future. Check the device clock.'
            )
        if value < now - timedelta(days=MAX_BACKDATE_DAYS):
            raise serializers.ValidationError(
                f'A visit older than {MAX_BACKDATE_DAYS} days can no longer be '
                'synced. Ask your manager to enter it.'
            )
        return value

    def validate(self, attrs):
        # Exactly (0, 0) is in the Gulf of Guinea. In practice it means the
        # device handed back an empty fix.
        if attrs['latitude'] == 0 and attrs['longitude'] == 0:
            raise serializers.ValidationError(
                {'latitude': 'No usable GPS fix was received. Try again outdoors.'}
            )
        return attrs


class CheckInSerializer(GpsFieldsSerializer):
    """Opens a visit."""

    site = serializers.PrimaryKeyRelatedField(queryset=Site.objects.all())
    purpose = serializers.ChoiceField(
        choices=VisitPurpose, default=VisitPurpose.FOLLOW_UP
    )
    remarks = serializers.CharField(
        required=False, allow_blank=True, max_length=2000, default=''
    )
    sync_id = serializers.UUIDField(required=False)

    def validate_site(self, value):
        if not value.is_active:
            raise serializers.ValidationError('That site is no longer active.')
        return value


class CheckOutSerializer(GpsFieldsSerializer):
    """Closes a visit, and carries what the visit found."""

    stage_observed = serializers.ChoiceField(
        choices=SiteStage, required=False, allow_blank=True, default=''
    )
    competitor_brands = serializers.ListField(
        child=serializers.CharField(max_length=80),
        required=False,
        default=list,
    )
    expected_order_value = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    follow_up_date = serializers.DateField(required=False, allow_null=True)
    remarks = serializers.CharField(
        required=False, allow_blank=True, max_length=2000, default=''
    )
    sync_id = serializers.UUIDField(required=False)

    def validate_expected_order_value(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError('A value cannot be negative.')
        return value

    def validate_follow_up_date(self, value):
        if value is not None and value < timezone.localdate():
            raise serializers.ValidationError(
                'A follow-up cannot be scheduled for a day that has passed.'
            )
        return value


class AddImageSerializer(serializers.Serializer):
    """A photo taken during the visit."""

    image = serializers.ImageField()
    tag = serializers.ChoiceField(choices=SiteImageTag, default=SiteImageTag.OTHER)
    caption = serializers.CharField(
        required=False, allow_blank=True, max_length=255, default=''
    )
    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True
    )
    longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True
    )
    captured_at = serializers.DateTimeField(required=False)


class CancelVisitSerializer(serializers.Serializer):
    """A cancelled visit has to say why, or the gap tells nobody anything."""

    reason = serializers.CharField(max_length=255)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('Give a reason for cancelling.')
        return value.strip()
