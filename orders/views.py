"""Order endpoints, mounted under /api/<version>/orders/."""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.generics import (
    GenericAPIView,
    ListCreateAPIView,
    RetrieveUpdateDestroyAPIView,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Order, OrderStatus
from .permissions import (
    CanActOnOrder,
    CanPlaceOrders,
    CanSubmitOrder,
    OrderQuerysetMixin,
)
from .serializers import (
    CancelOrderSerializer,
    OrderSerializer,
    OrderWriteSerializer,
    SubmitOrderSerializer,
)

# Every read joins the customer and the employee and pulls the lines in one
# more query, rather than one per order per line.
ORDER_QUERYSET = (
    Order.objects.select_related('customer', 'employee')
    .prefetch_related('items__product')
)


class OrderPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class TotalAwareOrderingFilter(OrderingFilter):
    """Accepts `?ordering=total` for the grand total.

    "Order by total amount" is the requirement; `grand_total` is the column.
    Both work, the same way `price` works on the catalogue.
    """

    aliases = {'total': 'grand_total', '-total': '-grand_total'}

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering
        return [self.aliases.get(field, field) for field in ordering]

    def remove_invalid_fields(self, queryset, fields, view, request):
        translated = [self.aliases.get(field, field) for field in fields]
        return super().remove_invalid_fields(queryset, translated, view, request)


class OrderListCreateView(OrderQuerysetMixin, ListCreateAPIView):
    """The orders this user may see, and the way to raise one.

    **Query**
    * `search` — matches the order number, the customer's name or their code
    * `status` — draft, submitted, cancelled, completed
    * `employee` — a user id; ignored unless the caller may see other people's
      orders, since otherwise there is nothing to narrow
    * `customer` — a customer id
    * `date_from`, `date_to` — inclusive, on `order_date` (`YYYY-MM-DD`)
    * `ordering` — `order_date`, `total` (or `grand_total`), `created_at`,
      `order_number`, each with `-` for descending
    * `page`, `page_size` — page size caps at 100

    A field executive sees their own orders. Holding `view_team_reports`
    widens that to the whole team.

    **Responses**
    * `200` — a paginated list
    * `201` — the order, priced, in the shape a list entry has
    * `400` — a field failed validation, keyed by field name
    * `401` — missing or invalid access token
    * `403` — the role does not allow raising orders
    """

    permission_classes = [IsAuthenticated, CanPlaceOrders]
    pagination_class = OrderPagination
    filter_backends = [SearchFilter, TotalAwareOrderingFilter]

    search_fields = ['order_number', 'customer__name', 'customer__code']
    ordering_fields = ['order_date', 'grand_total', 'created_at', 'order_number']
    ordering = ['-order_date', '-created_at']

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return OrderWriteSerializer
        return OrderSerializer

    def get_queryset(self):
        queryset = self.scoped_orders(ORDER_QUERYSET)
        params = self.request.query_params

        status_filter = params.get('status', '').strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        employee = params.get('employee', '').strip()
        if employee:
            queryset = queryset.filter(employee_id=employee)

        customer = params.get('customer', '').strip()
        if customer:
            queryset = queryset.filter(customer_id=customer)

        date_from = params.get('date_from', '').strip()
        if date_from:
            queryset = queryset.filter(order_date__gte=date_from)

        date_to = params.get('date_to', '').strip()
        if date_to:
            queryset = queryset.filter(order_date__lte=date_to)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # The serializer's `create` is already atomic; this keeps the whole
        # request in one transaction so nothing added here later escapes it.
        with transaction.atomic():
            order = serializer.save()

        return Response(
            OrderSerializer(order).data, status=status.HTTP_201_CREATED
        )


class OrderDetailView(OrderQuerysetMixin, RetrieveUpdateDestroyAPIView):
    """One order: read it, amend it while it is a draft, or discard it.

    **DELETE discards a draft.** Only a draft — an order that has been
    submitted is a commitment somebody downstream may already be acting on, so
    it is cancelled with a reason rather than deleted without one. A submitted
    order returns `409` here and points at /cancel/.

    **Responses**
    * `200` — the order (GET, PUT, PATCH)
    * `204` — the draft was discarded
    * `400` — a field failed validation, keyed by field name
    * `401` — missing or invalid access token
    * `403` — not this user's order, or it has left draft
    * `404` — no such order, or not one this user may see
    * `409` — a submitted order cannot be deleted
    """

    permission_classes = [IsAuthenticated, CanPlaceOrders, CanActOnOrder]

    def get_queryset(self):
        return self.scoped_orders(ORDER_QUERYSET)

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return OrderWriteSerializer
        return OrderSerializer

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        order = self.get_object()

        serializer = self.get_serializer(order, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            order = serializer.save()

        return Response(OrderSerializer(order).data)

    def destroy(self, request, *args, **kwargs):
        order = self.get_object()

        if order.status != OrderStatus.DRAFT:
            return Response(
                {
                    'detail': (
                        'Only a draft can be deleted. Cancel this order '
                        'instead, with a reason.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            order.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class SubmitOrderView(OrderQuerysetMixin, GenericAPIView):
    """Books the order.

    Totals are recomputed on the way through rather than trusted from the
    draft: a product's rate may have moved since the draft was saved, and the
    figure that matters is the one at the moment of booking.

    **Responses**
    * `200` — the submitted order
    * `400` — empty, or not in a state that can be submitted, including one
      that has been submitted already
    * `401` — missing or invalid access token
    * `403` — not this user's order
    * `404` — no such order
    """

    # Deliberately not `CanActOnOrder`: that class refuses any write to an
    # order past draft, which turns a re-submit into a 403 about amending.
    # Whether the transition is legal is the serializer's answer, and a 400.
    permission_classes = [IsAuthenticated, CanPlaceOrders, CanSubmitOrder]
    serializer_class = SubmitOrderSerializer

    def get_queryset(self):
        return self.scoped_orders(ORDER_QUERYSET)

    def post(self, request, *args, **kwargs):
        order = self.get_object()

        serializer = self.get_serializer(
            data=request.data, context={'order': order, 'request': request}
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            order.recalculate(save=False)
            order.status = OrderStatus.SUBMITTED
            order.submitted_at = timezone.now()
            order.save()

        return Response(OrderSerializer(order).data)


class CancelOrderView(OrderQuerysetMixin, GenericAPIView):
    """Cancels the order, with a reason.

    A draft may be cancelled by whoever raised it. A submitted order needs
    `cancel_orders` — by then it is a commitment, not a note to self.

    **Responses**
    * `200` — the cancelled order
    * `400` — no reason given, or already cancelled or completed
    * `401` — missing or invalid access token
    * `403` — a submitted order, and the caller is not a manager
    * `404` — no such order
    """

    permission_classes = [IsAuthenticated, CanPlaceOrders, CanActOnOrder]
    serializer_class = CancelOrderSerializer

    def get_queryset(self):
        return self.scoped_orders(ORDER_QUERYSET)

    def post(self, request, *args, **kwargs):
        order = self.get_object()

        if order.is_terminal:
            return Response(
                {
                    'detail': (
                        f'This order is already '
                        f'{order.get_status_display().lower()}.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = timezone.now()
            order.cancellation_reason = serializer.validated_data['reason']
            order.save()

        return Response(OrderSerializer(order).data)
