"""Building the public payloads, and caching them.

Every one of these endpoints is hit by every device at start-up and answers
the same thing to all of them, which is exactly the shape that should be
cached. Each is stored under a version-stamped key; an admin write bumps the
stamp rather than deleting individual keys, so one line invalidates everything
without anybody having to remember the list.
"""

from django.conf import settings as django_settings
from django.core.cache import cache
from django.utils import timezone

from administration.settings_registry import FEATURE_PREFIX, current as current_settings

CACHE_TTL = 300
STAMP_KEY = 'appinfo:stamp'


def _stamp():
    """The current cache generation. Missing means "start at one"."""
    stamp = cache.get(STAMP_KEY)
    if stamp is None:
        stamp = 1
        cache.set(STAMP_KEY, stamp, None)
    return stamp


def cache_key(name):
    return f'appinfo:{_stamp()}:{name}'


def invalidate():
    """Bumps the generation, retiring every cached payload at once.

    Cheaper and safer than deleting keys one at a time: a new key that somebody
    forgets to add to a delete list is a stale response nobody notices.
    """
    try:
        cache.incr(STAMP_KEY)
    except ValueError:
        # Nothing cached yet — the next read seeds it.
        cache.set(STAMP_KEY, 1, None)


def cached(name, builder):
    key = cache_key(name)
    payload = cache.get(key)
    if payload is None:
        payload = builder()
        cache.set(key, payload, CACHE_TTL)
    return payload


def app_config():
    """What the app needs to configure itself, from one place.

    Values come from the administration settings store, except the three the
    server genuinely enforces — those are read from where they actually take
    effect, so this endpoint cannot disagree with the behaviour it describes.
    """
    stored = current_settings()

    def value(key):
        return stored[key]['value']

    features = {
        key[len(FEATURE_PREFIX):]: block['value']
        for key, block in stored.items()
        if key.startswith(FEATURE_PREFIX)
    }

    return {
        'company_name': value('company_name'),
        'support_email': value('support_email'),
        'support_phone': value('support_phone'),

        'attendance_radius': value('attendance_radius_meters'),

        # Read from Django settings, not from the store: this is the flag the
        # attendance module actually checks before refusing a punch without a
        # photo. A stored copy could drift from it and would be believed.
        'selfie_required': bool(
            getattr(django_settings, 'ATTENDANCE_SELFIE_REQUIRED', True)
        ),
        # Not configurable, and true because the punch and check-in
        # serializers require latitude and longitude — there is no code path
        # that records one without a fix.
        'gps_required': True,

        'max_image_size_mb': value('max_image_size_mb'),
        'max_image_size_bytes': int(float(value('max_image_size_mb')) * 1024 * 1024),
        'allowed_image_formats': [
            item.strip().lower()
            for item in str(value('allowed_image_formats')).split(',')
            if item.strip()
        ],

        # The server's zone, which is what every `day` and `order_date` in the
        # system is computed in.
        'timezone': django_settings.TIME_ZONE,

        'maintenance_mode': bool(value('maintenance_mode')),
        'maintenance_message': value('maintenance_message'),

        'feature_flags': features,

        # Which of the above the server enforces, and which the client is
        # trusted to honour. Published so nobody has to guess — the same
        # distinction /admin/settings/ makes.
        'enforced_by_server': [
            'selfie_required',
            'gps_required',
            'timezone',
            'maintenance_mode',
        ],
    }


def announcements():
    from .models import Announcement

    return [
        {
            'id': str(row.pk),
            'title': row.title,
            'message': row.message,
            'priority': row.priority,
            'start_date': row.start_date.isoformat(),
            'end_date': row.end_date.isoformat() if row.end_date else None,
            'active': True,
        }
        for row in Announcement.live()
    ]


def legal_document(kind):
    from .models import LegalDocument

    document = LegalDocument.current(kind)
    if document is None:
        return None

    return {
        'kind': document.kind,
        'title': document.title,
        'version': document.version,
        'effective_date': document.effective_date.isoformat(),
        'content': document.content,
        'updated_at': document.updated_at.isoformat(),
    }


def app_version(platform):
    from .models import AppRelease

    release = AppRelease.current(platform)
    if release is None:
        return None

    return {
        'platform': release.platform,
        'latest_version': release.version,
        'minimum_supported_version': release.minimum_supported_version,
        'force_update': release.force_update,
        'download_url': release.download_url,
        'release_notes': release.release_notes,
        'updated_at': release.updated_at.isoformat(),
    }


def server_time():
    """Not cached — it is the one value that must not be."""
    return timezone.now().isoformat()
