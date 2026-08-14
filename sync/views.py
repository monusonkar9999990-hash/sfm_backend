"""Sync endpoints, mounted under /api/<version>/sync/."""

import time

from django.utils import timezone
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from . import services
from .models import SyncBatch
from .registry import SUPPORTED_ENTITIES, describe
from .serializers import SyncDownloadQuerySerializer, SyncUploadSerializer

# Bumped when the wire format changes in a way a client has to care about.
# Published by /sync/status/ so a device can refuse to sync against a server
# it does not understand, rather than silently mis-parsing it.
SYNC_PROTOCOL_VERSION = 1


def collect_files(uploaded, record_count):
    """Groups `file.<index>.<field>` uploads by the record they belong to.

    Returns `(files, error)`. A name that does not parse, or that points at a
    record outside the batch, is an error rather than something to ignore: a
    device that believed it attached a selfie and got a punch without one has
    lost the photo, and silence is the worst way to tell it.
    """
    files = {}

    for name in uploaded:
        parts = name.split('.', 2)
        if len(parts) != 3 or parts[0] != 'file':
            return None, {
                'files': (
                    f"'{name}' is not a valid attachment name. Use "
                    f"'file.<record index>.<field>'."
                )
            }

        _, raw_index, field = parts
        if not raw_index.isdigit():
            return None, {
                'files': f"'{name}' does not name a record position."
            }

        index = int(raw_index)
        if index >= record_count:
            return None, {
                'files': (
                    f"'{name}' is for record {index}, but the batch has "
                    f'{record_count}.'
                )
            }
        if not field:
            return None, {'files': f"'{name}' does not name a field."}

        # `getlist` rather than indexing: a field sent twice would otherwise
        # silently keep only the last one.
        for upload in uploaded.getlist(name):
            files.setdefault(index, {})[field] = upload

    return files, None


class SyncUploadView(GenericAPIView):
    """Takes a batch of records recorded while the device was offline.

    Each record is routed to the endpoint that would have handled it online,
    so every rule those endpoints enforce applies here unchanged — including
    their permissions. A device syncing an order without `place_orders` gets
    the same 403 for that record it would have got at the time.

    **Idempotency.** Send an `idempotency_key`. The same key from the same
    user returns the first attempt's response without touching a row, so a
    retry after a dropped reply costs nothing and yields the same server ids.

    **Partial success is the normal case.** The batch answers `200` whenever
    it was processed, and each record carries its own status: `applied`,
    `duplicate`, `conflict` or `failed`. One bad record does not reject the
    other ninety-nine, and a failed record leaves nothing behind — each is
    written in its own savepoint.

    **Photos.** A record that carries one — a punch and its selfie — is sent
    as `multipart/form-data` instead of JSON. `records` is then a JSON string
    in a form field, and each file is named `file.<index>.<field>`, where
    `<index>` is the record's position in that array:

        idempotency_key = "…"
        records         = [{"entity_type": "attendance", …}]
        file.0.selfie   = <the image>

    Indexed by position rather than by `local_id` because a form field name is
    client-controlled and an id may contain anything; a position cannot be
    malformed. JSON uploads are unaffected and remain the normal case.

    **Responses**
    * `200` — the batch was processed; read `results` for what happened to each
    * `201` — same, on the first delivery of this key
    * `400` — the envelope was malformed: unknown entity, missing server id,
      too many records, the same row twice, or a file naming a record that is
      not in the batch
    * `401` — missing or invalid access token
    """

    permission_classes = [IsAuthenticated]
    serializer_class = SyncUploadSerializer
    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        files, error = collect_files(request.FILES, len(data['records']))
        if error is not None:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        response, created = services.apply_batch(
            user=request.user,
            idempotency_key=data['idempotency_key'],
            device_id=data.get('device_id', ''),
            app_version=data.get('app_version', ''),
            records=data['records'],
            files=files,
        )

        return Response(
            response,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SyncDownloadView(GenericAPIView):
    """Everything this device may see that changed since it last asked.

    **Query**
    * `last_sync_at` — an ISO timestamp. Omitted, this is a first sync and
      everything comes back.
    * `entities` — a comma-separated subset, for a device that only needs one
      module refreshed.
    * `limit` — records per entity, default 200, capped at 500.

    Each entity block carries `has_more` and a `cursor`. When `has_more` is
    true, send the cursor back as `last_sync_at` to collect the rest — the
    rows are ordered by the column being filtered on, so resuming cannot skip
    a record or repeat one.

    Scoping is per entity: a device gets its own attendance, beats, visits and
    orders, and the whole customer book, because everyone sells to the same
    customers.

    **Responses**
    * `200` — the changes
    * `400` — a malformed `last_sync_at` or an unknown entity
    * `401` — missing or invalid access token
    """

    permission_classes = [IsAuthenticated]
    serializer_class = SyncDownloadQuerySerializer
    http_method_names = ['get', 'head', 'options']

    def get(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        filters = serializer.validated_data

        started = time.monotonic()
        since = filters.get('last_sync_at')

        payload, total = services.collect_changes(
            user=request.user,
            since=since,
            entities=filters.get('entities') or None,
            limit=filters.get('limit') or services.DEFAULT_DOWNLOAD_LIMIT,
        )
        duration_ms = int((time.monotonic() - started) * 1000)

        services.log_download(
            user=request.user,
            device_id=filters.get('device_id', ''),
            since=since,
            payload=payload,
            total=total,
            duration_ms=duration_ms,
        )

        return Response(
            {
                # The device stores this and sends it back as `last_sync_at`.
                # Taken from the server's clock, never the device's — a phone
                # whose clock is ten minutes slow would otherwise re-download
                # ten minutes of records forever, or worse, skip them.
                'server_time': timezone.now().isoformat(),
                'last_sync_at': since.isoformat() if since else None,
                'records_returned': total,
                'duration_ms': duration_ms,
                'entities': payload,
            }
        )


@extend_schema(
    responses={200: OpenApiTypes.OBJECT},
    summary='What this server supports, and where this device left off',
)
class SyncStatusView(GenericAPIView):
    """What this server supports, and where this device left off.

    A client calls it before syncing to check the protocol version and to
    discover which entities and operations it may send — so adding a module to
    sync does not need a client release to be usable.

    **Responses**
    * `200` — the status
    * `401` — missing or invalid access token
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'head', 'options']

    def get(self, request, *args, **kwargs):
        last_batch = (
            SyncBatch.objects.filter(user=request.user)
            .order_by('-started_at')
            .first()
        )

        return Response(
            {
                'server_time': timezone.now().isoformat(),
                'sync_version': SYNC_PROTOCOL_VERSION,
                'supported_entities': list(SUPPORTED_ENTITIES),
                'entities': describe(),
                'limits': {
                    'max_batch_records': services.MAX_BATCH_RECORDS,
                    'default_download_limit': services.DEFAULT_DOWNLOAD_LIMIT,
                    'max_download_limit': services.MAX_DOWNLOAD_LIMIT,
                },
                'last_upload': (
                    {
                        'batch_id': str(last_batch.pk),
                        'at': last_batch.started_at.isoformat(),
                        'status': last_batch.status,
                        'records': last_batch.records_total,
                        'applied': last_batch.records_applied,
                        'failed': last_batch.records_failed,
                    }
                    if last_batch
                    else None
                ),
            }
        )
