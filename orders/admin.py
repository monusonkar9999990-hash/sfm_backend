from django.contrib import admin

from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    # Every one of these is computed or frozen at the moment the line was
    # written. Editing them here would put the order's totals out of step with
    # its lines without going through `recalculate`.
    readonly_fields = ('unit_price', 'gst_percent', 'line_total')
    autocomplete_fields = ('product',)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'order_number',
        'customer',
        'employee',
        'status',
        'order_date',
        'grand_total',
    )
    list_filter = ('status', 'order_date')
    search_fields = ('order_number', 'customer__name', 'customer__code')
    date_hierarchy = 'order_date'
    ordering = ('-order_date',)
    inlines = [OrderItemInline]

    readonly_fields = (
        'id',
        'order_number',
        'subtotal',
        'discount_total',
        'gst_total',
        'grand_total',
        'submitted_at',
        'cancelled_at',
        'sync_id',
        'created_at',
        'updated_at',
    )

    fieldsets = (
        (None, {'fields': ('order_number', 'customer', 'employee', 'status')}),
        ('Detail', {'fields': ('order_date', 'remarks')}),
        (
            'Totals',
            {
                'fields': (
                    'subtotal',
                    'discount_total',
                    'gst_total',
                    'grand_total',
                ),
                'description': (
                    'Computed from the lines below. Save the order after '
                    'changing an item to bring these back into step.'
                ),
            },
        ),
        (
            'Lifecycle',
            {'fields': ('submitted_at', 'cancelled_at', 'cancellation_reason')},
        ),
        (
            'Audit',
            {
                'fields': ('id', 'sync_id', 'created_at', 'updated_at'),
                'classes': ('collapse',),
            },
        ),
    )

    def save_related(self, request, form, formsets, change):
        """Totals follow the lines, even when the lines were edited here."""
        super().save_related(request, form, formsets, change)
        form.instance.recalculate()
