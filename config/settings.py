"""
Django settings for the Sales Force Management (SFM) backend.

Every deployment-specific value is read from the environment, with a `.env`
file loaded for local development. Nothing secret is hard-coded here, so the
same file runs on a laptop and on a server — only the environment changes.

Docs: https://docs.djangoproject.com/en/6.0/ref/settings/
"""

import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# BASE_DIR is the folder holding manage.py.
BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

def _load_env_file(path):
    """Reads `KEY=value` lines from a .env file into the environment.

    Real environment variables always win, so a server's own configuration is
    never overwritten by a file that happened to be deployed with the code.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / '.env')


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_int(key, default):
    value = os.environ.get(key)
    if value is None or not value.strip():
        return default
    return int(value)


def env_list(key, default=''):
    return [item.strip() for item in env(key, default).split(',') if item.strip()]


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

# Off unless something says otherwise.
#
# The default used to be True, which fails open: a deployment that forgets
# DJANGO_DEBUG gets tracebacks containing settings on every 500, ALLOWED_HOSTS
# effectively disabled, and — because the SECRET_KEY guard below only fires
# when DEBUG is off — every token signed with the development key printed in
# this file. Forgetting a variable should cost a clear error at start-up, not a
# silent downgrade in production. Local development sets DJANGO_DEBUG=True in
# .env; see .env.example.
DEBUG = env_bool('DJANGO_DEBUG', False)

SECRET_KEY = env('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        # Refuse to start rather than sign tokens with a guessable key.
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off. '
            'For local development, copy .env.example to .env.'
        )
    SECRET_KEY = 'django-insecure-local-development-key-do-not-deploy'

# In DEBUG anything on the machine may connect; in production the hosts are
# named explicitly.
ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]')

# Needed for the admin login form once the API sits behind a domain.
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS')

# The platform names the host it will send traffic to, and it is not knowable
# before the service exists — which makes it a chicken-and-egg if it has to be
# typed into a setting by hand. Render publishes it as an environment variable,
# so it is trusted here rather than being one more thing to remember on the
# first deploy. Every other host still has to be named explicitly.
_platform_host = env('RENDER_EXTERNAL_HOSTNAME', '')
if _platform_host:
    if _platform_host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_platform_host)
    origin = f'https://{_platform_host}'
    if origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(origin)

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Django 6 already defaults to BigAutoField; stated so the choice is explicit.
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'corsheaders',
    'rest_framework',
    # Stores refresh tokens that have been rotated or logged out, so a stolen
    # refresh token can be revoked before it expires.
    'rest_framework_simplejwt.token_blacklist',
    # Generates the OpenAPI schema from the views and serializers themselves,
    # so the document cannot drift from the API the way a hand-written one does.
    'drf_spectacular',
]

LOCAL_APPS = [
    'accounts',
    'attendance',
    'beats',
    'sitevisits',
    'customers',
    'products',
    'orders',
    # Owns no tables — it reads the six modules above. Installed so its tests
    # are discovered and its app registry entry exists.
    'reports',
    # Owns only a ledger: the business data an upload carries is written by
    # the module that owns it, through that module's own endpoint.
    'sync',
    # Labelled `administration`, not `admin` — that label belongs to
    # django.contrib.admin and Django refuses to start with two apps sharing
    # one. Its API is still mounted at /api/<version>/admin/.
    'administration',
    # The public payloads the app reads before anybody signs in.
    'appinfo',
    # Owns no tables either: it watches the modules above and tells the
    # management portal that something moved. See `realtime/__init__.py` for
    # why the socket carries no business data.
    'realtime',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    # Above CommonMiddleware, which is where the docs put it: a preflight has
    # to be answered before anything else decides to redirect it.
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',

    # Both below AuthenticationMiddleware, which is what sets request.user for
    # session callers. The maintenance gate resolves a bearer token itself for
    # API callers, since DRF has not authenticated yet at this point.
    'administration.middleware.MaintenanceModeMiddleware',
    # Outermost of the two on the way out: it wraps the response so the audit
    # context is still set while the maintenance gate answers.
    'administration.middleware.AuditContextMiddleware',
]

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]


# --------------------------------------------------------------------------
# Database — MySQL, or Postgres through DATABASE_URL
# --------------------------------------------------------------------------
# Read first because it decides which of the two blocks below applies. A
# managed platform hands out one connection string; a laptop and a VPS use the
# DB_* settings and MySQL.
DATABASE_URL = env('DATABASE_URL', '')

# Requires MySQL 8.0.11+ (Django 6 minimum) and the mysqlclient driver.
#
# Server-side setup, run once. The application must not connect as root:
#
#   CREATE DATABASE sfm_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
#   CREATE USER 'sfm_user'@'%' IDENTIFIED BY '<strong-password>';
#   GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX,
#         REFERENCES ON sfm_db.* TO 'sfm_user'@'%';
#   -- the test runner creates and drops its own copy of the schema:
#   GRANT ALL PRIVILEGES ON `test\_sfm\_db`.* TO 'sfm_user'@'%';
#   FLUSH PRIVILEGES;

db_password = env('DB_PASSWORD', '')
if not db_password and not DEBUG and not DATABASE_URL:
    # A blank password reaching production means the .env never got deployed.
    #
    # Skipped when DATABASE_URL is set: the credentials are inside that string,
    # and demanding DB_PASSWORD as well means a Postgres deployment refuses to
    # start over a MySQL setting it will never read.
    raise ImproperlyConfigured(
        'DB_PASSWORD must be set when DJANGO_DEBUG is off. '
        '(Not needed when DATABASE_URL carries the credentials.)'
    )


def _mysql_options():
    """Driver options handed straight to mysqlclient on every new connection."""
    options = {
        'charset': 'utf8mb4',
        # Without full strict mode MySQL silently repairs bad input: an
        # over-long remark is truncated, an out-of-range decimal is clamped,
        # a division by zero becomes NULL. For order amounts and quantities
        # that has to be an error, not a saved half-truth.
        'init_command': (
            "SET sql_mode='STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
            "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'"
        ),
        # MySQL defaults to REPEATABLE READ, where a request keeps reading the
        # snapshot taken at its first query — so a long request can overwrite a
        # value it never saw change. READ COMMITTED is what Django recommends.
        'isolation_level': env('DB_ISOLATION_LEVEL', 'read committed'),
        # Fail fast instead of hanging a worker when the server is unreachable.
        'connect_timeout': env_int('DB_CONNECT_TIMEOUT', 10),
    }

    # Managed MySQL (RDS, Cloud SQL, Azure) requires TLS. Both keys stay unset
    # locally, so development connects plain.
    ssl_ca = env('DB_SSL_CA', '')
    if ssl_ca:
        options['ssl'] = {'ca': ssl_ca}

    ssl_mode = env('DB_SSL_MODE', '')
    if ssl_mode:
        options['ssl_mode'] = ssl_mode

    return options


def _database_from_url(url):
    """A `DATABASE_URL` as Django's `DATABASES['default']`.

    Managed platforms hand out one connection string rather than five separate
    settings, and Render is one of them. Supported so the same code runs there
    without a second settings module — and *only* when the variable is set, so
    a laptop with MySQL in `.env` is unaffected.

    Postgres only, deliberately. Render offers no managed MySQL, and a URL
    parser that silently accepted `mysql://` here would be claiming a
    combination nobody has run.
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {'postgres', 'postgresql'}:
        raise ImproperlyConfigured(
            f'DATABASE_URL must be a postgres:// URL, not {parsed.scheme!r}. '
            'Leave it unset to use the DB_* settings and MySQL.'
        )

    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': (parsed.path or '/').lstrip('/'),
        'USER': unquote(parsed.username or ''),
        'PASSWORD': unquote(parsed.password or ''),
        'HOST': parsed.hostname or '',
        'PORT': str(parsed.port or ''),
        'CONN_MAX_AGE': env_int('DB_CONN_MAX_AGE', 60),
        'CONN_HEALTH_CHECKS': True,
        'ATOMIC_REQUESTS': env_bool('DB_ATOMIC_REQUESTS', False),
        # Render refuses an unencrypted connection to its managed Postgres.
        #
        # `or 'require'` rather than a default argument: the local `.env` sets
        # `DB_SSL_MODE=` empty for MySQL, and an empty string is a value — it
        # would be passed straight through to libpq as a blank sslmode and the
        # connection would fail somewhere far from here.
        'OPTIONS': {'sslmode': env('DB_SSL_MODE', '') or 'require'},
    }


