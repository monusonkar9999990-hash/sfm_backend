"""Serializers for the customer endpoints.

The read payload follows the Flutter client's `CustomerModel.fromJson` field
for field, so nothing has to be translated on the way in.
"""

from rest_framework import serializers

from .models import (
    GSTIN_PATTERN,
    MOBILE_PATTERN,
    PINCODE_PATTERN,
    Customer,
    CustomerType,
    normalise_mobile,
)


class CustomerSerializer(serializers.ModelSerializer):
    """A customer, in the shape `CustomerModel.fromJson` reads."""

    # DRF renders a DecimalField as a string to keep every paisa exact, and
    # the client reads this one through `parseMoney`, which takes either.
    # Sent as a number anyway: the value is a limit, not a ledger entry, and a
    # bare number is what the mock service has always produced.
    credit_limit = serializers.FloatField(allow_null=True)

    class Meta:
        model = Customer
        fields = (
            'id',
            'code',
            'name',
            'contact_person',
            'phone',
            'email',
            'type',
            'address',
            'city',
            'state',
            'pincode',
            'gstin',
            'credit_limit',
            'is_active',
            'created_at',
        )
        read_only_fields = fields


class CustomerCreateSerializer(serializers.Serializer):
    """Registering a customer from the field.

    Every rule here is one the Flutter form already enforces. That is not
    duplication for its own sake — the form is a courtesy to the person
    typing, and this is the guarantee for everyone reading the table later.
    """

    name = serializers.CharField(max_length=150)
    contact_person = serializers.CharField(max_length=120)
    phone = serializers.CharField(max_length=16)
    email = serializers.EmailField(required=False, allow_blank=True)
    type = serializers.ChoiceField(choices=CustomerType.choices)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True)
    city = serializers.CharField(max_length=80)
    state = serializers.CharField(max_length=80)
    pincode = serializers.CharField(max_length=6)
    gstin = serializers.CharField(
        max_length=15, required=False, allow_blank=True
    )
    credit_limit = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=0
    )

    def validate_phone(self, value):
        digits = normalise_mobile(value)
        if not MOBILE_PATTERN.match(digits):
            raise serializers.ValidationError(
                'Enter a 10-digit mobile number starting with 6, 7, 8 or 9.'
            )
        if Customer.objects.filter(phone=digits).exists():
            raise serializers.ValidationError(
                'A customer is already registered on this number.'
            )
        return digits

    def validate_pincode(self, value):
        pincode = value.strip()
        if not PINCODE_PATTERN.match(pincode):
            raise serializers.ValidationError('Enter a valid 6-digit PIN code.')
        return pincode

    def validate_gstin(self, value):
        gstin = (value or '').strip().upper()
        if not gstin:
            return ''
        if not GSTIN_PATTERN.match(gstin):
            raise serializers.ValidationError(
                'Enter a valid 15-character GSTIN.'
            )
        # Uniqueness lives here rather than in a database constraint: MySQL has
        # no partial unique index, and a plain one would treat every customer
        # without a GSTIN as a duplicate of every other.
        if Customer.objects.filter(gstin=gstin).exists():
            raise serializers.ValidationError(
                'This GSTIN is already registered to another customer.'
            )
        return gstin

    def validate(self, attrs):
        name = attrs['name'].strip()
        city = attrs['city'].strip()

        # Checked here as well as by the database constraint so the caller gets
        # a sentence instead of a 500 from an IntegrityError.
        if Customer.objects.filter(name__iexact=name, city__iexact=city).exists():
            raise serializers.ValidationError(
                {'name': f'{name} is already registered in {city}.'}
            )

        attrs['name'] = name
        attrs['city'] = city
        return attrs

    def create(self, validated_data):
        user = self.context['request'].user
        return Customer.objects.create(
            created_by=user,
            # The territory the customer sits in is the one the person who
            # onboarded them works — the only signal available at this point.
            territory=getattr(user, 'primary_territory', None),
            **validated_data,
        )


class CustomerUpdateSerializer(serializers.Serializer):
    """Correcting a customer already on the books.

    Every field is optional and only what was sent is written, so two people
    fixing different details of the same shop do not overwrite each other —
    and a record queued on a device with no signal carries the one field that
    changed rather than a whole stale copy of the customer.

    The uniqueness rules are the create serializer's, with one difference that
    matters: they exclude the customer being edited. Without that, saving a
    record without touching its phone number would fail because that number is
    already registered — to itself.
    """

    name = serializers.CharField(max_length=150, required=False)
    contact_person = serializers.CharField(max_length=120, required=False)
    phone = serializers.CharField(max_length=16, required=False)
    email = serializers.EmailField(required=False, allow_blank=True)
    type = serializers.ChoiceField(choices=CustomerType.choices, required=False)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True)
    city = serializers.CharField(max_length=80, required=False)
    state = serializers.CharField(max_length=80, required=False)
    pincode = serializers.CharField(max_length=6, required=False)
    gstin = serializers.CharField(max_length=15, required=False, allow_blank=True)
    credit_limit = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=0
    )

    def _others(self):
        return Customer.objects.exclude(pk=self.instance.pk)

    def validate_phone(self, value):
        digits = normalise_mobile(value)
        if not MOBILE_PATTERN.match(digits):
            raise serializers.ValidationError(
                'Enter a 10-digit mobile number starting with 6, 7, 8 or 9.'
            )
        if self._others().filter(phone=digits).exists():
            raise serializers.ValidationError(
                'A customer is already registered on this number.'
            )
        return digits

    def validate_pincode(self, value):
        pincode = value.strip()
        if not PINCODE_PATTERN.match(pincode):
            raise serializers.ValidationError('Enter a valid 6-digit PIN code.')
        return pincode

    def validate_gstin(self, value):
        gstin = (value or '').strip().upper()
        if not gstin:
            return ''
        if not GSTIN_PATTERN.match(gstin):
            raise serializers.ValidationError('Enter a valid 15-character GSTIN.')
        if self._others().filter(gstin=gstin).exists():
            raise serializers.ValidationError(
                'This GSTIN is already registered to another customer.'
            )
        return gstin

    def validate(self, attrs):
        name = attrs.get('name', self.instance.name).strip()
        city = attrs.get('city', self.instance.city).strip()

        if self._others().filter(name__iexact=name, city__iexact=city).exists():
            raise serializers.ValidationError(
                {'name': f'{name} is already registered in {city}.'}
            )

        if 'name' in attrs:
            attrs['name'] = name
        if 'city' in attrs:
            attrs['city'] = city
        return attrs

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=[*validated_data, 'updated_at'])
        return instance
