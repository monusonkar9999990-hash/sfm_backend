"""Admin for beats and their plans.

Routes are maintained here — this is where a supervisor builds a beat, orders
its stops and assigns it. Plans are read-only: what happened on a run is
evidence, the same as attendance.
"""

from django.contrib import admin

from .models import Beat, BeatOutlet, BeatPlan, BeatPlanVisit


class BeatOutletInline(admin.TabularInline):
    model = BeatOutlet
    extra = 1
    fields = ('sequence', 'customer_ref', 'customer_name', 'address', 'phone')
    ordering = ('sequence',)


@admin.register(Beat)
class BeatAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'city', 'assigned_user', 'schedule_label', 'outlet_count', 'is_active')
    list_filter = ('is_active', 'frequency', 'city', 'territory')
    search_fields = ('code', 'name', 'area', 'city')
    autocomplete_fields = ('assigned_user', 'territory')
    inlines = [BeatOutletInline]

    @admin.display(description='runs on')
    def schedule_label(self, obj):
        return obj.schedule_label

    @admin.display(description='outlets')
    def outlet_count(self, obj):
        return obj.outlet_count


class BeatPlanVisitInline(admin.TabularInline):
    model = BeatPlanVisit
    extra = 0
    can_delete = False
    fields = ('sequence', 'customer_name', 'status', 'visited_at', 'skip_reason')
    readonly_fields = fields
    ordering = ('sequence',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(BeatPlan)
class BeatPlanAdmin(admin.ModelAdmin):
    list_display = ('date', 'beat', 'user', 'status', 'planned_outlet_count', 'covered', 'skipped')
    list_filter = ('status', 'date')
    search_fields = ('beat__code', 'beat__name', 'user__employee_code', 'user__full_name')
    date_hierarchy = 'date'
    inlines = [BeatPlanVisitInline]
    readonly_fields = tuple(
        field.name for field in BeatPlan._meta.fields if field.name != 'remarks'
    )

    @admin.display(description='covered')
    def covered(self, obj):
        return obj.covered_count

    @admin.display(description='skipped')
    def skipped(self, obj):
        return obj.skipped_count

    def has_add_permission(self, request):
        # Plans are made by scheduling a beat, not by typing one in.
        return False

    def has_delete_permission(self, request, obj=None):
        return False
