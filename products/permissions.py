"""Permission classes for the product endpoints."""

from rest_framework.permissions import SAFE_METHODS, BasePermission


class CanEditCatalogue(BasePermission):
    """Anyone signed in may read the catalogue; editing needs the permission.

    A field executive has to see every product to raise an order, so gating
    the read would break the order screens for the people the app is for.
    Changing a rate is master data, which is what `edit_master_data` exists
    for — one of the codenames `accounts.BusinessPermission` already
    registers, so no new permission is introduced here.
    """

    message = 'Your role does not allow changes to the product catalogue.'

    required_permission = 'edit_master_data'

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        codename = getattr(view, 'required_permission', self.required_permission)
        return bool(request.user and request.user.has_perm(f'accounts.{codename}'))
