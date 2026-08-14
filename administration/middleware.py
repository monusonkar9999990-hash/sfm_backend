"""Two pieces of request-level behaviour the administration module owns.

Both are middleware rather than changes to existing views, which is what lets
this module add auditing and a maintenance switch across nine modules without
editing any of them.
"""

import json

from django.core.cache import cache
from django.http import JsonResponse

from . import audit
from .models import AuditAction


class AuditContextMiddleware:
    """Publishes the current request so model signals can attribute their work,
    and records sign-in and sign-out directly.

    Authentication is the one event with no model save behind it — SimpleJWT
    issues a token without touching a row this module watches — so it is read
    off the response of the auth endpoints instead. A 200 from the login path
    is a sign-in; a 401 from it is a failed attempt, which is the line most
    worth having when somebody asks who has been trying.
    """

    LOGIN_PATHS = ('/auth/login/',)
    LOGOUT_PATHS = ('/auth/logout/',)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = audit.set_request(request)
        try:
            # Read on the way in, while the stream is still readable. After the
            # view has parsed it, `request.body` raises RawPostDataException —
            # which surfaced as a 500 on every sign-in until this moved here.
            # Touching `.body` now also caches it, so DRF still gets its copy.
            identifier = self._identifier(request)

            response = self.get_response(request)
            self._record_auth(request, response, identifier)
            return response
        finally:
            audit.reset_request(token)

    def _record_auth(self, request, response, identifier):
        if request.method != 'POST':
            return

        path = request.path
        is_login = any(path.endswith(suffix) for suffix in self.LOGIN_PATHS)
        is_logout = any(path.endswith(suffix) for suffix in self.LOGOUT_PATHS)
        if not (is_login or is_logout):
            return

        status = response.status_code

        if is_logout:
            if status < 400:
                audit.record(
                    action=AuditAction.LOGOUT,
                    entity='accounts.User',
                    entity_id=getattr(request.user, 'pk', '') or '',
                    summary='Signed out',
                    request=request,
                )
            return

        if status < 400:
            # `request.user` is anonymous on a login request — the view
            # authenticates by hand and returns tokens. The identifier from
            # the body is what there is to record.
            audit.record(
                action=AuditAction.LOGIN,
                entity='accounts.User',
                entity_id=self._user_id(response),
                summary=f'Signed in as {identifier}' if identifier else 'Signed in',
                changes={'identifier': identifier},
                actor=None,
                request=request,
            )
        elif status in (400, 401, 403):
            audit.record(
                action=AuditAction.LOGIN_FAILED,
                entity='accounts.User',
                summary=(
                    f'Failed sign-in for {identifier}'
                    if identifier
                    else 'Failed sign-in'
                ),
                changes={'identifier': identifier, 'status': status},
                request=request,
            )

    def _identifier(self, request):
        """The account somebody tried to use. Never the password — the body is
        read here only for that one field."""
        if request.method != 'POST':
            return ''
        if not any(request.path.endswith(p) for p in self.LOGIN_PATHS):
            return ''

        try:
            body = json.loads(request.body or b'{}')
        except (ValueError, UnicodeDecodeError, Exception):
            return ''
        if not isinstance(body, dict):
            return ''
        return str(body.get('identifier') or body.get('username') or '')[:150]

    @staticmethod
    def _user_id(response):
        data = getattr(response, 'data', None)
        if isinstance(data, dict) and isinstance(data.get('user'), dict):
            return data['user'].get('id') or ''
        return ''


class MaintenanceModeMiddleware:
    """Closes the API while `maintenance_mode` is on.

    Administrators are let through — somebody has to be able to turn it back
    off, and locking the switch behind the switch is a support call at three
    in the morning. Sign-in stays open for the same reason: an administrator
    whose token expired during the window still needs a way in.

    The flag is cached for [CACHE_TTL] seconds, and the settings endpoint
    clears the key when it writes. Without the cache this is a database query
    on *every* API request in the system — a permanent cost for a switch used
    twice a year — and it showed up immediately as a extra query in four
    unrelated performance tests.

    The TTL is the safety net rather than the mechanism: the invalidation on
    write makes the change immediate in the process that made it, and the ten
    seconds bound how long any other worker can be stale. A cache with only
    invalidation and no expiry is how a maintenance switch ends up stuck on.
    """

    ALWAYS_OPEN = (
        '/auth/login/',
        '/auth/refresh/',
        '/admin/settings/',
        # The public configuration endpoints. Closing these during maintenance
        # would be self-defeating: `/app-config/` is where the client reads
        # that maintenance is on and what message to show, and `/app-version/`
        # is what tells a stranded user whether updating would help.
        '/app-config/',
        '/app-version/',
        '/privacy/',
        '/terms/',
        '/announcements/',
    )

    CACHE_KEY = 'administration:maintenance_mode'
    CACHE_TTL = 10

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._should_block(request):
            from .settings_registry import value_of

            return JsonResponse(
                {
                    'detail': value_of('maintenance_message'),
                    'maintenance_mode': True,
                },
                status=503,
            )
        return self.get_response(request)

    def _should_block(self, request):
        # Only the API is closed. The Django admin and the media files stay up,
        # because that is where the person fixing it is working.
        if not request.path.startswith('/api/'):
            return False

        if any(request.path.endswith(suffix) for suffix in self.ALWAYS_OPEN):
            return False

        if not self._maintenance_on():
            return False

        return not self._is_administrator(request)

    @classmethod
    def _maintenance_on(cls):
        cached = cache.get(cls.CACHE_KEY)
        if cached is not None:
            return cached

        from .settings_registry import value_of

        try:
            flag = bool(value_of('maintenance_mode'))
        except Exception:
            # Before the first migration the table does not exist yet, and a
            # missing settings table means nothing has been switched on. Not
            # cached — the next request should look again.
            return False

        cache.set(cls.CACHE_KEY, flag, cls.CACHE_TTL)
        return flag

    @staticmethod
    def _is_administrator(request):
        """Resolves the bearer token here rather than trusting `request.user`.

        Middleware runs before DRF authenticates, so `request.user` is still
        whatever the session backend made of it — anonymous for every mobile
        client, which would lock administrators out of their own maintenance
        window. The token is decoded explicitly instead.
        """
        user = getattr(request, 'user', None)

        if user is None or not getattr(user, 'is_authenticated', False):
            from rest_framework_simplejwt.authentication import JWTAuthentication

            try:
                # Reads only the Authorization header out of META, so a plain
                # Django request is all it needs.
                result = JWTAuthentication().authenticate(request)
            except Exception:
                # An expired or malformed token is not an administrator. The
                # view behind this will produce the proper 401.
                return False
            user = result[0] if result else None

        if user is None or not getattr(user, 'is_authenticated', False):
            return False

        return user.is_superuser or user.has_perm('accounts.edit_configuration')
