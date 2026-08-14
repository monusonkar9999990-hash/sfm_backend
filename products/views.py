"""Product endpoints, mounted under /api/<version>/products/."""

from django.db import transaction
from rest_framework import status
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Product
from .permissions import CanEditCatalogue
from .serializers import ProductSerializer, ProductWriteSerializer


class CataloguePagination(PageNumberPagination):
    """The project default of 20, with a page size the caller can raise.

    Declared here rather than in settings so no other module's page size
    changes: a catalogue is browsed and searched, an attendance history is
    scrolled, and they do not want the same page.
    """

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class PriceAwareOrderingFilter(OrderingFilter):
    """Accepts `?ordering=price` as a synonym for the selling price.

    "Order by price" is what the requirement says and what a caller will try
    first; `selling_price` is what the column is called. Both work.
    """

    aliases = {'price': 'selling_price', '-price': '-selling_price'}

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering
        return [self.aliases.get(field, field) for field in ordering]

    def remove_invalid_fields(self, queryset, fields, view, request):
        # Translate before validating, or `price` is dropped as unknown before
        # `get_ordering` ever sees it.
        translated = [self.aliases.get(field, field) for field in fields]
        return super().remove_invalid_fields(queryset, translated, view, request)


class ProductListCreateView(ListCreateAPIView):
    """The catalogue, and the way to add to it.

    **Query**
    * `search` — matches the name, product code or brand
    * `category` — one of cement, steel, paint, adhesive, tiles, plumbing,
      electrical, other
    * `active` — `true` or `false`; omitted, both are returned
    * `ordering` — `name`, `price` (or `selling_price`), `mrp`, `created_at`,
      each with a `-` prefix for descending
    * `page`, `page_size` — page size caps at 100

    **Responses**
    * `200` — a paginated list
    * `201` — the product, in the shape a list entry has
    * `400` — a field failed validation, keyed by field name
    * `401` — missing or invalid access token
    * `403` — the role does not allow editing the catalogue
    """

    permission_classes = [IsAuthenticated, CanEditCatalogue]
    pagination_class = CataloguePagination
    filter_backends = [SearchFilter, PriceAwareOrderingFilter]

    search_fields = ['name', 'product_code', 'brand']
    ordering_fields = ['name', 'selling_price', 'mrp', 'created_at', 'stock_quantity']
    ordering = ['name']

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return ProductWriteSerializer
        return ProductSerializer

    def get_queryset(self):
        queryset = Product.objects.all()

        category = self.request.query_params.get('category', '').strip()
        if category:
            queryset = queryset.filter(category=category)

        active = self.request.query_params.get('active', '').strip().lower()
        if active in ('true', '1'):
            queryset = queryset.filter(active=True)
        elif active in ('false', '0'):
            queryset = queryset.filter(active=False)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # One statement, one transaction. A product is a single row today, but
        # this is the seam a price-history or stock-ledger write lands in, and
        # a half-written catalogue entry is worse than a refused one.
        with transaction.atomic():
            product = serializer.save()

        return Response(
            ProductSerializer(product).data, status=status.HTTP_201_CREATED
        )


class ProductDetailView(RetrieveUpdateDestroyAPIView):
    """One product: read it, change it, or withdraw it.

    **DELETE is a withdrawal, not an erasure.** The row is marked inactive and
    disappears from `?active=true`, but it is still there — an order raised
    last month names this product, and deleting the row would either cascade
    into that order's history or fail against its foreign key. Both are worse
    than a flag. The response is still `204 No Content`.

    **Responses**
    * `200` — the product (GET, PUT, PATCH)
    * `204` — withdrawn
    * `400` — a field failed validation, keyed by field name
    * `401` — missing or invalid access token
    * `403` — the role does not allow editing the catalogue
    * `404` — no such product
    """

    permission_classes = [IsAuthenticated, CanEditCatalogue]
    queryset = Product.objects.all()

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return ProductWriteSerializer
        return ProductSerializer

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        product = self.get_object()

        serializer = self.get_serializer(product, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            product = serializer.save()

        return Response(ProductSerializer(product).data)

    def destroy(self, request, *args, **kwargs):
        product = self.get_object()

        with transaction.atomic():
            product.active = False
            product.save(update_fields=['active', 'updated_at'])

        return Response(status=status.HTTP_204_NO_CONTENT)
