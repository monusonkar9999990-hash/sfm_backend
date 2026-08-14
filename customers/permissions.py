"""Permission classes for the customer endpoints."""

from rest_framework.permissions import BasePermission


class HasBusinessPermission(BasePermission):
    """Grants access when the user holds `required_permission` on the view.

    Same contract as the attendance, beat and site visit modules: the codename
    is one of the SFM permissions registered by `accounts.BusinessPermission`.
    Copied rather than imported, as in those modules — a shared permissions
    package is a refactor for the day a fifth module wants it, not a
    dependency to introduce from the fourth.
    """

    message = 'Your role does not allow this action.'

    def has_permission(self, request, view):
        codename = getattr(view, 'required_permission', None)
        if codename is None:
            return True
        return bool(request.user and request.user.has_perm(f'accounts.{codename}'))
