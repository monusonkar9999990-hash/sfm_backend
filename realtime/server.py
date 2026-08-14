"""The Socket.IO server, and the ticket that gets a browser onto it.

**Why a ticket rather than the JWT.** The portal holds its tokens in
`httpOnly` cookies precisely so no script on the page can read them. A socket
authenticated with a bearer token would undo that: the token would have to be
handed to JavaScript first. So the portal's *server* — which does hold the
token — asks for a one-shot ticket and passes that to the browser instead. A
ticket is worth sixty seconds, is spent on first use, and opens nothing except
this socket.

**Why threading mode.** The project is a WSGI Django served by `runserver` in
development and gunicorn in production, and `socketio.WSGIApp` slots in front
of it without moving anything to ASGI. Long-polling works everywhere; a
WebSocket upgrade happens where the WSGI server supports it and falls back
silently where it does not, which is what Socket.IO exists to do.
"""

import secrets

import socketio
from django.conf import settings
from django.core.cache import cache

# How long a ticket is worth anything. Long enough for a page load, short
# enough that one written to a log is stale before anybody reads the log.
TICKET_TTL_SECONDS = 60

TICKET_PREFIX = 'realtime:ticket:'

#: Everyone whose figures cover the organisation. One room rather than a
#: fan-out per user: they are all being told the same thing.
TEAM_ROOM = 'scope:team'


def _origins():
    """Which origins may open a socket.

    Mirrors the CORS rule the REST API already follows — wide open while
    `DEBUG` is on, because the portal's dev server moves ports, and an explicit
    list otherwise.
    """
    if getattr(settings, 'CORS_ALLOW_ALL_ORIGINS', False):
        return '*'
    return list(getattr(settings, 'CORS_ALLOWED_ORIGINS', [])) or []


sio = socketio.Server(
    async_mode='threading',
    cors_allowed_origins=_origins(),
    # The portal is the only client and it reconnects on its own; a server-side
    # log line per poll would drown everything else in the console.
    logger=False,
    engineio_logger=False,
)


def issue_ticket(user):
    """A one-shot ticket for [user], to be spent on the next connect."""
    ticket = secrets.token_urlsafe(32)
    cache.set(
        f'{TICKET_PREFIX}{ticket}',
        {
            'user_id': str(user.pk),
            'team': bool(user.has_perm('accounts.view_team_reports')),
        },
        TICKET_TTL_SECONDS,
    )
    return ticket


def _spend_ticket(ticket):
    """Reads a ticket and destroys it, so a replay finds nothing.

    Not atomic against a simultaneous second use — Django's cache has no
    compare-and-delete — but the window is one round trip on a LAN, and what
    it protects is a read-only notification channel rather than an action.
    """
    if not ticket:
        return None
    key = f'{TICKET_PREFIX}{ticket}'
    payload = cache.get(key)
    cache.delete(key)
    return payload


@sio.event
def connect(sid, environ, auth=None):
    """Refuses anybody who did not bring a live ticket."""
    payload = _spend_ticket((auth or {}).get('ticket'))
    if payload is None:
        # Raising is how python-socketio reports a refused handshake; the
        # client sees `connect_error` and stops retrying with the same ticket.
        raise socketio.exceptions.ConnectionRefusedError(
            'A valid ticket is required.'
        )

    sio.save_session(sid, payload)
    sio.enter_room(sid, f'user:{payload["user_id"]}')
    if payload['team']:
        sio.enter_room(sid, TEAM_ROOM)

    # Tells the client what it is subscribed to, which is also how the portal
    # decides whether to show "live across the team" or "live, your records".
    sio.emit('ready', {'scope': 'team' if payload['team'] else 'self'}, to=sid)


@sio.event
def disconnect(sid, reason=None):
    """Nothing to clean up — rooms go with the session."""


def notify(entity, *, owner_id=None, action='changed'):
    """Tells whoever is listening that [entity] moved.

    Deliberately contentless. The portal reacts by re-fetching through its own
    authenticated path, so this carries no figures and needs no authorisation
    beyond "may this person watch at all", which the ticket already settled.

    Never raises. A punch recorded in the field must not fail because a
    notification could not be delivered to a dashboard nobody is watching.
    """
    if not getattr(settings, 'REALTIME_ENABLED', True):
        return

    payload = {'entity': entity, 'action': action}

    try:
        sio.emit('changed', payload, room=TEAM_ROOM)
        if owner_id:
            sio.emit('changed', payload, room=f'user:{owner_id}')
    except Exception:  # noqa: BLE001 — see the docstring: never raise.
        pass


def wsgi_application(django_application):
    """Puts the socket in front of Django, sharing one port.

    Everything that is not `/socket.io/` falls through to Django untouched, so
    the API, the admin and the Flutter portal keep answering exactly as before.
    """
    return socketio.WSGIApp(sio, django_application)
