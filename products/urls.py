"""Product routes, mounted under /api/<version>/products/."""

from django.urls import path

from .views import ProductDetailView, ProductListCreateView

app_name = 'products'

urlpatterns = [
    path('', ProductListCreateView.as_view(), name='list'),
    path('<uuid:pk>/', ProductDetailView.as_view(), name='detail'),
]
