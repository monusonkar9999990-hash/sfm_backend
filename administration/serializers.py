"""Serializers for the administration endpoints."""

from django.contrib.auth.models import Permission
from django.db import transaction
from rest_framework import serializers

from accounts.models import (
    Department,
    Designation,
    InviteRequest,
    Role,
    Territory,
    User,
    UserTerritory,
)

from .roles import business_permissions
from .settings_registry import REGISTRY


def codenames_of(role):
    """A role's permission codenames, read from the prefetch cache.

    Deliberately not `Role.permission_codenames`, which is the right property
    everywhere else but wrong in a list: it calls `.order_by('codename')` on
    the related manager, and any queryset modification steps around a
    `prefetch_related` cache and issues a fresh query. On a list of employees
    that is one query per row — twelve employees cost sixteen queries before
    this existed, two cost six.

    Sorting in Python instead keeps the cache and costs nothing on a set this
    size. The property is left alone: other callers fetch one role at a time,
    where it is fine.
    """
    if role is None or role.group_id is None:
        return []
    return sorted(permission.codename for permission in role.group.permissions.all())


# ------------------------------------------------------------------ employees


class EmployeeSerializer(serializers.ModelSerializer):
    """An employee, as an administrator reads them."""

    role_name = serializers.CharField(source='role.name', read_only=True)
    role_code = serializers.CharField(source='role.code', read_only=True)
    department_name = serializers.CharField(
        source='department.name', read_only=True
    )
    designation_name = serializers.CharField(
        source='designation.name', read_only=True
    )
    reporting_manager_name = serializers.CharField(
        source='reporting_manager.full_name', read_only=True
    )
    permissions = serializers.SerializerMethodField()
    territories = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id',
            'employee_code',
            'full_name',
            'email',
            'mobile',
            'status',
            'is_active',
            'is_staff',
            'must_change_password',
            'role',
            'role_name',
            'role_code',
            'department',
            'department_name',
            'designation',
            'designation_name',
            'reporting_manager',
            'reporting_manager_name',
            'permissions',
            'territories',
            'last_login',
            'created_at',
            'updated_at',
        )
        read_only_fields = fields

    def get_permissions(self, obj) -> list:
        return codenames_of(obj.role if obj.role_id else None)

    def get_territories(self, obj) -> list:
        return [
            {
                'id': str(link.territory_id),
                'name': link.territory.name,
                'is_primary': link.is_primary,
            }
            for link in obj.territory_links.all()
        ]


