"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/

Django is wrapped in the Socket.IO application so that the live-updates socket
and the API share one port and one process. Everything that is not
``/socket.io/`` falls through to Django untouched, so the API, the admin and
the Flutter portal are unaffected by its presence.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_application = get_wsgi_application()

# Imported after `get_wsgi_application()`: the app registry has to be ready
# before `realtime` — and the models its signals hang off — can be imported.
from realtime.server import wsgi_application  # noqa: E402

application = wsgi_application(django_application)
