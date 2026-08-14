"""The sync ledger.

This module stores no business data. Every record a device uploads ends up in
the table the owning module already had — an offline punch becomes an
`attendance.Attendance` row, an offline order becomes an `orders.Order`. What
lives here is the account of what was uploaded, what happened to it, and how
long it took.

That separation is deliberate. A sync system that keeps its own copy of the
data has two sources of truth and a reconciliation problem; one that keeps
only a ledger has neither.
"""

import uuid

from django.conf import settings
from django.db import models

from accounts.models import TimeStampedUUIDModel


class BatchStatus(models.TextChoices):
    PROCESSING = 'processing', 'Processing'
    COMPLETED = 'completed', 'Completed'
    # Every record in the batch failed. The batch itself still succeeded as a
    # request — the device does not need to retry the transport, it needs to
    # fix the payloads.
    FAILED = 'failed', 'Failed'


class RecordStatus(models.TextChoices):
    APPLIED = 'applied', 'Applied'
    # The record was already on the server: same `sync_id`, second delivery.
    # Not an error — the expected outcome of a retry after a dropped reply.
    DUPLICATE = 'duplicate', 'Duplicate'
    # The client edited a record someone else had already changed.
    CONFLICT = 'conflict', 'Conflict'
    FAILED = 'failed', 'Failed'


class Operation(models.TextChoices):
    CREATE = 'create', 'Create'
    UPDATE = 'update', 'Update'
    DELETE = 'delete', 'Delete'


class SyncBatch(TimeStampedUUIDModel):
    """One upload from one device.

    `idempotency_key` is what makes a retry safe at the batch level. A device
    that uploads, loses the reply and uploads again sends the same key; the
    second request returns the first request's stored response without
    touching a single row.

    That is a stronger guarantee than per-record `sync_id` alone. Per-record
    idempotency stops duplicate rows; this also stops duplicate *work* and
    guarantees the device sees the same server ids both times.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sync_batches',
    )

    # Free text: a device id is whatever the app can produce on the platform
    # it is running on, and none of them are a UUID everywhere.
    device_id = models.CharField(max_length=128, blank=True, default='')
    app_version = models.CharField(max_length=32, blank=True, default='')

    idempotency_key = models.CharField(max_length=64)

    status = models.CharField(
        max_length=12, choices=BatchStatus, default=BatchStatus.PROCESSING
    )

    records_total = models.PositiveIntegerField(default=0)
    records_applied = models.PositiveIntegerField(default=0)
    records_duplicate = models.PositiveIntegerField(default=0)
    records_conflicted = models.PositiveIntegerField(default=0)
    records_failed = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    # The exact body that was sent back, so a replay is byte-for-byte the
    # answer the device already had — including the server ids it may have
    # already written into its local rows.
    response = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-started_at']
        verbose_name_plural = 'sync batches'
        constraints = [
            # Scoped to the user, not global: two devices belonging to
            # different people can pick the same key without colliding, and a
            # key is only ever replayed for the account that sent it.
            models.UniqueConstraint(
                fields=['user', 'idempotency_key'], name='one_batch_per_key_per_user'
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'started_at']),
            models.Index(fields=['device_id']),
        ]

    def __str__(self):
        return f'{self.idempotency_key} ({self.records_total} records)'


class SyncRecord(TimeStampedUUIDModel):
    """What happened to one record in a batch.

    Kept per record rather than per batch because "the batch failed" is not
    something a support desk can act on. "Order SO-2608-A3F91C was rejected
    because its customer had been deactivated" is.
    """

    batch = models.ForeignKey(
        SyncBatch, on_delete=models.CASCADE, related_name='records'
    )

    entity_type = models.CharField(max_length=32)

    # The id the row has on the device. Opaque to the server, echoed back so
    # the client can match the answer to the row it came from.
    local_id = models.CharField(max_length=128)
    server_id = models.CharField(max_length=64, blank=True, default='')

    operation = models.CharField(max_length=8, choices=Operation)
    status = models.CharField(max_length=12, choices=RecordStatus)

    http_status = models.PositiveSmallIntegerField(null=True, blank=True)

    # The error or conflict body, as the owning module worded it. Not
    # rewritten here: the attendance module already says "You have already
    # checked in today" better than this module could.
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['batch', 'status']),
            models.Index(fields=['entity_type', 'status']),
            # The record-level duplicate check runs this lookup once per
            # uploaded record — it is the hottest query in the module.
            models.Index(fields=['entity_type', 'local_id', 'status']),
        ]

    def __str__(self):
        return f'{self.entity_type}/{self.local_id} -> {self.status}'


class SyncDownloadLog(TimeStampedUUIDModel):
    """One download, for the same reason uploads are logged.

    A device that keeps asking for everything since 1970 is a bug worth
    seeing, and it is invisible without this.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sync_downloads',
    )
    device_id = models.CharField(max_length=128, blank=True, default='')

    # Null on a first sync, which is exactly the case worth being able to
    # count.
    since = models.DateTimeField(null=True, blank=True)

    records_returned = models.PositiveIntegerField(default=0)
    entities = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'created_at'])]

    def __str__(self):
        return f'{self.user_id} <- {self.records_returned} records'


def new_idempotency_key():
    """For clients that do not send one. A key they did not choose cannot
    dedupe their retry, so this is a fallback, not a default worth relying
    on — the response says so."""
    return uuid.uuid7().hex
