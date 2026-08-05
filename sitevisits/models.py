"""Site visits: who called on which project site, when, and what they found.

A visit is opened on arrival and closed on leaving, so the duration is the
difference between two stamps the server wrote — not a number a device
reported. Photos hang off the visit rather than replacing it, because a site
is worth more as a series of pictures over months than as one.
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


def visit_photo_path(instance, filename):
    """Photos land under the visit they belong to."""
    suffix = Path(filename).suffix.lower() or '.jpg'
    return f'site-visits/{instance.visit_id}/{instance.pk}{suffix}'


class SiteStage(models.TextChoices):
    FOUNDATION = 'foundation', 'Foundation'
    STRUCTURE = 'structure', 'Structure'
    BRICKWORK = 'brickwork', 'Brickwork'
    PLASTER = 'plaster', 'Plastering'
    FINISHING = 'finishing', 'Finishing'
    COMPLETED = 'completed', 'Completed'


class VisitPurpose(models.TextChoices):
    NEW_LEAD = 'new_lead', 'New lead'
    FOLLOW_UP = 'follow_up', 'Follow-up'
    ORDER_COLLECTION = 'order_collection', 'Order collection'
    PAYMENT_COLLECTION = 'payment_collection', 'Payment collection'
    TECHNICAL_SUPPORT = 'technical_support', 'Technical support'
    COMPLAINT = 'complaint', 'Complaint'


class VisitStatus(models.TextChoices):
    IN_PROGRESS = 'in_progress', 'In progress'
    COMPLETED = 'completed', 'Completed'
    CANCELLED = 'cancelled', 'Cancelled'


class SiteImageTag(models.TextChoices):
    SITE_FRONT = 'site_front', 'Site front'
    WORK_IN_PROGRESS = 'work_in_progress', 'Work in progress'
    MATERIAL_STACK = 'material_stack', 'Material stack'
    COMPETITOR_PRODUCT = 'competitor_product', 'Competitor product'
    OTHER = 'other', 'Other'


class Site(TimeStampedUUIDModel):
    """A project site a customer is building.

    The customer's details sit here as text for the same reason a beat's
    outlets do: the customer module does not exist yet. `customer_ref` is the
    external key it becomes a foreign key on, and the payload already calls it
    `customer_id`.
    """

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=20, unique=True)
    customer_ref = models.CharField(max_length=64)
    customer_name = models.CharField(max_length=150)

    address = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=80, blank=True, default='')
    pincode = models.CharField(max_length=6, blank=True, default='')

    contact_person = models.CharField(max_length=120, blank=True, default='')
    contact_phone = models.CharField(max_length=16, blank=True, default='')

    stage = models.CharField(
        max_length=12, choices=SiteStage, default=SiteStage.FOUNDATION
    )
    estimated_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    expected_closure = models.DateField(null=True, blank=True)

    # Where the site actually is, which is what a check-in gets measured
    # against. Optional, because plenty of sites are added from an office
    # before anyone has stood on them.
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )

    territory = models.ForeignKey(
        'accounts.Territory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sites',
    )
    remarks = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['stage', 'is_active']),
            models.Index(fields=['customer_ref']),
        ]

    def __str__(self):
        return f'{self.code} {self.name}'

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        self.name = self.name.strip()
        super().save(*args, **kwargs)

    @property
    def has_coordinates(self):
        return self.latitude is not None and self.longitude is not None

    def distance_from(self, latitude, longitude):
        """Metres between the site and a fix, or None if nobody plotted it."""
        if not self.has_coordinates:
            return None
        return haversine_metres(self.latitude, self.longitude, latitude, longitude)


class SiteVisit(TimeStampedUUIDModel):
    """One call on one site."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='site_visits'
    )
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name='visits')

    purpose = models.CharField(
        max_length=20, choices=VisitPurpose, default=VisitPurpose.FOLLOW_UP
    )
    status = models.CharField(
        max_length=12, choices=VisitStatus, default=VisitStatus.IN_PROGRESS
    )

    check_in_at = models.DateTimeField()
    check_in_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    check_in_longitude = models.DecimalField(max_digits=9, decimal_places=6)
    check_in_accuracy_meters = models.FloatField(null=True, blank=True)
    check_in_address = models.CharField(max_length=255, blank=True, default='')

    # How far the executive was from where the site is plotted. Recorded and
    # left for a supervisor to read rather than used to refuse the visit — a
    # site's pin is often approximate while the person is standing on it.
    check_in_distance_meters = models.FloatField(null=True, blank=True)

    check_out_at = models.DateTimeField(null=True, blank=True)
    check_out_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    check_out_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    check_out_accuracy_meters = models.FloatField(null=True, blank=True)
    check_out_address = models.CharField(max_length=255, blank=True, default='')

    # What the visit found.
    stage_observed = models.CharField(
        max_length=12, choices=SiteStage, blank=True, default=''
    )
    competitor_brands = models.JSONField(default=list, blank=True)
    expected_order_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    follow_up_date = models.DateField(null=True, blank=True)
    remarks = models.TextField(blank=True, default='')

    # Written on check-out from the two server-held stamps, so a device with a
    # skewed clock cannot lengthen its own visit.
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)

    # Device-generated, so a visit opened with no signal and retried does not
    # become two. Same contract as attendance and beat plans.
    sync_id = models.UUIDField(default=uuid.uuid7, editable=False)

    class Meta:
        ordering = ['-check_in_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'sync_id'], name='one_visit_per_sync_id'
            ),
            models.CheckConstraint(
                condition=models.Q(check_out_at__isnull=True)
                | models.Q(check_out_at__gte=models.F('check_in_at')),
                name='visit_check_out_after_check_in',
            ),
        ]
        indexes = [
            models.Index(fields=['user', '-check_in_at']),
            models.Index(fields=['status']),
            models.Index(fields=['follow_up_date']),
        ]

    def __str__(self):
        return f'{self.site.name} on {self.check_in_at:%Y-%m-%d}'

    @property
    def is_open(self):
        return self.status == VisitStatus.IN_PROGRESS

    def worked_minutes(self):
        end = self.check_out_at or timezone.now()
        return max(0, int((end - self.check_in_at).total_seconds() // 60))

    def close(self, at, **fields):
        """Applies a check-out to this visit, without saving it."""
        self.check_out_at = at
        self.status = VisitStatus.COMPLETED
        for field, value in fields.items():
            setattr(self, field, value)
        self.duration_minutes = self.worked_minutes()


class SiteVisitImage(TimeStampedUUIDModel):
    """A photo taken during a visit, stamped with where it was taken."""

    visit = models.ForeignKey(
        SiteVisit, on_delete=models.CASCADE, related_name='images'
    )
    image = models.ImageField(upload_to=visit_photo_path)
    tag = models.CharField(
        max_length=20, choices=SiteImageTag, default=SiteImageTag.OTHER
    )
    caption = models.CharField(max_length=255, blank=True, default='')

    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['captured_at']
        indexes = [models.Index(fields=['visit', 'tag'])]

    def __str__(self):
        return f'{self.get_tag_display()} on {self.visit_id}'
