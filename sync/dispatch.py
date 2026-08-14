"""Calling the real endpoints from inside the sync batch.

The upload endpoint does not know how to create an order, a punch or a visit.
It routes each uploaded record to the view that already does, and reports what
came back.

That is the whole design, and it is worth being explicit about why. Every rule
those modules enforce — the geofence on a punch, the catalogue rate on an
order line, "you have already checked in today", the permission each action
needs — exists in exactly one place. A sync module that wrote rows directly
would be a second implementation of all of it, drifting from the first the day
either changed. This one cannot drift, because it is the first.

It also means the modules needed no changes to become syncable. They were
already built for it: each write endpoint accepts a device-generated `sync_id`
and returns the original record on a repeat, and each accepts `captured_at`
so a punch keeps the moment it happened rather than the moment it uploaded.
"""

import io
import json
import uuid

from django.http import HttpRequest
from rest_framework.authentication import BaseAuthentication


class InternalAuthentication(BaseAuthentication):
    """Authenticates an internally-built request as the batch's owner.

    The user has already been authenticated once — by the JWT on the upload
    request itself — and this hands that same user to the view being called.
    Nothing here trusts anything from the network: `_sync_user` is set by this
    process, on a request object this process built.
    """

    def authenticate(self, request):
        user = getattr(request, '_sync_user', None)
        if user is None:
            # `request` here is DRF's wrapper; the attribute lives on the
            # Django request underneath it.
            user = getattr(getattr(request, '_request', None), '_sync_user', None)
        return (user, None) if user is not None else None


def build_request(*, method, path, user, payload=None, query=None, files=None):
    """A Django request, assembled rather than routed.

    Deliberately hand-built instead of using `django.test.RequestFactory`:
    that is test scaffolding, and this runs on every upload in production.
    Everything a DRF view actually reads is set here — the method, the body,
    the parsed content type, and enough of `META` for `build_absolute_uri` to
    produce the media URLs that serializers put in their responses.

    With [files] the body is multipart instead of JSON, so a record that
    carries a photo — a punch and its selfie — reaches the owning endpoint the
    same way it would have online. See [build_multipart_body] for why the
    body is encoded here rather than parsed by Django's own uploader.
    """
    if files:
        body, content_type = build_multipart_body(payload or {}, files)
    else:
        body = json.dumps(payload or {}).encode('utf-8')
        content_type = 'application/json'

    request = HttpRequest()
    request.method = method.upper()
    request.path = path
    request.path_info = path
    request._body = body
    request._read_started = False
    # `WSGIRequest.__init__` normally sets this from the socket; a bare
    # `HttpRequest` has no `_stream` at all, and `HttpRequest.read()` — which
    # is how DRF's parser gets at the body — goes straight through it.
    request._stream = io.BytesIO(body)
    request._sync_user = user
    request.user = user
    request.GET = request.GET.copy()
    for key, value in (query or {}).items():
        request.GET[key] = value

    request.META = {
        'REQUEST_METHOD': request.method,
        'PATH_INFO': path,
        'CONTENT_TYPE': content_type,
        'CONTENT_LENGTH': str(len(body)),
        # `build_absolute_uri` needs a host. Its only job in a sync response is
        # to render media URLs, and those are relative to this server either
        # way.
        'SERVER_NAME': 'localhost',
        'SERVER_PORT': '8000',
        'wsgi.url_scheme': 'http',
    }

    return request


def build_multipart_body(payload, files):
    """Encodes fields and files as one multipart body.

    Written out by hand rather than assembled with a library because the whole
    point of this module is that the record reaches the owning view exactly as
    an online request would: Django's `MultiPartParser` reads this back into
    `request.POST` and `request.FILES`, and the module's own serializer then
    validates the image the same way it validates an uploaded one — including
    the Pillow check that rejects a PHP script named `.jpg`.

    Values are stringified because that is what a real form sends; a
    serializer's `to_internal_value` is what turns `"28.6139"` back into a
    number, and it already does that for every online upload.
    """
    boundary = f'----SyncBoundary{uuid.uuid4().hex}'
    marker = f'--{boundary}'.encode()
    parts = []

    for name, value in (payload or {}).items():
        if value is None:
            continue
        # A list field — `competitor_brands` — repeats its name, which is how
        # HTML forms carry one and how DRF reads one back.
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            parts.append(marker)
            parts.append(
                f'Content-Disposition: form-data; name="{name}"'.encode()
            )
            parts.append(b'')
            parts.append(_as_bytes(item))

    for name, upload in files.items():
        filename = getattr(upload, 'name', None) or 'upload'
        content_type = (
            getattr(upload, 'content_type', None) or 'application/octet-stream'
        )
        parts.append(marker)
        parts.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"'
            ).encode()
        )
        parts.append(f'Content-Type: {content_type}'.encode())
        parts.append(b'')
        parts.append(_read_upload(upload))

    parts.append(f'--{boundary}--'.encode())
    parts.append(b'')

    return b'\r\n'.join(parts), f'multipart/form-data; boundary={boundary}'


def _as_bytes(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, bool):
        # `True` would encode as `"True"`, which no serializer accepts.
        return b'true' if value else b'false'
    return str(value).encode('utf-8')


def _read_upload(upload):
    """The whole file, from the start.

    An `UploadedFile` that has already been read once — by a validator, or by
    a previous record in the same batch — is left at its end, and reading it
    again would attach an empty image without any error to show for it.
    """
    if hasattr(upload, 'seek'):
        upload.seek(0)
    data = upload.read()
    if hasattr(upload, 'seek'):
        upload.seek(0)
    return data


def call(
    view_class,
    *,
    method,
    path,
    user,
    payload=None,
    query=None,
    files=None,
    **kwargs,
):
    """Runs one endpoint and returns its DRF response.

    `authentication_classes` is passed through `as_view()`, which is Django's
    documented way to override a class attribute per view instance — not a
    patch and not a monkeypatch. Permissions are left exactly as the view
    declares them: a device that syncs an order without `place_orders` gets
    the same 403 it would get online, which is the point.
    """
    request = build_request(
        method=method,
        path=path,
        user=user,
        payload=payload,
        query=query,
        files=files,
    )

    view = view_class.as_view(authentication_classes=[InternalAuthentication])
    response = view(request, **kwargs)

    # DRF responses from a directly-called view are not yet rendered; the data
    # is what this module wants anyway, so nothing needs rendering.
    return response
