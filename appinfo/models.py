"""The things the app reads before anybody signs in.

Legal documents, release information and announcements. Application settings
are deliberately *not* here — those live in `administration.AppSetting`, which
already has a store, an admin API and an audit trail. `/app-config/` is a
public projection of that store, not a second copy of it.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from accounts.models import TimeStampedUUIDModel


def parse_version(value):
    """`1.2.10` -> `(1, 2, 10)`, for comparing versions as numbers.

    String comparison gets this wrong in the one case that matters: `'1.10.0'
    < '1.9.0'` is true alphabetically and false in every other sense. Anything
    unparseable sorts lowest, so a malformed client version is treated as old
    rather than as current — the safe direction.
    """
    parts = []
    for chunk in str(value or '').split('.'):
        digits = ''.join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


class DocumentKind(models.TextChoices):
    PRIVACY = 'privacy', 'Privacy policy'
    TERMS = 'terms', 'Terms and conditions'


class LegalDocument(TimeStampedUUIDModel):
    """One version of one legal document.

    Versions are kept rather than overwritten. Somebody accepted a particular
    wording on a particular day, and the text they agreed to has to still
    exist — replacing the row would quietly rewrite what every existing user
    consented to.

    The public endpoint serves the newest published version whose effective
    date has arrived, so a policy can be written and dated ahead of time.
    """

    kind = models.CharField(max_length=10, choices=DocumentKind)
    title = models.CharField(max_length=150)
    version = models.CharField(max_length=20)
    effective_date = models.DateField()
    content = models.TextField()

    is_published = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='legal_documents',
    )

    class Meta:
        ordering = ['-effective_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['kind', 'version'], name='one_version_per_document_kind'
            ),
        ]
        indexes = [
            models.Index(fields=['kind', 'is_published', '-effective_date']),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} v{self.version}'

    @classmethod
    def current(cls, kind, *, on=None):
        """The version in force, or None if nothing has been published yet."""
        return (
            cls.objects.filter(
                kind=kind,
                is_published=True,
                effective_date__lte=on or timezone.localdate(),
            )
            .order_by('-effective_date', '-created_at')
            .first()
        )


class Platform(models.TextChoices):
    ANDROID = 'android', 'Android'
    IOS = 'ios', 'iOS'
    WEB = 'web', 'Web'


class AppRelease(TimeStampedUUIDModel):
    """What the client should be running.

    `force_update` is stored as the publisher's intent, and the endpoint also
    computes a verdict when the caller says which version it is on. Those are
    different questions: the flag says "this release is mandatory", the
    verdict says "*you* must update", and only the second one can answer a
    client that is already newer than the minimum.
    """

    platform = models.CharField(
        max_length=10, choices=Platform, default=Platform.ANDROID
    )

    version = models.CharField(max_length=20)
    minimum_supported_version = models.CharField(max_length=20)

    force_update = models.BooleanField(default=False)
    download_url = models.URLField(blank=True, default='')
    release_notes = models.TextField(blank=True, default='')

    # Exactly one per platform is served. Enforced in `save`, since MySQL has
    # no partial unique index to express "one row where is_current".
    is_current = models.BooleanField(default=True)

    class Meta:
        ordering = ['platform', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['platform', 'version'], name='one_release_per_version'
            ),
        ]
        indexes = [models.Index(fields=['platform', 'is_current'])]

    def __str__(self):
        return f'{self.get_platform_display()} {self.version}'

    def save(self, *args, **kwargs):
        self.version = self.version.strip()
        self.minimum_supported_version = self.minimum_supported_version.strip()
        super().save(*args, **kwargs)

        if self.is_current:
            # Only one current release per platform. Done after the write so
            # this row is the survivor.
            AppRelease.objects.filter(
                platform=self.platform, is_current=True
            ).exclude(pk=self.pk).update(is_current=False)

    @classmethod
    def current(cls, platform=Platform.ANDROID):
        return cls.objects.filter(platform=platform, is_current=True).first()

    def verdict_for(self, client_version):
        """`up_to_date`, `update_available` or `update_required`.

        The three states the client has to tell apart, decided here rather
        than in the app — the rule for when an update becomes mandatory is a
        server decision, and a client that computed it could not be updated to
        change it.
        """
        if not client_version:
            return None

        client = parse_version(client_version)

        if client < parse_version(self.minimum_supported_version):
            return 'update_required'
        if client < parse_version(self.version):
            return 'update_required' if self.force_update else 'update_available'
        return 'up_to_date'


class Priority(models.TextChoices):
    LOW = 'low', 'Low'
    NORMAL = 'normal', 'Normal'
    HIGH = 'high', 'High'
    CRITICAL = 'critical', 'Critical'


class Announcement(TimeStampedUUIDModel):
    """Something to tell everybody, for a while."""

    title = models.CharField(max_length=150)
    message = models.TextField()
    priority = models.CharField(
        max_length=10, choices=Priority, default=Priority.NORMAL
    )

    start_date = models.DateTimeField(default=timezone.now)
    # Open-ended when null: an announcement that runs until somebody turns it
    # off, which is what most of them are.
    end_date = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='announcements',
    )

    class Meta:
        # Not `-priority`: these are stored as words, and sorting them
        # alphabetically puts `normal` above `critical`. The rank below is
        # applied by `live()`, which is what the API actually serves.
        ordering = ['-start_date']
        indexes = [
            models.Index(fields=['is_active', 'start_date', 'end_date']),
        ]

    # Highest first. Kept next to the choices so adding one is a single edit.
    RANK = {
        Priority.CRITICAL: 0,
        Priority.HIGH: 1,
        Priority.NORMAL: 2,
        Priority.LOW: 3,
    }

    def __str__(self):
        return self.title

    @property
    def is_live(self):
        now = timezone.now()
        if not self.is_active or self.start_date > now:
            return False
        return self.end_date is None or self.end_date >= now

    @classmethod
    def live(cls):
        """Active, started, not yet finished — most urgent first."""
        now = timezone.now()
        return (
            cls.objects.filter(is_active=True, start_date__lte=now)
            .filter(models.Q(end_date__isnull=True) | models.Q(end_date__gte=now))
            .annotate(
                rank=models.Case(
                    *[
                        models.When(priority=value, then=models.Value(rank))
                        for value, rank in cls.RANK.items()
                    ],
                    default=models.Value(99),
                    output_field=models.IntegerField(),
                )
            )
            .order_by('rank', '-start_date')
        )
