"""What settings exist, what they mean, and — honestly — whether the server
actually reads them.

That last column is the important one. It is easy to build a settings screen
where every switch stores a value and only some of them do anything, and an
administrator who flips `maintenance_mode` expecting the API to close has to
find out the hard way. So each setting declares where it takes effect:

* ``enforced`` — this server reads it on every relevant request.
* ``advisory`` — it is stored and served, and the mobile client is expected to
  honour it. The server does not.

Nothing here is decorative, but the two kinds are not the same promise, and
`GET /admin/settings/` returns the distinction with the value.

Several of these overlap with `settings.py` values that come from the
environment (`ATTENDANCE_*`, `THROTTLE_*`). Those are read once at start-up by
code this module does not own, so overriding them here would need each owner
to re-read on every request. Where that has not been done, the setting is
marked advisory rather than quietly shadowing an env var that still wins.
"""

from decimal import Decimal

from .models import SettingType

ENFORCED = 'enforced'
ADVISORY = 'advisory'


class Setting:
    def __init__(
        self, key, *, label, type, default, effect, note, minimum=None, maximum=None
    ):
        self.key = key
        self.label = label
        self.type = type
        self.default = default
        self.effect = effect
        self.note = note
        self.minimum = minimum
        self.maximum = maximum

    def coerce(self, raw):
        """Turns whatever arrived over JSON into the declared type.

        Raises `ValueError` with a sentence, which the serializer turns into a
        400 keyed on the setting.
        """
        if self.type == SettingType.BOOLEAN:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.lower() in ('true', 'false'):
                return raw.lower() == 'true'
            raise ValueError(f'{self.label} is a yes/no setting.')

        if self.type == SettingType.INTEGER:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f'{self.label} must be a whole number.') from None
            return self._bounded(value)

        if self.type == SettingType.DECIMAL:
            try:
                value = float(Decimal(str(raw)))
            except Exception:
                raise ValueError(f'{self.label} must be a number.') from None
            return self._bounded(value)

        value = str(raw).strip()
        if not value:
            raise ValueError(f'{self.label} cannot be blank.')
        return value

    def _bounded(self, value):
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f'{self.label} cannot be below {self.minimum}.')
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f'{self.label} cannot be above {self.maximum}.')
        return value


