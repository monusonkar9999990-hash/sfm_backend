"""Products: the catalogue a field executive sells from.

Prices are `Decimal`, never float. A rate that drifts by a paisa per bag is a
rounding error nobody notices until an invoice is disputed, and a catalogue is
the one table where that difference compounds across every order ever raised.

The category and unit keys are the ones the Flutter client's `ProductCategory`
and `ProductUnit` enums already use. They are the contract; matching them here
means the client parses this payload without a translation layer.
"""

import secrets
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from accounts.models import TimeStampedUUIDModel

# Below this, the catalogue shows a product as low on stock rather than
# available. Field policy, not code — an administrator changes it in .env
# without a release, the same way the attendance thresholds work.
LOW_STOCK_THRESHOLD = getattr(settings, 'PRODUCTS_LOW_STOCK_THRESHOLD', 25)


class ProductCategory(models.TextChoices):
    CEMENT = 'cement', 'Cement'
    STEEL = 'steel', 'TMT steel'
    PAINT = 'paint', 'Paint & putty'
    ADHESIVE = 'adhesive', 'Adhesives'
    TILES = 'tiles', 'Tiles & stone'
    PLUMBING = 'plumbing', 'Plumbing'
    ELECTRICAL = 'electrical', 'Electrical'
    OTHER = 'other', 'Other'


class ProductUnit(models.TextChoices):
    BAG = 'bag', 'per bag'
    KILOGRAM = 'kg', 'per kg'
    TONNE = 'tonne', 'per tonne'
    LITRE = 'litre', 'per litre'
    PIECE = 'piece', 'per piece'
    BOX = 'box', 'per box'
    SQUARE_FOOT = 'sqft', 'per sq ft'


class StockStatus(models.TextChoices):
    """Derived from `stock_quantity`, never stored.

    A status column and a quantity column disagree the first time one of them
    is written without the other, so only the number is kept and the word is
    computed from it.
    """

    IN_STOCK = 'in_stock', 'In stock'
    LOW_STOCK = 'low_stock', 'Low stock'
    OUT_OF_STOCK = 'out_of_stock', 'Out of stock'


class Product(TimeStampedUUIDModel):
    """One sellable item."""

    # Auto-generated when the caller does not supply one. Random, not derived
    # from the UUIDv7 primary key — a v7 key opens with a millisecond
    # timestamp, so its leading digits are identical for every row created in
    # the same window, which is the one thing a unique code must not be.
    product_code = models.CharField(max_length=24, unique=True, blank=True)

    name = models.CharField(max_length=150)
    category = models.CharField(
        max_length=12, choices=ProductCategory, default=ProductCategory.OTHER
    )
    brand = models.CharField(max_length=80, blank=True, default='')
    description = models.TextField(blank=True, default='')
    unit = models.CharField(
        max_length=8, choices=ProductUnit, default=ProductUnit.PIECE
    )

    mrp = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0'))]
    )
    selling_price = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0'))]
    )
    gst_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('18.00'),
        validators=[MinValueValidator(Decimal('0'))],
    )

    stock_quantity = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    # Device-generated when a client starts sending one; server-generated
    # until then. Same contract as attendance, beats, site visits and
    # customers.
    sync_id = models.UUIDField(default=uuid.uuid7, editable=False)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['category', 'active']),
            models.Index(fields=['brand']),
            models.Index(fields=['name']),
        ]
        constraints = [
            # Enforced by the database as well as by the serializer. MySQL 8.0.16
            # and later honour CHECK, so a price written by the admin, a data
            # migration or a shell session is held to the same rule as one that
            # arrives over the API.
            models.CheckConstraint(
                condition=models.Q(selling_price__lte=models.F('mrp')),
                name='selling_price_within_mrp',
            ),
            models.CheckConstraint(
                condition=models.Q(mrp__gte=0)
                & models.Q(selling_price__gte=0)
                & models.Q(gst_percent__gte=0),
                name='prices_are_not_negative',
            ),
        ]

    def __str__(self):
        return f'{self.product_code} {self.name}'

    def save(self, *args, **kwargs):
        self.name = self.name.strip()
        self.brand = self.brand.strip()
        self.product_code = self.product_code.strip().upper()

        if not self.product_code:
            self.product_code = f'PRD-{secrets.token_hex(4).upper()}'

        super().save(*args, **kwargs)

    @property
    def stock_status(self):
        if self.stock_quantity <= 0:
            return StockStatus.OUT_OF_STOCK
        if self.stock_quantity <= LOW_STOCK_THRESHOLD:
            return StockStatus.LOW_STOCK
        return StockStatus.IN_STOCK

    @property
    def is_orderable(self):
        return self.active and self.stock_quantity > 0

    @property
    def discount_percent(self):
        """How far below MRP the selling price sits, as a percentage."""
        if self.mrp <= 0:
            return Decimal('0.00')
        saving = (self.mrp - self.selling_price) / self.mrp * 100
        return saving.quantize(Decimal('0.01'))
