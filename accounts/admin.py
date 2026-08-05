"""Admin registration.

The stock UserAdmin is reused rather than replaced, so password hashing, the
"change password" link and permission widgets keep working — only the field
layout changes, because this user model has no username.
"""

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from .models import (
    Department,
    Designation,
    InviteRequest,
    Role,
    Territory,
    User,
    UserTerritory,
)


class SFMUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('employee_code', 'full_name', 'email', 'mobile')


class SFMUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = '__all__'


class UserTerritoryInline(admin.TabularInline):
    model = UserTerritory
    extra = 0
    autocomplete_fields = ['territory']
    fields = ('territory', 'is_primary', 'assigned_from', 'assigned_to')


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = SFMUserCreationForm
    form = SFMUserChangeForm
    model = User
    inlines = [UserTerritoryInline]

    list_display = (
        'employee_code',
        'full_name',
        'mobile',
        'email',
        'role',
        'department',
        'status',
        'is_active',
    )
    list_filter = ('status', 'is_active', 'is_staff', 'role', 'department', 'designation')
    search_fields = ('employee_code', 'full_name', 'email', 'mobile')
    ordering = ('full_name',)
    autocomplete_fields = ('reporting_manager', 'role', 'department', 'designation')
    readonly_fields = ('last_login', 'created_at', 'updated_at', 'manager_path')

    fieldsets = (
        (None, {'fields': ('employee_code', 'password')}),
        ('Personal', {'fields': ('full_name', 'email', 'mobile', 'profile_photo')}),
        (
            'Organisation',
            {'fields': ('role', 'department', 'designation', 'reporting_manager')},
        ),
        (
            'Access',
            {
                # is_active is derived from status in User.save(), so it is
                # shown for reference but not edited here.
                'fields': (
                    'status',
                    'must_change_password',
                    'is_staff',
                    'is_superuser',
                    'groups',
                    'user_permissions',
                )
            },
        ),
        ('Dates', {'fields': ('date_joined', 'last_login', 'created_at', 'updated_at')}),
        ('Derived', {'classes': ('collapse',), 'fields': ('manager_path',)}),
    )

    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': (
                    'employee_code',
                    'full_name',
                    'email',
                    'mobile',
                    'password1',
                    'password2',
                ),
            },
        ),
    )


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_system', 'permission_count', 'user_count')
    list_filter = ('is_system',)
    search_fields = ('name', 'code')
    readonly_fields = ('group', 'created_at', 'updated_at')

    @admin.display(description='permissions')
    def permission_count(self, obj):
        return len(obj.permission_codenames)

    @admin.display(description='users')
    def user_count(self, obj):
        return obj.users.count()

    def has_delete_permission(self, request, obj=None):
        # Mirrors the rule the mobile admin screen enforces: a system role is
        # never deletable, so no account can be left with nothing to assign.
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'code')


@admin.register(Designation)
class DesignationAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'level', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'code')


@admin.register(Territory)
class TerritoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'kind', 'parent', 'is_active')
    list_filter = ('kind', 'is_active')
    search_fields = ('name', 'code')
    autocomplete_fields = ('parent',)
    readonly_fields = ('path',)


@admin.register(InviteRequest)
class InviteRequestAdmin(admin.ModelAdmin):
    """The queue an administrator works through.

    Approving is the only way an account comes into existence from a request,
    and it happens here rather than through an API — there is no endpoint that
    turns a stranger into a user.
    """

    list_display = (
        'employee_code',
        'full_name',
        'email',
        'mobile',
        'status',
        'created_at',
        'reviewed_by',
    )
    list_filter = ('status', 'created_at')
    search_fields = ('employee_code', 'full_name', 'email', 'mobile')
    date_hierarchy = 'created_at'
    readonly_fields = (
        'full_name',
        'employee_code',
        'email',
        'mobile',
        'message',
        'status',
        'reviewed_by',
        'reviewed_at',
        'created_user',
        'created_at',
        'updated_at',
    )
    actions = ['approve_selected', 'reject_selected']

    def has_add_permission(self, request):
        # Requests arrive from the endpoint, not from this form.
        return False

    @admin.action(description='Approve — create an invited user')
    def approve_selected(self, request, queryset):
        created, skipped = 0, 0
        for invite in queryset:
            if not invite.is_pending:
                skipped += 1
                continue
            try:
                invite.approve(reviewed_by=request.user)
                created += 1
            except (ValidationError, IntegrityError) as exc:
                # One bad row must not stop the rest of the batch.
                self.message_user(
                    request,
                    f'{invite.employee_code}: {exc}',
                    level=messages.ERROR,
                )
        if created:
            self.message_user(
                request,
                f'{created} account{"" if created == 1 else "s"} created and '
                'invited.',
                level=messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                f'{skipped} request{"" if skipped == 1 else "s"} were already '
                'reviewed and were left alone.',
                level=messages.WARNING,
            )

    @admin.action(description='Reject')
    def reject_selected(self, request, queryset):
        rejected = 0
        for invite in queryset.filter(status=InviteRequest.Status.PENDING):
            invite.reject(reviewed_by=request.user)
            rejected += 1
        self.message_user(
            request,
            f'{rejected} request{"" if rejected == 1 else "s"} rejected.',
            level=messages.SUCCESS,
        )
