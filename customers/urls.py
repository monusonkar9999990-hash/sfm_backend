"""Customer routes, mounted under /api/<version>/customers/."""

from django.urls import path

from .views import CustomerDetailView, CustomerListCreateView

app_name = 'customers'

urlpatterns = [
    path('', CustomerListCreateView.as_view(), name='list'),
    path('<uuid:pk>/', CustomerDetailView.as_view(), name='detail'),
]
