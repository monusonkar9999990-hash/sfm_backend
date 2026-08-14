"""Serializers for the product endpoints.

The read payload carries a few keys twice — `product_code` and `sku`,
`gst_percent` and `gst_rate`, `active` and `is_active`. Those are aliases of
one another, not separate values: the Flutter client's `ProductModel.fromJson`
already reads the second name of each pair, and emitting both means the
catalogue screens parse this response without a translation layer or a client
release. Each pair is rendered from the same column, so they cannot disagree.
"""

from decimal import Decimal

from rest_framework import serializers

from .models import Product, ProductCategory, ProductUnit


class ProductSerializer(serializers.ModelSerializer):
    """A product, ready to render.

    Money is rendered as a number rather than DRF's default string. The client
    reads `mrp` with `as num?`, and a string there is a crash on first
    contact — the same bug this project has already been bitten by once, on
    order totals.
    """

    mrp = serializers.FloatField(read_only=True)
    selling_price = serializers.FloatField(read_only=True)
    gst_percent = serializers.FloatField(read_only=True)

    # --------------------------------------------------- client-side aliases
    sku = serializers.CharField(source='product_code', read_only=True)
    gst_rate = serializers.FloatField(source='gst_percent', read_only=True)
    is_active = serializers.BooleanField(source='active', read_only=True)

    # Derived, never stored — see `Product.stock_status`.
    stock = serializers.CharField(source='stock_status', read_only=True)

    discount_percent = serializers.FloatField(read_only=True)
    is_orderable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Product
        fields = (
            'id',
            'product_code',
            'sku',
            'name',
            'category',
            'brand',
            'description',
            'unit',
            'mrp',
            'selling_price',
            'gst_percent',
            'gst_rate',
            'discount_percent',
            'stock_quantity',
            'stock',
            'active',
            'is_active',
            'is_orderable',
            'created_at',
            'updated_at',
        )
        read_only_fields = fields


class ProductWriteSerializer(serializers.ModelSerializer):
    """Creating and updating a product.

    `product_code` is optional: left out, the model generates one. Supplied, it
    has to be unique — which the field's own validator enforces against the
    table, and the database enforces again underneath.
    """

    product_code = serializers.CharField(
        max_length=24, required=False, allow_blank=True
    )

    class Meta:
        model = Product
        fields = (
            'product_code',
            'name',
            'category',
            'brand',
            'description',
            'unit',
            'mrp',
            'selling_price',
            'gst_percent',
            'stock_quantity',
            'active',
        )
        extra_kwargs = {
            'name': {'required': True},
            'mrp': {'required': True},
            'selling_price': {'required': True},
            'category': {'choices': ProductCategory.choices},
            'unit': {'choices': ProductUnit.choices},
        }

    def validate_product_code(self, value):
        code = (value or '').strip().upper()
        if not code:
            # Blank is not an error — the model fills it in. Returned as '' so
            # `save()` sees a falsy value and generates one.
            return ''

        clash = Product.objects.filter(product_code=code)
        if self.instance is not None:
            # On an update, the product's own code is not a duplicate of
            # itself.
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                f'{code} is already used by another product.'
            )
        return code

    # The floor on mrp, selling_price and gst_percent is not repeated here.
    # Each of those model fields carries a MinValueValidator, which DRF picks
    # up and runs before any `validate_<field>` method — so a check written
    # here would be unreachable, and a nicer message would never be seen. The
    # ceiling has no such validator, which is why this one method exists.
    def validate_gst_percent(self, value):
        if value is not None and value > Decimal('100'):
            raise serializers.ValidationError('GST cannot be more than 100%.')
        return value

    def validate(self, attrs):
        # On a PATCH only some of these arrive, so the missing half comes off
        # the row being edited — otherwise a lone `selling_price` would be
        # compared against nothing and let through.
        mrp = attrs.get('mrp', getattr(self.instance, 'mrp', None))
        selling = attrs.get(
            'selling_price', getattr(self.instance, 'selling_price', None)
        )

        if mrp is not None and selling is not None and selling > mrp:
            raise serializers.ValidationError(
                {
                    'selling_price': (
                        f'Selling price ({selling}) cannot be more than the '
                        f'MRP ({mrp}).'
                    )
                }
            )

        return attrs
