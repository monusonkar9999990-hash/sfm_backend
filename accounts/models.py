"""Identity and organisation structure for the SFM backend.

The user model carries only what a request authorises against — role,
department, manager chain and territory. Everything HR-only (salary,
documents, leave) belongs on the future Employee model, joined one-to-one, so
that changing an HR field never touches the authentication table.
"""

import uuid
from pathlib import Path

from django.contrib.auth.models import AbstractBaseUser, Group, PermissionsMixin
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .managers import UserManager


def user_photo_path(instance, filename):
    """One photo per user, under a folder named by the user's id.

    Deterministic rather than timestamped: re-uploading replaces the old file
    instead of leaving orphans behind in media storage.
    """
    return f'users/{instance.pk}/profile{Path(filename).suffix.lower()}'


class TimeStampedUUIDModel(models.Model):
    """Primary key and audit columns shared by every table in this app.

    UUIDv7 rather than uuid4: it is time-ordered, so InnoDB appends near the
    end of the clustered index instead of writing into random pages, and a
    record created offline on a device can carry its own id without risking a
    collision when it finally syncs.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------- master data


class Department(TimeStampedUUIDModel):
    name = models.CharField(max_length=80, unique=True)
    code = models.CharField(max_length=20, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        self.name = self.name.strip()
        super().save(*args, **kwargs)


class Designation(TimeStampedUUIDModel):
    name = models.CharField(max_length=80, unique=True)
    code = models.CharField(max_length=20, unique=True)

    # Seniority, used to order dropdowns and to sanity-check that a manager
    # outranks their report. Higher is more senior.
    level = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-level', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        self.name = self.name.strip()
        super().save(*args, **kwargs)


class Territory(TimeStampedUUIDModel):
    """A sales geography, nested: Zone > Region > City > Beat area."""

    class Kind(models.TextChoices):
        ZONE = 'zone', 'Zone'
        REGION = 'region', 'Region'
        CITY = 'city', 'City'
        BEAT_AREA = 'beat_area', 'Beat area'

    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20, unique=True)
    kind = models.CharField(max_length=12, choices=Kind, default=Kind.CITY)
    parent = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='children',
    )
    is_active = models.BooleanField(default=True)

    # Materialised path, e.g. '/<zone-id>/<region-id>/<city-id>/'. Answers
    # "everything under this territory" with one indexed LIKE instead of a
    # recursive query on every report.
    path = models.CharField(max_length=512, editable=False, db_index=True, default='')

    class Meta:
        ordering = ['kind', 'name']
        indexes = [models.Index(fields=['kind', 'is_active'])]

    def __str__(self):
        return f'{self.name} ({self.get_kind_display()})'

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        self.name = self.name.strip()
        # The UUID default is evaluated when the instance is built, so the pk
        # is already available here on an insert.
        parent_path = self.parent.path if self.parent_id else '/'
        self.path = f'{parent_path}{self.pk}/'
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = {*kwargs['update_fields'], 'path', 'code', 'name'}
        super().save(*args, **kwargs)

    @property
    def descendants(self):
        """Every territory below this one, at any depth."""
        return Territory.objects.filter(path__startswith=self.path).exclude(pk=self.pk)


# ------------------------------------------------------------ roles and perms


class BusinessPermission(models.Model):
    """Has no table of its own — it exists only to register the SFM
    permissions as real `auth.Permission` rows, so they can be attached to the
    Group behind every Role and checked with `user.has_perm()`.

    The codenames are a published contract: the mobile client resolves them
    with `Permission.fromKey()`, so renaming one breaks the app.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ('mark_attendance', 'Mark attendance'),
            ('log_site_visits', 'Log site visits'),
            ('plan_beats', 'Plan beats'),
            ('onboard_customers', 'Onboard customers and sites'),
            ('place_orders', 'Place orders'),
            ('approve_discount', 'Approve discounts above the field cap'),
            ('cancel_orders', 'Cancel orders'),
            ('view_pricing', 'View the full rate card'),
            ('view_reports', 'View reports'),
            ('view_team_reports', "View the whole team's figures"),
            ('export_data', 'Export report data'),
            ('manage_users', 'Add and suspend users'),
            ('manage_roles', 'Edit roles and permissions'),
            ('edit_master_data', 'Edit master data'),
            ('edit_configuration', 'Change app configuration'),
        ]


