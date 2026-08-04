"""Admin for attendance review.

Records are read-only here on purpose: attendance is evidence, and an admin
who can silently rewrite a punch destroys the reason for collecting it. Only
the note stays editable, for a supervisor's remark.
"""

from django.contrib import admin

from .models import Attendance, GeoFence


@admin.register(GeoFence)
class GeoFenceAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'latitude', 'longitude', 'radius_meters', 'is_active')
    list_filter = ('is_active', 'territory')
    search_fields = ('name', 'code')
    autocomplete_fields = ('territory',)


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = (
        'day',
        'user',
        'punch_in_at',
        'punch_out_at',
        'is_late',
        'punch_in_within_fence',
        'worked_minutes',
        'source',
    )
    list_filter = ('is_late', 'source', 'punch_in_within_fence', 'day')
    search_fields = ('user__employee_code', 'user__full_name')
    date_hierarchy = 'day'
    autocomplete_fields = ('user',)
    readonly_fields = tuple(
        field.name for field in Attendance._meta.fields if field.name != 'note'
    )

    def has_add_permission(self, request):
        # Attendance is created by punching, not by typing.
        return False

    def has_delete_permission(self, request, obj=None):
        return False
