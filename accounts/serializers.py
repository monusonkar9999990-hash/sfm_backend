"""Serializers for the authentication endpoints.

Nothing here writes to the user table except the password change — these
serializers exist to validate credentials and to shape the user payload the
mobile client reads.
"""

import re

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from django.db.models import Q

from .models import BusinessPermission, InviteRequest

User = get_user_model()

# The same shapes the User model enforces, so a request that would be
# impossible to approve is refused at the door.
EMPLOYEE_CODE = re.compile(r'^[A-Z0-9][A-Z0-9\-/]{1,19}$')
E164 = re.compile(r'^\+[1-9]\d{7,14}$')


class UserSerializer(serializers.ModelSerializer):
    """The signed-in user.

    `name`, `phone`, `role` and `territory` are the keys the Flutter client's
    `UserModel.fromJson` already looks for. The canonical field names are sent
    *alongside* them rather than instead, so the app keeps working today while
    the payload stays honest about what the database actually stores.
    """

    name = serializers.CharField(source='full_name', read_only=True)
    phone = serializers.CharField(source='mobile', read_only=True, allow_null=True)
    role = serializers.SerializerMethodField()
    role_id = serializers.SerializerMethodField()
    territory = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    designation = serializers.SerializerMethodField()
    reporting_manager = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id',
            'employee_code',
            'name',
            'full_name',
            'email',
            'phone',
            'mobile',
            'role',
            'role_id',
            'territory',
            'department',
            'designation',
            'reporting_manager',
            'profile_photo',
            'status',
            'is_active',
            'must_change_password',
            'date_joined',
            'permissions',
        )
        read_only_fields = fields

    def get_role(self, obj) -> str:
        """Role name, because the client renders it directly."""
        return obj.role.name if obj.role_id else ''

    def get_role_id(self, obj) -> str:
        return str(obj.role_id) if obj.role_id else ''

    def get_territory(self, obj) -> str:
        """Primary posting. A user may cover several; the client shows one."""
        territory = obj.primary_territory
        return territory.name if territory else 'Unassigned'

    def get_department(self, obj) -> str:
        return obj.department.name if obj.department_id else ''

    def get_designation(self, obj) -> str:
        return obj.designation.name if obj.designation_id else ''

    def get_reporting_manager(self, obj) -> str:
        return obj.reporting_manager.full_name if obj.reporting_manager_id else ''

    def get_permissions(self, obj) -> list:
        """Permission keys this user holds, e.g. `['place_orders', ...]`.

        Sent with the profile rather than baked into the access token, so a
        role change takes effect on the next request instead of waiting for
        the token to expire.
        """
        if obj.is_superuser:
            # get_all_permissions() would also return every add/change/delete/
            # view row Django generates per model, which the client has no use
            # for. Only the published SFM keys are sent.
            return sorted(code for code, _ in BusinessPermission._meta.permissions)
        return sorted(obj.role.permission_codenames) if obj.role_id else []


class LoginSerializer(TokenObtainPairSerializer):
    """Exchanges one identifier plus a password for a token pair.

    The input field is `identifier` rather than `employee_code`: the
    authentication backend accepts a mobile number, an email address or an
    employee code, and the API should not pretend otherwise.
    """

    username_field = 'identifier'

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Small, stable claims only. Permissions are deliberately left out —
        # embedding them would keep a revoked permission alive until the
        # access token expired.
        token['employee_code'] = user.employee_code
        token['role'] = user.role.code if user.role_id else None
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = UserSerializer(self.user, context=self.context).data
        # Hoisted out of the user object as well, because the client routes on
        # it immediately after sign-in.
        data['must_change_password'] = self.user.must_change_password
        return data


class ChangePasswordSerializer(serializers.Serializer):
    """Validates a password change for the requesting user."""

    current_password = serializers.CharField(
        write_only=True, style={'input_type': 'password'}
    )
    new_password = serializers.CharField(
        write_only=True, style={'input_type': 'password'}
    )
    confirm_password = serializers.CharField(
        write_only=True, style={'input_type': 'password'}
    )

    def validate_current_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Your current password is not correct.')
        return value

    def validate(self, attrs):
        user = self.context['request'].user

        if attrs['new_password'] != attrs['confirm_password']:
            raise serializers.ValidationError(
                {'confirm_password': 'The two passwords do not match.'}
            )
        if attrs['new_password'] == attrs['current_password']:
            raise serializers.ValidationError(
                {'new_password': 'The new password must differ from the current one.'}
            )

        try:
            # Django's validators raise its own ValidationError, which DRF does
            # not translate on its own — unwrapped it would surface as a 500.
            validate_password(attrs['new_password'], user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'new_password': list(exc.messages)})

        return attrs


