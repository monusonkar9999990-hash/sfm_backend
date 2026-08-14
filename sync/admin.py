from django.contrib import admin

from .models import SyncBatch, SyncDownloadLog, SyncRecord


class SyncRecordInline(admin.TabularInline):
    model = SyncRecord
    extra = 0
    can_delete = False
    fields = (
        'entity_type',
        'operation',
        'local_id',
        'server_id',
        'status',
        'http_status',
        'detail',
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SyncBatch)
class SyncBatchAdmin(admin.ModelAdmin):
    list_display = (
        'idempotency_key',
        'user',
        'device_id',
        'status',
        'records_total',
        'records_applied',
        'records_failed',
        'duration_ms',
        'started_at',
    )
    list_filter = ('status', 'started_at', 'app_version')
    search_fields = ('idempotency_key', 'device_id', 'user__employee_code')
    date_hierarchy = 'started_at'
    inlines = [SyncRecordInline]

    # A ledger is only worth having if nobody edits it.
    readonly_fields = tuple(
        field.name for field in SyncBatch._meta.fields
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SyncDownloadLog)
class SyncDownloadLogAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'device_id',
        'since',
        'records_returned',
        'duration_ms',
        'created_at',
    )
    list_filter = ('created_at',)
    search_fields = ('device_id', 'user__employee_code')
    date_hierarchy = 'created_at'
    readonly_fields = tuple(field.name for field in SyncDownloadLog._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