DATABASES = {
    'default': _database_from_url(DATABASE_URL) if DATABASE_URL else {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': env('DB_NAME', 'sfm_db'),
        'USER': env('DB_USER', 'root'),
        'PASSWORD': db_password,
        'HOST': env('DB_HOST', '127.0.0.1'),
        'PORT': env('DB_PORT', '3306'),
        # Persistent connections: MySQL's handshake is expensive and the field
        # app makes many small requests. The health check discards connections
        # the server already closed on wait_timeout, instead of handing a dead
        # one to the next request.
        'CONN_MAX_AGE': env_int('DB_CONN_MAX_AGE', 60),
        'CONN_HEALTH_CHECKS': True,
        # Wraps every request in a transaction. Left off until the write-heavy
        # endpoints exist; turn it on with DB_ATOMIC_REQUESTS=True so a failed
        # order cannot leave its line items behind.
        'ATOMIC_REQUESTS': env_bool('DB_ATOMIC_REQUESTS', False),
        'OPTIONS': _mysql_options(),
        'TEST': {
            'CHARSET': 'utf8mb4',
            'COLLATION': 'utf8mb4_unicode_ci',
        },
    }
}


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
# Set before the first migration, which is the only moment this is cheap.
# USERNAME_FIELD on this model is the employee code; signing in with an email
# address or a mobile number is handled by IdentifierBackend below.
AUTH_USER_MODEL = 'accounts.User'

