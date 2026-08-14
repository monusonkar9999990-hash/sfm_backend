"""What counts as "something changed".

One receiver per module that the portal reports on, each naming the person the
record belongs to so a field executive watching their own figures is told about
their own work and nothing else.

Attached with `dispatch_uid` so a double import — which happens under the
autoreloader — cannot register the same receiver twice and send every event
twice.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from attendance.models import Attendance
from beats.models import BeatPlan
from customers.models import Customer
from orders.models import Order
from sitevisits.models import SiteVisit

from .server import notify


@receiver(post_save, sender=Attendance, dispatch_uid='realtime.attendance')
def attendance_changed(sender, instance, created, **kwargs):
    notify(
        'attendance',
        owner_id=instance.user_id,
        action='created' if created else 'updated',
    )


@receiver(post_save, sender=SiteVisit, dispatch_uid='realtime.sitevisit')
def site_visit_changed(sender, instance, created, **kwargs):
    notify(
        'site_visits',
        owner_id=instance.user_id,
        action='created' if created else 'updated',
    )


@receiver(post_save, sender=Order, dispatch_uid='realtime.order')
def order_changed(sender, instance, created, **kwargs):
    notify(
        'orders',
        owner_id=instance.employee_id,
        action='created' if created else 'updated',
    )


@receiver(post_save, sender=Customer, dispatch_uid='realtime.customer')
def customer_changed(sender, instance, created, **kwargs):
    # `created_by` is who onboarded them, and is null for anything seeded or
    # written from the admin — a broadcast to the team room still goes out.
    notify(
        'customers',
        owner_id=getattr(instance, 'created_by_id', None),
        action='created' if created else 'updated',
    )


@receiver(post_save, sender=BeatPlan, dispatch_uid='realtime.beatplan')
def beat_plan_changed(sender, instance, created, **kwargs):
    notify(
        'beats',
        owner_id=instance.user_id,
        action='created' if created else 'updated',
    )


@receiver(post_delete, sender=BeatPlan, dispatch_uid='realtime.beatplan.delete')
def beat_plan_removed(sender, instance, **kwargs):
    # A plan pulled off somebody's day changes the coverage figures as surely
    # as one added to it, and the delete is the only signal that says so.
    notify('beats', owner_id=instance.user_id, action='deleted')