class LogoutSerializer(serializers.Serializer):
    """Validates that a refresh token is usable and belongs to the caller."""

    refresh = serializers.CharField(write_only=True)

    def validate_refresh(self, value):
        try:
            token = RefreshToken(value)
        except TokenError:
            raise serializers.ValidationError(
                'This refresh token is invalid, expired or already blacklisted.'
            )

        # Without this check anyone holding a stolen token string could log
        # another user out, which is a denial of service with no credentials.
        if str(token.get('user_id')) != str(self.context['request'].user.pk):
            raise serializers.ValidationError('This token belongs to another user.')

        self.token = token
        return value

    def save(self, **kwargs):
        try:
            self.token.blacklist()
        except AttributeError:  # pragma: no cover - blacklist app is installed
            raise serializers.ValidationError(
                'Token blacklisting is not enabled on this server.'
            )
        return self.token


@extend_schema_serializer(component_name='AuthInviteRequest')
class InviteRequestSerializer(serializers.Serializer):
    """Validates someone asking to be given an account.

    Deliberately says nothing about whether the person is already registered.
    An endpoint that answers "that email already has an account" is a free
    directory of who works here, and this one is open to the internet.
    """

    full_name = serializers.CharField(max_length=150)
    employee_code = serializers.CharField(max_length=20)
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    mobile = serializers.CharField(max_length=16, required=False, allow_blank=True)
    message = serializers.CharField(
        max_length=1000, required=False, allow_blank=True, default=''
    )

    # Write-only, and hashed before it is stored. It is never echoed back, it
    # never reaches a log line, and the account it will belong to does not
    # exist yet — an administrator still has to approve the request first.
    #
    # Optional, so a client that does not ask for one keeps working: the
    # request is then recorded exactly as it always was, and the account is
    # created with a password to be set on first sign-in.
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=128,
        trim_whitespace=False,
        style={'input_type': 'password'},
    )
    confirm_password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=128,
        trim_whitespace=False,
        style={'input_type': 'password'},
    )

    def validate_employee_code(self, value):
        code = value.strip().upper()
        if not EMPLOYEE_CODE.match(code):
            raise serializers.ValidationError(
                'Use letters, digits, hyphen or slash, e.g. SFM-0142.'
            )
        return code

    def validate_mobile(self, value):
        mobile = value.strip()
        if mobile and not E164.match(mobile):
            raise serializers.ValidationError('Use E.164 format, e.g. +919876543210.')
        return mobile

    def validate(self, attrs):
        if not attrs.get('email') and not attrs.get('mobile'):
            raise serializers.ValidationError(
                {'email': 'Give an email address or a mobile number so we can reply.'}
            )
        attrs['full_name'] = attrs['full_name'].strip()
        self._validate_password(attrs)
        return attrs

    def _validate_password(self, attrs):
        """Checks a chosen password against the project's own rules.

        Half a pair is refused rather than quietly ignored: somebody who typed
        a password and left the confirmation empty has not chosen anything,
        and recording the request without it would hand them an account whose
        password is not the one they think it is.

        The rules are `AUTH_PASSWORD_VALIDATORS` — the same set the
        change-password endpoint enforces — so a password accepted here is one
        the account can keep.
        """
        password = attrs.get('password') or ''
        confirm = attrs.get('confirm_password') or ''
        if not password and not confirm:
            attrs['password'] = ''
            return

        if not password:
            raise serializers.ValidationError({'password': 'Choose a password.'})
        if not confirm:
            raise serializers.ValidationError(
                {'confirm_password': 'Type the password once more.'}
            )
        if password != confirm:
            raise serializers.ValidationError(
                {'confirm_password': 'The two passwords do not match.'}
            )

        # An unsaved instance, purely so the similarity validator can compare
        # the password against the name and email that were just given.
        candidate = User(
            employee_code=attrs.get('employee_code', ''),
            full_name=attrs.get('full_name', ''),
            email=attrs.get('email') or None,
        )
        try:
            # Django's validators raise its own ValidationError, which DRF does
            # not translate on its own — unwrapped it would surface as a 500.
            validate_password(password, candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})

    def record(self):
        """Records the request, unless there is nothing to record.

        Returns `None` when the person already has an account or already has a
        request waiting. The view answers the same either way — the caller
        cannot tell which happened, which is the point.

        Not named `create`: DRF's `save()` asserts that `create()` returns an
        object, and "deliberately recorded nothing" is a legitimate outcome
        here.
        """
        validated_data = self.validated_data
        email = (validated_data.get('email') or '').strip().lower()
        mobile = (validated_data.get('mobile') or '').strip()
        code = validated_data['employee_code']

        already_a_user = User.objects.filter(
            Q(employee_code__iexact=code)
            | (Q(email__iexact=email) if email else Q(pk__in=[]))
            | (Q(mobile=mobile) if mobile else Q(pk__in=[]))
        ).exists()
        if already_a_user:
            return None

        if InviteRequest.open_for(code, email, mobile) is not None:
            return None

        password = validated_data.get('password') or ''
        return InviteRequest.objects.create(
            full_name=validated_data['full_name'],
            employee_code=code,
            email=email,
            mobile=mobile,
            message=validated_data.get('message', ''),
            # Hashed here, so the plain text exists only for the length of this
            # request and is never written anywhere.
            password_hash=make_password(password) if password else '',
        )
