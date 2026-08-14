"""Writing audit rows, and knowing who to attribute them to.

A model signal fires deep inside a save and has no idea which request caused
it. The middleware puts the current request in a `ContextVar` on the way in,
and this module reads it back out — which is how a `post_save` on an order
ends up recording the IP address of the person who placed it.

`ContextVar` rather than thread-local: it is correct under async views as well
as sync ones, and Django has been able to run either for several versions now.

**No request context means no audit row.** A fixture built in a test, a
management command, a shell session — none of those have an actor or an
address, and a row saying "somebody changed something from nowhere" is noise
in the one table that has to stay readable. That is a deliberate limit, not an
oversight: the trail covers what came in through the API.
"""

import contextvars

_request = contextvars.ContextVar('audit_request', default=None)

# Never written to an audit row, at any nesting depth.
SENSITIVE = frozenset(
    {
        'password',
        'new_password',
        'current_password',
        'confirm_password',
        'token',
        'access',
        'refresh',
        'otp',
        'authorization',
    }
)


def set_request(request):
    return _request.set(request)


def reset_request(token):
    _request.reset(token)


def current_request():
    return _request.get()


def redact(payload):
    """A copy with anything credential-shaped replaced.

    An audit trail that records the password somebody set is worse than no
    audit trail — it turns the one table administrators are encouraged to read
    into a credential store.
    """
    if isinstance(payload, dict):
        return {
            key: '[redacted]' if key.lower() in SENSITIVE else redact(value)
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [redact(item) for item in payload]
    return payload


def client_ip(request):
    """The caller's address, preferring the proxy header when there is one.

    Only the first entry of `X-Forwarded-For` is taken — the rest are the
    proxies, and the whole header is client-supplied, so this is a best effort
    for a log rather than anything to make a decision on.
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None


def record(
    *,
    action,
    entity,
    entity_id='',
    summary='',
    changes=None,
    actor=None,
    request=None,
):
    """Writes one audit row, or returns None when there is nothing to attribute.

    Deliberately swallows its own failures. An audit write must never be the
    reason a customer could not be saved — the trail is a record of the work,
    not a participant in it.
    """
    from .models import AuditLog

    request = request or current_request()
    if request is None and actor is None:
        return None

    if actor is None:
        actor = getattr(request, 'user', None)
    if actor is not None and not getattr(actor, 'is_authenticated', False):
        actor = None

    try:
        return AuditLog.objects.create(
            actor=actor,
            actor_code=getattr(actor, 'employee_code', '') or '',
            action=action,
            entity=entity,
            entity_id=str(entity_id or ''),
            summary=summary[:255],
            changes=redact(changes or {}),
            ip_address=client_ip(request) if request is not None else None,
            user_agent=(
                request.META.get('HTTP_USER_AGENT', '')[:255]
                if request is not None
                else ''
            ),
            request_path=(request.path[:255] if request is not None else ''),
        )
    except Exception:
        return None


def label_for(instance):
    """`orders.Order` — the app label and model name."""
    meta = instance._meta
    return f'{meta.app_label}.{meta.object_name}'
