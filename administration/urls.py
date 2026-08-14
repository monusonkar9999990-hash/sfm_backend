"""Administration routes, mounted under /api/<version>/admin/.

Not to be confused with `/admin/`, which is Django's own admin site. This one
lives under the versioned API prefix.
"""

from django.urls import path

from .views import (
    ApproveInviteView,
    AuditLogDetailView,
    AuditLogListView,
    EmployeeDetailView,
    EmployeeListCreateView,
    EmployeeStatusView,
    InviteRequestListView,
    PermissionListView,
    RejectInviteView,
    RoleDetailView,
    RoleListCreateView,
    RolePermissionsView,
    SettingsView,
)

app_name = 'administration'

urlpatterns = [
    path('employees/', EmployeeListCreateView.as_view(), name='employee-list'),
    path('employees/<uuid:pk>/', EmployeeDetailView.as_view(), name='employee-detail'),
    path(
        'employees/<uuid:pk>/status/',
        EmployeeStatusView.as_view(),
        name='employee-status',
    ),

    path('roles/', RoleListCreateView.as_view(), name='role-list'),
    path('roles/<uuid:pk>/', RoleDetailView.as_view(), name='role-detail'),
    path(
        'roles/<uuid:pk>/permissions/',
        RolePermissionsView.as_view(),
        name='role-permissions',
    ),

    path('permissions/', PermissionListView.as_view(), name='permission-list'),

    path('invite-requests/', InviteRequestListView.as_view(), name='invite-list'),
    path(
        'invite-requests/<uuid:pk>/approve/',
        ApproveInviteView.as_view(),
        name='invite-approve',
    ),
    path(
        'invite-requests/<uuid:pk>/reject/',
        RejectInviteView.as_view(),
        name='invite-reject',
    ),

    path('settings/', SettingsView.as_view(), name='settings'),

    path('audit-logs/', AuditLogListView.as_view(), name='audit-list'),
    path('audit-logs/<uuid:pk>/', AuditLogDetailView.as_view(), name='audit-detail'),
]
