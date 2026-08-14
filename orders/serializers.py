"""Serializers for the order endpoints.

The rule that shapes this whole file: the client says *what* was ordered —
which customer, which products, how many, what was knocked off — and the
server decides what it *costs*. Rates come off the catalogue, totals are
computed, and any figure the client sends for them is ignored rather than
rejected, because rejecting it would only teach callers to stop sending it.
"""

from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from customers.models import Customer
from products.models import Product

from .models import Order, OrderItem, OrderStatus, money


class OrderItemReadSerializer(serializers.ModelSerializer):
    """One line, priced.

    Carries the product's name, code and unit alongside the id so an order can
    be rendered without a second request. Those three are read through the
    foreign key rather than frozen onto the line: the money is frozen, which
    is what `unit_price` and `gst_percent` are for, but a product renamed for
    clarity should read the new way everywhere.
    """

    product_id = serializers.UUIDField(source='product.id', read_only=True)
    title = serializers.CharField(source='product.name', read_only=True)
    sku = serializers.CharField(source='product.product_code', read_only=True)
    unit = serializers.CharField(source='product.unit', read_only=True)

    unit_price = serializers.FloatField(read_only=True)
    discount = serializers.FloatField(read_only=True)
    gst_percent = serializers.FloatField(read_only=True)
    gst_rate = serializers.FloatField(source='gst_percent', read_only=True)
    line_total = serializers.FloatField(read_only=True)

    gross = serializers.FloatField(read_only=True)
    taxable = serializers.FloatField(read_only=True)
    gst_amount = serializers.FloatField(read_only=True)

    class Meta:
        model = OrderItem
        fields = (
            'id',
            'product',
            'product_id',
            'title',
            'sku',
            'unit',
            'quantity',
            'unit_price',
            'discount',
            'gst_percent',
            'gst_rate',
            'gross',
            'taxable',
            'gst_amount',
            'line_total',
        )
        read_only_fields = fields


class OrderItemWriteSerializer(serializers.Serializer):
    """What the client is allowed to say about a line.

    Three things: which product, how many, and what was knocked off. The rate
    and the tax are not on this list — they come from the catalogue.
    """

    product = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)
    discount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=Decimal('0.00'),
        min_value=Decimal('0'),
    )

    def validate_product(self, value):
        product = Product.objects.filter(pk=value).first()
        if product is None:
            raise serializers.ValidationError('No such product.')
        if not product.active:
            raise serializers.ValidationError(
                f'{product.name} has been withdrawn from the catalogue.'
            )
        return product

    def validate(self, attrs):
        product = attrs['product']
        gross = money(Decimal(attrs['quantity']) * product.selling_price)
        discount = attrs.get('discount') or Decimal('0.00')

        if discount > gross:
            raise serializers.ValidationError(
                {
                    'discount': (
                        f'A discount of {discount} is more than the line is '
                        f'worth ({gross}).'
                    )
                }
            )

        return attrs


class OrderSerializer(serializers.ModelSerializer):
    """An order, priced and ready to render."""

    items = OrderItemReadSerializer(many=True, read_only=True)

    customer_id = serializers.UUIDField(source='customer.id', read_only=True)
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_code = serializers.CharField(source='customer.code', read_only=True)

    employee_id = serializers.UUIDField(source='employee.id', read_only=True)
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    employee_code = serializers.CharField(
        source='employee.employee_code', read_only=True
    )

    # Numbers, not DRF's default decimal-as-string. The client reads these
    # with `as num?`, and this project has already been bitten once by a
    # string arriving where a number was expected.
    subtotal = serializers.FloatField(read_only=True)
    discount_total = serializers.FloatField(read_only=True)
    gst_total = serializers.FloatField(read_only=True)
    grand_total = serializers.FloatField(read_only=True)

    item_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id',
            'order_number',
            'customer',
            'customer_id',
            'customer_name',
            'customer_code',
            'employee',
            'employee_id',
            'employee_name',
            'employee_code',
            'status',
            'order_date',
            'remarks',
            'items',
            'item_count',
            'subtotal',
            'discount_total',
            'gst_total',
            'grand_total',
            'submitted_at',
            'cancelled_at',
            'cancellation_reason',
            'created_at',
            'updated_at',
        )
        read_only_fields = fields

    def get_item_count(self, obj) -> int:
        return obj.items.count()


