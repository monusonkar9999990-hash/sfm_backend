from django.contrib import admin

from .models import AppSetting, AuditLog


@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    list_display = ('key', 'value', 'value_type', 'updated_by', 'updated_at')
    list_filter = ('value_type',)
    search_fields = ('key',)
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        'created_at',
        'actor_code',
        'action',
        'entity',
        'entity_id',
        'ip_address',
    )
    list_filter = ('action', 'entity', 'created_at')
    search_fields = ('actor_code', 'summary', 'entity', 'entity_id', 'ip_address')
    date_hierarchy = 'created_at'
    readonly_fields = tuple(field.name for field in AuditLog._meta.fields)

    # Append-only, here as well as over the API. A trail somebody can edit
    # from the admin is not a trail.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