AUTHENTICATION_BACKENDS = [
    'accounts.backends.IdentifierBackend',
    # Kept as a fallback so the Django admin still authenticates if the custom
    # backend is ever disabled.
    'django.contrib.auth.backends.ModelBackend',
]

# Mobile numbers are stored in E.164. These two let someone type just their
# national number at the login screen.
DEFAULT_COUNTRY_CODE = env('DEFAULT_COUNTRY_CODE', '+91')
NATIONAL_NUMBER_LENGTH = env_int('NATIONAL_NUMBER_LENGTH', 10)

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    # JWT is the only credential the mobile client carries. Session auth stays
    # available so the browsable API and admin still work during development.
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    # Closed by default: an endpoint has to opt out with AllowAny, so a new
    # view can never be published unprotected by accident.
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': env_int('API_PAGE_SIZE', 20),
    'DEFAULT_FILTER_BACKENDS': [
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.FormParser',
        # Selfies and site photos arrive as multipart uploads.
        'rest_framework.parsers.MultiPartParser',
    ],
    'DEFAULT_RENDERER_CLASSES': (
        [
            'rest_framework.renderers.JSONRenderer',
            'rest_framework.renderers.BrowsableAPIRenderer',
        ]
        if DEBUG
        else ['rest_framework.renderers.JSONRenderer']
    ),
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': env('THROTTLE_ANON', '30/min'),
        'user': env('THROTTLE_USER', '2000/hour'),
        # Reserved for the sign-in and OTP views built in the next step.
        'login': env('THROTTLE_LOGIN', '10/min'),
        'otp': env('THROTTLE_OTP', '5/min'),
        # An invite request is a once-in-a-career action, so the ceiling is
        # low enough to make a flood of them pointless.
        'invite': env('THROTTLE_INVITE', '5/hour'),
    },
    'DEFAULT_VERSIONING_CLASS': 'rest_framework.versioning.URLPathVersioning',
    'DEFAULT_VERSION': 'v1',
    'ALLOWED_VERSIONS': ['v1'],
    'DATETIME_FORMAT': 'iso-8601',
    'TEST_REQUEST_DEFAULT_FORMAT': 'json',
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}


# --------------------------------------------------------------------------
# OpenAPI
# --------------------------------------------------------------------------
# The schema is generated from the views and serializers, so it cannot drift
# from the API the way a hand-maintained document does. The docstring on each
# view — the "**Responses**" blocks written throughout this codebase — becomes
# the endpoint description.

SPECTACULAR_SETTINGS = {
    'TITLE': 'Sales Force Management API',
    'DESCRIPTION': (
        'Field sales backend: attendance, beat plans, site visits, customers, '
        'products, orders, reporting, offline sync and administration.\n\n'
        'All endpoints are versioned under `/api/v1/`. Everything except the '
        'public configuration endpoints (`/privacy/`, `/terms/`, '
        '`/app-version/`, `/app-config/`, `/announcements/`) requires a JWT '
        'bearer token from `/api/v1/auth/login/`.'
    ),
    'VERSION': '1.0.0',
    # The schema endpoint should not appear in its own schema.
    'SERVE_INCLUDE_SCHEMA': False,
    'SCHEMA_PATH_PREFIX': '/api/v[0-9]',
    'SERVERS': [{'url': '/', 'description': 'This server'}],
}


# --------------------------------------------------------------------------
# SimpleJWT
# --------------------------------------------------------------------------

SIMPLE_JWT = {
    # A field executive works offline for stretches, so the access token is
    # generous and the refresh token lasts a working week.
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=env_int('JWT_ACCESS_MINUTES', 60)),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=env_int('JWT_REFRESH_DAYS', 7)),
    # Every refresh issues a new refresh token and blacklists the old one, so a
    # copied token stops working the moment the real device refreshes.
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,

    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'VERIFYING_KEY': '',
    'AUDIENCE': None,
    'ISSUER': None,
    'LEEWAY': 0,

    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'USER_AUTHENTICATION_RULE':
        'rest_framework_simplejwt.authentication.default_user_authentication_rule',

    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',
    'TOKEN_USER_CLASS': 'rest_framework_simplejwt.models.TokenUser',

    'JTI_CLAIM': 'jti',
}


