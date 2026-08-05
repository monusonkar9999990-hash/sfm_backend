"""Permission classes for the site visit endpoints."""

from rest_framework.permissions import BasePermission


class HasBusinessPermission(BasePermission):
    """Grants access when the user holds `required_permission` on the view.

    Same contract as the attendance and beat modules: the codename is one of
    the SFM permissions registered by `accounts.BusinessPermission`.
    """

    message = 'Your role does not allow this action.'

    def has_permission(self, request, view):
        codename = getattr(view, 'required_permission', None)
        if codename is None:
            return True
        return bool(request.user and request.user.has_perm(f'accounts.{codename}'))


class IsVisitOwner(BasePermission):
    """A visit belongs to the person who made it.

    A supervisor reads the team's visits through the reporting endpoints,
    which apply their own scoping — there is no read exemption here.
    """

    message = 'This visit belongs to another user.'

    def has_object_permission(self, request, view, obj):
        return obj.user_id == request.user.id
