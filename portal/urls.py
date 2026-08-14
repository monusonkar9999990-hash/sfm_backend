"""Routes for the web portal, mounted by config/urls.py under `/portal/`."""

from django.urls import path, re_path

from .views import asset, index

app_name = 'portal'

urlpatterns = [
    path('', index, name='index'),
    # Everything else under the prefix: the Flutter bootstrap, main.dart.js,
    # the asset bundle, canvaskit. Last so it cannot shadow the entry point.
    re_path(r'^(?P<path>.+)$', asset, name='asset'),
]
