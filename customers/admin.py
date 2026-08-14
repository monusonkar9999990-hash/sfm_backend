from django.contrib import admin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        'code',
        'name',
        'type',
        'city',
        'phone',
        'contact_person',
        'is_active',
        'created_at',
    )
    list_filter = ('type', 'is_active', 'state', 'city')
    search_fields = ('code', 'name', 'phone', 'contact_person', 'gstin')
    readonly_fields = ('id', 'code', 'sync_id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    autocomplete_fields = ()

    fieldsets = (
        (None, {'fields': ('name', 'code', 'type', 'is_active')}),
        ('Contact', {'fields': ('contact_person', 'phone', 'email')}),
        ('Address', {'fields': ('address', 'city', 'state', 'pincode')}),
        ('Trade', {'fields': ('gstin', 'credit_limit', 'territory')}),
        (
            'Audit',
            {
                'fields': ('id', 'sync_id', 'created_by', 'created_at', 'updated_at'),
                'classes': ('collapse',),
            },
        ),
    )
