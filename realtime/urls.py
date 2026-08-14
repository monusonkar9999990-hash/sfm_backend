"""Mounted under /api/<version>/realtime/.

The socket itself is not routed here — it never reaches Django's URL
resolver. `socketio.WSGIApp` sits in front of the whole project in
`config/wsgi.py` and answers `/socket.io/` before Django sees the request.
"""

from django.urls import path

from .views import RealtimeTicketView

app_name = 'realtime'

urlpatterns = [
    path('ticket/', RealtimeTicketView.as_view(), name='ticket'),
]
