"""Who may use the administration API.

One class per capability, each naming a codename `accounts.BusinessPermission`
registers. Reads and writes are separated where they differ: a manager may
need to look up an employee without being able to suspend one.

Nothing here grants by role name. Roles are data — an administrator can create
a new one tonight — so a check against "is this user an Admin" would be a rule
this module could not keep true. Permissions are the contract; roles are how
they are bundled.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission


class HasBusinessPermission(BasePermission):
    """Grants when the user holds `required_permission` on the view.

    Same contract as the attendance, beat, site visit and customer modules —
    copied rather than imported, as those did, because a shared permissions
    package is a refactor for its own change rather than a dependency to
    introduce from the tenth module.
    """

    message = 'Your role does not allow this.'

    def has_permission(self, request, view):
        codename = getattr(view, 'required_permission', None)
        if codename is None:
            return True
        return bool(request.user and request.user.has_perm(f'accounts.{codename}'))


class SplitPermission(BasePermission):
    """Different codenames for reading and for writing.

    Set `read_permission` and `write_permission` on the view. Either may be
    None, meaning "any authenticated user".
    """

    message = 'Your role does not allow this.'

    def has_permission(self, request, view):
        codename = (
            getattr(view, 'read_permission', None)
            if request.method in SAFE_METHODS
            else getattr(view, 'write_permission', None)
        )
        if codename is None:
            return True

        allowed = bool(
            request.user and request.user.has_perm(f'accounts.{codename}')
        )
        if not allowed:
            self.message = (
                f'This needs the "{codename.replace("_", " ")}" permission, '
                f'which your role does not have.'
            )
        return allowed
