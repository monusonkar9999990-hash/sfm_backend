"""Authentication backend that accepts a mobile number, an email address or
an employee code as the identifier.

`USERNAME_FIELD` stays `employee_code` — an identifier HR owns and nobody
changes. Letting people sign in with what they actually remember is this
backend's job, not the model's.
"""

import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

# Anything that is only digits, spaces, dashes or a leading +, and long enough
# to be a phone number.
_MOBILE_SHAPE = re.compile(r'^\+?[\d\s-]{7,17}$')


class IdentifierBackend(ModelBackend):
    """Resolves one identifier to at most one user, then checks the password."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        identifier = (
            username
            or kwargs.get('identifier')
            or kwargs.get(UserModel.USERNAME_FIELD)
        )
        if not identifier or password is None:
            return None

        identifier = identifier.strip()
        candidates = list(
            UserModel._default_manager.filter(self._lookup(identifier))[:2]
        )

        if len(candidates) != 1:
            # Run the hasher anyway. Without this, a missing user answers
            # measurably faster than a wrong password, which tells an attacker
            # which numbers are registered.
            UserModel().set_password(password)
            return None

        user = candidates[0]
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def _lookup(self, identifier):
        if '@' in identifier:
            # The database collation is utf8mb4_unicode_ci, so `=` is already
            # case-insensitive; iexact keeps the intent readable and portable.
            return Q(email__iexact=identifier)
        if _MOBILE_SHAPE.match(identifier):
            return Q(mobile=self._to_e164(identifier))
        return Q(employee_code__iexact=identifier)

    @staticmethod
    def _to_e164(raw):
        """Turns what a person types into the stored format.

        Numbers are stored in E.164, but nobody types a country code on their
        own phone, so a bare national number gets the default prefix.
        """
        digits = re.sub(r'[^\d+]', '', raw)
        if digits.startswith('+'):
            return digits
        default_code = getattr(settings, 'DEFAULT_COUNTRY_CODE', '+91')
        national_length = getattr(settings, 'NATIONAL_NUMBER_LENGTH', 10)
        if len(digits) > national_length:
            # Typed with the country code but without the plus.
            return f'+{digits}'
        return f'{default_code}{digits}'
