"""Customers: the dealers, distributors and retailers a territory sells to.

This is the module the `customer_ref` columns on `beats.BeatOutlet` and
`sitevisits.Site` were left waiting for. Both of those hold a customer's id as
plain text with the customer's name copied alongside it, which is what let
those modules ship before this one existed.

Those columns are deliberately left as text in this change. Turning them into
foreign keys means backfilling a `Customer` row for every ref two finished,
tested modules already hold — including the demo data and every test factory —
and that migration is worth doing on its own, not folded into the change that
introduces the table it points at.
"""

import re
import secrets
import uuid

from django.conf import settings
from django.db import models

from accounts.models import TimeStampedUUIDModel

# Ten digits opening 6-9. The same rule the Flutter form enforces, repeated
# here because a client-side check is a courtesy and this is the guarantee.
MOBILE_PATTERN = re.compile(r'^[6-9][0-9]{9}$')

# 15 characters: two state digits, a PAN, an entity digit, 'Z', a checksum.
GSTIN_PATTERN = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{3}$')

PINCODE_PATTERN = re.compile(r'^[1-9][0-9]{5}$')


def normalise_mobile(value):
    """`+91 98765 43210`, `09876543210`, `9876543210` -> `9876543210`.

    A customer's phone is a business contact number, not a credential — unlike
    `accounts.User.mobile`, which is stored E.164 because people sign in with
    it. Ten digits is what the field staff read off a visiting card and what
    every screen in the app displays, so ten digits is what is stored.
    """
    digits = re.sub(r'\D', '', str(value or ''))
    if digits.startswith('91') and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith('0') and len(digits) == 11:
        digits = digits[1:]
    return digits


class CustomerType(models.TextChoices):
    """Mirrors the client's `CustomerType` enum, key for key.

    The keys travel over the wire, so they are the contract — the labels are
    only what Django's admin shows.
    """

    DEALER = 'dealer', 'Dealer'
    DISTRIBUTOR = 'distributor', 'Distributor'
    RETAILER = 'retailer', 'Retailer'
    CONTRACTOR = 'contractor', 'Contractor'
    ARCHITECT = 'architect', 'Architect'


class Customer(TimeStampedUUIDModel):
    """A business the field team sells to."""

    name = models.CharField(max_length=150)

    # Random rather than a counter: a counter needs a lock to stay unique
    # under concurrent registration, and this needs nothing.
    #
    # Explicitly NOT taken from the front of the UUIDv7 primary key. A v7 key
    # opens with a 48-bit millisecond timestamp, so its leading hex digits are
    # identical for every row written in the same several-hour window — the
    # one thing a unique code must never be. The tail is the random half.
    code = models.CharField(max_length=20, unique=True, blank=True)

    contact_person = models.CharField(max_length=120)
    phone = models.CharField(max_length=10, unique=True)
    email = models.EmailField(blank=True, default='')

    type = models.CharField(
        max_length=12, choices=CustomerType, default=CustomerType.RETAILER
    )

    address = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=80)
    pincode = models.CharField(max_length=6)

    # Blank rather than null: MySQL has no partial unique index, so a nullable
    # unique column cannot be expressed here anyway, and one empty-string
    # convention beats two ways of saying "not given". Uniqueness of a real
    # GSTIN is enforced in the serializer, where the blank case can be
    # excluded — see `CustomerCreateSerializer.validate_gstin`.
    gstin = models.CharField(max_length=15, blank=True, default='')

    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    territory = models.ForeignKey(
        'accounts.Territory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customers',
    )

    # Who onboarded them. Kept even when that user is deleted, because the
    # customer outlives the employee who signed them up.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='onboarded_customers',
    )

    is_active = models.BooleanField(default=True)

    # Device-generated when the client starts sending one; server-generated
    # until then. Same contract as attendance, beats and site visits.
    sync_id = models.UUIDField(default=uuid.uuid7, editable=False)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['type', 'is_active']),
            models.Index(fields=['city']),
        ]
        constraints = [
            # Two shops can share a name across cities — a "Verma Hardware" in
            # Noida and one in Jaipur are different businesses — so the pair is
            # what has to be unique, not the name.
            models.UniqueConstraint(
                fields=['name', 'city'], name='one_customer_per_name_per_city'
            ),
        ]

    def __str__(self):
        return f'{self.code} {self.name}'

    def save(self, *args, **kwargs):
        self.name = self.name.strip()
        self.contact_person = self.contact_person.strip()
        self.phone = normalise_mobile(self.phone)
        self.email = self.email.strip().lower()
        self.gstin = self.gstin.strip().upper()
        self.city = self.city.strip()
        self.state = self.state.strip()

        if not self.code:
            self.code = f'CUS-{secrets.token_hex(4).upper()}'

        super().save(*args, **kwargs)

    @property
    def short_address(self):
        return f'{self.city}, {self.state} — {self.pincode}'
