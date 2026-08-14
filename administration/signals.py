"""Audit receivers.

Attached to the models worth a trail rather than to the views that change
them, which is what lets nine finished modules gain auditing without a line
changing in any of them. A row written by the API, by the Django admin or by a
management command lands in the same table.

`update_fields` is used where the caller supplied it, so an audit row can say
*what* changed rather than only that something did. Django does not diff for
you, and re-reading every row before every save to produce a true before/after
would double the write cost of the whole application — a price worth paying
for a bank ledger and not for a beat plan.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from accounts.models import InviteRequest, Role, User
from customers.models import Customer
from orders.models import Order
from products.models import Product

from . import audit
from .models import AuditAction, AuditLog

# Every model whose changes are recorded. Adding one is a line here.
AUDITED = (User, Role, Product, Customer, Order, InviteRequest)


def _summary(instance, created):
    verb = 'Created' if created else 'Updated'
    return f'{verb} {audit.label_for(instance)} {instance}'[:255]


@receiver(post_save)
def record_save(sender, instance, created, **kwargs):
    if sender not in AUDITED:
        return

    # No request means no actor and no address — a fixture, a shell session, a
    # migration. See the note in `audit.record`.
    if audit.current_request() is None:
        return

    audit.record(
        action=AuditAction.CREATE if created else AuditAction.UPDATE,
        entity=audit.label_for(instance),
        entity_id=instance.pk,
        summary=_summary(instance, created),
        changes={'fields': sorted(kwargs.get('update_fields') or [])} or {},
    )


@receiver(post_delete)
def record_delete(sender, instance, **kwargs):
    if sender not in AUDITED:
        return

    if audit.current_request() is None:
        return

    audit.record(
        action=AuditAction.DELETE,
        entity=audit.label_for(instance),
        entity_id=instance.pk,
        summary=f'Deleted {audit.label_for(instance)} {instance}'[:255],
    )


@receiver(post_save, sender=AuditLog)
def never_audit_the_audit(sender, instance, **kwargs):
    """A guard with no body, and a name that is the documentation.

    `AuditLog` is not in `AUDITED`, so writing one cannot trigger another. If
    somebody adds it, this receiver is where the infinite loop will be
    explained.
    """
    return
