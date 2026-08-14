"""Application settings and the audit trail.

Everything else the administration module manages — users, roles, permissions,
invite requests — already has a model in `accounts`. This file adds only the
two things that did not exist: a place to keep operational settings, and a
record of who changed what.
"""

from django.conf import settings
from django.db import models

from accounts.models import TimeStampedUUIDModel


class SettingType(models.TextChoices):
    STRING = 'string', 'Text'
    INTEGER = 'integer', 'Whole number'
    DECIMAL = 'decimal', 'Decimal'
    BOOLEAN = 'boolean', 'Yes/No'


class AppSetting(TimeStampedUUIDModel):
    """One operational setting, stored so it can be changed without a deploy.

    The value is JSON so a boolean stays a boolean and a number stays a
    number — a `CharField` holding `"true"` needs every reader to remember to
    parse it, and one of them eventually will not.

    Which settings exist, what type each is and what it defaults to lives in
    `settings_registry.py`, not here. This table holds only the values an
    administrator has actually overridden; anything absent falls back to the
    registry's default, so a fresh install behaves the same as a configured
    one.
    """

    key = models.CharField(max_length=64, unique=True)
    value = models.JSONField()
    value_type = models.CharField(max_length=10, choices=SettingType)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_settings',
    )

    class Meta:
        ordering = ['key']

    def __str__(self):
        return f'{self.key} = {self.value}'


class AuditAction(models.TextChoices):
    LOGIN = 'login', 'Signed in'
    LOGIN_FAILED = 'login_failed', 'Failed sign-in'
    LOGOUT = 'logout', 'Signed out'
    CREATE = 'create', 'Created'
    UPDATE = 'update', 'Updated'
    DELETE = 'delete', 'Deleted'
    APPROVE = 'approve', 'Approved'
    REJECT = 'reject', 'Rejected'
    SETTINGS_UPDATE = 'settings_update', 'Settings changed'
    PERMISSIONS_UPDATE = 'permissions_update', 'Permissions changed'


class AuditLog(TimeStampedUUIDModel):
    """Who did what, to which record, from where.

    Append-only by convention and by interface: there is no endpoint that
    updates or deletes one, and the Django admin registers it read-only. A
    trail somebody can edit is not a trail.

    `actor` is nullable on purpose. A failed sign-in has no authenticated user
    by definition, and losing that row would hide exactly the events worth
    keeping.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
    )

    # Kept as text as well as by foreign key: the code survives the account
    # being deleted, which is when a trail is most likely to be read.
    actor_code = models.CharField(max_length=20, blank=True, default='')

    action = models.CharField(max_length=20, choices=AuditAction)

    # `orders.Order`, `accounts.User` — the app label and model name, so a
    # reader can find the row without guessing.
    entity = models.CharField(max_length=64)
    entity_id = models.CharField(max_length=64, blank=True, default='')

    # A short human summary, so a list is readable without opening each row.
    summary = models.CharField(max_length=255, blank=True, default='')

    # Field-level before/after where the receiver could work it out, and the
    # request body's shape where it could not. Never credentials — see
    # `audit.redact`.
    changes = models.JSONField(default=dict, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default='')
    request_path = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['actor', '-created_at']),
            models.Index(fields=['entity', 'entity_id']),
            models.Index(fields=['action', '-created_at']),
        ]

    def __str__(self):
        return f'{self.actor_code or "system"} {self.action} {self.entity}'
