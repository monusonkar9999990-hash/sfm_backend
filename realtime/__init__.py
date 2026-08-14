"""Live change notifications for the management portal.

Owns no tables. It watches the modules that do and tells connected browsers
that something moved, so a dashboard on a wall stops being a snapshot of
whenever somebody last pressed refresh.

**What travels over the socket is not data.** An event carries the name of the
entity that changed and nothing else — no figures, no names, no rows. The
portal answers it by re-rendering through its own authenticated server-side
fetch, so every number on screen still arrives through the same permission
checks it always did. That is deliberate: a second delivery path for business
data is a second place to get authorisation wrong.
"""

default_app_config = 'realtime.apps.RealtimeConfig'
