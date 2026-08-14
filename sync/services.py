"""Applying an upload batch, and collecting a download.

Three layers of protection against duplicate data, because a phone on a bad
connection will retry, and each layer catches a case the others miss:

1. **The batch key.** Same `idempotency_key` from the same user returns the
   first attempt's stored response without touching a row. This is the layer
   that matters when the device sent everything successfully and then lost the
   reply — the work is not redone, and the device sees the same server ids it
   would have seen the first time.

2. **The record's `local_id`.** A record whose `local_id` this user has
   already had applied is answered from the ledger with its original
   `server_id`. This is what catches the same row arriving in a *different*
   batch — a device that regenerated its batch key, or split one queue across
   two uploads.

3. **The module's own `sync_id`.** Attendance, beat plans, site visits and
   orders each dedupe on a device-generated id of their own, and answer a
   repeat with the original record. This is the net under the other two, and
   it was there before this module existed.

Nothing here rewrites what the owning modules say. A punch outside the
geofence is refused in the attendance module's words; an order for a
deactivated customer in the orders module's. This file decides *whether to
dispatch*, and records what came back.
"""

import json
import time

from django.db import transaction
from django.utils import timezone
from rest_framework import status as http
from rest_framework.utils.encoders import JSONEncoder

from . import dispatch
from .models import (
    BatchStatus,
    Operation,
    RecordStatus,
    SyncBatch,
    SyncDownloadLog,
    SyncRecord,
)
from .registry import REGISTRY, entity_for

# A device sending more than this in one request is queueing wrongly, and the
# request will time out before it finishes. The limit is enforced in the
# serializer; it lives here so both places quote the same number.
MAX_BATCH_RECORDS = 200

# Per entity, per download. The response carries `has_more` so a device knows
# to come back rather than assuming it is up to date.
DEFAULT_DOWNLOAD_LIMIT = 200
MAX_DOWNLOAD_LIMIT = 500


def json_safe(value):
    """Plain JSON types, using DRF's own encoder.

    A serializer's `.data` is not JSON — it holds `UUID`, `Decimal` and
    `datetime` objects that DRF's renderer converts on the way out. The ledger
    stores this same structure in a `JSONField`, which cannot, so it is
    converted once here and both the stored copy and the returned copy come
    from that. Converting once is also what makes the replay guarantee real:
    the body a device gets on a retry is the identical structure it got the
    first time, not a re-serialisation of it.
    """
    return json.loads(json.dumps(value, cls=JSONEncoder))


class RecordOutcome:
    """What happened to one uploaded record."""

    def __init__(
        self, *, record, status, server_id='', http_status=None, detail=None, data=None
    ):
        self.record = record
        self.status = status
        self.server_id = str(server_id or '')
        self.http_status = http_status
        self.detail = detail or {}
        self.data = data

    def as_dict(self):
        payload = {
            'local_id': self.record['local_id'],
            'entity_type': self.record['entity_type'],
            'operation': self.record['operation'],
            'status': self.status,
            'server_id': self.server_id,
            'http_status': self.http_status,
        }
        if self.detail:
            payload['detail'] = self.detail
        if self.data is not None:
            payload['data'] = self.data
        return payload


# --------------------------------------------------------------------- upload


