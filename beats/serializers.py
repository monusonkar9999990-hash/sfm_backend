"""Serializers for the beat endpoints.

Payload keys follow the Flutter client's `BeatModel` and `BeatPlanModel`:
`assigned_user_id`, `weekdays`, `outlets[{customer_id, ...}]`,
`planned_outlet_count`, `covered_customer_ids`.
"""

from django.utils import timezone
from rest_framework import serializers

from .models import (
    Beat,
    BeatOutlet,
    BeatPlan,
    BeatPlanStatus,
    BeatPlanVisit,
    VisitStatus,
)


class BeatOutletSerializer(serializers.ModelSerializer):
    """A stop on the route. `customer_id` is the key the client reads."""

    customer_id = serializers.CharField(source='customer_ref', read_only=True)

    class Meta:
        model = BeatOutlet
        fields = ('customer_id', 'customer_name', 'address', 'phone', 'sequence')
        read_only_fields = fields


class BeatSerializer(serializers.ModelSerializer):
    """A standing route, with its stops in call order."""

    assigned_user_id = serializers.CharField(source='assigned_user.id', read_only=True)
    outlets = BeatOutletSerializer(many=True, read_only=True)
    outlet_count = serializers.IntegerField(read_only=True)
    schedule_label = serializers.CharField(read_only=True)

    class Meta:
        model = Beat
        fields = (
            'id',
            'name',
            'code',
            'area',
            'city',
            'assigned_user_id',
            'frequency',
            'weekdays',
            'outlets',
            'outlet_count',
            'schedule_label',
            'is_active',
            'created_at',
        )
        read_only_fields = fields


class BeatPlanVisitSerializer(serializers.ModelSerializer):
    customer_id = serializers.CharField(source='customer_ref', read_only=True)

    class Meta:
        model = BeatPlanVisit
        fields = (
            'id',
            'customer_id',
            'customer_name',
            'sequence',
            'status',
            'visited_at',
            'skip_reason',
        )
        read_only_fields = fields


class BeatPlanSerializer(serializers.ModelSerializer):
    """One day's run.

    `covered_customer_ids` is derived from the visit rows rather than stored,
    so the flat list the client expects can never drift from the per-stop
    detail the backend keeps.
    """

    beat_id = serializers.CharField(source='beat.id', read_only=True)
    beat_name = serializers.CharField(source='beat.name', read_only=True)
    beat_code = serializers.CharField(source='beat.code', read_only=True)
    user_id = serializers.CharField(source='user.id', read_only=True)
    covered_customer_ids = serializers.SerializerMethodField()
    visits = BeatPlanVisitSerializer(many=True, read_only=True)
    covered_count = serializers.IntegerField(read_only=True)
    skipped_count = serializers.IntegerField(read_only=True)
    coverage = serializers.FloatField(read_only=True)
    is_fully_covered = serializers.BooleanField(read_only=True)

    class Meta:
        model = BeatPlan
        fields = (
            'id',
            'beat_id',
            'beat_name',
            'beat_code',
            'user_id',
            'date',
            'status',
            'planned_outlet_count',
            'covered_customer_ids',
            'covered_count',
            'skipped_count',
            'coverage',
            'is_fully_covered',
            'visits',
            'remarks',
            'started_at',
            'closed_at',
            'sync_id',
            'created_at',
        )
        read_only_fields = fields

    def get_covered_customer_ids(self, obj) -> list:
        return [
            visit.customer_ref
            for visit in obj.visits.all()
            if visit.status == VisitStatus.VISITED
        ]


class BeatPlanCreateSerializer(serializers.Serializer):
    """Schedules a beat onto a day."""

    beat = serializers.PrimaryKeyRelatedField(queryset=Beat.objects.all())
    date = serializers.DateField()
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    sync_id = serializers.UUIDField(required=False)

    def validate_beat(self, value):
        if not value.is_active:
            raise serializers.ValidationError(
                'This beat is inactive and cannot be planned.'
            )
        if value.outlets.count() == 0:
            raise serializers.ValidationError(
                'This beat has no outlets yet, so there is nothing to run.'
            )

        user = self.context['request'].user
        # An executive plans their own beats. A supervisor scheduling someone
        # else's day is a different endpoint with a different permission.
        if value.assigned_user_id not in (None, user.id):
            raise serializers.ValidationError('This beat is assigned to someone else.')
        return value

    def validate_date(self, value):
        if value < timezone.localdate():
            raise serializers.ValidationError(
                'Beats cannot be planned for a date in the past.'
            )
        return value

    def validate(self, attrs):
        beat, date = attrs['beat'], attrs['date']
        # A warning rather than a refusal: a beat legitimately gets run off its
        # usual day to catch up, and the API should not fight that.
        attrs['_off_schedule'] = not beat.runs_on(date)
        return attrs


class SkipVisitSerializer(serializers.Serializer):
    """A skip has to say why — an unexplained gap in coverage is useless."""

    reason = serializers.CharField(max_length=255)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('Give a reason for skipping this outlet.')
        return value.strip()


class EmptySerializer(serializers.Serializer):
    """For actions that take no body, so the schema has something to show."""


class CompleteBeatSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


class MarkVisitedSerializer(serializers.Serializer):
    """Optional stamp for a visit recorded offline."""

    visited_at = serializers.DateTimeField(required=False)

    def validate_visited_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError(
                'This visit is in the future. Check the device clock.'
            )
        return value