class Role(TimeStampedUUIDModel):
    """A named bundle of permissions.

    The permissions themselves live on a Django Group, one per role, so that
    `user.has_perm()`, DRF's model permissions and the Django admin all keep
    working without a parallel permission system.
    """

    name = models.CharField(max_length=60, unique=True)
    code = models.CharField(max_length=40, unique=True)
    description = models.TextField(blank=True, default='')

    # System roles ship with the product and cannot be deleted, so an account
    # can never be left with no role to assign.
    is_system = models.BooleanField(default=False)

    group = models.OneToOneField(
        Group,
        on_delete=models.PROTECT,
        related_name='role',
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = self.name.strip()
        self.code = self.code.strip().lower().replace(' ', '_')

        if self.group_id is None:
            self.group = Group.objects.get_or_create(name=self.name)[0]
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = {*kwargs['update_fields'], 'group'}
        elif self.group.name != self.name:
            # Keep the Group's name in step, otherwise the admin's group list
            # drifts away from the role list.
            self.group.name = self.name
            self.group.save(update_fields=['name'])

        super().save(*args, **kwargs)

    @property
    def permission_codenames(self):
        """The keys the mobile client expects in a role payload."""
        if self.group_id is None:
            return []
        return list(
            self.group.permissions.order_by('codename').values_list('codename', flat=True)
        )


# -------------------------------------------------------------------- user


class User(AbstractBaseUser, PermissionsMixin):
    """A person who can sign in.

    AbstractBaseUser rather than AbstractUser: there is no username in this
    product, the name is one field rather than two, and `date_joined` here
    means the HR joining date — not the moment the row was written, which is
    what `created_at` records.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        INVITED = 'invited', 'Invited'
        SUSPENDED = 'suspended', 'Suspended'

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)

    # Identity ------------------------------------------------------------
    # USERNAME_FIELD, because it is the one identifier HR issues and nobody
    # ever changes. Signing in still works with email or mobile — that is the
    # authentication backend's job, not this field's.
    employee_code = models.CharField(
        max_length=20,
        unique=True,
        validators=[
            RegexValidator(
                r'^[A-Z0-9][A-Z0-9\-/]{1,19}$',
                'Use letters, digits, hyphen or slash, e.g. SFM-0142.',
            )
        ],
    )
    full_name = models.CharField(max_length=150)
    email = models.EmailField(max_length=254, unique=True, null=True, blank=True)
    mobile = models.CharField(
        max_length=16,
        unique=True,
        null=True,
        blank=True,
        validators=[
            RegexValidator(
                r'^\+[1-9]\d{7,14}$',
                'Use E.164 format, e.g. +919876543210.',
            )
        ],
    )

    # Organisation --------------------------------------------------------
    role = models.ForeignKey(
        Role, on_delete=models.PROTECT, null=True, blank=True, related_name='users'
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name='users'
    )
    designation = models.ForeignKey(
        Designation, on_delete=models.PROTECT, null=True, blank=True, related_name='users'
    )
    reporting_manager = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='direct_reports',
    )
    # Materialised path up the reporting chain, so "everyone under me" is one
    # indexed prefix match. The hierarchy changes a few times a year; team
    # reports run all day.
    manager_path = models.CharField(
        max_length=512, editable=False, db_index=True, default=''
    )
    territories = models.ManyToManyField(
        Territory, through='UserTerritory', related_name='users', blank=True
    )

    # Profile and state ---------------------------------------------------
    profile_photo = models.ImageField(upload_to=user_photo_path, null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status, default=Status.INVITED)
    is_active = models.BooleanField(
        default=True,
        help_text='Unset automatically unless the status is Active.',
    )
    is_staff = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=True)

    # HR joining date, not a row timestamp.
    date_joined = models.DateField(default=timezone.localdate)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = 'employee_code'
    EMAIL_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name', 'email']

    class Meta:
        ordering = ['full_name']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email__isnull=False) | models.Q(mobile__isnull=False),
                name='user_has_a_login_identifier',
            ),
            models.CheckConstraint(
                condition=~models.Q(reporting_manager=models.F('id')),
                name='user_is_not_their_own_manager',
            ),
        ]
        indexes = [
            models.Index(fields=['department', 'is_active']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.employee_code} · {self.full_name}'

    # Django admin and password-reset flows expect these two.
    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name.split(' ')[0] if self.full_name else self.employee_code

    @property
    def initials(self):
        parts = self.full_name.split()
        if not parts:
            return '?'
        if len(parts) == 1:
            return parts[0][0].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def primary_territory(self):
        link = self.territory_links.filter(is_primary=True).select_related('territory').first()
        return link.territory if link else None

    @property
    def subordinates(self):
        """Everyone below this user in the reporting chain, at any depth."""
        return User.objects.filter(manager_path__startswith=self.manager_path).exclude(
            pk=self.pk
        )

    def has_business_permission(self, codename):
        """`user.has_business_permission('place_orders')`.

        Wraps has_perm so callers do not have to know the app label the
        permission happens to be registered under.
        """
        return self.has_perm(f'accounts.{codename}')

    def save(self, *args, **kwargs):
        requested_fields = kwargs.get('update_fields')

        self.employee_code = self.employee_code.strip().upper()
        self.full_name = self.full_name.strip()
        # Empty strings would collide with each other under a unique index;
        # NULLs do not. Anything blank becomes NULL.
        self.email = self.email.strip().lower() if self.email else None
        self.mobile = self.mobile.strip() if self.mobile else None

        # One switch: the business status decides whether signing in works.
        self.is_active = self.status == self.Status.ACTIVE

        previous_path = self.manager_path
        # Recomputing needs the manager row. Skipped on targeted saves such as
        # the last_login write on every sign-in, which would otherwise pay for
        # a join it cannot change the answer of.
        if (
            not self.manager_path
            or requested_fields is None
            or 'reporting_manager' in requested_fields
        ):
            self._guard_manager_cycle()
            parent_path = (
                self.reporting_manager.manager_path if self.reporting_manager_id else '/'
            )
            self.manager_path = f'{parent_path}{self.pk}/'

        if requested_fields is not None:
            kwargs['update_fields'] = {
                *requested_fields,
                'employee_code',
                'full_name',
                'email',
                'mobile',
                'is_active',
                'manager_path',
            }

        super().save(*args, **kwargs)

        if previous_path and previous_path != self.manager_path:
            self._rebuild_subordinate_paths()

        if requested_fields is None or 'role' in requested_fields:
            self._sync_role_group()

    def _guard_manager_cycle(self):
        """Refuse a reporting line that loops back to this user.

        A CHECK constraint stops someone being their own manager, but the
        database cannot see A -> B -> A. Left alone it does not raise: the
        paths quietly rewrite each other until this user appears inside its
        own ancestor chain, and from then on every "everyone under me" query
        answers wrongly. Better to refuse the save.
        """
        seen = {self.pk}
        manager = self.reporting_manager
        while manager is not None:
            if manager.pk in seen:
                raise ValidationError(
                    {
                        'reporting_manager': (
                            'That would create a loop in the reporting line — '
                            f'{manager.full_name or manager.employee_code} already '
                            'reports to this user, directly or through someone else.'
                        )
                    }
                )
            seen.add(manager.pk)
            manager = manager.reporting_manager

    def _sync_role_group(self):
        """Put the user in the Group that carries their role's permissions.

        Django resolves `has_perm` through group membership, so assigning a
        Role means nothing until the user actually belongs to the Group behind
        it. Groups added by hand in the admin are left alone — only the
        role-derived membership is swapped.
        """
        role_group_ids = set(
            Group.objects.filter(role__isnull=False).values_list('id', flat=True)
        )
        current = set(self.groups.values_list('id', flat=True))
        target = {self.role.group_id} if self.role_id and self.role.group_id else set()

        desired = (current - role_group_ids) | target
        if desired != current:
            self.groups.set(desired)
            # Permission lookups are cached per instance; drop the stale copy.
            for attr in ('_perm_cache', '_user_perm_cache', '_group_perm_cache'):
                self.__dict__.pop(attr, None)

    def _rebuild_subordinate_paths(self):
        """Re-stamp the chain below this user after a manager change.

        Rare enough to do inline: an org chart moves a handful of people at a
        time, not thousands.
        """
        for report in self.direct_reports.all():
            report.manager_path = f'{self.manager_path}{report.pk}/'
            report.save(update_fields=['manager_path'])


class UserTerritory(TimeStampedUUIDModel):
    """Which geographies a user covers, and since when.

    A through model rather than a plain M2M because an assignment has its own
    facts: whether it is the primary posting, and the dates it ran for.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='territory_links')
    territory = models.ForeignKey(
        Territory, on_delete=models.PROTECT, related_name='user_links'
    )
    is_primary = models.BooleanField(default=False)
    assigned_from = models.DateField(default=timezone.localdate)
    assigned_to = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'user territories'
        ordering = ['-is_primary', 'territory__name']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'territory'], name='one_link_per_user_and_territory'
            )
        ]

    def __str__(self):
        # ASCII on purpose: this string gets printed to a Windows console,
        # whose cp1252 encoding cannot represent an arrow.
        return f'{self.user.employee_code} -> {self.territory.name}'

    def save(self, *args, **kwargs):
        # "Only one primary per user" cannot be a partial unique index here:
        # MySQL does not support them. Enforced in code instead, inside a
        # transaction so two concurrent saves cannot both win.
        with transaction.atomic():
            if self.is_primary:
                UserTerritory.objects.filter(user=self.user, is_primary=True).exclude(
                    pk=self.pk
                ).update(is_primary=False)
            super().save(*args, **kwargs)