# --------------------------------------------------------------------------
# Cross-origin requests
# --------------------------------------------------------------------------
# A native Android or iOS build never sends an Origin header, so none of this
# applies to it. It exists for the Flutter web build and for anyone calling
# the API from a browser tool.

CORS_ALLOWED_ORIGINS = env_list('CORS_ALLOWED_ORIGINS')

# Flutter web picks a random port on every `flutter run`, which no fixed
# allow-list can keep up with. Wide open in development, explicit in
# production — and the guard is DEBUG, not a separate flag somebody can
# forget to turn off.
CORS_ALLOW_ALL_ORIGINS = DEBUG

CORS_ALLOW_CREDENTIALS = False


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
# At or below this quantity the catalogue reports a product as low on stock
# rather than available. Commercial policy, not code — cement moves by the
# hundred and a switchgear line by the dozen, so this is expected to be tuned.
PRODUCTS_LOW_STOCK_THRESHOLD = env_int('PRODUCTS_LOW_STOCK_THRESHOLD', 25)


# --------------------------------------------------------------------------
# Attendance
# --------------------------------------------------------------------------
# Field policy, not code: an administrator changes these without a release.

# A punch-in at or after this local hour counts as late.
ATTENDANCE_LATE_HOUR = env_int('ATTENDANCE_LATE_HOUR', 10)

# A punch without a selfie is refused. Kept switchable for pilots where the
# camera permission has not been rolled out yet.
ATTENDANCE_SELFIE_REQUIRED = env_bool('ATTENDANCE_SELFIE_REQUIRED', True)

# Off by default: a punch outside every fence is stored and flagged rather
# than refused, because field staff legitimately start the day at a customer
# site. Turn it on for office-bound teams.
ATTENDANCE_ENFORCE_GEOFENCE = env_bool('ATTENDANCE_ENFORCE_GEOFENCE', False)

# A GPS fix worse than this is not worth recording — at 500 m of uncertainty
# the geofence result means nothing.
ATTENDANCE_MAX_ACCURACY_METERS = env_int('ATTENDANCE_MAX_ACCURACY_METERS', 100)

# How stale a punch captured offline may be when it finally syncs, and how
# far ahead of the server a device's clock may run.
ATTENDANCE_MAX_BACKDATE_DAYS = env_int('ATTENDANCE_MAX_BACKDATE_DAYS', 7)
ATTENDANCE_CLOCK_SKEW_MINUTES = env_int('ATTENDANCE_CLOCK_SKEW_MINUTES', 5)


# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'

# Timestamps are stored in UTC and rendered in the field team's timezone.
TIME_ZONE = env('DJANGO_TIME_ZONE', 'Asia/Kolkata')

USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# Static and media files
# --------------------------------------------------------------------------
# static/  — files committed with the project (admin overrides, logos)
# staticfiles/ — collectstatic output, served by nginx in production
# media/   — user uploads: attendance selfies, site photos, documents

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = '/media/'
MEDIA_ROOT = Path(env('DJANGO_MEDIA_ROOT', str(BASE_DIR / 'media')))

# True on a platform where the application process is also the web server —
# Render, Fly, a bare container — and there is no nginx in front to serve
# `staticfiles/`. WhiteNoise then does it, which is what keeps the admin and
# the API docs from rendering unstyled.
SERVE_STATIC_FILES = env_bool('SERVE_STATIC_FILES', False)

if SERVE_STATIC_FILES:
    # Directly after SecurityMiddleware, which is where WhiteNoise documents
    # it: a static file should be answered without the session, the CORS
    # headers or the audit log being assembled first.
    MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        # Hashed filenames in production so a deploy cannot serve a stale asset.
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage'
            if DEBUG
            else (
                'whitenoise.storage.CompressedManifestStaticFilesStorage'
                if SERVE_STATIC_FILES
                else 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'
            )
        ),
    },
}

# Photos from a phone camera are large; anything bigger than this is streamed
# to a temporary file instead of being held in memory.
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int('FILE_UPLOAD_MAX_MEMORY_SIZE', 5 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int('DATA_UPLOAD_MAX_MEMORY_SIZE', 10 * 1024 * 1024)
FILE_UPLOAD_PERMISSIONS = 0o644


# --------------------------------------------------------------------------
# Security (applied only when DEBUG is off)
# --------------------------------------------------------------------------

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SESSION_COOKIE_HTTPONLY = True

if not DEBUG:
    # The load balancer terminates TLS and forwards this header.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env_int('DJANGO_HSTS_SECONDS', 60 * 60 * 24 * 30)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': env('DJANGO_LOG_LEVEL', 'INFO'),
    },
    'loggers': {
        'django.db.backends': {
            # Set to DEBUG temporarily to see every SQL statement.
            'level': 'INFO',
            'handlers': ['console'],
            'propagate': False,
        },
    },
}
