"""Attendance: one punch-in and punch-out per person per day.

Every punch carries where it happened and a selfie, because an attendance
record that cannot be tied to a place and a face is worth very little in the
field. Out-of-fence punches are recorded and flagged rather than refused by
default — a rejected punch just becomes an argument with a supervisor, while a
flagged one can be reviewed.
"""

import math
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone

from accounts.models import TimeStampedUUIDModel

EARTH_RADIUS_METERS = 6371000


def haversine_metres(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in metres."""
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def selfie_path(instance, filename):
    """Selfies land under the month they belong to, keyed by the record."""
    suffix = Path(filename).suffix.lower() or '.jpg'
    return f'attendance/{instance.day:%Y/%m}/{instance.pk}{suffix}'


class GeoFence(TimeStampedUUIDModel):
    """A circle a punch is expected to fall inside.

    The radius belongs to the fence rather than to a global setting: a head
    office lobby and a warehouse yard do not deserve the same tolerance.
    """

    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20, unique=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    radius_meters = models.PositiveIntegerField(default=300)
    territory = models.ForeignKey(
        'accounts.Territory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='geofences',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        indexes = [models.Index(fields=['is_active'])]

    def __str__(self):
        return f'{self.name} ({self.radius_meters} m)'

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        self.name = self.name.strip()
        super().save(*args, **kwargs)

    def distance_to(self, latitude, longitude):
        return haversine_metres(self.latitude, self.longitude, latitude, longitude)

    def contains(self, latitude, longitude):
        return self.distance_to(latitude, longitude) <= self.radius_meters

    @classmethod
    def nearest(cls, latitude, longitude):
        """The closest active fence and the distance to it.

        Returns `(None, None)` when no fence is configured at all — a system
        with nothing to compare against must not reject every punch.
        """
        fences = list(cls.objects.filter(is_active=True))
        if not fences:
            return None, None
        closest = min(fences, key=lambda fence: fence.distance_to(latitude, longitude))
        return closest, closest.distance_to(latitude, longitude)


class Attendance(TimeStampedUUIDModel):
    """One person's working day."""

    class Source(models.TextChoices):
        MOBILE = 'mobile', 'Mobile app'
        WEB = 'web', 'Web'
        ADMIN = 'admin', 'Entered by an administrator'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='attendance'
    )

    # Derived from punch_in_at in the project timezone and stored, so "has this
    # person already punched in today" is a unique index rather than a range
    # scan over timestamps.
    day = models.DateField()

    punch_in_at = models.DateTimeField()
    punch_in_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    punch_in_longitude = models.DecimalField(max_digits=9, decimal_places=6)
    punch_in_accuracy_meters = models.FloatField(null=True, blank=True)
    punch_in_address = models.CharField(max_length=255, blank=True, default='')
    punch_in_selfie = models.ImageField(upload_to=selfie_path, null=True, blank=True)
    punch_in_geofence = models.ForeignKey(
        GeoFence,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='punch_ins',
    )
    punch_in_distance_meters = models.FloatField(null=True, blank=True)
    punch_in_within_fence = models.BooleanField(null=True, blank=True)

    punch_out_at = models.DateTimeField(null=True, blank=True)
    punch_out_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    punch_out_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    punch_out_accuracy_meters = models.FloatField(null=True, blank=True)
    punch_out_address = models.CharField(max_length=255, blank=True, default='')
    punch_out_selfie = models.ImageField(upload_to=selfie_path, null=True, blank=True)
    punch_out_geofence = models.ForeignKey(
        GeoFence,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='punch_outs',
    )
    punch_out_distance_meters = models.FloatField(null=True, blank=True)
    punch_out_within_fence = models.BooleanField(null=True, blank=True)

    is_late = models.BooleanField(default=False)
    worked_minutes = models.PositiveIntegerField(null=True, blank=True)
    note = models.TextField(blank=True, default='')
    source = models.CharField(max_length=10, choices=Source, default=Source.MOBILE)

    # Idempotency keys generated on the device. A phone that punches in with no
    # signal retries when it reconnects, sometimes more than once; replaying
    # the same id returns the original record instead of creating a second.
    sync_id = models.UUIDField(default=uuid.uuid7, editable=False)
    punch_out_sync_id = models.UUIDField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ['-punch_in_at']
        verbose_name_plural = 'attendance'
        constraints = [
            # The duplicate check-in guard, enforced by the database rather
            # than by a test in the view that a race can slip past.
            models.UniqueConstraint(
                fields=['user', 'day'], name='one_attendance_per_user_per_day'
            ),
            models.UniqueConstraint(
                fields=['user', 'sync_id'], name='one_record_per_sync_id'
            ),
            models.CheckConstraint(
                condition=models.Q(punch_out_at__isnull=True)
                | models.Q(punch_out_at__gte=models.F('punch_in_at')),
                name='punch_out_after_punch_in',
            ),
        ]
        indexes = [
            models.Index(fields=['user', '-day']),
            models.Index(fields=['day', 'is_late']),
        ]

    def __str__(self):
        return f'{self.user.employee_code} {self.day}'

    @property
    def is_open(self):
        return self.punch_out_at is None

    def worked_duration_minutes(self):
        end = self.punch_out_at or timezone.now()
        return max(0, int((end - self.punch_in_at).total_seconds() // 60))

    def close(self, at, **punch_out_fields):
        """Apply a punch-out to this record, without saving it."""
        self.punch_out_at = at
        for field, value in punch_out_fields.items():
            setattr(self, field, value)
        self.worked_minutes = self.worked_duration_minutes()
