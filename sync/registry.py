"""Which endpoint handles which uploaded record, and what comes back down.

One entry per syncable entity. Adding a seventh module to offline sync means
adding an entry here — not writing another handler, another validator and
another set of tests for rules that already exist elsewhere.

Two things each entry has to say:

* **Up.** For `create`, `update` and `delete`, the view that does it and the
  URL kwargs it needs. Where an update is action-shaped rather than
  field-shaped — a punch is *checked out*, a beat is *started* — the actions
  are named, and the record's payload picks one with `"action"`.
* **Down.** The queryset the device is allowed to see, the serializer that
  renders it, and the joins to make so a page of fifty orders is not fifty-one
  queries.
"""

from dataclasses import dataclass, field
from typing import Callable

from attendance.models import Attendance
from attendance.serializers import AttendanceSerializer
from attendance.views import CheckInView as AttendanceCheckInView
from attendance.views import CheckOutView as AttendanceCheckOutView
from beats.models import BeatPlan, BeatPlanVisit
from beats.serializers import BeatPlanSerializer, BeatPlanVisitSerializer
from beats.views import (
    BeatPlanListCreateView,
    CompleteBeatView,
    MarkVisitedView,
    SkipVisitView,
    StartBeatView,
)
from customers.models import Customer
from customers.serializers import CustomerSerializer
from customers.views import CustomerDetailView, CustomerListCreateView
from orders.models import Order
from orders.serializers import OrderSerializer
from orders.views import (
    CancelOrderView,
    OrderDetailView,
    OrderListCreateView,
    SubmitOrderView,
)
from sitevisits.models import SiteVisit
from sitevisits.serializers import SiteVisitSerializer
from sitevisits.views import AddImageView, CancelVisitView
from sitevisits.views import CheckInView as VisitCheckInView
from sitevisits.views import CheckOutView as VisitCheckOutView


@dataclass(frozen=True)
class Route:
    """One endpoint, and how to reach it from a record's payload."""

    view: type
    method: str = 'post'
    path: str = '/'

    # Payload keys lifted into URL kwargs. `{'pk': 'server_id'}` means "take
    # the record's server_id and pass it as pk".
    url_kwargs: dict = field(default_factory=dict)

    # Payload keys the URL consumed, so they are not also sent in the body.
    strip: tuple = ()


@dataclass(frozen=True)
class Entity:
    key: str
    model: type
    download_serializer: type

    # How a row is tied to the person syncing. Every download is filtered by
    # this, so one device can never pull another's records.
    owner_field: str | None

    create: Route | None = None
    update: Route | None = None
    delete: Route | None = None

    # Named update actions, for the modules whose updates are verbs.
    actions: dict = field(default_factory=dict)

    select_related: tuple = ()
    prefetch_related: tuple = ()

    # Master data everyone shares: no owner column, so no per-user filter.
    shared: bool = False

    def scoped(self, user):
        queryset = self.model.objects.all()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        if self.shared or not self.owner_field:
            return queryset
        return queryset.filter(**{self.owner_field: user})


