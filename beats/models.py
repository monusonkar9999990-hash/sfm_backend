"""Beats: named routes of outlets, and the days they are run.

A `Beat` is the standing route. A `BeatPlan` is one day's run of it, and it
carries its own copy of the stops as `BeatPlanVisit` rows. The copy is the
point: an outlet added to the route next month must not silently appear in
last month's coverage figures.
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from accounts.models import TimeStampedUUIDModel

WEEKDAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def validate_weekdays(value):
    """`[1, 4]` — ISO weekdays, the same numbering `DateTime.weekday` uses."""
    if not isinstance(value, list) or not value:
        raise ValidationError('Pick at least one day for the beat to run.')
    if any(not isinstance(day, int) or not 1 <= day <= 7 for day in value):
        raise ValidationError('Weekdays must be numbers from 1 (Mon) to 7 (Sun).')
    if len(set(value)) != len(value):
        raise ValidationError('A weekday is listed twice.')


class BeatFrequency(models.TextChoices):
    WEEKLY = 'weekly', 'Weekly'
    FORTNIGHTLY = 'fortnightly', 'Fortnightly'
    MONTHLY = 'monthly', 'Monthly'


class BeatPlanStatus(models.TextChoices):
    PLANNED = 'planned', 'Planned'
    IN_PROGRESS = 'in_progress', 'In progress'
    COMPLETED = 'completed', 'Completed'
    MISSED = 'missed', 'Missed'


class VisitStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    VISITED = 'visited', 'Visited'
    SKIPPED = 'skipped', 'Skipped'


class Beat(TimeStampedUUIDModel):
    """A route one executive covers on set weekdays."""

    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20, unique=True)
    area = models.CharField(max_length=120)
    city = models.CharField(max_length=80)
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='beats',
    )
    territory = models.ForeignKey(
        'accounts.Territory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='beats',
    )
    frequency = models.CharField(
        max_length=12, choices=BeatFrequency, default=BeatFrequency.WEEKLY
    )
    # ISO weekday numbers. A JSON column rather than a join table: the list is
    # tiny, always read whole, and never queried by element.
    weekdays = models.JSONField(default=list, validators=[validate_weekdays])
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        indexes = [models.Index(fields=['assigned_user', 'is_active'])]

    def __str__(self):
        return f'{self.code} {self.name}'

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        self.name = self.name.strip()
        super().save(*args, **kwargs)

    @property
    def outlet_count(self):
        return self.outlets.count()

    @property
    def schedule_label(self):
        """`Mon, Thu` — run days in week order."""
        if not self.weekdays:
            return 'Unscheduled'
        return ', '.join(WEEKDAY_NAMES[day - 1] for day in sorted(self.weekdays))

    def runs_on(self, date):
        return date.isoweekday() in (self.weekdays or [])


class BeatOutlet(TimeStampedUUIDModel):
    """One stop on a route, in the order it should be called on.

    The customer's details are held here rather than behind a foreign key
    because the customer module does not exist yet. `customer_ref` is the
    external key it will become an FK on; the payload already calls it
    `customer_id`, so that change will not move the client.
    """

    beat = models.ForeignKey(Beat, on_delete=models.CASCADE, related_name='outlets')
    customer_ref = models.CharField(max_length=64)
    customer_name = models.CharField(max_length=150)
    address = models.CharField(max_length=255, blank=True, default='')
    phone = models.CharField(max_length=16, blank=True, default='')
    sequence = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ['sequence']
        constraints = [
            models.UniqueConstraint(
                fields=['beat', 'customer_ref'], name='one_stop_per_customer_per_beat'
            )
        ]

    def __str__(self):
        return f'{self.sequence}. {self.customer_name}'


class BeatPlan(TimeStampedUUIDModel):
    """One day's run of a beat."""

    beat = models.ForeignKey(Beat, on_delete=models.PROTECT, related_name='plans')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='beat_plans'
    )
    date = models.DateField()
    status = models.CharField(
        max_length=12, choices=BeatPlanStatus, default=BeatPlanStatus.PLANNED
    )
    # Frozen when the plan is made, so a route edited later cannot rewrite
    # yesterday's denominator.
    planned_outlet_count = models.PositiveSmallIntegerField(default=0)
    remarks = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # Device-generated, so a plan created offline and retried does not become
    # two plans. Same contract as the attendance module.
    sync_id = models.UUIDField(default=uuid.uuid7, editable=False)

    class Meta:
        ordering = ['date', 'beat__name']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'beat', 'date'], name='one_plan_per_beat_per_day'
            ),
            models.UniqueConstraint(
                fields=['user', 'sync_id'], name='one_plan_per_sync_id'
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'date']),
            models.Index(fields=['date', 'status']),
        ]

    def __str__(self):
        return f'{self.beat.code} on {self.date}'

    @property
    def is_open(self):
        return self.status in {BeatPlanStatus.PLANNED, BeatPlanStatus.IN_PROGRESS}

    @property
    def covered_count(self):
        return self.visits.filter(status=VisitStatus.VISITED).count()

    @property
    def skipped_count(self):
        return self.visits.filter(status=VisitStatus.SKIPPED).count()

    @property
    def coverage(self):
        """0..1 share of planned outlets actually visited."""
        if not self.planned_outlet_count:
            return 0.0
        return self.covered_count / self.planned_outlet_count

    @property
    def is_fully_covered(self):
        return (
            self.planned_outlet_count > 0
            and self.covered_count >= self.planned_outlet_count
        )

    def snapshot_outlets(self):
        """Copy the route's stops onto this plan.

        Called once, when the plan is created. From then on the plan owns its
        own list and the route is free to change.
        """
        outlets = list(self.beat.outlets.all())
        BeatPlanVisit.objects.bulk_create(
            [
                BeatPlanVisit(
                    plan=self,
                    outlet=outlet,
                    customer_ref=outlet.customer_ref,
                    customer_name=outlet.customer_name,
                    sequence=outlet.sequence,
                )
                for outlet in outlets
            ]
        )
        self.planned_outlet_count = len(outlets)
        return outlets


class BeatPlanVisit(TimeStampedUUIDModel):
    """One stop on one day's run, and what became of it."""

    plan = models.ForeignKey(BeatPlan, on_delete=models.CASCADE, related_name='visits')
    # SET_NULL rather than CASCADE: dropping a stop from the route must not
    # erase the fact that it was visited last Tuesday.
    outlet = models.ForeignKey(
        BeatOutlet,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='plan_visits',
    )
    customer_ref = models.CharField(max_length=64)
    customer_name = models.CharField(max_length=150)
    sequence = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(
        max_length=10, choices=VisitStatus, default=VisitStatus.PENDING
    )
    visited_at = models.DateTimeField(null=True, blank=True)
    skip_reason = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['sequence']
        constraints = [
            models.UniqueConstraint(
                fields=['plan', 'customer_ref'], name='one_visit_per_customer_per_plan'
            )
        ]
        indexes = [models.Index(fields=['plan', 'status'])]

    def __str__(self):
        return f'{self.customer_name} ({self.get_status_display()})'

    @property
    def is_pending(self):
        return self.status == VisitStatus.PENDING
