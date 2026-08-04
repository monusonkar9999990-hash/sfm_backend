"""Permission classes for the beat endpoints."""

from rest_framework.permissions import BasePermission


class HasBusinessPermission(BasePermission):
    """Grants access when the user holds `required_permission` on the view.

    Same contract as the attendance module: the codename is one of the SFM
    permissions registered by `accounts.BusinessPermission`, so a role edited
    in the admin takes effect everywhere at once.
    """

    message = 'Your role does not allow this action.'

    def has_permission(self, request, view):
        codename = getattr(view, 'required_permission', None)
        if codename is None:
            return True
        return bool(request.user and request.user.has_perm(f'accounts.{codename}'))


class IsPlanOwner(BasePermission):
    """Only the executive who owns a plan may read or run it.

    A supervisor's view of the team is a separate reporting endpoint with its
    own scoping rules, so there is no read exemption here.
    """

    message = 'This beat plan belongs to another user.'

    def has_object_permission(self, request, view, obj):
        # Accepts a plan or a visit, since the visit actions look the owner up
        # through the plan they hang off.
        owner_id = getattr(obj, 'user_id', None) or obj.plan.user_id
        return owner_id == request.user.id