def apply_batch(
    *,
    user,
    idempotency_key,
    device_id,
    app_version,
    records,
    files=None,
):
    """Runs a batch and returns the response body.

    The batch row is claimed before any record is processed, so two uploads
    racing on the same key cannot both do the work — the loser finds the
    winner's row and waits for its answer.

    [files] maps a record's position in [records] to the uploads that belong
    to it: `{0: {'selfie': <UploadedFile>}}`. Position rather than `local_id`
    because a form field name is client-controlled and an id may contain
    anything; an index cannot be malformed.
    """
    started = time.monotonic()

    batch, created = SyncBatch.objects.get_or_create(
        user=user,
        idempotency_key=idempotency_key,
        defaults={
            'device_id': device_id or '',
            'app_version': app_version or '',
            'records_total': len(records),
        },
    )

    if not created:
        # Seen before. If it finished, hand back exactly what was sent then.
        if batch.status != BatchStatus.PROCESSING:
            return {**batch.response, 'replayed': True}, False

        # Still in flight, or abandoned mid-way by a crashed worker. Reporting
        # it as in-progress is honest; guessing is not.
        return {
            'batch_id': str(batch.pk),
            'idempotency_key': batch.idempotency_key,
            'replayed': True,
            'status': BatchStatus.PROCESSING,
            'detail': (
                'This batch is already being processed. Retry in a moment to '
                'collect the result.'
            ),
            'results': [],
            'summary': _summary([]),
        }, False

    outcomes = [
        _apply_record(
            user=user,
            record=record,
            files=(files or {}).get(index),
        )
        for index, record in enumerate(records)
    ]

    duration_ms = int((time.monotonic() - started) * 1000)
    summary = _summary(outcomes)

    response = json_safe(
        {
            'batch_id': str(batch.pk),
            'idempotency_key': batch.idempotency_key,
            'replayed': False,
            'processed_at': timezone.now().isoformat(),
            'duration_ms': duration_ms,
            'summary': summary,
            'results': [outcome.as_dict() for outcome in outcomes],
        }
    )

    SyncRecord.objects.bulk_create(
        [
            SyncRecord(
                batch=batch,
                entity_type=outcome.record['entity_type'],
                local_id=outcome.record['local_id'],
                server_id=outcome.server_id,
                operation=outcome.record['operation'],
                status=outcome.status,
                http_status=outcome.http_status,
                detail=json_safe(outcome.detail),
            )
            for outcome in outcomes
        ]
    )

    batch.status = (
        BatchStatus.FAILED
        if outcomes and summary['failed'] == len(outcomes)
        else BatchStatus.COMPLETED
    )
    batch.records_total = len(outcomes)
    batch.records_applied = summary['applied']
    batch.records_duplicate = summary['duplicate']
    batch.records_conflicted = summary['conflict']
    batch.records_failed = summary['failed']
    batch.completed_at = timezone.now()
    batch.duration_ms = duration_ms
    batch.response = response
    batch.save()

    return response, True


def _summary(outcomes):
    counts = {'total': len(outcomes), 'applied': 0, 'duplicate': 0, 'conflict': 0,
              'failed': 0}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return counts


