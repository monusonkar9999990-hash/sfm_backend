from django.contrib import admin

from .models import Announcement, AppRelease, LegalDocument


@admin.register(LegalDocument)
class LegalDocumentAdmin(admin.ModelAdmin):
    list_display = ('kind', 'version', 'title', 'effective_date', 'is_published')
    list_filter = ('kind', 'is_published')
    search_fields = ('title', 'version', 'content')
    date_hierarchy = 'effective_date'
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(AppRelease)
class AppReleaseAdmin(admin.ModelAdmin):
    list_display = (
        'platform',
        'version',
        'minimum_supported_version',
        'force_update',
        'is_current',
    )
    list_filter = ('platform', 'is_current', 'force_update')
    search_fields = ('version', 'release_notes')
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'priority', 'start_date', 'end_date', 'is_active')
    list_filter = ('priority', 'is_active')
    search_fields = ('title', 'message')
    date_hierarchy = 'start_date'
    readonly_fields = ('id', 'created_at', 'updated_at')
