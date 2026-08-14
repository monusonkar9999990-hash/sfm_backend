from django.contrib import admin

from .models import Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        'product_code',
        'name',
        'brand',
        'category',
        'unit',
        'mrp',
        'selling_price',
        'gst_percent',
        'stock_quantity',
        'active',
    )
    list_filter = ('category', 'active', 'unit', 'brand')
    search_fields = ('product_code', 'name', 'brand')
    readonly_fields = ('id', 'sync_id', 'created_at', 'updated_at')
    ordering = ('name',)
    list_editable = ('stock_quantity', 'active')

    fieldsets = (
        (None, {'fields': ('name', 'product_code', 'brand', 'category', 'active')}),
        ('Detail', {'fields': ('description', 'unit')}),
        ('Pricing', {'fields': ('mrp', 'selling_price', 'gst_percent')}),
        ('Stock', {'fields': ('stock_quantity',)}),
        (
            'Audit',
            {
                'fields': ('id', 'sync_id', 'created_at', 'updated_at'),
                'classes': ('collapse',),
            },
        ),
    )
