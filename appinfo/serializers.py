"""Serializers for the public payloads and the admin CRUD behind them."""

from rest_framework import serializers

from .models import Announcement, AppRelease, LegalDocument, Platform


# --------------------------------------------------------------- admin CRUD


class LegalDocumentSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True
    )
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = LegalDocument
        fields = (
            'id',
            'kind',
            'title',
            'version',
            'effective_date',
            'content',
            'is_published',
            'is_current',
            'created_by',
            'created_by_name',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_by', 'created_at', 'updated_at')

        # DRF derives a UniqueTogetherValidator from the model constraint and
        # runs it before `validate()`, which puts the message under
        # `non_field_errors` — where a form cannot show it against the field
        # somebody has to change. Suppressed here so the check below owns it;
        # the database constraint is still there, and `perform_write` turns a
        # racing violation into a 409.
        validators = []

    def get_is_current(self, obj) -> bool:
        current = LegalDocument.current(obj.kind)
        return current is not None and current.pk == obj.pk

    def validate(self, attrs):
        kind = attrs.get('kind', getattr(self.instance, 'kind', None))
        version = attrs.get('version', getattr(self.instance, 'version', None))

        clash = LegalDocument.objects.filter(kind=kind, version=version)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {'version': f'Version {version} of this document already exists.'}
            )

        return attrs


class AppReleaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppRelease
        fields = (
            'id',
            'platform',
            'version',
            'minimum_supported_version',
            'force_update',
            'download_url',
            'release_notes',
            'is_current',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        from .models import parse_version

        version = attrs.get('version', getattr(self.instance, 'version', None))
        minimum = attrs.get(
            'minimum_supported_version',
            getattr(self.instance, 'minimum_supported_version', None),
        )

        if version and minimum and parse_version(minimum) > parse_version(version):
            raise serializers.ValidationError(
                {
                    'minimum_supported_version': (
                        f'The minimum ({minimum}) is above the release itself '
                        f'({version}), which would tell every client to '
                        f'update to a version that does not exist.'
                    )
                }
            )

        return attrs


class AnnouncementSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True
    )
    is_live = serializers.BooleanField(read_only=True)

    class Meta:
        model = Announcement
        fields = (
            'id',
            'title',
            'message',
            'priority',
            'start_date',
            'end_date',
            'is_active',
            'is_live',
            'created_by',
            'created_by_name',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_by', 'created_at', 'updated_at')

    def validate(self, attrs):
        start = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        end = attrs.get('end_date', getattr(self.instance, 'end_date', None))

        if start and end and end <= start:
            raise serializers.ValidationError(
                {'end_date': 'The announcement would end before it began.'}
            )

        return attrs


# ------------------------------------------------------------------- public


class AppVersionQuerySerializer(serializers.Serializer):
    """`/app-version/` takes the platform and, optionally, what the caller is
    running — which is what turns a description of the latest release into an
    answer about *this* device."""

    platform = serializers.ChoiceField(
        choices=Platform.choices, required=False, default=Platform.ANDROID
    )
    current_version = serializers.CharField(
        max_length=20, required=False, allow_blank=True
    )