def _apply_record(*, user, record, files=None):
    """One record, in its own savepoint.

    A record that fails rolls back only itself. The alternative — one
    transaction for the whole batch — means a single bad row on a phone that
    has been offline for a week rejects the other ninety-nine, and the device
    has no way to work out which one was at fault.
    """
    entity = entity_for(record['entity_type'])
    if entity is None:
        return RecordOutcome(
            record=record,
            status=RecordStatus.FAILED,
            http_status=http.HTTP_400_BAD_REQUEST,
            detail={'entity_type': f"'{record['entity_type']}' cannot be synced."},
        )

    # Layer 2: this exact row, already applied for this user, in any batch.
    already = (
        SyncRecord.objects.filter(
            batch__user=user,
            entity_type=record['entity_type'],
            local_id=record['local_id'],
            status=RecordStatus.APPLIED,
        )
        .order_by('created_at')
        .first()
    )
    if already is not None:
        return RecordOutcome(
            record=record,
            status=RecordStatus.DUPLICATE,
            server_id=already.server_id,
            http_status=http.HTTP_200_OK,
            detail={'detail': 'Already applied on an earlier upload.'},
        )

    route, error = _resolve_route(entity, record)
    if error is not None:
        return RecordOutcome(
            record=record,
            status=RecordStatus.FAILED,
            http_status=http.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    conflict = _detect_conflict(entity=entity, record=record, user=user)
    if conflict is not None:
        return conflict

    payload = dict(record.get('payload') or {})
    payload.pop('action', None)

    # URL kwargs are lifted out first, then anything left over is stripped.
    # The other order removes `plan_id` from the payload before the router
    # can read it, and every nested route fails asking for what it just threw
    # away.
    url_kwargs = {}
    for kwarg, source in route.url_kwargs.items():
        value = record.get(source) or payload.pop(source, None)
        if not value:
            return RecordOutcome(
                record=record,
                status=RecordStatus.FAILED,
                http_status=http.HTTP_400_BAD_REQUEST,
                detail={source: f'{source} is required for this operation.'},
            )
        url_kwargs[kwarg] = value

    for key in route.strip:
        payload.pop(key, None)

    path = route.path.format(**url_kwargs)

    try:
        with transaction.atomic():
            response = dispatch.call(
                route.view,
                method=route.method,
                path=path,
                user=user,
                payload=payload,
                files=files,
                **url_kwargs,
            )
            data = getattr(response, 'data', None)

            if response.status_code >= 400:
                # Raised so the savepoint unwinds, then caught immediately
                # below — a rejected write must leave nothing behind.
                raise _RecordRejected(response.status_code, data)
    except _RecordRejected as rejected:
        return RecordOutcome(
            record=record,
            status=(
                RecordStatus.CONFLICT
                if rejected.status_code == http.HTTP_409_CONFLICT
                else RecordStatus.FAILED
            ),
            http_status=rejected.status_code,
            detail=_as_detail(rejected.data),
        )

    server_id = ''
    if isinstance(data, dict):
        server_id = data.get('id') or ''

    return RecordOutcome(
        record=record,
        # A create that answers 200 rather than 201 is a module telling us it
        # recognised the `sync_id` — layer 3.
        status=(
            RecordStatus.DUPLICATE
            if record['operation'] == Operation.CREATE
            and response.status_code == http.HTTP_200_OK
            else RecordStatus.APPLIED
        ),
        server_id=server_id,
        http_status=response.status_code,
        data=data,
    )


class _RecordRejected(Exception):
    def __init__(self, status_code, data):
        self.status_code = status_code
        self.data = data
        super().__init__(f'rejected with {status_code}')


def _as_detail(data):
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    return {'detail': data}


def _resolve_route(entity, record):
    """Picks the endpoint for this record, or explains why there isn't one."""
    operation = record['operation']

    if operation == Operation.CREATE:
        if entity.create is None:
            return None, {
                'operation': f'{entity.key} cannot be created from a device.'
            }
        return entity.create, None

    if operation == Operation.DELETE:
        if entity.delete is None:
            return None, {
                'operation': f'{entity.key} cannot be deleted from a device.'
            }
        return entity.delete, None

    # An update is either a named action or a field edit.
    action = (record.get('payload') or {}).get('action')
    if action:
        route = entity.actions.get(action)
        if route is None:
            known = sorted(entity.actions) or ['(none)']
            return None, {
                'action': (
                    f"'{action}' is not something {entity.key} does. "
                    f'Known actions: {", ".join(known)}.'
                )
            }
        return route, None

    if entity.update is None:
        known = sorted(entity.actions)
        if known:
            return None, {
                'action': (
                    f'{entity.key} is updated by action. Send one of: '
                    f'{", ".join(known)}.'
                )
            }
        return None, {'operation': f'{entity.key} cannot be updated from a device.'}

    return entity.update, None


def _detect_conflict(*, entity, record, user):
    """Optimistic concurrency, on the version the device last saw.

    `sync_version` is the `updated_at` the record had when the device took its
    copy. If the server's is newer, somebody else changed the row while this
    device was offline, and applying the device's version would silently throw
    that change away. The upload is refused and the current server state comes
    back with it, so the device can merge rather than guess.

    Last-write-wins is used only where the client did not claim a version —
    a device that says nothing about what it saw is not making a claim to
    check.
    """
    if record['operation'] == Operation.CREATE:
        return None

    claimed = record.get('sync_version')
    server_id = record.get('server_id')
    if not claimed or not server_id:
        return None

    current = entity.scoped(user).filter(pk=server_id).first()
    if current is None:
        return None

    updated_at = getattr(current, 'updated_at', None)
    if updated_at is None or updated_at <= claimed:
        return None

    return RecordOutcome(
        record=record,
        status=RecordStatus.CONFLICT,
        server_id=str(server_id),
        http_status=http.HTTP_409_CONFLICT,
        detail={
            'detail': (
                'This record changed on the server after your device last saw '
                'it. Nothing was overwritten.'
            ),
            'your_version': claimed.isoformat(),
            'server_version': updated_at.isoformat(),
        },
        data=entity.download_serializer(current).data,
    )


# ------------------------------------------------------------------- download


def collect_changes(*, user, since, entities=None, limit=DEFAULT_DOWNLOAD_LIMIT):
    """Everything this user may see that changed after `since`.

    Ordered by `updated_at` so a device that hits the page limit can resume
    from the last row it received rather than starting again.
    """
    limit = max(1, min(limit, MAX_DOWNLOAD_LIMIT))
    wanted = entities or list(REGISTRY)

    payload = {}
    total = 0

    for key in wanted:
        entity = REGISTRY.get(key)
        if entity is None:
            continue

        queryset = entity.scoped(user)
        if since is not None:
            queryset = queryset.filter(updated_at__gt=since)

        rows = list(queryset.order_by('updated_at', 'pk')[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]

        payload[key] = {
            'count': len(rows),
            'has_more': has_more,
            # Where to resume from. Null when nothing came back, so a device
            # keeps the cursor it already had.
            'cursor': rows[-1].updated_at.isoformat() if rows else None,
            'records': entity.download_serializer(rows, many=True).data,
        }
        total += len(rows)

    return payload, total


def log_download(*, user, device_id, since, payload, total, duration_ms):
    return SyncDownloadLog.objects.create(
        user=user,
        device_id=device_id or '',
        since=since,
        records_returned=total,
        entities={key: block['count'] for key, block in payload.items()},
        duration_ms=duration_ms,
    )
