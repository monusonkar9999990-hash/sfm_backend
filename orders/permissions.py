"""Permission classes for the order endpoints.

No new codenames are introduced. Three that `accounts.BusinessPermission`
already registers carry the whole module:

* `place_orders`      — may raise an order at all
* `view_team_reports` — may see orders other than their own
* `cancel_orders`     — the manager-level override: may cancel a submitted
                        order, and may amend one after it has left draft

That last mapping is worth stating plainly, because the requirement says
"except by authorized users" without naming them. `cancel_orders` is the
closest existing permission that means "may overrule an order's normal
lifecycle", so it is the one used. Changing it later is a one-line edit here
rather than a hunt through the views.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import OrderStatus


class CanPlaceOrders(BasePermission):
    """Reading is open to any signed-in user; the queryset does the narrowing.

    Which orders a person can see is decided by `OrderQuerysetMixin`, not
    here — a permission class that returned False for a read would give a 403
    where an empty list is the truthful answer.
    """

    message = 'Your role does not allow raising orders.'

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.has_perm('accounts.place_orders'))


class CanActOnOrder(BasePermission):
    """Object-level rules for changing one order.

    * A draft belongs to whoever raised it: they may edit, delete and submit.
    * Once submitted, only `cancel_orders` may touch it.
    * Cancelled and completed orders are history — nobody edits them, with or
      without a permission. That is the one rule no role overrides, because an
      order that can change after it was cancelled is not a record of
      anything.
    """

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            # Visibility is the queryset's job; by the time an object is in
            # hand it has already been scoped.
            return True

        user = request.user
        is_owner = obj.employee_id == user.id
        is_manager = user.has_perm('accounts.cancel_orders')

        if obj.is_terminal:
            self.message = (
                f'A {obj.get_status_display().lower()} order cannot be '
                f'changed.'
            )
            return False

        if obj.status == OrderStatus.SUBMITTED and not is_manager:
            self.message = (
                'This order has been submitted. Ask a manager to amend it.'
            )
            return False

        if not is_owner and not is_manager:
            self.message = 'This order belongs to another user.'
            return False

        return True


class CanSubmitOrder(BasePermission):
    """Ownership only — whether the move itself is legal is a 400, not a 403.

    Submitting is a state transition, and "this order was already submitted"
    is a fact about the order, not about the caller's rights. Routed through
    `CanActOnOrder` it came back as 403 "ask a manager to amend it", which
    answers a question nobody asked. The serializer says what is wrong with
    the transition; this class only answers whether the order is theirs.
    """

    message = 'This order belongs to another user.'

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True

        user = request.user
        return obj.employee_id == user.id or user.has_perm(
            'accounts.cancel_orders'
        )


class OrderQuerysetMixin:
    """Scopes the queryset to what the signed-in user may see.

    Their own orders, unless they hold `view_team_reports` — in which case
    the whole table, which is what a manager's figures are made of.
    """

    def scoped_orders(self, queryset):
        user = self.request.user
        if user.has_perm('accounts.view_team_reports'):
            return queryset
        return queryset.filter(employee=user)
