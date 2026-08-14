"""Serializers for the sync endpoints.

The upload serializer validates the *envelope* only — that a record names a
known entity, a known operation and a payload that is at least an object. What
is inside the payload is the owning module's business, and it will be checked
by that module's own serializer when the record is dispatched. Duplicating
those rules here is exactly the drift this module is built to avoid.
"""

import json

from rest_framework import serializers

from .models import Operation, new_idempotency_key
from .registry import SUPPORTED_ENTITIES
from .services import MAX_BATCH_RECORDS


class SyncRecordSerializer(serializers.Serializer):
    """One offline record, as the device queued it."""

    entity_type = serializers.ChoiceField(choices=SUPPORTED_ENTITIES)

    # The device's own primary key for the row. Opaque here and echoed back so
    # the client can match the answer to the row it came from — and used to
    # recognise the same row arriving twice.
    local_id = serializers.CharField(max_length=128)

    server_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, allow_null=True
    )

    operation = serializers.ChoiceField(choices=Operation.choices)

    payload = serializers.DictField(required=False, default=dict)

    # When the device recorded it. Passed through to the modules that accept a
    # `captured_at`, so a punch keeps the moment it happened rather than the
    # moment the signal came back.
    device_timestamp = serializers.DateTimeField(required=False, allow_null=True)

    # The `updated_at` the device last saw on this record. Present, it is an
    # optimistic-concurrency claim and a stale update is refused; absent, the
    # write is applied as-is.
    sync_version = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, attrs):
        operation = attrs['operation']

        if operation in (Operation.UPDATE, Operation.DELETE) and not attrs.get(
            'server_id'
        ):
            raise serializers.ValidationError(
                {
                    'server_id': (
                        f'A {operation} needs the server id of the record it '
                        f'changes.'
                    )
                }
            )

        return attrs


class SyncUploadSerializer(serializers.Serializer):
    """A batch of records from one device."""

    # Optional so a first-time or simple client still works, but a key the
    # client did not choose cannot dedupe that client's retry — the response
    # says which one was used either way.
    idempotency_key = serializers.CharField(
        max_length=64, required=False, allow_blank=True
    )

    device_id = serializers.CharField(
        max_length=128, required=False, allow_blank=True
    )
    app_version = serializers.CharField(
        max_length=32, required=False, allow_blank=True
    )

    records = SyncRecordSerializer(many=True)

    def to_internal_value(self, data):
        """Accepts `records` as a JSON string, for a multipart batch.

        A multipart body has no nested types — every field arrives as a
        string — so a device sending photos sends the record array as JSON in
        one field. Expanded here rather than in the view, so exactly the same
        validation runs whichever way the batch arrived.
        """
        records = data.get('records') if hasattr(data, 'get') else None

        if isinstance(records, str):
            try:
                parsed = json.loads(records)
            except ValueError:
                raise serializers.ValidationError(
                    {'records': 'Send a JSON array of records.'}
                )
            if not isinstance(parsed, list):
                raise serializers.ValidationError(
                    {'records': 'Send a JSON array of records.'}
                )
            # A `QueryDict` is immutable, and `.dict()` would drop repeated
            # keys — there are none in this envelope, and the records are
            # being replaced wholesale anyway.
            data = {
                key: data[key]
                for key in data
                if key != 'records' and not key.startswith('file.')
            }
            data['records'] = parsed

        return super().to_internal_value(data)

    def validate_records(self, value):
        if not value:
            raise serializers.ValidationError('Send at least one record.')

        if len(value) > MAX_BATCH_RECORDS:
            raise serializers.ValidationError(
                f'Send at most {MAX_BATCH_RECORDS} records per batch; this '
                f'one has {len(value)}.'
            )

        # A batch that contains the same local row twice is a client bug, and
        # letting it through would make the response ambiguous — two results
        # with the same `local_id` and no way to tell which is which.
        seen = set()
        for record in value:
            key = (record['entity_type'], record['local_id'])
            if key in seen:
                raise serializers.ValidationError(
                    f"{record['entity_type']}/{record['local_id']} appears "
                    f'twice in this batch.'
                )
            seen.add(key)

        return value

    def validate(self, attrs):
        attrs['idempotency_key'] = (
            attrs.get('idempotency_key') or new_idempotency_key()
        )
        return attrs


class SyncDownloadQuerySerializer(serializers.Serializer):
    """The download's query string."""

    # Absent means a first sync: everything this user may see.
    last_sync_at = serializers.DateTimeField(required=False, allow_null=True)

    entities = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=500)
    device_id = serializers.CharField(
        max_length=128, required=False, allow_blank=True
    )

    def validate_entities(self, value):
        if not value:
            return []

        wanted = [item.strip() for item in value.split(',') if item.strip()]
        unknown = [item for item in wanted if item not in SUPPORTED_ENTITIES]
        if unknown:
            raise serializers.ValidationError(
                f'Unknown entity types: {", ".join(unknown)}. '
                f'Known: {", ".join(SUPPORTED_ENTITIES)}.'
            )
        return wanted
