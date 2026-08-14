"""Gunicorn settings for production.

    gunicorn config.wsgi:application -c config/gunicorn.conf.py

Everything here is overridable by environment variable, because the right
worker count depends on the box and the right timeout depends on how big the
photos are.
"""

import multiprocessing
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Bind to a port on localhost and let nginx be the only thing facing the
# internet. Gunicorn's own HTTP parser is not written to be exposed directly:
# it has no protection against slow-client attacks, which nginx buffers away.
#
# A managed platform is the exception: it puts its own proxy in front and tells
# the process which port to listen on through `PORT`. Binding to localhost
# there means a container that starts, passes no health check and is killed —
# so `PORT` switches this to every interface. Nothing is exposed directly in
# either case; the platform's proxy plays nginx's part.
if os.environ.get('GUNICORN_BIND'):
    bind = os.environ['GUNICORN_BIND']
elif os.environ.get('PORT'):
    bind = f"0.0.0.0:{os.environ['PORT']}"
else:
    bind = '127.0.0.1:8000'

# The usual starting point. Two workers on a one-core box is not an
# under-provision — one worker means a single slow request blocks everything
# behind it, health check included.
workers = _int('GUNICORN_WORKERS', multiprocessing.cpu_count() * 2 + 1)

# Threads, not extra processes, for the waiting this app actually does: MySQL
# round trips and writing uploaded photos to disk. Both release the GIL.
threads = _int('GUNICORN_THREADS', 2)
worker_class = os.environ.get('GUNICORN_WORKER_CLASS', 'gthread')

# A field executive on one bar of signal uploading a selfie is a slow request,
# not a stuck one. 30s (the default) kills those; nginx is configured to give
# up at the same point, so neither waits on a request the other has abandoned.
timeout = _int('GUNICORN_TIMEOUT', 120)
graceful_timeout = _int('GUNICORN_GRACEFUL_TIMEOUT', 30)

# Slightly above nginx's keepalive_timeout, so nginx closes idle connections
# rather than gunicorn closing one nginx still believes it holds.
keepalive = _int('GUNICORN_KEEPALIVE', 5)

# Recycle workers to cap the damage from a slow leak. The jitter stops every
# worker retiring on the same request and leaving a gap in capacity.
max_requests = _int('GUNICORN_MAX_REQUESTS', 1000)
max_requests_jitter = _int('GUNICORN_MAX_REQUESTS_JITTER', 100)

# Logs to stdout/stderr, which is what systemd's journal and every container
# runtime collect. Writing to files inside the app directory means logs that
# nobody rotates and nobody reads.
accesslog = os.environ.get('GUNICORN_ACCESS_LOG', '-')
errorlog = os.environ.get('GUNICORN_ERROR_LOG', '-')
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'info')

# %({x-forwarded-for}i)s rather than %(h)s: behind nginx every request appears
# to come from 127.0.0.1, so the default access log records nothing useful.
access_log_format = (
    '%({x-forwarded-for}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sus'
)

# Trust the proxy headers only from the local nginx. Left as '*' this is how a
# client spoofs its own IP into your logs and past any IP-based rule.
forwarded_allow_ips = os.environ.get('GUNICORN_FORWARDED_ALLOW_IPS', '127.0.0.1')

proc_name = 'sfm-backend'
