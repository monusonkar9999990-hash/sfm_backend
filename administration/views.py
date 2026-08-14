"""Administration endpoints, mounted under /api/<version>/admin/."""

import uuid

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.generics import (
    GenericAPIView,
    ListAPIView,
    ListCreateAPIView,
    RetrieveAPIView,
    RetrieveUpdateDestroyAPIView,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import InviteRequest, Role, User

from . import audit, settings_registry
from .middleware import MaintenanceModeMiddleware
from .models import AppSetting, AuditAction, AuditLog
from .permissions import SplitPermission
from .roles import business_permissions
from .serializers import (
    ApproveInviteSerializer,
    AuditLogSerializer,
    EmployeeSerializer,
    EmployeeStatusSerializer,
    EmployeeWriteSerializer,
    InviteRequestSerializer,
    PermissionSerializer,
    RejectInviteSerializer,
    RolePermissionsSerializer,
    RoleSerializer,
    RoleWriteSerializer,
    SettingsUpdateSerializer,
)


class AdminPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 200


class AdminView(GenericAPIView):
    """Shared plumbing. Every endpoint below is authenticated and permissioned;
    the codenames differ per view."""

    permission_classes = [IsAuthenticated, SplitPermission]
    pagination_class = AdminPagination


EMPLOYEE_QUERYSET = User.objects.select_related(
    'role', 'role__group', 'department', 'designation', 'reporting_manager'
).prefetch_related(
    'territory_links__territory',
    # Without this the permission list on each employee is a query per row.
    'role__group__permissions',
)


# ------------------------------------------------------------------ employees


class EmployeeListCreateView(AdminView, ListCreateAPIView):
    """The people with accounts, and the way to add one.

    **Query** `search` (code, name, email, mobile) · `status` · `role` (code or
    id) · `department` · `is_active` · `ordering`
    (`full_name`, `employee_code`, `created_at`, `last_login`)

    Reading needs `manage_users` as well as writing: a staff list is personal
    data, not a directory.

    **Responses** `200` · `201` · `400` · `401` · `403`
    """

    read_permission = 'manage_users'
    write_permission = 'manage_users'

    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['employee_code', 'full_name', 'email', 'mobile']
    ordering_fields = ['full_name', 'employee_code', 'created_at', 'last_login']
    ordering = ['full_name']

    def get_serializer_class(self):
        return (
            EmployeeWriteSerializer
            if self.request.method == 'POST'
            else EmployeeSerializer
        )

    def get_queryset(self):
        queryset = EMPLOYEE_QUERYSET.all()
        params = self.request.query_params

        status_filter = params.get('status', '').strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        role = params.get('role', '').strip()
        if role:
            # A code is what a human types; an id is what a form posts. Both
            # are accepted, but the id half is only added when the value could
            # be one — `role__id='sales_executive'` raises rather than simply
            # not matching, and Django evaluates both sides of an OR.
            matches = Q(role__code=role)
            try:
                matches |= Q(role__id=uuid.UUID(role))
            except (ValueError, AttributeError, TypeError):
                pass
            queryset = queryset.filter(matches)

        department = params.get('department', '').strip()
        if department:
            queryset = queryset.filter(department_id=department)

        is_active = params.get('is_active', '').strip().lower()
        if is_active in ('true', '1'):
            queryset = queryset.filter(is_active=True)
        elif is_active in ('false', '0'):
            queryset = queryset.filter(is_active=False)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            employee = serializer.save()

        return Response(
            EmployeeSerializer(employee).data, status=status.HTTP_201_CREATED
        )


class EmployeeDetailView(AdminView, RetrieveUpdateDestroyAPIView):
    """One employee.

    **DELETE suspends rather than erases.** An employee owns attendance,
    orders and visits, and those foreign keys are PROTECT — a hard delete
    would either fail or take the records with it. The account is set to
    `suspended`, which is what "remove this person's access" actually means.
    The response is still `204`.

    **Responses** `200` · `204` · `400` · `401` · `403` · `404`
    """

    read_permission = 'manage_users'
    write_permission = 'manage_users'
    queryset = EMPLOYEE_QUERYSET

    def get_serializer_class(self):
        return (
            EmployeeWriteSerializer
            if self.request.method in ('PUT', 'PATCH')
            else EmployeeSerializer
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        employee = self.get_object()

        serializer = self.get_serializer(
            employee, data=request.data, partial=partial
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            employee = serializer.save()

        return Response(EmployeeSerializer(employee).data)

    def destroy(self, request, *args, **kwargs):
        employee = self.get_object()

        if employee.pk == request.user.pk:
            return Response(
                {'detail': 'You cannot suspend your own account.'},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            employee.status = User.Status.SUSPENDED
            employee.is_active = False
            employee.save(update_fields=['status', 'is_active', 'updated_at'])

        return Response(status=status.HTTP_204_NO_CONTENT)


class EmployeeStatusView(AdminView):
    """Activate or suspend an account without touching anything else.

    **Responses** `200` · `400` · `401` · `403` · `404` · `409`
    """

    read_permission = 'manage_users'
    write_permission = 'manage_users'
    serializer_class = EmployeeStatusSerializer
    queryset = EMPLOYEE_QUERYSET
    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        employee = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data['status']

        if employee.pk == request.user.pk and new_status != User.Status.ACTIVE:
            return Response(
                {'detail': 'You cannot suspend your own account.'},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            employee.status = new_status
            employee.is_active = new_status == User.Status.ACTIVE
            employee.save(update_fields=['status', 'is_active', 'updated_at'])

        return Response(EmployeeSerializer(employee).data)


# ---------------------------------------------------------------------- roles


class RoleListCreateView(AdminView, ListCreateAPIView):
    """The roles, and the way to add one.

    Reading is open to any signed-in user: a client needs the role list to
    render a picker, and a role name is not sensitive. Creating one needs
    `manage_roles`.

    **Responses** `200` · `201` · `400` · `401` · `403`
    """

    read_permission = None
    write_permission = 'manage_roles'

    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'code', 'description']
    ordering_fields = ['name', 'code', 'created_at']
    ordering = ['name']

    def get_serializer_class(self):
        return (
            RoleWriteSerializer
            if self.request.method == 'POST'
            else RoleSerializer
        )

    def get_queryset(self):
        return (
            Role.objects.select_related('group')
            .prefetch_related('group__permissions')
            .annotate(user_count=Count('users'))
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            role = serializer.save()

        return Response(
            RoleSerializer(role).data, status=status.HTTP_201_CREATED
        )


class RoleDetailView(AdminView, RetrieveUpdateDestroyAPIView):
    """One role.

    A system role cannot be deleted, and neither can one somebody is using —
    an account with no role has no permissions, and orphaning a team is not
    something a DELETE should do quietly.

    **Responses** `200` · `204` · `400` · `401` · `403` · `404` · `409`
    """

    read_permission = None
    write_permission = 'manage_roles'

    def get_serializer_class(self):
        return (
            RoleWriteSerializer
            if self.request.method in ('PUT', 'PATCH')
            else RoleSerializer
        )

    def get_queryset(self):
        return (
            Role.objects.select_related('group')
            .prefetch_related('group__permissions')
            .annotate(user_count=Count('users'))
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        role = self.get_object()

        serializer = self.get_serializer(role, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            role = serializer.save()

        return Response(RoleSerializer(self.get_queryset().get(pk=role.pk)).data)

    def destroy(self, request, *args, **kwargs):
        role = self.get_object()

        if role.is_system:
            return Response(
                {
                    'detail': (
                        f'{role.name} ships with the product and cannot be '
                        f'deleted.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        in_use = User.objects.filter(role=role).count()
        if in_use:
            return Response(
                {
                    'detail': (
                        f'{in_use} employee(s) still have this role. Move them '
                        f'to another role first.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        group = role.group
        with transaction.atomic():
            role.delete()
            if group is not None:
                group.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class RolePermissionsView(AdminView):
    """Replaces a role's permission set.

    **Responses** `200` · `400` · `401` · `403` · `404`
    """

    read_permission = None
    write_permission = 'manage_roles'
    serializer_class = RolePermissionsSerializer
    queryset = Role.objects.select_related('group')
    http_method_names = ['put', 'options']

    def put(self, request, *args, **kwargs):
        role = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        codenames = serializer.validated_data['permissions']

        before = set(role.permission_codenames)

        with transaction.atomic():
            role.group.permissions.set(
                business_permissions().filter(codename__in=codenames)
            )

        after = set(role.permission_codenames)

        audit.record(
            action=AuditAction.PERMISSIONS_UPDATE,
            entity='accounts.Role',
            entity_id=role.pk,
            summary=f'Changed permissions on {role.name}',
            changes={
                'granted': sorted(after - before),
                'revoked': sorted(before - after),
            },
            request=request,
        )

        fresh = (
            Role.objects.select_related('group')
            .prefetch_related('group__permissions')
            .annotate(user_count=Count('users'))
            .get(pk=role.pk)
        )
        return Response(RoleSerializer(fresh).data)


class PermissionListView(AdminView, ListAPIView):
    """Every permission a role can be given.

    Read-only and unpaginated: there are fewer than twenty, and a client
    building a permission matrix wants them all at once.

    **Responses** `200` · `401`
    """

    read_permission = None
    write_permission = 'manage_roles'
    serializer_class = PermissionSerializer
    pagination_class = None

    def get_queryset(self):
        return business_permissions().order_by('codename')


# ------------------------------------------------------------ invite requests


class InviteRequestListView(AdminView, ListAPIView):
    """People waiting for an account.

    **Query** `status` (`pending`, `approved`, `rejected`) · `search` ·
    `ordering`

    **Responses** `200` · `401` · `403`
    """

    read_permission = 'approve_registrations'
    write_permission = 'approve_registrations'
    serializer_class = InviteRequestSerializer

    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['full_name', 'employee_code', 'email', 'mobile']
    ordering_fields = ['created_at', 'status', 'employee_code']
    ordering = ['-created_at']

    def get_queryset(self):
        queryset = InviteRequest.objects.select_related(
            'reviewed_by', 'created_user'
        )

        status_filter = self.request.query_params.get('status', '').strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        return queryset


class ApproveInviteView(AdminView):
    """Turns a request into an account.

    Where the requester chose a password when they applied, the account keeps
    it: the hash recorded with the request is carried across, and they sign in
    with what they typed. Approval is still the gate — nothing existed until
    an administrator pressed this.

    Where they did not, the account is created without a usable password and
    flagged `must_change_password`, so they set their own from the invite.

    Either way an administrator never types, sees or transmits somebody else's
    password.

    **Responses** `200` · `400` · `401` · `403` · `404` · `409`
    """

    read_permission = 'approve_registrations'
    write_permission = 'approve_registrations'
    serializer_class = ApproveInviteSerializer
    queryset = InviteRequest.objects.select_related('reviewed_by', 'created_user')
    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        invite = self.get_object()

        if not invite.is_pending:
            return Response(
                {
                    'detail': (
                        f'This request was already '
                        f'{invite.get_status_display().lower()}.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        code = data.get('employee_code') or invite.employee_code
        if User.objects.filter(employee_code=code).exists():
            return Response(
                {'employee_code': f'{code} already belongs to an employee.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            employee = User.objects.create_user(
                employee_code=code,
                full_name=invite.full_name,
                email=invite.email or None,
                mobile=invite.mobile or None,
                password=None,
                status=User.Status.ACTIVE,
                role=data.get('role'),
                department=data.get('department'),
                designation=data.get('designation'),
            )

            # The password the requester chose, if they chose one. Copied as
            # the hash it already is; see InviteRequest.adopt_password.
            chose_own_password = invite.adopt_password(employee)

            territory = data.get('territory')
            if territory is not None:
                from accounts.models import UserTerritory

                UserTerritory.objects.create(
                    user=employee, territory=territory, is_primary=True
                )

            invite.status = InviteRequest.Status.APPROVED
            invite.reviewed_by = request.user
            invite.reviewed_at = timezone.now()
            invite.review_note = data.get('note', '')
            invite.created_user = employee
            invite.save()

        audit.record(
            action=AuditAction.APPROVE,
            entity='accounts.InviteRequest',
            entity_id=invite.pk,
            summary=f'Approved {invite.full_name} as {code}',
            changes={'employee_code': code, 'user_id': str(employee.pk)},
            request=request,
        )

        return Response(
            {
                'invite': InviteRequestSerializer(invite).data,
                'employee': EmployeeSerializer(employee).data,
                # Said plainly rather than implied: nothing was emailed. There
                # is no mail backend configured in this project, and a
                # response that claimed otherwise would be a lie the support
                # desk pays for.
                'notification': {
                    'sent': False,
                    'detail': (
                        'No notification was sent — no mail or SMS backend is '
                        'configured. Tell them their account is ready; they '
                        'sign in with the password they chose when they '
                        'applied.'
                        if chose_own_password
                        else 'No notification was sent — no mail or SMS '
                        'backend is configured. Tell them their employee code '
                        'and ask them to use "forgot password" to set one.'
                    ),
                },
            },
            status=status.HTTP_200_OK,
        )


class RejectInviteView(AdminView):
    """Declines a request, with a reason.

    **Responses** `200` · `400` · `401` · `403` · `404` · `409`
    """

    read_permission = 'approve_registrations'
    write_permission = 'approve_registrations'
    serializer_class = RejectInviteSerializer
    queryset = InviteRequest.objects.select_related('reviewed_by', 'created_user')
    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        invite = self.get_object()

        if not invite.is_pending:
            return Response(
                {
                    'detail': (
                        f'This request was already '
                        f'{invite.get_status_display().lower()}.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data['reason']

        with transaction.atomic():
            invite.status = InviteRequest.Status.REJECTED
            invite.reviewed_by = request.user
            invite.reviewed_at = timezone.now()
            invite.review_note = reason
            invite.save()

        audit.record(
            action=AuditAction.REJECT,
            entity='accounts.InviteRequest',
            entity_id=invite.pk,
            summary=f'Rejected {invite.full_name}: {reason}',
            changes={'reason': reason},
            request=request,
        )

        return Response(InviteRequestSerializer(invite).data)


# ------------------------------------------------------------------- settings


class SettingsView(AdminView):
    """Reads and writes the operational settings.

    Each setting reports an `effect`: `enforced` means this server reads it on
    every relevant request, `advisory` means it is stored and served for the
    client to honour. The difference is published because a switch that stores
    a value and changes nothing is worse than no switch.

    `PUT` takes a partial map — send only what changes.

    **Responses** `200` · `400` · `401` · `403`
    """

    read_permission = 'edit_configuration'
    write_permission = 'edit_configuration'
    serializer_class = SettingsUpdateSerializer
    http_method_names = ['get', 'put', 'head', 'options']

    def get(self, request, *args, **kwargs):
        return Response({'settings': settings_registry.current()})

    def put(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        changes = serializer.validated_data

        before = {
            key: block['value']
            for key, block in settings_registry.current().items()
            if key in changes
        }

        with transaction.atomic():
            for key, value in changes.items():
                AppSetting.objects.update_or_create(
                    key=key,
                    defaults={
                        'value': value,
                        'value_type': settings_registry.REGISTRY[key].type,
                        'updated_by': request.user,
                    },
                )

        # The maintenance gate reads a cached copy on every request; clearing
        # it here is what makes flipping the switch take effect now rather
        # than whenever the TTL happens to lapse.
        cache.delete(MaintenanceModeMiddleware.CACHE_KEY)

        # `/app-config/` is a public projection of this store and is cached
        # too. Imported here rather than at module level: `appinfo` imports
        # this module's settings registry and permissions, so a top-level
        # import in this direction would be a cycle.
        from appinfo.services import invalidate as invalidate_public_config

        invalidate_public_config()

        audit.record(
            action=AuditAction.SETTINGS_UPDATE,
            entity='administration.AppSetting',
            summary=f'Changed {", ".join(sorted(changes))}',
            changes={'before': before, 'after': changes},
            request=request,
        )

        return Response({'settings': settings_registry.current()})


# ----------------------------------------------------------------- audit logs


AUDIT_QUERYSET = AuditLog.objects.select_related('actor')


class AuditLogListView(AdminView, ListAPIView):
    """The trail.

    **Query** `action` · `entity` · `entity_id` · `actor` · `date_from` ·
    `date_to` · `search` (summary, actor code, entity) · `ordering`

    **Responses** `200` · `401` · `403`
    """

    read_permission = 'view_audit_logs'
    write_permission = 'view_audit_logs'
    serializer_class = AuditLogSerializer

    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['summary', 'actor_code', 'entity', 'entity_id']
    ordering_fields = ['created_at', 'action', 'entity']
    ordering = ['-created_at']

    def get_queryset(self):
        queryset = AUDIT_QUERYSET.all()
        params = self.request.query_params

        for field in ('action', 'entity', 'entity_id'):
            value = params.get(field, '').strip()
            if value:
                queryset = queryset.filter(**{field: value})

        actor = params.get('actor', '').strip()
        if actor:
            queryset = queryset.filter(actor_id=actor)

        date_from = params.get('date_from', '').strip()
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)

        date_to = params.get('date_to', '').strip()
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        return queryset


class AuditLogDetailView(AdminView, RetrieveAPIView):
    """One entry.

    **Responses** `200` · `401` · `403` · `404`
    """

    read_permission = 'view_audit_logs'
    write_permission = 'view_audit_logs'
    serializer_class = AuditLogSerializer
    queryset = AUDIT_QUERYSET