class EmployeeWriteSerializer(serializers.ModelSerializer):
    """Creating and editing an employee.

    `password` is optional on create. Left out, the account is created without
    a usable password and the person sets one from the invite — which is the
    flow this product is built around. An administrator typing a colleague's
    password into a form is not a flow worth supporting.
    """

    password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, min_length=8
    )
    territories = serializers.PrimaryKeyRelatedField(
        queryset=Territory.objects.all(), many=True, required=False
    )
    primary_territory = serializers.PrimaryKeyRelatedField(
        queryset=Territory.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = User
        fields = (
            'employee_code',
            'full_name',
            'email',
            'mobile',
            'status',
            'role',
            'department',
            'designation',
            'reporting_manager',
            'password',
            'territories',
            'primary_territory',
            'is_staff',
        )
        extra_kwargs = {
            'employee_code': {'required': True},
            'full_name': {'required': True},
        }

    def validate_employee_code(self, value):
        code = value.strip().upper()
        clash = User.objects.filter(employee_code=code)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                f'{code} already belongs to another employee.'
            )
        return code

    def validate(self, attrs):
        email = attrs.get('email', getattr(self.instance, 'email', None))
        mobile = attrs.get('mobile', getattr(self.instance, 'mobile', None))

        if not email and not mobile:
            raise serializers.ValidationError(
                {
                    'email': (
                        'An employee needs an email address or a mobile '
                        'number — those are what they sign in with.'
                    )
                }
            )

        manager = attrs.get('reporting_manager')
        if manager is not None and self.instance is not None:
            if manager.pk == self.instance.pk:
                raise serializers.ValidationError(
                    {'reporting_manager': 'Somebody cannot report to themselves.'}
                )

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        password = validated_data.pop('password', None)
        territories = validated_data.pop('territories', [])
        primary = validated_data.pop('primary_territory', None)

        user = User.objects.create_user(
            employee_code=validated_data.pop('employee_code'),
            full_name=validated_data.pop('full_name'),
            email=validated_data.pop('email', None),
            mobile=validated_data.pop('mobile', None),
            password=password or None,
            **validated_data,
        )

        self._set_territories(user, territories, primary)
        return user

    @transaction.atomic
    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        territories = validated_data.pop('territories', None)
        primary = validated_data.pop('primary_territory', None)

        for field, value in validated_data.items():
            setattr(instance, field, value)

        if password:
            instance.set_password(password)
            # They did not choose it, so they change it on first use.
            instance.must_change_password = True

        instance.save()

        if territories is not None or primary is not None:
            self._set_territories(instance, territories or [], primary)

        return instance

    @staticmethod
    def _set_territories(user, territories, primary):
        wanted = {t.pk: t for t in territories}
        if primary is not None:
            wanted[primary.pk] = primary

        UserTerritory.objects.filter(user=user).exclude(
            territory_id__in=wanted
        ).delete()

        for territory in wanted.values():
            UserTerritory.objects.update_or_create(
                user=user,
                territory=territory,
                defaults={
                    'is_primary': primary is not None and territory.pk == primary.pk
                },
            )


class EmployeeStatusSerializer(serializers.Serializer):
    """Activating or suspending an account."""

    status = serializers.ChoiceField(choices=User.Status.choices)
    reason = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )


# ---------------------------------------------------------------------- roles


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ('id', 'codename', 'name')
        read_only_fields = fields


class RoleSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()
    user_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Role
        fields = (
            'id',
            'name',
            'code',
            'description',
            'is_system',
            'permissions',
            'user_count',
            'created_at',
            'updated_at',
        )
        read_only_fields = fields

    def get_permissions(self, obj) -> list:
        return codenames_of(obj)


class RoleWriteSerializer(serializers.ModelSerializer):
    permissions = serializers.ListField(
        child=serializers.CharField(), required=False
    )

    class Meta:
        model = Role
        fields = ('name', 'code', 'description', 'permissions')

    def validate_permissions(self, value):
        available = {p.codename for p in business_permissions()}
        unknown = [name for name in value if name not in available]
        if unknown:
            raise serializers.ValidationError(
                f'Unknown permissions: {", ".join(sorted(unknown))}. '
                f'Known: {", ".join(sorted(available))}.'
            )
        return value

    def validate_code(self, value):
        code = value.strip().lower().replace(' ', '_')
        clash = Role.objects.filter(code=code)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(f'{code} is already a role.')
        return code

    @transaction.atomic
    def create(self, validated_data):
        codenames = validated_data.pop('permissions', [])
        role = Role.objects.create(**validated_data)
        self._apply(role, codenames)
        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        codenames = validated_data.pop('permissions', None)

        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()

        if codenames is not None:
            self._apply(instance, codenames)
        return instance

    @staticmethod
    def _apply(role, codenames):
        role.group.permissions.set(
            business_permissions().filter(codename__in=codenames)
        )


class RolePermissionsSerializer(serializers.Serializer):
    """The body of `PUT /admin/roles/{id}/permissions/`.

    A full replacement, not a patch: "these are the permissions now" is a
    sentence somebody can reason about, and a partial update of a permission
    set is how a role quietly keeps something it was supposed to lose.
    """

    permissions = serializers.ListField(child=serializers.CharField())

    def validate_permissions(self, value):
        available = {p.codename for p in business_permissions()}
        unknown = [name for name in value if name not in available]
        if unknown:
            raise serializers.ValidationError(
                f'Unknown permissions: {", ".join(sorted(unknown))}.'
            )
        return value