class OrderWriteSerializer(serializers.ModelSerializer):
    """Creating and replacing an order.

    `status` is absent on purpose. An order moves between states through
    /submit/ and /cancel/, which check what the move is allowed to be — a
    settable status field would route straight around them.
    """

    customer = serializers.UUIDField()
    items = OrderItemWriteSerializer(many=True)

    class Meta:
        model = Order
        fields = ('customer', 'order_date', 'remarks', 'items')

    def validate_customer(self, value):
        customer = Customer.objects.filter(pk=value, is_active=True).first()
        if customer is None:
            raise serializers.ValidationError(
                'No such customer — register the customer first.'
            )
        return customer

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError(
                'An order needs at least one product on it.'
            )

        # Caught here rather than by the unique constraint, so the caller gets
        # a sentence naming the product instead of a database error.
        seen = set()
        for line in value:
            product = line['product']
            if product.pk in seen:
                raise serializers.ValidationError(
                    f'{product.name} is on this order twice — send one line '
                    f'with the total quantity instead.'
                )
            seen.add(product.pk)

        return value

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop('items')
        customer = validated_data.pop('customer')

        order = Order.objects.create(
            customer=customer,
            employee=self.context['request'].user,
            **validated_data,
        )
        self._write_items(order, items)
        return order.recalculate()

    @transaction.atomic
    def update(self, instance, validated_data):
        items = validated_data.pop('items', None)

        if 'customer' in validated_data:
            instance.customer = validated_data.pop('customer')
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()

        if items is not None:
            # Replaced wholesale rather than reconciled line by line. An order
            # is small, and "these are the lines now" is a rule anyone can
            # hold in their head — a merge is where off-by-one bugs live.
            instance.items.all().delete()
            self._write_items(instance, items)

        return instance.recalculate()

    @staticmethod
    def _write_items(order, items):
        """Writes the lines at catalogue rates.

        Inside the caller's transaction: if the fourth line of a five-line
        order fails, the first three go back with it and no half-order is left
        behind.
        """
        for line in items:
            product = line['product']
            OrderItem.objects.create(
                order=order,
                product=product,
                quantity=line['quantity'],
                # The two figures the client does not get to choose.
                unit_price=product.selling_price,
                gst_percent=product.gst_percent,
                discount=line.get('discount') or Decimal('0.00'),
            )


class CancelOrderSerializer(serializers.Serializer):
    """Why the order was cancelled.

    Required, because an unexplained cancellation tells whoever reads the
    report nothing they can act on — the same rule the beat module applies to
    a skipped outlet.
    """

    reason = serializers.CharField(max_length=255)

    def validate_reason(self, value):
        reason = value.strip()
        if len(reason) < 3:
            raise serializers.ValidationError(
                'Give a reason somebody reading this next month can use.'
            )
        return reason


class SubmitOrderSerializer(serializers.Serializer):
    """Submitting takes no payload; this exists so the endpoint documents and
    validates like every other one."""

    def validate(self, attrs):
        order = self.context['order']

        if order.status == OrderStatus.SUBMITTED:
            raise serializers.ValidationError(
                'This order has already been submitted.'
            )
        if order.status != OrderStatus.DRAFT:
            raise serializers.ValidationError(
                f'A {order.get_status_display().lower()} order cannot be '
                f'submitted.'
            )
        if not order.items.exists():
            raise serializers.ValidationError(
                'An order needs at least one product on it before it can be '
                'submitted.'
            )

        return attrs
