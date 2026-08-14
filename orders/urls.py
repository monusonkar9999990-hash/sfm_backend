"""Order routes, mounted under /api/<version>/orders/."""

from django.urls import path

from .views import (
    CancelOrderView,
    OrderDetailView,
    OrderListCreateView,
    SubmitOrderView,
)

app_name = 'orders'

urlpatterns = [
    path('', OrderListCreateView.as_view(), name='list'),
    path('<uuid:pk>/', OrderDetailView.as_view(), name='detail'),
    path('<uuid:pk>/submit/', SubmitOrderView.as_view(), name='submit'),
    path('<uuid:pk>/cancel/', CancelOrderView.as_view(), name='cancel'),
]