# ------------------------------------------------------------ invite requests


class InviteRequestSerializer(serializers.ModelSerializer):
    reviewed_by_name = serializers.CharField(
        source='reviewed_by.full_name', read_only=True
    )
    created_user_code = serializers.CharField(
        source='created_user.employee_code', read_only=True
    )

    class Meta:
        model = InviteRequest
        fields = (
            'id',
            'full_name',
            'employee_code',
            'email',
            'mobile',
            'message',
            'status',
            'reviewed_by',
            'reviewed_by_name',
            'reviewed_at',
            'review_note',
            'created_user',
            'created_user_code',
            'created_at',
        )
        read_only_fields = fields


class ApproveInviteSerializer(serializers.Serializer):
    """What an approval decides.

    The requester said what their employee code was; nobody checked. The
    administrator confirms or corrects it here, which is the entire point of
    the review step.
    """

    employee_code = serializers.CharField(max_length=20, required=False)
    role = serializers.PrimaryKeyRelatedField(
        queryset=Role.objects.all(), required=False, allow_null=True
    )
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(), required=False, allow_null=True
    )
    designation = serializers.PrimaryKeyRelatedField(
        queryset=Designation.objects.all(), required=False, allow_null=True
    )
    territory = serializers.PrimaryKeyRelatedField(
        queryset=Territory.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_employee_code(self, value):
        code = value.strip().upper()
        if User.objects.filter(employee_code=code).exists():
            raise serializers.ValidationError(
                f'{code} already belongs to an employee.'
            )
        return code


class RejectInviteSerializer(serializers.Serializer):
    """A rejection needs a reason, for the same reason a skipped beat stop
    does: an unexplained "no" tells the next reader nothing."""

    reason = serializers.CharField(max_length=255)

    def validate_reason(self, value):
        reason = value.strip()
        if len(reason) < 3:
            raise serializers.ValidationError(
                'Give a reason somebody reading this next month can use.'
            )
        return reason


# ------------------------------------------------------------------- settings


class SettingsUpdateSerializer(serializers.Serializer):
    """`PUT /admin/settings/` — a partial map of key to new value.

    Only known keys are accepted, and each is coerced to its declared type, so
    `"true"` from a form and `true` from JSON both land as a boolean.
    """

    def to_internal_value(self, data):
        if not isinstance(data, dict) or not data:
            raise serializers.ValidationError(
                {'detail': 'Send a map of setting keys to values.'}
            )

        unknown = [key for key in data if key not in REGISTRY]
        if unknown:
            raise serializers.ValidationError(
                {
                    'detail': (
                        f'Unknown settings: {", ".join(sorted(unknown))}. '
                        f'Known: {", ".join(sorted(REGISTRY))}.'
                    )
                }
            )

        cleaned = {}
        errors = {}
        for key, raw in data.items():
            try:
                cleaned[key] = REGISTRY[key].coerce(raw)
            except ValueError as error:
                errors[key] = str(error)

        if errors:
            raise serializers.ValidationError(errors)

        return cleaned


# ----------------------------------------------------------------- audit logs


class AuditLogSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    actor = serializers.UUIDField(source='actor_id', read_only=True)
    actor_code = serializers.CharField(read_only=True)
    actor_name = serializers.CharField(source='actor.full_name', read_only=True)
    action = serializers.CharField(read_only=True)
    entity = serializers.CharField(read_only=True)
    entity_id = serializers.CharField(read_only=True)
    summary = serializers.CharField(read_only=True)
    changes = serializers.JSONField(read_only=True)
    ip_address = serializers.IPAddressField(read_only=True)
    user_agent = serializers.CharField(read_only=True)
    request_path = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
