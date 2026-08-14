"""Orders: what a field executive booked, for whom, at what price.

Every figure on an order is computed here from the quantities and the rates —
never read from the request. A client that could post its own `grand_total`
could post any number at all, and an order is the one record in this system
that turns into money.

The arithmetic, once, so it can be checked:

    line gross    = quantity x unit_price
    line taxable  = gross - discount
    line gst      = taxable x gst_percent / 100
    line_total    = taxable + gst

    subtotal       = sum of gross
    discount_total = sum of discount
    gst_total      = sum of line gst
    grand_total    = subtotal - discount_total + gst_total

The last line is also the sum of every `line_total`, and both are asserted
against each other in the tests — if the two ever disagree, the rounding is
wrong somewhere.

`Decimal` throughout, quantized per line. Floats would drift by a paisa per
line and a rupee per invoice, which is exactly the kind of error that surfaces
as a disputed bill six weeks later.
"""

import secrets
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from accounts.models import TimeStampedUUIDModel

TWO_PLACES = Decimal('0.01')


def money(value):
    """Two decimal places, half up — the way an invoice rounds."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


class OrderStatus(models.TextChoices):
    """The four states this module recognises.

    Note for whoever wires the Flutter client up: its `OrderStatus` enum runs
    `draft, placed, confirmed, dispatched, delivered, cancelled`. `draft` and
    `cancelled` line up; `submitted` and `completed` do not have counterparts
    there. That mapping is a decision for the client phase, and inventing one
    here — by emitting a fabricated `placed` alongside `submitted` — would put
    a status on the wire that this module never actually sets.
    """

    DRAFT = 'draft', 'Draft'
    SUBMITTED = 'submitted', 'Submitted'
    CANCELLED = 'cancelled', 'Cancelled'
    COMPLETED = 'completed', 'Completed'


class Order(TimeStampedUUIDModel):
    """One booking against one customer."""

    order_number = models.CharField(max_length=24, unique=True, blank=True)

    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.PROTECT,
        related_name='orders',
    )

    # PROTECT, not CASCADE: deleting an employee must not take the revenue
    # they booked with them.
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='orders',
    )

    status = models.CharField(
        max_length=10, choices=OrderStatus, default=OrderStatus.DRAFT
    )
    order_date = models.DateField(default=timezone.localdate)
    remarks = models.TextField(blank=True, default='')

    # All four are derived from the items and rewritten by `recalculate`.
    # Stored rather than computed on read so a report can sum them in SQL
    # without walking every line.
    subtotal = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00')
    )
    discount_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00')
    )
    gst_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00')
    )
    grand_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00')
    )

    submitted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True, default='')

    # Device-generated when a client starts sending one; server-generated
    # until then. Same contract as every other module here.
    sync_id = models.UUIDField(default=uuid.uuid7, editable=False)

    class Meta:
        ordering = ['-order_date', '-created_at']
        indexes = [
            models.Index(fields=['status', 'order_date']),
            models.Index(fields=['employee', 'status']),
            models.Index(fields=['customer']),
        ]

    def __str__(self):
        return f'{self.order_number} · {self.customer.name}'

    def save(self, *args, **kwargs):
        if not self.order_number:
            # `SO-2608-A3F91C`: year, month, then random. Not a per-month
            # counter — a counter needs a lock to stay unique under concurrent
            # booking, and not the head of the UUIDv7 key either, which is a
            # millisecond timestamp and therefore identical across rows
            # written together.
            stamp = (self.order_date or timezone.localdate()).strftime('%y%m')
            self.order_number = f'SO-{stamp}-{secrets.token_hex(3).upper()}'

        super().save(*args, **kwargs)

    # ------------------------------------------------------------- lifecycle

    @property
    def is_editable(self):
        """A draft is the only state its owner may change."""
        return self.status == OrderStatus.DRAFT

    @property
    def is_terminal(self):
        """Cancelled and completed orders are history, not work in progress."""
        return self.status in (OrderStatus.CANCELLED, OrderStatus.COMPLETED)

    # ------------------------------------------------------------- the maths

    def recalculate(self, save=True):
        """Rewrites the four totals from the lines that are actually there.

        Called after every write that could have moved a line. Sums the
        per-line figures rather than recomputing from scratch, so the order's
        totals and its items can never tell different stories.
        """
        subtotal = Decimal('0.00')
        discount_total = Decimal('0.00')
        gst_total = Decimal('0.00')

        for item in self.items.all():
            subtotal += item.gross
            discount_total += item.discount
            gst_total += item.gst_amount

        self.subtotal = money(subtotal)
        self.discount_total = money(discount_total)
        self.gst_total = money(gst_total)
        self.grand_total = money(subtotal - discount_total + gst_total)

        if save:
            self.save(
                update_fields=[
                    'subtotal',
                    'discount_total',
                    'gst_total',
                    'grand_total',
                    'updated_at',
                ]
            )
        return self


class OrderItem(TimeStampedUUIDModel):
    """One product line on an order.

    `unit_price` and `gst_percent` are copied off the product when the line is
    written and never read from it again. A rate revision next quarter must
    not silently restate an order booked this one.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')

    # PROTECT: a product that has been sold cannot be deleted out from under
    # the order that sold it. The catalogue withdraws products by clearing
    # `active` instead, which is why DELETE /products/ does not erase rows.
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.PROTECT,
        related_name='order_items',
    )

    quantity = models.PositiveIntegerField()

    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    # An absolute amount off this line, not a percentage — a percentage of a
    # quantity of a rate is three roundings deep, and field staff negotiate in
    # rupees off, not in percent.
    discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00')
    )

    gst_percent = models.DecimalField(max_digits=5, decimal_places=2)

    # Stored for the same reason the order's totals are: so a report can read
    # it without recomputing. Written by `save`, never by the client.
    line_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00')
    )

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name='order_item_quantity_positive'
            ),
            models.CheckConstraint(
                condition=models.Q(discount__gte=0), name='order_item_discount_positive'
            ),
            # One line per product. Ordering the same cement twice on one order
            # is a quantity, not a second line.
            models.UniqueConstraint(
                fields=['order', 'product'], name='one_line_per_product_per_order'
            ),
        ]

    def __str__(self):
        return f'{self.quantity} x {self.product.name}'

    # ------------------------------------------------------------- the maths

    @property
    def gross(self):
        return money(Decimal(self.quantity) * self.unit_price)

    @property
    def taxable(self):
        return money(self.gross - self.discount)

    @property
    def gst_amount(self):
        return money(self.taxable * self.gst_percent / Decimal('100'))

    def compute_line_total(self):
        return money(self.taxable + self.gst_amount)

    def save(self, *args, **kwargs):
        self.line_total = self.compute_line_total()
        super().save(*args, **kwargs)
