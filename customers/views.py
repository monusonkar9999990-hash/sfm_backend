"""Customer endpoints, mounted under /api/<version>/customers/."""

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Customer
from .permissions import HasBusinessPermission
from .serializers import (
    CustomerCreateSerializer,
    CustomerSerializer,
    CustomerUpdateSerializer,
)


class CustomerListCreateView(ListCreateAPIView):
    """The customers on the books, and the way to add one.

    **Query** `search` matches the name, code, contact person, phone or city.
    `type` filters to one of dealer, distributor, retailer, contractor or
    architect.

    Reads are open to any signed-in user — a field executive picking a
    customer for an order needs the whole list, not their own entries. Writing
    needs `onboard_customers`.

    **Responses**
    * `200` — a paginated list
    * `201` — the customer, in the same shape a list entry has
    * `400` — a field failed validation, keyed by field name
    * `401` — missing or invalid access token
    * `403` — the role does not allow onboarding
    * `409` — the name, phone or GSTIN is already registered
    """

    permission_classes = [IsAuthenticated, HasBusinessPermission]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return CustomerCreateSerializer
        return CustomerSerializer

    @property
    def required_permission(self):
        # Only the write needs the permission; gating the read as well would
        # stop an executive from choosing who an order is for.
        return 'onboard_customers' if self.request.method == 'POST' else None

    def get_queryset(self):
        queryset = Customer.objects.filter(is_active=True)

        search = self.request.query_params.get('search', '').strip()
        if search:
            from django.db.models import Q

            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(code__icontains=search)
                | Q(contact_person__icontains=search)
                | Q(phone__icontains=search)
                | Q(city__icontains=search)
            )

        customer_type = self.request.query_params.get('type', '').strip()
        if customer_type:
            queryset = queryset.filter(type=customer_type)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            # The save gets its own savepoint. Without one, an IntegrityError
            # leaves the surrounding transaction unusable — every later query,
            # including the one that renders this error, fails with
            # "You can't execute queries until the end of the 'atomic' block".
            with transaction.atomic():
                customer = serializer.save()
        except IntegrityError:
            # The serializer checks for duplicates, but two registrations of
            # the same shop can arrive between that check and this write. The
            # database has the last word; this turns its error into an answer.
            return Response(
                {'detail': 'This customer is already registered.'},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            CustomerSerializer(customer).data, status=status.HTTP_201_CREATED
        )


class CustomerDetailView(RetrieveUpdateAPIView):
    """One customer, and the way to correct one.

    A `PATCH` writes only the fields it was sent. That is what makes an edit
    made in the field safe to replay later: the phone number somebody fixed on
    a device with no signal goes up on its own, without carrying back a copy
    of the rest of the record as it stood that morning.

    Reading is open to any signed-in user, the same as the list. Writing needs
    `onboard_customers` — the permission that governs putting a customer on
    the books governs changing one.

    **Responses**
    * `200` — the customer
    * `400` — a field failed validation, keyed by field name
    * `401` — missing or invalid access token
    * `403` — the role does not allow onboarding
    * `404` — no such customer, or it has been deactivated
    * `409` — those details belong to another customer
    """

    permission_classes = [IsAuthenticated, HasBusinessPermission]
    queryset = Customer.objects.filter(is_active=True)

    def get_serializer_class(self):
        if self.request.method in ('PATCH', 'PUT'):
            return CustomerUpdateSerializer
        return CustomerSerializer

    @property
    def required_permission(self):
        return None if self.request.method in ('GET', 'HEAD') else 'onboard_customers'

    def update(self, request, *args, **kwargs):
        serializer = self.get_serializer(
            self.get_object(), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)

        try:
            # Its own savepoint, for the reason the create has one: a race on
            # the name/city constraint must come back as an answer, not leave
            # the transaction unusable.
            with transaction.atomic():
                customer = serializer.save()
        except IntegrityError:
            return Response(
                {'detail': 'Another customer is already registered on those details.'},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(CustomerSerializer(customer).data)
