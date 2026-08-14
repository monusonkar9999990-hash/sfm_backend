"""Views that hand the built portal to a browser.

The portal is the same Flutter application the field staff carry, compiled for
the web with `--base-href /portal/`. It is served from this project rather than
its own host for one reason worth stating: the API then lives at the same
origin, so the browser treats `/api/v1/...` as a same-site call and no CORS
policy has to be relaxed to let the portal talk to it.
"""

from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.views.static import serve as static_serve

# `web/` holds the output of `flutter build web`. It is deliberately not under
# STATIC_ROOT: collectstatic would fingerprint the filenames, and the Flutter
# bootstrap loads several of them by their literal names.
PORTAL_ROOT = Path(settings.BASE_DIR) / 'portal' / 'web'

INDEX = PORTAL_ROOT / 'index.html'


def _index_response():
    if not INDEX.exists():
        raise Http404(
            'The portal has not been built. Run, from the Flutter project:\n'
            '  flutter build web --base-href /portal/ '
            '--dart-define-from-file=config/web-portal.json\n'
            'then copy build/web into sfm_backend/portal/web.'
        )
    # No caching on the entry point. Every other file the build emits carries a
    # hashed name, but index.html keeps its own — a browser holding yesterday's
    # copy would go on asking for a main.dart.js that the new build renamed.
    response = FileResponse(INDEX.open('rb'), content_type='text/html')
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


def index(request):
    """The portal's entry point, at `/portal/`."""
    return _index_response()


def asset(request, path):
    """Any file under `/portal/`, falling back to the entry point.

    A single-page app owns its own routing, so a URL it invented — a bookmarked
    dashboard, a reload after navigating — must return index.html rather than a
    404 and let the app read the path itself. Only genuinely missing *files*
    (a stale asset name, say) should 404, which is why the fallback is limited
    to paths with no file extension.
    """
    try:
        return static_serve(request, path, document_root=PORTAL_ROOT)
    except Http404:
        if Path(path).suffix:
            raise
        return _index_response()
