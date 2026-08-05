"""Admin for sites and their visits.

Sites are maintained here — this is where a supervisor adds a project and
plots it on the map. Visits are read-only: what somebody found on a site on a
given day is evidence, the same as attendance and beat runs.
"""

from django.contrib import admin

from .models import Site, SiteVisit, SiteVisitImage


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = (
        'code',
        'name',
        'customer_name',
        'city',
        'stage',
        'plotted',
        'is_active',
    )
    list_filter = ('stage', 'is_active', 'city', 'territory')
    search_fields = ('code', 'name', 'customer_name', 'customer_ref', 'city')
    autocomplete_fields = ('territory',)

    @admin.display(boolean=True, description='on the map')
    def plotted(self, obj):
        return obj.has_coordinates


class SiteVisitImageInline(admin.TabularInline):
    model = SiteVisitImage
    extra = 0
    can_delete = False
    fields = ('image', 'tag', 'caption', 'captured_at')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SiteVisit)
class SiteVisitAdmin(admin.ModelAdmin):
    list_display = (
        'check_in_at',
        'site',
        'user',
        'purpose',
        'status',
        'duration_minutes',
        'photo_count',
        'follow_up_date',
    )
    list_filter = ('status', 'purpose', 'stage_observed', 'check_in_at')
    search_fields = (
        'site__name',
        'site__code',
        'user__employee_code',
        'user__full_name',
    )
    date_hierarchy = 'check_in_at'
    inlines = [SiteVisitImageInline]
    readonly_fields = tuple(
        field.name for field in SiteVisit._meta.fields if field.name != 'remarks'
    )

    @admin.display(description='photos')
    def photo_count(self, obj):
        return obj.images.count()

    def has_add_permission(self, request):
        # A visit is created by standing at a site, not by typing.
        return False

    def has_delete_permission(self, request, obj=None):
        return False
