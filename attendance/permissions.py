"""Permission classes for the attendance endpoints.

The codename checked here is one of the fifteen SFM permissions registered by
`accounts.BusinessPermission`, so a role granted `mark_attendance` in the
admin immediately unlocks these endpoints — no second permission system.
"""

from rest_framework.permissions import BasePermission


class HasBusinessPermission(BasePermission):
    """Grants access when the user holds `required_permission` on the view."""

    message = 'Your role does not allow this action.'

    def has_permission(self, request, view):
        codename = getattr(view, 'required_permission', None)
        if codename is None:
            return True
        return bool(request.user and request.user.has_perm(f'accounts.{codename}'))


class IsOwnRecord(BasePermission):
    """A user may only read their own attendance.

    Supervisors read a team's attendance through the reporting endpoints,
    which apply their own scoping — this class is deliberately narrow.
    """

    def has_object_permission(self, request, view, obj):
        return obj.user_id == request.user.id