REGISTRY: dict[str, Entity] = {
    'attendance': Entity(
        key='attendance',
        model=Attendance,
        download_serializer=AttendanceSerializer,
        owner_field='user',
        create=Route(AttendanceCheckInView, path='/api/v1/attendance/check-in/'),
        actions={
            'check_out': Route(
                AttendanceCheckOutView, path='/api/v1/attendance/check-out/'
            ),
        },
        select_related=('user', 'punch_in_geofence', 'punch_out_geofence'),
    ),
    'beat_plans': Entity(
        key='beat_plans',
        model=BeatPlan,
        download_serializer=BeatPlanSerializer,
        owner_field='user',
        create=Route(BeatPlanListCreateView, path='/api/v1/beats/plans/'),
        actions={
            'start': Route(
                StartBeatView,
                path='/api/v1/beats/plans/{pk}/start/',
                url_kwargs={'pk': 'server_id'},
            ),
            'complete': Route(
                CompleteBeatView,
                path='/api/v1/beats/plans/{pk}/complete/',
                url_kwargs={'pk': 'server_id'},
            ),
        },
        select_related=('beat', 'user'),
        prefetch_related=('visits',),
    ),
    'beat_visits': Entity(
        key='beat_visits',
        model=BeatPlanVisit,
        download_serializer=BeatPlanVisitSerializer,
        # A stop belongs to its plan, which belongs to a person.
        owner_field='plan__user',
        actions={
            'visit': Route(
                MarkVisitedView,
                path='/api/v1/beats/plans/{pk}/visits/{visit_pk}/visit/',
                url_kwargs={'pk': 'plan_id', 'visit_pk': 'server_id'},
                strip=('plan_id',),
            ),
            'skip': Route(
                SkipVisitView,
                path='/api/v1/beats/plans/{pk}/visits/{visit_pk}/skip/',
                url_kwargs={'pk': 'plan_id', 'visit_pk': 'server_id'},
                strip=('plan_id',),
            ),
        },
        select_related=('plan',),
    ),
    'site_visits': Entity(
        key='site_visits',
        model=SiteVisit,
        download_serializer=SiteVisitSerializer,
        owner_field='user',
        create=Route(VisitCheckInView, path='/api/v1/site-visits/check-in/'),
        actions={
            'check_out': Route(
                VisitCheckOutView,
                path='/api/v1/site-visits/{pk}/check-out/',
                url_kwargs={'pk': 'server_id'},
            ),
            'cancel': Route(
                CancelVisitView,
                path='/api/v1/site-visits/{pk}/cancel/',
                url_kwargs={'pk': 'server_id'},
            ),
            # A site photo taken with no signal. Reaches the same endpoint an
            # online one does, carrying its image in the multipart batch — the
            # route was missing, so a queued photo had nowhere to go.
            'add_image': Route(
                AddImageView,
                path='/api/v1/site-visits/{pk}/images/',
                url_kwargs={'pk': 'server_id'},
            ),
        },
        select_related=('site', 'user'),
        prefetch_related=('images',),
    ),
    'customers': Entity(
        key='customers',
        model=Customer,
        download_serializer=CustomerSerializer,
        # Master data: everybody sells to the same book, so a device syncs the
        # whole of it rather than the slice it happened to create.
        owner_field=None,
        shared=True,
        create=Route(CustomerListCreateView, path='/api/v1/customers/'),
        # An edit made in the field — a corrected phone number, a new credit
        # limit — reaches the same detail endpoint it would online. That view
        # and its permissions already existed; only this line was missing,
        # which is why an offline customer edit had nowhere to go.
        update=Route(
            CustomerDetailView,
            method='patch',
            path='/api/v1/customers/{pk}/',
            url_kwargs={'pk': 'server_id'},
        ),
    ),
    'orders': Entity(
        key='orders',
        model=Order,
        download_serializer=OrderSerializer,
        owner_field='employee',
        create=Route(OrderListCreateView, path='/api/v1/orders/'),
        update=Route(
            OrderDetailView,
            method='patch',
            path='/api/v1/orders/{pk}/',
            url_kwargs={'pk': 'server_id'},
        ),
        delete=Route(
            OrderDetailView,
            method='delete',
            path='/api/v1/orders/{pk}/',
            url_kwargs={'pk': 'server_id'},
        ),
        actions={
            'submit': Route(
                SubmitOrderView,
                path='/api/v1/orders/{pk}/submit/',
                url_kwargs={'pk': 'server_id'},
            ),
            'cancel': Route(
                CancelOrderView,
                path='/api/v1/orders/{pk}/cancel/',
                url_kwargs={'pk': 'server_id'},
            ),
        },
        select_related=('customer', 'employee'),
        prefetch_related=('items__product',),
    ),
}

SUPPORTED_ENTITIES = tuple(REGISTRY)


def entity_for(entity_type):
    return REGISTRY.get(entity_type)


def describe():
    """What `/sync/status/` publishes, so a client can find out what it may
    send without a release note."""
    return [
        {
            'entity_type': key,
            'operations': sorted(
                op
                for op, route in (
                    ('create', entity.create),
                    ('update', entity.update),
                    ('delete', entity.delete),
                )
                if route is not None
            ),
            'actions': sorted(entity.actions),
            'shared': entity.shared,
        }
        for key, entity in REGISTRY.items()
    ]