REGISTRY = {
    setting.key: setting
    for setting in [
        Setting(
            'maintenance_mode',
            label='Maintenance mode',
            type=SettingType.BOOLEAN,
            default=False,
            effect=ENFORCED,
            note=(
                'Closes the API to everyone except administrators. Enforced by '
                'MaintenanceModeMiddleware on every request.'
            ),
        ),
        Setting(
            'maintenance_message',
            label='Maintenance message',
            type=SettingType.STRING,
            default='The system is down for maintenance. Try again shortly.',
            effect=ENFORCED,
            note='The sentence returned with the 503 while maintenance mode is on.',
        ),
        Setting(
            'attendance_radius_meters',
            label='Attendance radius (metres)',
            type=SettingType.INTEGER,
            default=300,
            minimum=25,
            maximum=5000,
            effect=ADVISORY,
            note=(
                'The default a new geofence is created with. Existing fences '
                'carry their own radius, and the attendance check reads that, '
                'not this.'
            ),
        ),
        Setting(
            'login_throttle_per_minute',
            label='Sign-in attempts per minute',
            type=SettingType.INTEGER,
            default=10,
            minimum=1,
            maximum=1000,
            effect=ADVISORY,
            note=(
                'The live limit is DRF\'s `login` throttle scope, read from '
                'THROTTLE_LOGIN at start-up. Change it there; this records the '
                'intended value.'
            ),
        ),
        Setting(
            'app_version',
            label='Current app version',
            type=SettingType.STRING,
            default='1.0.0',
            effect=ADVISORY,
            note='Published to the mobile client, which decides what to do about it.',
        ),
        Setting(
            'minimum_app_version',
            label='Minimum supported app version',
            type=SettingType.STRING,
            default='1.0.0',
            effect=ADVISORY,
            note='The client refuses to run below this. The server does not check.',
        ),
        Setting(
            'default_gst_percent',
            label='Default GST %',
            type=SettingType.DECIMAL,
            default=18.0,
            minimum=0,
            maximum=100,
            effect=ADVISORY,
            note=(
                'What the product form pre-fills. A saved product carries its '
                'own rate, and an order line freezes the rate it was booked at.'
            ),
        ),
        Setting(
            'default_working_hours',
            label='Working hours per day',
            type=SettingType.DECIMAL,
            default=8.0,
            minimum=1,
            maximum=24,
            effect=ADVISORY,
            note=(
                'Used by the client to draw a day\'s progress. The attendance '
                'report measures actual hours and does not divide by this.'
            ),
        ),
        Setting(
            'low_stock_threshold',
            label='Low stock threshold',
            type=SettingType.INTEGER,
            default=25,
            minimum=0,
            maximum=100000,
            effect=ADVISORY,
            note=(
                'The catalogue reads PRODUCTS_LOW_STOCK_THRESHOLD from the '
                'environment. This records the intended value.'
            ),
        ),
        # ------------------------------------------------ published to /app-config/
        Setting(
            'company_name',
            label='Company name',
            type=SettingType.STRING,
            default='Sales Force Management',
            effect=ADVISORY,
            note='Shown in the app header and on printed documents.',
        ),
        Setting(
            'support_email',
            label='Support email',
            type=SettingType.STRING,
            default='support@example.com',
            effect=ADVISORY,
            note='Where the app sends a user who taps "contact support".',
        ),
        Setting(
            'support_phone',
            label='Support phone',
            type=SettingType.STRING,
            default='+911800000000',
            effect=ADVISORY,
            note='Dialled by the app; never used by the server.',
        ),
        Setting(
            'max_image_size_mb',
            label='Maximum image size (MB)',
            type=SettingType.DECIMAL,
            default=5.0,
            minimum=0.1,
            maximum=50,
            effect=ADVISORY,
            note=(
                'The client compresses to this before uploading. There is no '
                'server-side size check on selfies or site photos today, so '
                'this is a client budget rather than a limit that would '
                'reject an oversized file.'
            ),
        ),
        Setting(
            'allowed_image_formats',
            label='Allowed image formats',
            type=SettingType.STRING,
            default='jpg,jpeg,png',
            effect=ADVISORY,
            note=(
                'What the picker offers. The server accepts anything Pillow '
                'can decode, which is what ImageField already enforces.'
            ),
        ),
        # Feature flags. Advisory by design: they hide a module in the client,
        # they do not close its endpoints. Closing an endpoint is what
        # permissions are for, and a flag that did both would be two ways to
        # say no that could disagree.
        Setting(
            'feature_products_enabled',
            label='Feature: products',
            type=SettingType.BOOLEAN,
            default=True,
            effect=ADVISORY,
            note='Hides the catalogue in the client. The API stays open.',
        ),
        Setting(
            'feature_orders_enabled',
            label='Feature: orders',
            type=SettingType.BOOLEAN,
            default=True,
            effect=ADVISORY,
            note='Hides order screens in the client. The API stays open.',
        ),
        Setting(
            'feature_reports_enabled',
            label='Feature: reports',
            type=SettingType.BOOLEAN,
            default=True,
            effect=ADVISORY,
            note='Hides reports in the client. The API stays open.',
        ),
        Setting(
            'feature_offline_sync_enabled',
            label='Feature: offline sync',
            type=SettingType.BOOLEAN,
            default=True,
            effect=ADVISORY,
            note='Tells the client whether to queue offline. The API stays open.',
        ),
        Setting(
            'feature_site_visits_enabled',
            label='Feature: site visits',
            type=SettingType.BOOLEAN,
            default=True,
            effect=ADVISORY,
            note='Hides site visits in the client. The API stays open.',
        ),
        Setting(
            'feature_customer_registration_enabled',
            label='Feature: customer registration',
            type=SettingType.BOOLEAN,
            default=True,
            effect=ADVISORY,
            note='Hides the registration form. The API stays open.',
        ),
    ]
}

# The keys `/app-config/` publishes as `feature_flags`, without their prefix.
FEATURE_PREFIX = 'feature_'

KNOWN_KEYS = tuple(REGISTRY)


def current():
    """Every setting, overridden value or registry default."""
    from .models import AppSetting

    stored = {row.key: row.value for row in AppSetting.objects.all()}

    return {
        key: {
            'key': key,
            'label': setting.label,
            'type': setting.type,
            'value': stored.get(key, setting.default),
            'default': setting.default,
            'is_overridden': key in stored,
            'effect': setting.effect,
            'note': setting.note,
        }
        for key, setting in REGISTRY.items()
    }


def value_of(key):
    """One setting's live value. Falls back to the default, so a caller never
    has to handle "not configured yet"."""
    from .models import AppSetting

    setting = REGISTRY[key]
    row = AppSetting.objects.filter(key=key).first()
    return row.value if row else setting.default
